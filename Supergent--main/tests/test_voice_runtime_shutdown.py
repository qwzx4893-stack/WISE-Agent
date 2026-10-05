"""Synthetic lifecycle tests; no audio capture, models, or GPU inference."""
from types import SimpleNamespace
import threading

from core.voice.runtime import VoiceRuntime, VoiceRuntimeState


def isolated_runtime():
    runtime = object.__new__(VoiceRuntime)
    runtime._lock = threading.RLock()
    runtime._interaction_lock = threading.Lock()
    runtime._is_running = False
    runtime._worker_thread = None
    runtime._state = VoiceRuntimeState.IDLE
    runtime._stop_event = threading.Event()
    runtime._interrupted_turn = threading.Event()
    runtime._total_interactions = 0
    runtime._set_state = lambda state: setattr(runtime, "_state", state)
    return runtime


def test_text_interaction_without_listener_still_releases_initialized_providers(monkeypatch):
    import core.brain.conversational_core as brain
    import core.session_service as sessions
    runtime = isolated_runtime()
    released = []
    worker = {"alive": False}
    def speak(*args, **kwargs):
        worker["alive"] = True  # Warm synthetic synthesis worker, not playback.
        return SimpleNamespace(success=True, latency_ms=0, error=None)
    def stop_tts():
        worker["alive"] = False
        released.append("tts")
    runtime.tts = SimpleNamespace(speak=speak, is_speaking=lambda: False, stop=stop_tts)
    runtime.microphone = SimpleNamespace(stop=lambda: released.append("microphone"))
    runtime.stt = SimpleNamespace(primary_provider=SimpleNamespace(stop=lambda: released.append("stt")))
    monkeypatch.setattr(sessions, "get_session_service", lambda: SimpleNamespace(
        get_or_create_session=lambda sid: SimpleNamespace(session_id=sid)))
    monkeypatch.setattr(brain, "get_conversational_core", lambda: SimpleNamespace(process_turn=lambda *a, **kw:
        SimpleNamespace(reply_text="Hello", error=None, task_id=None)))
    assert runtime.interact("Hello", session_id="isolated").success
    assert worker["alive"] and not runtime._is_running
    runtime.stop()
    assert not worker["alive"]
    assert released == ["tts", "microphone", "stt"]
    assert runtime._stop_event.is_set() and runtime._interrupted_turn.is_set()
    runtime.stop()  # Provider cleanup remains safe on repeated shutdown.
    assert released == ["tts", "microphone", "stt"] * 2


def test_provider_failure_does_not_skip_other_cleanup():
    runtime = isolated_runtime()
    released = []
    def stop_tts():
        raise OSError("synthetic cleanup failure")
    runtime.tts = SimpleNamespace(stop=stop_tts)
    runtime.microphone = SimpleNamespace(stop=lambda: released.append("microphone"))
    runtime.stt = SimpleNamespace(primary_provider=SimpleNamespace(stop=lambda: released.append("stt")))
    runtime.stop()
    assert released == ["microphone", "stt"]
    assert runtime._worker_thread is None


def test_timed_out_listener_is_retained_and_restart_refused(caplog):
    runtime = isolated_runtime()
    joins = []
    thread = SimpleNamespace(is_alive=lambda: True, join=lambda **kwargs: joins.append(kwargs))
    runtime._worker_thread = thread
    runtime.tts = SimpleNamespace(stop=lambda: None)
    runtime.microphone = SimpleNamespace(stop=lambda: None)
    runtime.stt = SimpleNamespace(primary_provider=SimpleNamespace(stop=lambda: None))
    runtime.stop()
    assert joins == [{"timeout": 1.5}]
    assert runtime._worker_thread is thread
    assert "shutdown was requested, not confirmed" in caplog.text
    assert runtime.start() is False
    assert "still stopping" in runtime._last_start_error
