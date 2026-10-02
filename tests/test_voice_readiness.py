"""Voice control must report real capability readiness, never a synthetic listener."""

from __future__ import annotations

import threading
import asyncio


def test_stt_status_uses_selected_provider_not_windows_language_packs():
    from core.voice.stt import SpeechToTextEngine
    manager = object.__new__(SpeechToTextEngine)
    class Provider:
        def is_available(self):
            return True
        def get_supported_languages(self):
            return ["ar", "en", "auto"]
    class Native(Provider):
        def get_supported_languages(self):
            return ["en-US"]
    manager.native_provider = Native()
    manager.primary_provider = Provider()
    info = manager.probe_environment()
    assert info["has_arabic_stt"] is True
    assert info["arabic_stt_status"] == "SELECTED_PROVIDER_AVAILABLE"
    assert info["installed_languages"] == ["en-US"]


def test_stt_worker_timeout_reports_failure_and_stops_worker(monkeypatch):
    import queue
    from types import SimpleNamespace
    from core.voice.stt import FasterWhisperCPUProvider
    provider = object.__new__(FasterWhisperCPUProvider)
    provider._lock = threading.RLock()
    provider._timer = None
    provider._model = "base"
    provider._proc = SimpleNamespace(poll=lambda: None,
        stdin=SimpleNamespace(write=lambda _: None, flush=lambda: None),
        stdout=SimpleNamespace(readline=lambda: ""))
    monkeypatch.setattr(provider, "is_available", lambda: True)
    stopped = []
    monkeypatch.setattr(provider, "stop", lambda: stopped.append(True))
    class TimeoutQueue:
        def put(self, value):
            pass
        def get(self, timeout):
            assert timeout == 45
            raise queue.Empty
    monkeypatch.setattr(queue, "Queue", lambda **kwargs: TimeoutQueue())
    result = provider.transcribe(b"\x00\x00", language="ar")
    assert not result.success
    assert "timed out" in result.error
    assert stopped == [True]


def test_english_only_whisper_model_does_not_advertise_arabic(monkeypatch):
    from core.voice.stt import FasterWhisperCPUProvider
    provider = object.__new__(FasterWhisperCPUProvider)
    provider._model = "base.en"
    monkeypatch.setattr(provider, "is_available", lambda: True)
    assert provider.get_supported_languages() == ["en"]


def test_arabic_speech_normalizer_pronounces_wise_as_a_word():
    from core.voice.tts import normalize_spoken_text

    assert normalize_spoken_text("مرحباً، أنا WISE.", language="ar") == "مرحباً، أنا وَايْز."
    assert normalize_spoken_text("WISE is ready.", language="en") == "WISE is ready."


def test_speech_frontend_uses_arabic_frontend_without_changing_display_text():
    from core.voice.speech_frontend import prepare_spoken_text

    spoken, language = prepare_spoken_text(
        "مرحباً WISE، افحص API رقم 12.",
        "AR",
        diacritize_arabic=lambda value: value,
    )

    assert language == "AR"
    assert "وَايْز" in spoken
    assert "إيه بي آي" in spoken
    assert "12" not in spoken


def test_speech_frontend_automatically_selects_english_for_english_reply():
    from core.voice.speech_frontend import prepare_spoken_text

    spoken, language = prepare_spoken_text("WISE API is ready.", "AR")

    assert language == "EN"
    assert "A P I" in spoken


def test_microphone_refuses_to_enter_recording_state_when_device_is_missing(monkeypatch):
    from core.voice.microphone import WindowsMicrophoneDriver

    mic = WindowsMicrophoneDriver()
    monkeypatch.setattr(mic, "is_available", lambda: False)

    assert mic.start_recording() is False
    assert mic.get_device_info()["is_recording"] is False


def test_voice_runtime_refuses_start_when_required_audio_capability_is_missing():
    from core.voice.runtime import VoiceRuntime, VoiceRuntimeState

    class _Microphone:
        def is_available(self):
            return False

    runtime = object.__new__(VoiceRuntime)
    runtime._lock = threading.RLock()
    runtime._is_running = False
    runtime.microphone = _Microphone()
    runtime.stt = type("STT", (), {"primary_provider": type("Provider", (), {"is_available": lambda self: True})()})()
    runtime.tts = type("TTS", (), {"primary_provider": type("Provider", (), {"is_available": lambda self: True})()})()
    states = []
    runtime._set_state = lambda state: states.append(state)

    assert runtime.start() is False
    assert states == [VoiceRuntimeState.ERROR]


