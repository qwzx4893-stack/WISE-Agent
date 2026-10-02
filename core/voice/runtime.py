# ==============================================================================
# WISE Persistent Voice Interface - Voice Runtime
# Architecture: Persistent, interruptible, low-idle voice runtime governing the
# cycle: IDLE -> LISTENING -> PROCESSING -> SPEAKING -> IDLE (with Barge-In).
# Strictly interfaces with authoritative P1.0 Cognitive Brain.
# ==============================================================================

from __future__ import annotations

import time
import logging
import threading
from enum import Enum
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field, asdict

from .vad import VoiceActivityDetector, VADResult
from .microphone import WindowsMicrophoneDriver
from .stt import SpeechToTextEngine, TranscriptionResult, BaseSTTProvider
from .tts import TextToSpeechEngine, TTSResult, BaseTTSProvider
from .barge_in import BargeInController, BargeInEvent

from core.context.world_state import get_world_state_engine
from core.orchestrator import get_closed_loop_orchestrator, OrchestrationCycleResult

LOG = logging.getLogger("WISE.Voice.Runtime")


class VoiceRuntimeState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    ERROR = "ERROR"


@dataclass
class VoiceInteractionResult:
    transcript: str
    response_text: str
    state: VoiceRuntimeState
    vad_latency_ms: float = 0.0
    stt_latency_ms: float = 0.0
    tts_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    interrupted: bool = False
    success: bool = True
    error: Optional[str] = None
    cycle_result: Optional[OrchestrationCycleResult] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transcript": self.transcript,
            "response_text": self.response_text,
            "state": self.state.value if hasattr(self.state, "value") else str(self.state),
            "vad_latency_ms": round(self.vad_latency_ms, 2),
            "stt_latency_ms": round(self.stt_latency_ms, 2),
            "tts_latency_ms": round(self.tts_latency_ms, 2),
            "total_latency_ms": round(self.total_latency_ms, 2),
            "interrupted": self.interrupted,
            "success": self.success,
            "error": self.error,
            "cycle_result": self.cycle_result.to_dict() if self.cycle_result else None,
        }


