# ==============================================================================
# WISE Unified World State Subsystem
# Architecture: Consolidated Environmental Model Unifying Eyes, Hands, and Brain
# Every state element maintains Source, Timestamp, Confidence, Provenance, and Scope.
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import logging
import threading
from typing import Any, Dict, List, Optional, TypeVar, Generic
from dataclasses import dataclass, field, asdict

from core.state.state_machine import get_state_machine, WiseState
from core.resource.resource_manager import get_resource_manager, HardwareMetrics
from core.windows.window_manager import get_window_manager, WindowInfo
from core.context.world_model import get_world_model_manager, ActiveWindowInfo, OSInfo, UserSessionInfo, FilesInfo
from core.vision.ui_tree import UIElementNode
from core.vision.ocr_engine import OCRResult
from core.hands.computer_use import ActionRecord

LOG = logging.getLogger("WISE.Context.WorldState")

T = TypeVar("T")


@dataclass
class StateItem(Generic[T]):
    """
    Standardized atomic state container.
    Guarantees strict auditability: Source, Timestamp, Freshness, Confidence, Provenance, and Scope.
    """
    key: str
    value: T
    source: str
    timestamp: float = field(default_factory=time.time)
    confidence: float = 1.0  # 0.0 to 1.0
    provenance: str = ""
    scope: str = "global"  # "active_window", "system", "workspace", "current_task", "visual"

    @property
    def freshness_seconds(self) -> float:
        """Dynamic age of the state item in seconds."""
        return max(0.0, time.time() - self.timestamp)

    def is_stale(self, ttl_seconds: float = 30.0, max_age_seconds: Optional[float] = None) -> bool:
        """Determines if the item has exceeded its time-to-live."""
        effective_ttl = max_age_seconds if max_age_seconds is not None else ttl_seconds
        return self.freshness_seconds > effective_ttl


    def to_dict(self) -> Dict[str, Any]:
        from core.security.transient_vault import redact_sensitive_payload
        val = self.value
        if hasattr(val, "to_dict"):
            val = val.to_dict()
        elif hasattr(val, "__dict__"):
            val = val.__dict__
        return {
            "key": self.key,
            "value": redact_sensitive_payload(val),
            "source": self.source,
            "timestamp": self.timestamp,
            "freshness_seconds": round(self.freshness_seconds, 2),
            "confidence": round(self.confidence, 3),
            "provenance": self.provenance,
            "scope": self.scope,
        }


