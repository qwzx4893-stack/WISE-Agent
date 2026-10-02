# ==============================================================================
# WISE Persistent Voice Interface - Barge-In / Interruption Controller
# Architecture: Instantaneous purge of active TTS output upon user speech detection,
# transitioning state from SPEAKING -> INTERRUPTED -> LISTENING.
# ==============================================================================

from __future__ import annotations

import time
import logging
import threading
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, field

from .tts import TextToSpeechEngine
from .vad import VoiceActivityDetector

LOG = logging.getLogger("WISE.Voice.BargeIn")


@dataclass
class BargeInEvent:
    timestamp: float
    reason: str
    purged_latency_ms: float
    previous_state: str
    active_tts_text: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "reason": self.reason,
            "purged_latency_ms": self.purged_latency_ms,
            "previous_state": self.previous_state,
            "active_tts_text": self.active_tts_text,
        }


class BargeInController:
    """
    Coordinates real-time voice barge-in interruptions.
    When user voice activity is detected while WISE is speaking, it immediately
    halts and purges the active TTS speech buffer (< 10ms) and invokes registered
    callbacks to transition the runtime into LISTENING state.
    """

    def __init__(
        self,
        tts_engine: TextToSpeechEngine,
        vad: Optional[VoiceActivityDetector] = None,
        on_barge_in: Optional[Callable[[BargeInEvent], None]] = None,
    ) -> None:
        self.tts_engine = tts_engine
        self.vad = vad or VoiceActivityDetector()
        self.on_barge_in = on_barge_in
        self._lock = threading.RLock()

        self._interruption_count = 0
        self._last_barge_in_event: Optional[BargeInEvent] = None
        self._enabled = True

    @property
    def interruption_count(self) -> int:
        with self._lock:
            return self._interruption_count

    @property
    def last_event(self) -> Optional[BargeInEvent]:
        with self._lock:
            return self._last_barge_in_event

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled

    def trigger_barge_in(
        self,
        reason: str = "user_speech_detected",
        current_state: str = "SPEAKING",
        active_tts_text: Optional[str] = None,
    ) -> Optional[BargeInEvent]:
        """
        Immediately purges active TTS speech output and records the barge-in event.
        Returns the BargeInEvent if an interruption occurred, or None if disabled.
        """
        with self._lock:
            if not self._enabled:
                return None

            t0 = time.perf_counter()
            # Instantaneous Win32 SAPI purge (SVSFPurgeBeforeSpeak)
            try:
                self.tts_engine.stop()
            except Exception as e:
                LOG.error("Failed to purge TTS during barge-in: %s", e)

            purge_latency_ms = (time.perf_counter() - t0) * 1000
            self._interruption_count += 1

            event = BargeInEvent(
                timestamp=time.time(),
                reason=reason,
                purged_latency_ms=purge_latency_ms,
                previous_state=current_state,
                active_tts_text=active_tts_text,
            )
            self._last_barge_in_event = event

            LOG.info(
                "Barge-In triggered: purged TTS in %.2f ms (Total interruptions: %d, Reason: %s)",
                purge_latency_ms,
                self._interruption_count,
                reason,
            )

            # Fire callback if registered
            if self.on_barge_in:
                try:
                    self.on_barge_in(event)
                except Exception as cb_err:
                    LOG.error("Barge-in callback error: %s", cb_err)

            return event

    def evaluate_audio_frame(
        self,
        pcm_frame: bytes,
        current_state: str,
        active_tts_text: Optional[str] = None,
    ) -> Optional[BargeInEvent]:
        """
        Inspects an incoming microphone PCM frame. If speech is detected while
        in SPEAKING state or while TTS is actively outputting audio, executes barge-in.
        """
        with self._lock:
            if not self._enabled:
                return None

            # Only interrupt if speaking or state indicates active speech
            is_speaking = current_state == "SPEAKING" or self.tts_engine.is_speaking()
            if not is_speaking:
                return None

            # Evaluate VAD
            vad_res = self.vad.process_frame(pcm_frame)
            if vad_res.is_speech and vad_res.confidence >= 0.5:
                return self.trigger_barge_in(
                    reason=f"speech_detected_rms_{vad_res.energy_rms:.1f}",
                    current_state=current_state,
                    active_tts_text=active_tts_text,
                )

            return None

    def reset_stats(self) -> None:
        with self._lock:
            self._interruption_count = 0
            self._last_barge_in_event = None
