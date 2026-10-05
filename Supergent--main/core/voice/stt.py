# ==============================================================================
# WISE Persistent Voice Interface - Speech-To-Text (STT) Provider Abstraction
# Architecture: Provider-independent STT engine with honest environmental probing.
# Distinguishes between native API availability and actual language pack installation.
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import logging
import subprocess
import base64
import json
import threading
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

LOG = logging.getLogger("WISE.Voice.STT")


@dataclass
class STTResult:
    text: str
    language: str
    confidence: float
    latency_ms: float
    provider_name: str
    is_simulation: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return bool(self.text) and self.error is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "provider_name": self.provider_name,
            "is_simulation": self.is_simulation,
            "success": self.success,
            "error": self.error,
        }


TranscriptionResult = STTResult


class BaseSTTProvider(ABC):
    """Abstract interface for Speech-To-Text engines."""

    @abstractmethod
    def transcribe(self, pcm_bytes: bytes, language: str = "auto") -> STTResult:
        """Transcribes PCM audio bytes (16-bit 16kHz mono) to text."""
        pass

    @abstractmethod
    def get_supported_languages(self) -> List[str]:
        """Returns list of language tags supported by this provider on this host."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Returns True if the engine is functional on the current system."""
        pass


class WindowsNativeSTTProvider(BaseSTTProvider):
    """
    Native Windows Speech Recognition Provider.
    Queries System.Speech.Recognition and WinRT engines on Windows.
    Strictly probes installed language packs and reports environmental limitations honestly.
    """

    def __init__(self) -> None:
        self._installed_languages: List[str] = []
        self._probe_installed_languages()

    def _probe_installed_languages(self) -> None:
        """Probes the host Windows OS for installed speech recognition engines."""
        if sys.platform != "win32":
            return

        try:
            # Query System.Speech installed recognizers via PowerShell
            cmd = (
                "Add-Type -AssemblyName System.Speech; "
                "[System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() "
                "| ForEach-Object { $_.Culture.Name }"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                langs = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
                self._installed_languages = langs
                LOG.info("Windows Native STT installed languages: %s", self._installed_languages)
        except Exception as e:
            LOG.warning("Failed to query Windows Speech Recognizers: %s", e)

    def is_available(self) -> bool:
        return sys.platform == "win32" and len(self._installed_languages) > 0

    def get_supported_languages(self) -> List[str]:
        return list(self._installed_languages)

    def transcribe(self, pcm_bytes: bytes, language: str = "auto") -> STTResult:
        """
        Transcribes audio using Windows speech recognition.
        If the requested language (e.g. Arabic) is not installed on Windows,
        honestly reports the environmental limitation instead of faking success.
        """
        t0 = time.perf_counter()

        if not pcm_bytes:
            return STTResult(
                text="",
                language=language,
                confidence=0.0,
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_name="WindowsNativeSTT",
                error="Empty audio input",
            )

        # Check language availability
        target_lang = "en-US" if language in ("auto", "en", "en-US") else language
        is_lang_installed = any(target_lang.lower() in l.lower() for l in self._installed_languages)

        if not is_lang_installed:
            latency = (time.perf_counter() - t0) * 1000
            err_msg = (
                f"Environmental Limitation: Host Windows system does not have speech recognition "
                f"language pack for '{language}' installed. Installed recognizers: {self._installed_languages}. "
                f"To enable Arabic STT natively, install 'Arabic (Saudi Arabia)' speech pack in Windows Settings."
            )
            LOG.warning(err_msg)
            return STTResult(
                text="",
                language=language,
                confidence=0.0,
                latency_ms=latency,
                provider_name="WindowsNativeSTT",
                error=err_msg,
            )

        # A recognizer may be installed while this desktop runtime has no
        # capture/streaming adapter configured for it.  Returning a canned
        # transcript here used to falsely report successful speech-to-text.
        # Report the capability gap until a real Windows recognizer adapter is
        # wired in.
        latency = (time.perf_counter() - t0) * 1000
        return STTResult(
            text="",
            language=target_lang,
            confidence=0.0,
            latency_ms=latency,
            provider_name="WindowsNativeSTT",
            error="Windows recognizer is installed, but no live audio transcription adapter is configured.",
        )


class AcousticBufferSTTProvider(BaseSTTProvider):
    """
    Deterministic Acoustic Buffer STT Provider.
    RESTRICTED USE: Exclusively for automated regression testing, CI validation,
    and architecture verification. Explicitly flagged as is_simulation=True.
    """

    def __init__(self) -> None:
        self._staged_transcripts: Dict[int, str] = {}
        self._default_transcript = "WISE acoustic validation request"

    def stage_transcript_for_audio(self, audio_bytes: bytes, text: str) -> None:
        """Stages an exact transcription for specific audio bytes in automated tests."""
        h = hash(audio_bytes)
        self._staged_transcripts[h] = text

    def load_test_utterance(self, audio_bytes: bytes, text: str) -> None:
        """Alias for stage_transcript_for_audio for automated unit tests."""
        self.stage_transcript_for_audio(audio_bytes, text)

    def set_default_transcript(self, text: str) -> None:
        self._default_transcript = text

    def is_available(self) -> bool:
        return True

    def get_supported_languages(self) -> List[str]:
        return ["en-US", "ar-SA", "auto"]

    def transcribe(self, pcm_bytes: bytes, language: str = "auto") -> STTResult:
        t0 = time.perf_counter()
        h = hash(pcm_bytes)
        text = self._staged_transcripts.get(h, self._default_transcript)
        latency = (time.perf_counter() - t0) * 1000
        return STTResult(
            text=text,
            language=language,
            confidence=1.0,
            latency_ms=latency,
            provider_name="AcousticBufferSTT (Test Only)",
            is_simulation=True,
        )


class FasterWhisperCPUProvider(BaseSTTProvider):
    """Real bilingual recognition in an isolated, persistent CPU process."""

    def __init__(self, model: str = "base", idle_unload_seconds: int = 180) -> None:
        from core.paths import REPO_ROOT

        self._python = REPO_ROOT / ".voice-venv" / "Scripts" / "python.exe"
        self._sidecar = REPO_ROOT / "core" / "voice" / "whisper_sidecar.py"
        self._model = model
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.RLock()
        self._idle_seconds = max(30, int(idle_unload_seconds))
        self._timer: Optional[threading.Timer] = None

    def is_available(self) -> bool:
        return self._python.is_file() and self._sidecar.is_file()

    def get_supported_languages(self) -> List[str]:
        if not self.is_available():
            return []
        return ["en"] if self._model.lower().endswith(".en") else ["ar", "en", "auto"]

    def _unload(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
            self._proc = None
            self._timer = None

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._unload()

    def transcribe(self, pcm_bytes: bytes, language: str = "auto") -> STTResult:
        started = time.perf_counter()
        if not self.is_available():
            return STTResult("", language, 0.0, 0.0, "faster-whisper CPU", error="isolated STT runtime is missing")
        try:
            with self._lock:
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                if not self._proc or self._proc.poll() is not None:
                    environment = os.environ.copy()
                    environment["WISE_STT_MODEL"] = self._model
                    environment["PYTHONIOENCODING"] = "utf-8"
                    self._proc = subprocess.Popen(
                        [str(self._python), "-u", str(self._sidecar)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                        bufsize=1, env=environment,
                    )
                if not self._proc.stdin or not self._proc.stdout:
                    raise RuntimeError("STT worker pipes are unavailable")
                payload = {"pcm16": base64.b64encode(pcm_bytes).decode("ascii"), "language": language}
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
                # A dead/hung worker must not hold the voice lock forever.
                import queue
                responses = queue.Queue(maxsize=1)
                stream = self._proc.stdout
                def read_response():
                    try:
                        responses.put(stream.readline())
                    except Exception:
                        responses.put("")
                threading.Thread(target=read_response, daemon=True, name="wise-stt-response").start()
                try:
                    line = responses.get(timeout=45)
                except queue.Empty:
                    raise TimeoutError("Speech recognition timed out; worker will be stopped")
                result = json.loads(line) if line else {"ok": False, "error": "STT worker exited"}
                self._timer = threading.Timer(self._idle_seconds, self._unload)
                self._timer.daemon = True
                self._timer.start()
            return STTResult(
                text=str(result.get("text") or ""),
                language=str(result.get("language") or language),
                confidence=float(result.get("confidence") or 0.0),
                latency_ms=(time.perf_counter() - started) * 1000,
                provider_name="faster-whisper CPU",
                error=None if result.get("ok") else str(result.get("error") or "recognition failed"),
            )
        except Exception as exc:
            self.stop()
            return STTResult("", language, 0.0, (time.perf_counter() - started) * 1000,
                             "faster-whisper CPU", error=str(exc))


class SpeechToTextEngine:
    """
    Unified Speech-To-Text Engine coordinating pluggable providers.
    Measures and logs exact transcription latencies.
    """

    def __init__(self, primary_provider: Optional[BaseSTTProvider] = None) -> None:
        from core.server_config import load_server_config

        self.native_provider = WindowsNativeSTTProvider()
        self.test_provider = AcousticBufferSTTProvider()
        if primary_provider is not None:
            self.primary_provider = primary_provider
        else:
            config = load_server_config()
            self.primary_provider = (
                FasterWhisperCPUProvider(model=str(config.get("stt_model") or "base"))
                if config.get("stt_engine", "whisper_cpu") == "whisper_cpu"
                else self.native_provider
            )
        self.language = str(load_server_config().get("stt_language", "auto")) if primary_provider is None else "auto"

    def set_provider(self, provider: BaseSTTProvider) -> None:
        self.primary_provider = provider

    def transcribe(self, pcm_bytes: bytes, language: str = "auto") -> STTResult:
        """Transcribes PCM audio using the configured primary provider with safe exception handling."""
        t0 = time.perf_counter()
        try:
            selected_language = self.language if language == "auto" else language
            return self.primary_provider.transcribe(pcm_bytes, language=selected_language)
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            LOG.error("STT provider transcription failed: %s", e)
            return STTResult(
                text="",
                language=language,
                confidence=0.0,
                latency_ms=latency,
                provider_name=type(self.primary_provider).__name__,
                error=str(e),
            )

    def probe_environment(self) -> Dict[str, Any]:
        """Probes the local Windows environment for STT capabilities and language packs."""
        supported = self.native_provider.get_supported_languages()
        selected_available = self.primary_provider.is_available()
        selected_languages = self.primary_provider.get_supported_languages() if selected_available else []
        has_english = any(l.lower().split("-")[0] == "en" for l in selected_languages)
        has_arabic = any(l.lower().split("-")[0] == "ar" for l in selected_languages)

        arabic_status = "UNAVAILABLE"
        if has_arabic:
            arabic_status = "NATIVE_AVAILABLE" if self.primary_provider is self.native_provider else "SELECTED_PROVIDER_AVAILABLE"
        else:
            arabic_status = "REQUIRES_WINDOWS_LANGUAGE_PACK"

        return {
            "native_stt_available": self.native_provider.is_available(),
            "selected_stt_available": selected_available,
            "selected_stt_provider": type(self.primary_provider).__name__,
            "selected_languages": selected_languages,
            "installed_languages": supported,
            "has_english_stt": has_english,
            "has_arabic_stt": has_arabic,
            "arabic_stt_status": arabic_status,
        }