@dataclass
class VisualStateSummary:
    has_active_capture: bool = False
    resolution: str = "0x0"
    ocr_text_preview: str = ""
    ocr_block_count: int = 0
    visual_targets: List[Dict[str, Any]] = field(default_factory=list)
    captured_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskProgressState:
    intent: str = ""
    current_step: int = 0
    total_steps: int = 0
    active_plan: List[str] = field(default_factory=list)
    completed_steps: List[str] = field(default_factory=list)
    pending_operations: List[Dict[str, Any]] = field(default_factory=list)
    verification_status: str = "NOT_STARTED"  # "NOT_STARTED", "IN_PROGRESS", "VERIFIED", "FAILED"
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VoiceSessionState:
    status: str = "IDLE"  # "IDLE", "LISTENING", "PROCESSING", "SPEAKING", "INTERRUPTED", "ERROR"
    mic_available: bool = True
    active_session_id: Optional[str] = None
    last_transcript: str = ""
    last_spoken_response: str = ""
    interruption_count: int = 0
    stt_latency_ms: float = 0.0
    tts_latency_ms: float = 0.0
    vad_latency_ms: float = 0.0
    total_voice_latency_ms: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WISEWorldState:
    """
    The Unified Cognitive Environmental Picture of WISE.
    Combines:
    - System & OS Attributes
    - Sensory & Visual State (Eyes)
    - Action & Execution History (Hands)
    - Intent, Plan, and Reasoning (Brain)
    - Persistent Voice Interaction State
    - Filesystem, Workspace & Hardware Telemetry
    """
    timestamp: float = field(default_factory=time.time)

    # Scoped State Items Map
    items: Dict[str, StateItem[Any]] = field(default_factory=dict)

    # Core High-Signal Domain Objects
    os_info: OSInfo = field(default_factory=OSInfo)
    user_session: UserSessionInfo = field(default_factory=UserSessionInfo)
    active_window: Optional[WindowInfo] = None
    open_windows: List[WindowInfo] = field(default_factory=list)
    open_dialogs: List[WindowInfo] = field(default_factory=list)
    ui_tree: Optional[UIElementNode] = None
    visual_state: VisualStateSummary = field(default_factory=VisualStateSummary)
    task_state: TaskProgressState = field(default_factory=TaskProgressState)
    voice_state: VoiceSessionState = field(default_factory=VoiceSessionState)
    previous_actions: List[ActionRecord] = field(default_factory=list)
    hardware_metrics: Optional[HardwareMetrics] = None
    files_info: FilesInfo = field(default_factory=FilesInfo)
    arbitrated_facts: List[Dict[str, Any]] = field(default_factory=list)
    memory_snippets: List[Dict[str, Any]] = field(default_factory=list)

    def get_item(self, key: str) -> Optional[StateItem[Any]]:
        return self.items.get(key)

    def add_item(self, item: StateItem[Any]) -> None:
        from core.security.transient_vault import is_sensitive_key, redact_sensitive_payload
        if is_sensitive_key(item.key):
            LOG.warning("Attempt to add sensitive item '%s' to World State blocked. Sanitizing value.", item.key)
            scrubbed_item = StateItem(
                key=item.key,
                value="[REDACTED:SENSITIVE_DATA]",
                source=item.source,
                timestamp=item.timestamp,
                confidence=item.confidence,
                provenance=item.provenance,
                scope=item.scope,
            )
            self.items[item.key] = scrubbed_item
        else:
            cleaned_val = redact_sensitive_payload(item.value)
            scrubbed_item = StateItem(
                key=item.key,
                value=cleaned_val,
                source=item.source,
                timestamp=item.timestamp,
                confidence=item.confidence,
                provenance=item.provenance,
                scope=item.scope,
            )
            self.items[item.key] = scrubbed_item

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "items_count": len(self.items),
            "items": {k: v.to_dict() for k, v in self.items.items()},
            "os": asdict(self.os_info),
            "user_session": asdict(self.user_session),
            "active_window": self.active_window.to_dict() if self.active_window else None,
            "open_windows_count": len(self.open_windows),
            "open_dialogs_count": len(self.open_dialogs),
            "ui_tree_elements_count": len(self.ui_tree.children) if self.ui_tree else 0,
            "visual_state": self.visual_state.to_dict(),
            "task_state": self.task_state.to_dict(),
            "voice_state": self.voice_state.to_dict(),
            "previous_actions_count": len(self.previous_actions),
            "resources": self.hardware_metrics.to_dict() if self.hardware_metrics else {},
            "files": asdict(self.files_info),
            "arbitrated_facts_count": len(self.arbitrated_facts),
        }


