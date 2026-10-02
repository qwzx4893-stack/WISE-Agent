# ==============================================================================
# WISE Persistent Voice Interface - Text-To-Speech (TTS) Engine & Providers
# Architecture: Provider-independent TTS with native Windows SAPI SpVoice,
# instantaneous purge/barge-in interruption, and honest voice registry probing.
# ==============================================================================

from __future__ import annotations

import sys
import time
import logging
import threading
import json
import subprocess
import uuid
import os
import re
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

LOG = logging.getLogger("WISE.Voice.TTS")


_ARABIC_LETTER = re.compile(r"[\u0600-\u06FF]")
_WISE_LATIN_TOKEN = re.compile(r"(?<![A-Za-z0-9_])wise(?![A-Za-z0-9_])", re.IGNORECASE)


def normalize_spoken_text(text: str, language: Optional[str] = None) -> str:
    """Make a few product names pronounceable without changing display text.

    Arabic voice models may spell the Latin product name ``WISE`` letter by
    letter.  The product name is spoken as "وايز", so transform only a
    standalone occurrence when the requested voice/text is Arabic.  English
    synthesis remains untouched, and this deliberately is not a general text
    rewriting or transliteration layer.
    """
    if not text:
        return text
    is_arabic = language == "ar" or (language is None and bool(_ARABIC_LETTER.search(text)))
    if not is_arabic:
        return text
    return _WISE_LATIN_TOKEN.sub("وَايْز", text)


@dataclass
class TTSResult:
    text: str
    voice_name: str
    latency_ms: float
    success: bool
    interrupted: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "voice_name": self.voice_name,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "interrupted": self.interrupted,
            "error": self.error,
        }


class BaseTTSProvider(ABC):
    """Abstract base class for Text-To-Speech providers."""

    @abstractmethod
    def speak(self, text: str, voice: Optional[str] = None, async_playback: bool = True) -> TTSResult:
        """Speaks text output."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Immediately stops/purges active speech playback (Barge-in)."""
        pass

    @abstractmethod
    def is_speaking(self) -> bool:
        """Returns True if speech is currently outputting to speaker."""
        pass

    @abstractmethod
    def get_installed_voices(self) -> List[str]:
        """Returns list of installed voice descriptions."""
        pass