class VoiceRuntime:
    """
    Unified Persistent Natural Voice Runtime for WISE.
    Runs persistently in the user's interactive session with minimal footprint.
    Does NOT continuously record audio to disk, does NOT continuously run STT/LLM/Vision.
    Only activates heavy pipelines when speech is detected by lightweight VAD.
    """

    def __init__(
        self,
        stt_provider: Optional[BaseSTTProvider] = None,
        tts_provider: Optional[BaseTTSProvider] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._state = VoiceRuntimeState.IDLE
        self._is_running = False
        self._external_stt_provider = stt_provider is not None
        self._external_tts_provider = tts_provider is not None

        # Subsystems
        self.vad = VoiceActivityDetector(sample_rate=16000, frame_duration_ms=20)
        self.microphone = WindowsMicrophoneDriver(sample_rate=16000, channels=1)
        self.stt = SpeechToTextEngine(primary_provider=stt_provider)
        self.tts = TextToSpeechEngine(primary_provider=tts_provider)
        self.barge_in = BargeInController(
            tts_engine=self.tts,
            vad=self.vad,
            on_barge_in=self._on_barge_in_triggered,
        )

        # Brain connection
        self.orchestrator = get_closed_loop_orchestrator()
        self.state_engine = get_world_state_engine()

        # Background listener thread
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Metrics & Telemetry
        self._last_vad_latency: float = 0.0
        self._last_stt_latency: float = 0.0
        self._last_tts_latency: float = 0.0
        self._last_total_latency: float = 0.0
        self._total_interactions: int = 0
        self._last_turn: Optional[Dict[str, Any]] = None
        self._last_start_error: Optional[str] = None
        self._current_speech_buffer: bytearray = bytearray()
        self._active_tts_text: Optional[str] = None
        self._interrupted_turn = threading.Event()

    @property
    def state(self) -> VoiceRuntimeState:
        with self._lock:
            return self._state

    def _set_state(self, new_state: VoiceRuntimeState) -> None:
        with self._lock:
            old_state = self._state
            self._state = new_state
            LOG.info("VoiceRuntime State Transition: %s -> %s", old_state.value, new_state.value)

            # Update World State
            try:
                if hasattr(self.state_engine, "update_voice_state"):
                    self.state_engine.update_voice_state(
                        status=new_state.value,
                        interruption_count=self.barge_in.interruption_count,
                        stt_latency_ms=self._last_stt_latency,
                        tts_latency_ms=self._last_tts_latency,
                        vad_latency_ms=self._last_vad_latency,
                        total_latency_ms=self._last_total_latency,
                    )
            except Exception as e:
                LOG.warning("Failed to sync voice state to world state: %s", e)

    def _on_barge_in_triggered(self, event: BargeInEvent) -> None:
        """Invoked when BargeInController purges active speech."""
        with self._lock:
            if self._state == VoiceRuntimeState.SPEAKING:
                self._interrupted_turn.set()
                self._set_state(VoiceRuntimeState.INTERRUPTED)
                # Rapidly pivot to LISTENING to capture new user speech
                self._set_state(VoiceRuntimeState.LISTENING)
                self._current_speech_buffer.clear()

    def start(self) -> bool:
        """
        Starts the persistent voice runtime.
        Opens audio input readiness and launches lightweight background event loop.
        """
        with self._lock:
            if self._is_running:
                return True

            # Settings may have changed while the singleton was stopped.
            if not getattr(self, "_external_stt_provider", True):
                self.stt = SpeechToTextEngine()
            if not getattr(self, "_external_tts_provider", True):
                self.tts = TextToSpeechEngine()
                self.barge_in.tts_engine = self.tts

            # Do not advertise a resident listener when any required part of
            # its live audio path is unavailable.  Test-only providers remain
            # injectable by tests but are never selected by production setup.
            microphone_ready = self.microphone.is_available()
            stt_ready = self.stt.primary_provider.is_available()
            tts_ready = bool(getattr(self.tts.primary_provider, "is_available", lambda: False)())
            resource_check = getattr(self.tts.primary_provider, "resource_status", None)
            resource = resource_check() if callable(resource_check) and tts_ready else {"ready": True}
            tts_ready = tts_ready and bool(resource.get("ready"))
            if not (microphone_ready and stt_ready and tts_ready):
                missing = []
                if not microphone_ready:
                    missing.append("microphone")
                if not stt_ready:
                    missing.append("speech-to-text")
                if not tts_ready:
                    missing.append("text-to-speech")
                self._last_start_error = str(resource.get("reason") or "Voice components unavailable: " + ", ".join(missing))
                LOG.warning("Voice runtime not started; unavailable components: %s", ", ".join(missing))
                self._set_state(VoiceRuntimeState.ERROR)
                return False

            if not self.microphone.start():
                self._last_start_error = "Microphone capture could not be opened"
                LOG.warning("Voice runtime not started; microphone capture could not be opened")
                self._set_state(VoiceRuntimeState.ERROR)
                return False

            self._stop_event.clear()
            self._last_start_error = None
            self._interrupted_turn.clear()
            self._is_running = True
            self._set_state(VoiceRuntimeState.IDLE)

            # Start background audio monitoring thread
            self._worker_thread = threading.Thread(
                target=self._background_audio_loop,
                name="WISE_Voice_Runtime_Worker",
                daemon=True,
            )
            self._worker_thread.start()
            LOG.info("WISE VoiceRuntime started successfully.")
            return True

    def stop(self) -> None:
        """
        Stops the persistent voice runtime cleanly without thread leakage.
        """
        with self._lock:
            if not self._is_running:
                return

            self._is_running = False
            self._stop_event.set()
            self._interrupted_turn.set()

            # Halts active audio output
            try:
                self.tts.stop()
            except Exception as e:
                LOG.warning("Error stopping TTS during shutdown: %s", e)

            # Stops microphone capture
            try:
                self.microphone.stop()
            except Exception as e:
                LOG.warning("Error stopping Microphone during shutdown: %s", e)

            try:
                stop_stt = getattr(self.stt.primary_provider, "stop", None)
                if stop_stt:
                    stop_stt()
            except Exception as e:
                LOG.warning("Error stopping STT worker during shutdown: %s", e)

            self._set_state(VoiceRuntimeState.IDLE)

        # Join thread outside lock
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)
            self._worker_thread = None

        LOG.info("WISE VoiceRuntime stopped cleanly.")

    def _background_audio_loop(self) -> None:
        """
        Lightweight background monitoring loop.
        Zero heavy processing in IDLE:
        Discards audio frames when no speech is detected.
        Zero permanent raw audio storage.
        """
        while not self._stop_event.is_set():
            try:
                # Read 20ms frame from microphone queue
                frame = self.microphone.read_frame(timeout=0.05)
                if not frame:
                    continue

                with self._lock:
                    current_state = self._state

                # 1. In SPEAKING state: check for user barge-in
                if current_state == VoiceRuntimeState.SPEAKING:
                    interruption = self.barge_in.evaluate_audio_frame(
                        pcm_frame=frame,
                        current_state="SPEAKING",
                        active_tts_text=self._active_tts_text,
                    )
                    if interruption is not None:
                        self._current_speech_buffer.extend(frame)
                    continue

                # 2. In IDLE state: run lightweight VAD
                if current_state == VoiceRuntimeState.IDLE:
                    vad_res = self.vad.process_frame(frame)
                    if vad_res.is_speech and vad_res.confidence >= 0.5:
                        self._set_state(VoiceRuntimeState.LISTENING)
                        self._current_speech_buffer.clear()
                        self._current_speech_buffer.extend(frame)
                    continue

                # 3. In LISTENING state: accumulate speech segment
                if current_state == VoiceRuntimeState.LISTENING:
                    self._current_speech_buffer.extend(frame)
                    vad_res = self.vad.process_frame(frame)

                    # Limit maximum speech buffer to 15 seconds to prevent unbounded memory growth
                    max_bytes = 16000 * 2 * 15
                    if len(self._current_speech_buffer) > max_bytes or vad_res.is_speech_ended:
                        # Extract audio data and transition to PROCESSING
                        audio_data = bytes(self._current_speech_buffer)
                        self._current_speech_buffer.clear()
                        self.vad.reset()
                        self._set_state(VoiceRuntimeState.PROCESSING)

                        # Dispatch processing asynchronously in worker thread
                        threading.Thread(
                            target=self._process_speech_segment,
                            args=(audio_data,),
                            name="WISE_Voice_Processor",
                            daemon=True,
                        ).start()

            except Exception as loop_err:
                LOG.error("Exception in VoiceRuntime audio loop: %s", loop_err)
                time.sleep(0.05)

    def _process_speech_segment(self, audio_data: bytes) -> None:
        """Processes an extracted speech segment through STT -> Cognitive Brain -> TTS."""
        if len(audio_data) < 3200:  # Less than 100ms
            self._set_state(VoiceRuntimeState.IDLE)
            return

        self._set_state(VoiceRuntimeState.PROCESSING)
        self._interrupted_turn.clear()
        self.interact(audio_data)

    def interact(
        self,
        transcript_or_audio: Union[str, bytes],
        session_id: Optional[str] = None,
    ) -> VoiceInteractionResult:
        """
        Executes a complete voice interaction cycle:
        Speech -> STT -> P1.0 Cognitive Brain -> Verification -> Response Text -> TTS.

        Accepts either:
        - raw PCM audio bytes (16kHz 16-bit mono)
        - transcribed text intent string
        """
        t_start = time.perf_counter()
        vad_lat = 0.0
        stt_lat = 0.0
        tts_lat = 0.0
        transcript = ""
        response_text = ""
        cycle_res: Optional[OrchestrationCycleResult] = None
        interrupted = False
        success = True
        error_msg: Optional[str] = None

        self._set_state(VoiceRuntimeState.PROCESSING)

        try:
            # 1. VAD & STT Stage
            if isinstance(transcript_or_audio, bytes):
                # Measure VAD latency
                t_vad = time.perf_counter()
                vad_res = self.vad.process_frame(transcript_or_audio[:640])
                vad_lat = (time.perf_counter() - t_vad) * 1000

                # Transcribe via STT
                stt_res = self.stt.transcribe(transcript_or_audio)
                stt_lat = stt_res.latency_ms
                transcript = stt_res.text.strip()
                if not stt_res.success:
                    error_msg = stt_res.error
            else:
                transcript = transcript_or_audio.strip()

            self._last_vad_latency = vad_lat
            self._last_stt_latency = stt_lat

            if not transcript:
                self._set_state(VoiceRuntimeState.IDLE)
                return VoiceInteractionResult(
                    transcript="",
                    response_text="",
                    state=VoiceRuntimeState.IDLE,
                    vad_latency_ms=vad_lat,
                    stt_latency_ms=stt_lat,
                    total_latency_ms=(time.perf_counter() - t_start) * 1000,
                    success=False,
                    error=error_msg or "No speech transcribed or empty audio input.",
                )

            # 2. Cognitive Brain Stage (Unified Conversational Core - Shared with Chat)
            LOG.info("Routing voice transcript to Unified Conversational Core: '%s'", transcript)
            try:
                from core.brain.conversational_core import get_conversational_core
                from core.session_service import get_session_service

                sid = session_id or "voice_session"
                ss = get_session_service()
                session = ss.get_or_create_session(sid)

                cc = get_conversational_core()
                turn_res = cc.process_turn(transcript, session_id=session.session_id, modality="voice")
                response_text = turn_res.reply_text
                success = (turn_res.error is None)
                if turn_res.task_id:
                    cycle_res = OrchestrationCycleResult(
                        intent=transcript,
                        success=success,
                        steps_executed=len(turn_res.milestones),
                        paused_for_human=turn_res.paused_for_human,
                        intervention_details=turn_res.intervention_details,
                        error=turn_res.error,
                    )
            except Exception as cc_err:
                LOG.warning("ConversationalCore direct route error (%s); falling back to direct orchestrator", cc_err)
                cycle_res = self.orchestrator.orchestrate_intent(transcript)
                success = cycle_res.success
                is_arabic = any("\u0600" <= c <= "\u06FF" for c in transcript)
                if cycle_res.needs_clarification:
                    response_text = cycle_res.clarification_message or (
                        "لا أملك معلومات كافية لإكمال هذا الطلب." if is_arabic else "I do not have enough information to proceed."
                    )
                elif not cycle_res.success:
                    response_text = f"An error occurred: {cycle_res.error}"
                else:
                    response_text = f"Task completed successfully in {cycle_res.steps_executed} steps."


            if response_text and not self._interrupted_turn.is_set():
                self._set_state(VoiceRuntimeState.SPEAKING)
                self._active_tts_text = response_text
                try:
                    tts_res = self.tts.speak(response_text, async_playback=True)
                    tts_lat = tts_res.latency_ms
                    self._last_tts_latency = tts_lat
                    if not tts_res.success and not self._interrupted_turn.is_set():
                        success = False
                        error_msg = tts_res.error or "speech synthesis failed"
                    # The microphone thread keeps running during generation and
                    # playback.  Remain SPEAKING until output really ends so
                    # a new utterance can interrupt it.
                    while self.tts.is_speaking() and not self._interrupted_turn.is_set() and not self._stop_event.is_set():
                        time.sleep(0.04)
                finally:
                    self._active_tts_text = None
                interrupted = self._interrupted_turn.is_set()
                with self._lock:
                    if self._state == VoiceRuntimeState.SPEAKING:
                        self._set_state(VoiceRuntimeState.IDLE)
            elif self._state == VoiceRuntimeState.PROCESSING:
                self._set_state(VoiceRuntimeState.IDLE)

        except Exception as e:
            LOG.error("Voice interaction error: %s", e)
            self._set_state(VoiceRuntimeState.ERROR)
            success = False
            error_msg = str(e)
            # Self-heal back to IDLE
            self._set_state(VoiceRuntimeState.IDLE)

        total_lat = (time.perf_counter() - t_start) * 1000
        self._last_total_latency = total_lat
        self._total_interactions += 1

        effective_error = error_msg or (cycle_res.error if cycle_res and not cycle_res.success else None)

        result = VoiceInteractionResult(
            transcript=transcript,
            response_text=response_text,
            state=self._state,
            vad_latency_ms=vad_lat,
            stt_latency_ms=stt_lat,
            tts_latency_ms=tts_lat,
            total_latency_ms=total_lat,
            interrupted=interrupted,
            success=success,
            error=effective_error,
            cycle_result=cycle_res,
        )
        self._last_turn = {"number": self._total_interactions, **result.to_dict()}
        return result

    def probe_voice_environment(self) -> Dict[str, Any]:
        """
        Probes the native Windows environment for speech and audio capabilities.
        Reports honestly:
        - Microphone status & WinMM devices
        - SAPI TTS voices & Arabic voice pack status
        - Windows Speech Recognition & Arabic STT language pack status
        """
        mic_info = self.microphone.get_device_info()
        stt_info = self.stt.probe_environment()
        tts_info = self.tts.probe_environment()

        return {
            "microphone": mic_info,
            "speech_to_text": stt_info,
            "text_to_speech": tts_info,
            "start_error": self._last_start_error,
            "barge_in_ready": bool(
                mic_info.get("is_available")
                and stt_info.get("selected_stt_available")
                and self.tts.primary_provider.is_available()
                and (tts_info.get("cloned_voice", {}).get("resource", {}).get("ready", True))
            ),
            "runtime_state": self._state.value,
            "total_interactions": self._total_interactions,
            "interruption_count": self.barge_in.interruption_count,
        }


# Global Singleton Voice Runtime
_VOICE_RUNTIME: Optional[VoiceRuntime] = None
_VR_LOCK = threading.Lock()


def get_voice_runtime(
    stt_provider: Optional[BaseSTTProvider] = None,
    tts_provider: Optional[BaseTTSProvider] = None,
) -> VoiceRuntime:
    global _VOICE_RUNTIME
    if _VOICE_RUNTIME is None:
        with _VR_LOCK:
            if _VOICE_RUNTIME is None:
                _VOICE_RUNTIME = VoiceRuntime(stt_provider=stt_provider, tts_provider=tts_provider)
    return _VOICE_RUNTIME