class WISEWorldStateEngine:
    """
    Central Engine responsible for synthesizing, updating, and serving the WISEWorldState.
    Queries Eyes, Hands, Brain, and OS subsystems without unnecessary background polling.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_snapshot: Optional[WISEWorldState] = None
        self._last_snapshot_time: float = 0.0
        self._cache_ttl_sec: float = 1.0  # 1 second freshness cache
        self._task_state: TaskProgressState = TaskProgressState()
        self._voice_state: VoiceSessionState = VoiceSessionState()

    def get_current_world_state(self, force_fresh: bool = False) -> WISEWorldState:
        """Produces a unified, high-integrity snapshot of the computer and cognitive state."""
        with self._lock:
            now = time.time()
            if not force_fresh and self._last_snapshot and (now - self._last_snapshot_time) < self._cache_ttl_sec:
                return self._last_snapshot

            t0 = time.perf_counter()
            wm_mgr = get_world_model_manager()
            win_mgr = get_window_manager()
            res_mgr = get_resource_manager()
            from core.hands import get_wise_hands
            hands = get_wise_hands()

            # 1. Fetch live metrics
            metrics = res_mgr.get_hardware_metrics()

            # 2. Window state
            active_win = win_mgr.get_foreground_window()
            all_wins = win_mgr.enumerate_windows(visible_only=True)
            dialogs: List[WindowInfo] = []
            if active_win:
                # If active foreground window is itself a modal dialog
                if active_win.class_name == "#32770" or any(tag in active_win.title.lower() for tag in ["dialog", "confirm", "replace", "حفظ باسم", "تأكيد", "warning"]):
                    dialogs.append(active_win)
                child_d = win_mgr.enumerate_child_dialogs(active_win.hwnd)
                for d in child_d:
                    if not any(d.hwnd == ex.hwnd for ex in dialogs):
                        dialogs.append(d)

            for w in all_wins:
                if any(w.hwnd == ex.hwnd for ex in dialogs):
                    continue
                if w.class_name == "#32770" or any(tag in w.title.lower() for tag in ["dialog", "confirm", "replace", "حفظ باسم", "تأكيد"]):
                    dialogs.append(w)

            # 3. UI Tree (Level 1 structured perception)
            from core.vision import get_ui_tree_extractor
            ui_extractor = get_ui_tree_extractor()
            active_tree = None
            if active_win:
                active_tree = ui_extractor.get_window_ui_tree(active_win.hwnd, max_depth=3)

            # 4. Previous actions
            recent_actions = hands.history[-10:] if hasattr(hands, "history") else []

            # 5. Build State Items
            items: Dict[str, StateItem[Any]] = {}

            # Active Window item
            if active_win:
                items["active_window"] = StateItem(
                    key="active_window",
                    value=active_win.title,
                    source="win32_window_manager",
                    timestamp=now,
                    confidence=1.0,
                    provenance=f"HWND: {active_win.hwnd}, PID: {active_win.process_id}",
                    scope="active_window",
                )

            # Resources item
            items["system_cpu"] = StateItem(
                key="system_cpu",
                value=metrics.cpu_percent,
                source="psutil_hardware_probe",
                timestamp=now,
                confidence=1.0,
                provenance=f"CPU: {metrics.cpu_name}",
                scope="system",
            )
            items["system_ram_mb"] = StateItem(
                key="system_ram_mb",
                value=metrics.ram_used_mb,
                source="psutil_hardware_probe",
                timestamp=now,
                confidence=1.0,
                provenance=f"Total: {metrics.ram_total_mb} MB",
                scope="system",
            )

            # GPU VRAM item
            items["gpu_vram_mb"] = StateItem(
                key="gpu_vram_mb",
                value=metrics.gpu_memory_used_mb,
                source="nvidia_smi_probe",
                timestamp=now,
                confidence=1.0,
                provenance=f"GPU: {metrics.gpu_name}",
                scope="system",
            )

            # Power item
            items["power_state"] = StateItem(
                key="power_state",
                value="AC_PLUGGED" if metrics.power_plugged else f"BATTERY_{metrics.battery_percent}%",
                source="psutil_battery_probe",
                timestamp=now,
                confidence=1.0,
                provenance="AC Power Broadcast / Battery Sensor",
                scope="system",
            )

            # Filesystem workspace item
            items["workspace_dir"] = StateItem(
                key="workspace_dir",
                value=str(wm_mgr.model.files_info.desktop_dir),
                source="filesystem_governor",
                timestamp=now,
                confidence=1.0,
                provenance="Windows Shell Known Folders",
                scope="workspace",
            )

            # Persistent Voice Interaction status item
            items["voice_status"] = StateItem(
                key="voice_status",
                value=self._voice_state.status,
                source="voice_runtime",
                timestamp=now,
                confidence=1.0,
                provenance=f"InterruptionCount: {self._voice_state.interruption_count}, Latency: {self._voice_state.total_voice_latency_ms:.1f}ms",
                scope="system",
            )

            # Assemble Unified World State
            state = WISEWorldState(
                timestamp=now,
                items=items,
                os_info=wm_mgr.model.os_info,
                user_session=wm_mgr.model.user_session,
                active_window=active_win,
                open_windows=all_wins,
                open_dialogs=dialogs,
                ui_tree=active_tree,
                task_state=TaskProgressState(**asdict(self._task_state)),
                voice_state=VoiceSessionState(**asdict(self._voice_state)),
                previous_actions=recent_actions,
                hardware_metrics=metrics,
                files_info=wm_mgr.model.files_info,
            )

            self._last_snapshot = state
            self._last_snapshot_time = now
            return state

    def update_task_progress(
        self,
        intent: str,
        current_step: int,
        total_steps: int,
        active_plan: List[str],
        verification_status: str,
    ) -> None:
        """Updates cognitive plan and progress state."""
        with self._lock:
            self._task_state.intent = intent
            self._task_state.current_step = current_step
            self._task_state.total_steps = total_steps
            self._task_state.active_plan = active_plan
            self._task_state.verification_status = verification_status
            self._task_state.updated_at = time.time()

            if self._last_snapshot:
                self._last_snapshot.task_state = TaskProgressState(**asdict(self._task_state))
                self._last_snapshot.add_item(StateItem(
                    key="task_intent",
                    value=intent,
                    source="user_intent_processor",
                    timestamp=time.time(),
                    confidence=1.0,
                    provenance="Direct User Input",
                    scope="current_task",
                ))

    def update_voice_state(
        self,
        status: str,
        interruption_count: int = 0,
        stt_latency_ms: float = 0.0,
        tts_latency_ms: float = 0.0,
        vad_latency_ms: float = 0.0,
        total_latency_ms: float = 0.0,
        last_transcript: str = "",
        last_spoken_response: str = "",
    ) -> None:
        """Updates persistent voice state and synchronizes with world snapshot."""
        with self._lock:
            self._voice_state.status = status
            self._voice_state.interruption_count = interruption_count
            self._voice_state.stt_latency_ms = stt_latency_ms
            self._voice_state.tts_latency_ms = tts_latency_ms
            self._voice_state.vad_latency_ms = vad_latency_ms
            self._voice_state.total_voice_latency_ms = total_latency_ms
            if last_transcript:
                self._voice_state.last_transcript = last_transcript
            if last_spoken_response:
                self._voice_state.last_spoken_response = last_spoken_response
            self._voice_state.updated_at = time.time()

            if self._last_snapshot:
                self._last_snapshot.voice_state = VoiceSessionState(**asdict(self._voice_state))
                self._last_snapshot.add_item(StateItem(
                    key="voice_status",
                    value=status,
                    source="voice_runtime",
                    timestamp=time.time(),
                    confidence=1.0,
                    provenance=f"InterruptionCount: {interruption_count}, Latency: {total_latency_ms:.1f}ms",
                    scope="system",
                ))

    def update_model_runtime_state(
        self,
        runtime_state: str,
        model_name: str = "",
        active_expert_count: int = 0,
        vram_used_mb: float = 0.0,
        ram_used_mb: float = 0.0,
        cache_hit_rate: float = 0.0,
        transfer_state: str = "IDLE",
        token_latency_ms: float = 0.0,
    ) -> None:
        """
        Publishes lightweight model runtime facts to World State.
        Maintains strict boundary: World State is a current-state representation,
        NOT the internal database of the model runtime.
        """
        with self._lock:
            now = time.time()
            payload = {
                "runtime_state": runtime_state,
                "model_name": model_name,
                "active_expert_count": active_expert_count,
                "vram_used_mb": round(vram_used_mb, 1),
                "ram_used_mb": round(ram_used_mb, 1),
                "cache_hit_rate_pct": round(cache_hit_rate, 1),
                "transfer_state": transfer_state,
                "token_latency_ms": round(token_latency_ms, 2),
            }
            if self._last_snapshot:
                self._last_snapshot.add_item(StateItem(
                    key="model_runtime",
                    value=payload,
                    source="moe_runtime",
                    timestamp=now,
                    confidence=1.0,
                    provenance=f"MoE Model: {model_name or 'unloaded'}, State: {runtime_state}",
                    scope="system",
                ))


# Global Singleton State Engine
_WORLD_STATE_ENGINE: Optional[WISEWorldStateEngine] = None
_WSE_LOCK = threading.Lock()


def get_world_state_engine() -> WISEWorldStateEngine:
    global _WORLD_STATE_ENGINE
    if _WORLD_STATE_ENGINE is None:
        with _WSE_LOCK:
            if _WORLD_STATE_ENGINE is None:
                _WORLD_STATE_ENGINE = WISEWorldStateEngine()
    return _WORLD_STATE_ENGINE