class WindowsSapiTTSProvider(BaseTTSProvider):
    """
    Native Windows SAPI.SpVoice Text-To-Speech provider.
    Zero external pip dependencies (uses built-in comtypes on Windows).
    Supports instantaneous purge-before-speak for barge-in interruptions.
    """

    # SAPI SVSFlags
    SVSFDefault = 0
    SVSFlagsAsync = 1
    SVSFPurgeBeforeSpeak = 2

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._voice_obj = None
        self._installed_voices: List[Dict[str, str]] = []
        self._is_speaking = False
        self._active_voice_name = "Default"
        self._has_arabic_voice = False

        self._init_sapi()

    def _init_sapi(self) -> None:
        """Initializes SAPI.SpVoice COM object and queries installed voices."""
        if sys.platform != "win32":
            return

        try:
            import comtypes.client
            self._voice_obj = comtypes.client.CreateObject("SAPI.SpVoice")
            voices = self._voice_obj.GetVoices()
            self._installed_voices = []

            for i in range(voices.Count):
                v = voices.Item(i)
                desc = v.GetDescription()
                self._installed_voices.append({"index": i, "description": desc, "token": v})
                if any(ar_keyword in desc.lower() for ar_keyword in ["arabic", "ar-", "hoda", "naayf", "tarif"]):
                    self._has_arabic_voice = True

            if self._installed_voices:
                self._active_voice_name = self._installed_voices[0]["description"]
            LOG.info("SAPI.SpVoice initialized. Installed voices: %s", [v["description"] for v in self._installed_voices])

        except Exception as e:
            LOG.error("Failed to initialize Windows SAPI.SpVoice: %s", e)

    def is_available(self) -> bool:
        return self._voice_obj is not None

    def get_installed_voices(self) -> List[str]:
        return [v["description"] for v in self._installed_voices]

    def has_arabic_voice(self) -> bool:
        return self._has_arabic_voice

    def is_speaking(self) -> bool:
        with self._lock:
            if not self._voice_obj:
                return False
            try:
                # SAPI SpVoice.Status.RunningState: 2 = SPRS_IS_SPEAKING
                status = self._voice_obj.Status
                return bool(status.RunningState == 2)
            except Exception:
                return self._is_speaking

    def stop(self) -> None:
        """
        Immediately purges any active speech output on the audio device.
        This provides instantaneous barge-in interruption.
        """
        with self._lock:
            if not self._voice_obj:
                self._is_speaking = False
                return

            try:
                # Purge active audio stream
                self._voice_obj.Speak("", self.SVSFPurgeBeforeSpeak)
                self._is_speaking = False
                LOG.info("SAPI speech playback purged immediately (Barge-in).")
            except Exception as e:
                LOG.warning("Failed to purge SAPI speech: %s", e)
                self._is_speaking = False

    def speak(self, text: str, voice: Optional[str] = None, async_playback: bool = True) -> TTSResult:
        """
        Synthesizes text into speech.
        If text contains Arabic characters and no Arabic voice is installed,
        reports the environmental limitation honestly while speaking safely.
        """
        t0 = time.perf_counter()

        with self._lock:
            if not self._voice_obj:
                return TTSResult(
                    text=text,
                    voice_name="None",
                    latency_ms=0.0,
                    success=False,
                    error="SAPI.SpVoice COM object unavailable",
                )

            # Detect Arabic text in payload
            has_arabic_chars = any("\u0600" <= c <= "\u06FF" for c in text)
            warning_msg = None

            if has_arabic_chars and not self._has_arabic_voice:
                warning_msg = (
                    f"Environmental Limitation: Host Windows system does not have an Arabic SAPI voice installed. "
                    f"Available voices: {[v['description'] for v in self._installed_voices]}. "
                    f"Playback dispatched using default English voice."
                )
                LOG.warning(warning_msg)

            try:
                flags = self.SVSFlagsAsync if async_playback else self.SVSFDefault
                # Dispatch speech
                self._is_speaking = True
                self._voice_obj.Speak(text, flags)
                latency = (time.perf_counter() - t0) * 1000

                return TTSResult(
                    text=text,
                    voice_name=self._active_voice_name,
                    latency_ms=latency,
                    success=True,
                    error=warning_msg,
                )
            except Exception as e:
                self._is_speaking = False
                LOG.error("SAPI.SpVoice Speak failed: %s", e)
                return TTSResult(
                    text=text,
                    voice_name=self._active_voice_name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    success=False,
                    error=str(e),
                )