def test_runtime_doctor_marks_unavailable_audio_as_degraded(monkeypatch):
    from core.runtime_doctor import ComponentStatus, RuntimeDoctor

    class _NativeTTS:
        def is_available(self):
            return False

    class _VoiceRuntime:
        state = type("State", (), {"value": "IDLE"})()
        tts = type("TTS", (), {"native_provider": _NativeTTS()})()

        def probe_voice_environment(self):
            return {
                "microphone": {"is_available": False},
                "speech_to_text": {"native_stt_available": False},
                "text_to_speech": {},
            }

    monkeypatch.setattr("core.voice.runtime.VoiceRuntime", _VoiceRuntime)

    check = RuntimeDoctor().inspect_audio()

    assert check.status == "WARN"
    assert check.code == ComponentStatus.NOT_CONFIGURED.value


def test_voice_start_endpoint_reports_capabilities_when_not_ready(monkeypatch):
    from api.server import v2_voice_start

    class _Runtime:
        def start(self):
            return False

        def probe_voice_environment(self):
            return {"microphone": {"is_available": False}}

    monkeypatch.setattr("core.voice.runtime.get_voice_runtime", lambda: _Runtime())

    response = asyncio.run(v2_voice_start())

    assert response["ok"] is False
    assert response["capabilities"]["microphone"]["is_available"] is False
    assert "not ready" in response["message"]


def test_local_model_voice_uses_gpu_only_with_safe_headroom(monkeypatch):
    from core.voice.tts import IndexTTSProvider

    provider = object.__new__(IndexTTSProvider)
    provider._proc = None
    provider._selected_device = "unselected"
    monkeypatch.setattr(provider, "_local_model_selected", lambda: True)
    monkeypatch.setattr(provider, "_free_vram_mb", lambda: 6500)
    provider.quality_profile = "balanced"
    assert provider.resource_status()["device"] == "cuda:0"

    monkeypatch.setattr(provider, "_free_vram_mb", lambda: 5000)
    denied = provider.resource_status()
    assert denied["ready"] is False
    assert denied["code"] == "VOICE_VRAM_INSUFFICIENT"
    assert denied["device"] == "unavailable"


def test_balanced_voice_profile_uses_lower_cold_headroom_than_legacy_gate(monkeypatch):
    from core.voice.tts import IndexTTSProvider

    provider = object.__new__(IndexTTSProvider)
    provider._proc = None
    provider._selected_device = "unselected"
    provider.quality_profile = "balanced"
    monkeypatch.setattr(provider, "_local_model_selected", lambda: True)
    monkeypatch.setattr(provider, "_free_vram_mb", lambda: 5300)

    status = provider.resource_status()

    assert status["ready"] is True
    assert status["required_vram_mb"] == 5200
    assert status["quality_profile"] == "balanced"


def test_voice_runtime_surfaces_vram_reason_before_opening_microphone():
    """A local LLM must not lose VRAM merely because voice was enabled."""
    from core.voice.runtime import VoiceRuntime, VoiceRuntimeState

    class _Microphone:
        opened = False

        def is_available(self):
            return True

        def start(self):
            self.opened = True
            return True

    class _Provider:
        def is_available(self):
            return True

        def resource_status(self):
            return {
                "ready": False,
                "reason": "تعذر تشغيل صوت وايز: ذاكرة بطاقة الرسوم المتاحة غير كافية مع النموذج المحلي.",
                "code": "VOICE_VRAM_INSUFFICIENT",
            }

    runtime = object.__new__(VoiceRuntime)
    runtime._lock = threading.RLock()
    runtime._is_running = False
    runtime._external_stt_provider = True
    runtime._external_tts_provider = True
    runtime.microphone = _Microphone()
    runtime.stt = type("STT", (), {"primary_provider": _Provider()})()
    runtime.tts = type("TTS", (), {"primary_provider": _Provider()})()
    runtime._last_start_error = None
    states = []
    runtime._set_state = lambda state: states.append(state)

    assert runtime.start() is False
    assert runtime.microphone.opened is False
    assert states == [VoiceRuntimeState.ERROR]
    assert runtime._last_start_error.startswith("تعذر تشغيل صوت وايز")


def test_cloud_model_can_use_cpu_fallback_without_local_model(monkeypatch):
    from core.voice.tts import IndexTTSProvider

    provider = object.__new__(IndexTTSProvider)
    provider._proc = None
    provider._selected_device = "unselected"
    monkeypatch.setattr(provider, "_local_model_selected", lambda: False)
    monkeypatch.setattr(provider, "_free_vram_mb", lambda: 5000)
    assert provider.resource_status()["device"] == "cpu"
