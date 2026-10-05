# ==============================================================================
# WISE Persistent Voice Interface - Voice Activity Detection (VAD)
# Architecture: Energy-envelope, Zero-Crossing Rate & Adaptive Noise Floor Tracker
# Deterministic, offline, zero-network, sub-millisecond per frame, <0.1% CPU.
# ==============================================================================

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple
import numpy as np

LOG = logging.getLogger("WISE.Voice.VAD")


@dataclass
class VADFrameResult:
    is_speech: bool
    rms_energy: float
    zcr: float
    noise_floor: float
    speech_detected_now: bool  # True on the exact frame speech started
    speech_ended_now: bool     # True on the exact frame speech completed
    timestamp: float = field(default_factory=time.time)

    @property
    def energy_rms(self) -> float:
        return self.rms_energy

    @property
    def is_speech_ended(self) -> bool:
        return self.speech_ended_now

    @property
    def confidence(self) -> float:
        threshold = max(300.0, self.noise_floor * 2.5)
        ratio = self.rms_energy / max(1.0, threshold)
        return min(1.0, max(0.0, ratio / 2.0))


VADResult = VADFrameResult


class VoiceActivityDetector:
    """
    Lightweight, robust Voice Activity Detector for 16-bit 16kHz mono PCM.
    Tracks ambient noise floor dynamically to adapt to changing environments.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_duration_ms: int = 20,
        energy_threshold_multiplier: float = 2.5,
        min_energy_threshold: float = 300.0,
        speech_onset_frames: int = 3,       # ~60ms to trigger speech start
        speech_hangover_frames: int = 25,    # ~500ms silence to trigger speech end
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.frame_size = int(sample_rate * (frame_duration_ms / 1000.0))  # 320 samples for 20ms
        self.frame_bytes = self.frame_size * 2  # 16-bit = 2 bytes per sample

        self.energy_threshold_multiplier = energy_threshold_multiplier
        self.min_energy_threshold = min_energy_threshold
        self.speech_onset_frames = speech_onset_frames
        self.speech_hangover_frames = speech_hangover_frames

        # State tracking
        self.noise_floor = min_energy_threshold
        self.consecutive_speech_frames = 0
        self.consecutive_silence_frames = 0
        self.is_in_speech = False
        self.noise_adapt_alpha = 0.95
        self.total_frames_processed = 0

    def reset(self) -> None:
        """Resets VAD state and counters."""
        self.noise_floor = self.min_energy_threshold
        self.consecutive_speech_frames = 0
        self.consecutive_silence_frames = 0
        self.is_in_speech = False
        self.total_frames_processed = 0

    def process_frame(self, pcm_bytes: bytes) -> VADFrameResult:
        """
        Processes a single audio frame (16-bit 16kHz PCM).
        Returns classification and boundary transition flags.
        """
        if not pcm_bytes:
            return VADFrameResult(
                is_speech=False,
                rms_energy=0.0,
                zcr=0.0,
                noise_floor=self.noise_floor,
                speech_detected_now=False,
                speech_ended_now=False,
            )

        # Convert bytes to numpy 16-bit signed integers
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        if len(samples) == 0:
            return VADFrameResult(
                is_speech=False,
                rms_energy=0.0,
                zcr=0.0,
                noise_floor=self.noise_floor,
                speech_detected_now=False,
                speech_ended_now=False,
            )

        # Compute RMS energy
        samples_float = samples.astype(np.float32)
        rms = float(np.sqrt(np.mean(samples_float ** 2)))

        # Compute Zero Crossing Rate (ZCR)
        diff_signs = np.diff(np.signbit(samples))
        zcr = float(np.mean(diff_signs != 0)) if len(diff_signs) > 0 else 0.0

        # Dynamic Threshold
        current_threshold = max(
            self.min_energy_threshold,
            self.noise_floor * self.energy_threshold_multiplier,
        )

        frame_is_active = rms > current_threshold
        speech_started = False
        speech_ended = False

        self.total_frames_processed += 1

        if frame_is_active:
            self.consecutive_speech_frames += 1
            self.consecutive_silence_frames = 0

            if not self.is_in_speech:
                if self.consecutive_speech_frames >= self.speech_onset_frames:
                    self.is_in_speech = True
                    speech_started = True
                    LOG.debug("VAD: Speech onset detected (RMS=%.1f, Thresh=%.1f)", rms, current_threshold)
        else:
            self.consecutive_silence_frames += 1
            self.consecutive_speech_frames = 0

            # Adapt noise floor during sustained non-speech
            if not self.is_in_speech and self.consecutive_silence_frames > 5:
                self.noise_floor = (
                    self.noise_adapt_alpha * self.noise_floor
                    + (1.0 - self.noise_adapt_alpha) * rms
                )

            if self.is_in_speech:
                if self.consecutive_silence_frames >= self.speech_hangover_frames:
                    self.is_in_speech = False
                    speech_ended = True
                    LOG.debug("VAD: Speech offset detected (Silence frames=%d)", self.consecutive_silence_frames)

        return VADFrameResult(
            is_speech=self.is_in_speech,
            rms_energy=rms,
            zcr=zcr,
            noise_floor=self.noise_floor,
            speech_detected_now=speech_started,
            speech_ended_now=speech_ended,
        )

    def is_speech(self, pcm_bytes: bytes) -> bool:
        """Convenience boolean check for a PCM frame."""
        return self.process_frame(pcm_bytes).is_speech