class IndexTTSProvider(BaseTTSProvider):
    """Optional IndexTTS-2.5 Arabic-first cloned-voice provider.

    IndexTTS is intentionally launched from its own official checkout and
    Python environment.  It needs no guessed transcript for the reference
    audio. The worker is kept warm for a short
    conversational burst and is always torn down by ``stop`` for reliable
    barge-in behaviour.
    """

    def __init__(
        self,
        reference_audio: Optional[str] = None,
        language: str = "ar",
        idle_unload_seconds: int = 120,
        gpt_checkpoint: Optional[str] = None,
        quality_profile: str = "balanced",
    ) -> None:
        from core.paths import MEMORY_DIR, REPO_ROOT

        requested_reference = Path(reference_audio) if reference_audio else (
            REPO_ROOT / "assets" / "voice" / "wise_indextts_reference_canonical.wav"
        )
        self.reference_audio = requested_reference if requested_reference.is_absolute() else REPO_ROOT / requested_reference
        requested_checkpoint = Path(gpt_checkpoint) if gpt_checkpoint else None
        self.gpt_checkpoint = (
            requested_checkpoint if requested_checkpoint is None or requested_checkpoint.is_absolute()
            else REPO_ROOT / requested_checkpoint
        )
        self.language = language if language in {"ar", "en", "zh", "ja", "es"} else "ar"
        self.quality_profile = (
            str(quality_profile).strip().lower()
            if str(quality_profile).strip().lower() in {"balanced", "quality"}
            else "balanced"
        )
        self._python = REPO_ROOT / "tools" / "index-tts" / ".venv" / "Scripts" / "python.exe"
        self._sidecar = REPO_ROOT / "core" / "voice" / "indextts_sidecar.py"
        self._model_dir = REPO_ROOT / "tools" / "index-tts" / "assets" / "voice" / "models" / "index-tts-2.5"
        self._output_dir = MEMORY_DIR / "voice" / "generated"
        self._proc: Optional[subprocess.Popen[str]] = None
        self._proc_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._is_speaking = False
        self._playback_generation = 0
        self._selected_device = "unselected"
        self._idle_unload_seconds = max(15, min(int(idle_unload_seconds), 3600))
        self._idle_timer: Optional[threading.Timer] = None

    def is_available(self) -> bool:
        required = ("config.yaml", "gpt.pth", "s2mel.pth", "codec.pth")
        return (
            self._python.is_file()
            and self._sidecar.is_file()
            and self.reference_audio.is_file()
            and (self.gpt_checkpoint is None or self.gpt_checkpoint.is_file())
            and all((self._model_dir / item).is_file() for item in required)
        )

    def get_installed_voices(self) -> List[str]:
        return ["WISE cloned voice (IndexTTS-2.5 Arabic)"] if self.is_available() else []

    # Measured budget, not the total amount that must remain unused forever.
    # The old 6.2 GB cold gate made voice effectively unusable on an 8 GB GPU
    # even though the BF16 balanced worker needs materially less after its
    # short decode peak.  The gate still reserves enough headroom to protect
    # a resident local LLM and never switches providers behind the user's back.
    _COLD_GPU_HEADROOM_MB = 5200
    _WARM_GPU_HEADROOM_MB = 1200

    @staticmethod
    def _local_model_selected() -> bool:
        """Read the current model choice without initializing a model runtime."""
        from core.paths import CONFIG_DIR

        try:
            registry = json.loads((CONFIG_DIR / "model_registry.json").read_text(encoding="utf-8"))
            if registry.get("active_provider_id") == "local" and registry.get("active_model_id"):
                return True
        except (OSError, ValueError):
            pass
        provider_module = sys.modules.get("core.models.provider_interface")
        provider = getattr(provider_module, "_ACTIVE_PROVIDER", None)
        backend = getattr(provider, "backend", None)
        loaded = getattr(backend, "is_loaded", None)
        provider_type = getattr(getattr(provider, "provider_type", None), "value", "")
        try:
            return bool(callable(loaded) and loaded() and provider_type in {
                "LOCAL_EMBEDDED", "LOCAL_MOE", "LFM2_5_COGNITIVE_CORE",
            })
        except Exception:
            return False

    @staticmethod
    def _free_vram_mb() -> Optional[int]:
        try:
            probe = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            values = [int(value.strip()) for value in probe.stdout.splitlines() if value.strip().isdigit()]
            return max(values) if values else None
        except Exception:
            return None

    def resource_status(self) -> Dict[str, Any]:
        """Protect a local LLM from voice GPU contention; no slow CPU fallback."""
        local = self._local_model_selected()
        profile = getattr(self, "quality_profile", "balanced")
        free_mb = self._free_vram_mb()
        resident = bool(self._proc and self._proc.poll() is None and self._selected_device == "cuda:0")
        required = self._WARM_GPU_HEADROOM_MB if resident else self._COLD_GPU_HEADROOM_MB
        if free_mb is not None and free_mb >= required:
            return {"ready": True, "device": "cuda:0", "free_vram_mb": free_mb,
                    "required_vram_mb": required, "local_model_selected": local,
                    "quality_profile": profile}
        if local:
            return {"ready": False, "device": "unavailable", "free_vram_mb": free_mb,
                    "required_vram_mb": required, "local_model_selected": True,
                    "quality_profile": profile,
                    "reason": "تعذر تشغيل صوت وايز: ذاكرة بطاقة الرسوم المتاحة غير كافية مع النموذج المحلي.",
                    "code": "VOICE_VRAM_INSUFFICIENT"}
        return {"ready": True, "device": "cpu", "free_vram_mb": free_mb,
                "required_vram_mb": required, "local_model_selected": False,
                "quality_profile": profile}

    def _choose_device(self) -> str:
        return str(self.resource_status()["device"])

    def _ensure_process(self) -> subprocess.Popen[str]:
        with self._proc_lock:
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None
            selected = self._choose_device()
            if selected == "unavailable":
                raise RuntimeError("VOICE_VRAM_INSUFFICIENT: insufficient GPU memory for WISE voice")
            if self._proc and self._proc.poll() is None:
                if selected == self._selected_device:
                    return self._proc
                self._proc.terminate()
                self._proc = None
            self._selected_device = selected
            environment = os.environ.copy()
            environment["WISE_TTS_DEVICE"] = self._selected_device
            environment["WISE_INDEXTTS_MODEL_DIR"] = str(self._model_dir)
            environment["WISE_TTS_QUALITY_PROFILE"] = self.quality_profile
            # Avoid a fragmented CUDA caching allocator retaining a high-water
            # mark after a long reply.  The sidecar also empties its cache when
            # each synthesis finishes.
            environment.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
            if self.gpt_checkpoint is not None:
                environment["WISE_INDEXTTS_GPT_CHECKPOINT"] = str(self.gpt_checkpoint)
            # CATT's official first-run downloader emits Unicode progress
            # markers; Windows' legacy console encoding otherwise aborts it.
            environment["PYTHONIOENCODING"] = "utf-8"
            self._proc = subprocess.Popen(
                [str(self._python), "-u", str(self._sidecar)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=environment,
            )
            return self._proc

    def _schedule_idle_unload(self) -> None:
        def _unload() -> None:
            with self._proc_lock:
                if self._proc and self._proc.poll() is None and not self._is_speaking:
                    self._proc.terminate()
                    self._proc = None
                self._idle_timer = None

        with self._proc_lock:
            if self._idle_timer:
                self._idle_timer.cancel()
            self._idle_timer = threading.Timer(self._idle_unload_seconds, _unload)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _play(self, output: Path, duration_seconds: float, async_playback: bool) -> None:
        if sys.platform != "win32":
            return
        try:
            import winsound

            flags = winsound.SND_FILENAME | (winsound.SND_ASYNC if async_playback else 0)
            self._playback_generation += 1
            generation = self._playback_generation
            self._is_speaking = True
            winsound.PlaySound(str(output), flags)
            self._is_speaking = bool(async_playback)
            if async_playback:
                def _finish() -> None:
                    time.sleep(max(0.0, duration_seconds))
                    if generation == self._playback_generation:
                        self._is_speaking = False
                threading.Thread(target=_finish, name="WISE-IndexTTS-Playback", daemon=True).start()
        except Exception as exc:
            LOG.warning("IndexTTS playback failed: %s", exc)
            self._is_speaking = False

    def speak(self, text: str, voice: Optional[str] = None, async_playback: bool = True) -> TTSResult:
        t0 = time.perf_counter()
        if not self.is_available():
            return TTSResult(
                text=text,
                voice_name="WISE cloned voice",
                latency_ms=0.0,
                success=False,
                error="IndexTTS-2.5 is not ready: the isolated runtime, reference audio, or official weights are missing.",
            )
        resource = self.resource_status()
        if not resource["ready"]:
            self.release_gpu_for_local_model()
            return TTSResult(
                text=text, voice_name="WISE cloned voice", latency_ms=(time.perf_counter() - t0) * 1000,
                success=False, error=str(resource["reason"]),
            )
        output = self._output_dir / f"wise_index_{uuid.uuid4().hex}.wav"
        job = {
            "operation": "synthesize",
            "text": text,
            "language": self.language.upper(),
            "reference_audio": str(self.reference_audio),
            "gpt_checkpoint": str(self.gpt_checkpoint) if self.gpt_checkpoint else "",
            "output": str(output),
        }
        try:
            with self._request_lock:
                proc = self._ensure_process()
                if not proc.stdin or not proc.stdout:
                    raise RuntimeError("IndexTTS sidecar pipes are unavailable")
                proc.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            result = json.loads(line) if line else {"ok": False, "error": "IndexTTS sidecar exited"}
            if not result.get("ok"):
                return TTSResult(text=text, voice_name="WISE cloned voice", latency_ms=(time.perf_counter() - t0) * 1000,
                                 success=False, error=str(result.get("error") or "generation failed"))
            self._play(output, float(result.get("duration_seconds") or 0.0), async_playback)
            self._schedule_idle_unload()
            return TTSResult(text=text, voice_name="WISE cloned voice", latency_ms=(time.perf_counter() - t0) * 1000, success=True)
        except Exception as exc:
            return TTSResult(text=text, voice_name="WISE cloned voice", latency_ms=(time.perf_counter() - t0) * 1000,
                             success=False, error=str(exc))

    def stop(self) -> None:
        self._playback_generation += 1
        self._is_speaking = False
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        with self._proc_lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
            self._proc = None

    def release_gpu_for_local_model(self) -> None:
        """Free the resident voice worker before a newly selected local LLM loads."""
        with self._proc_lock:
            if self._selected_device == "cuda:0" and self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
                self._proc = None
                self._selected_device = "unavailable"

    def is_speaking(self) -> bool:
        return self._is_speaking

    def status(self) -> Dict[str, Any]:
        return {
            "engine": "indextts-2.5",
            "available": self.is_available(),
            "reference_audio": str(self.reference_audio),
            "language": self.language,
            "sidecar_running": bool(self._proc and self._proc.poll() is None),
            "device": self._selected_device,
            "resource": self.resource_status(),
            "idle_unload_seconds": self._idle_unload_seconds,
            "quality_profile": self.quality_profile,
        }


class TextToSpeechEngine:
    """
    Unified Text-To-Speech Engine managing active TTS providers.
    Supports low-latency synthesis, immediate stopping, and honest environment probing.
    """

    def __init__(self, primary_provider: Optional[BaseTTSProvider] = None) -> None:
        self.native_provider = WindowsSapiTTSProvider()
        if primary_provider is not None:
            self.primary_provider = primary_provider
        else:
            self.primary_provider = self._configured_provider()

    def _configured_provider(self) -> BaseTTSProvider:
        try:
            from core.server_config import load_server_config
            cfg = load_server_config()
            if cfg.get("tts_engine") == "indextts":
                return IndexTTSProvider(
                    reference_audio=cfg.get("tts_reference_audio"),
                    language=cfg.get("tts_clone_language", "ar"),
                    idle_unload_seconds=cfg.get("tts_idle_unload_seconds", 45),
                    gpt_checkpoint=cfg.get("tts_indextts_gpt_checkpoint") or None,
                    quality_profile=cfg.get("tts_quality_profile", "balanced"),
                )
        except Exception as exc:
            LOG.warning("Could not load configured TTS provider: %s", exc)
        return self.native_provider

    def set_provider(self, provider: BaseTTSProvider) -> None:
        self.primary_provider = provider

    def speak(self, text: str, voice: Optional[str] = None, async_playback: bool = True) -> TTSResult:
        """Synthesizes text through the active TTS provider."""
        provider_language = getattr(self.primary_provider, "language", None)
        spoken_text = normalize_spoken_text(text, language=provider_language)
        return self.primary_provider.speak(spoken_text, voice=voice, async_playback=async_playback)

    def stop(self) -> None:
        """Immediately interrupts playback (Barge-in)."""
        self.primary_provider.stop()

    def is_speaking(self) -> bool:
        return self.primary_provider.is_speaking()

    def probe_environment(self) -> Dict[str, Any]:
        """Probes installed Windows TTS voices and reports Arabic/English support status."""
        installed = self.native_provider.get_installed_voices()
        has_ar = self.native_provider.has_arabic_voice()
        has_en = any("en" in v.lower() or "david" in v.lower() or "zira" in v.lower() for v in installed)

        cloned_provider = isinstance(self.primary_provider, IndexTTSProvider)
        selected = getattr(self.primary_provider, "_active_voice_name", None) or (
            "WISE cloned voice" if cloned_provider else "Default"
        )
        out = {
            "installed_voices": installed,
            "has_english_tts": has_en,
            "has_arabic_tts": has_ar,
            "selected_voice": selected,
            "arabic_tts_status": "NATIVE_AVAILABLE" if has_ar else "REQUIRES_WINDOWS_VOICE_PACKAGE",
        }
        if cloned_provider:
            out["cloned_voice"] = self.primary_provider.status()
            out["has_arabic_tts"] = self.primary_provider.is_available()
            out["has_english_tts"] = self.primary_provider.is_available()
            out["arabic_tts_status"] = "CLONED_VOICE_AVAILABLE" if self.primary_provider.is_available() else "CLONED_VOICE_NEEDS_REFERENCE"
        return out
