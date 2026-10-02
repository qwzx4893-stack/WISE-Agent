"""
WISE Core Operational State Machine.
Defines explicit operational states, valid transition rules, and resource-profile hooks.
Ensures zero-compute idle and strict resource lifecycle enforcement.
"""

from __future__ import annotations

import time
import logging
import threading
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

LOG = logging.getLogger("wise.state_machine")


class WiseState(str, Enum):
    BOOT = "BOOT"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    IDLE = "IDLE"
    AWAKENING = "AWAKENING"
    OBSERVING = "OBSERVING"
    REASONING = "REASONING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_PERMISSION = "WAITING_FOR_PERMISSION"
    RECOVERING = "RECOVERING"
    ERROR = "ERROR"
    SHUTDOWN = "SHUTDOWN"


@dataclass(frozen=True)
class ResourceProfile:
    """Hardware and worker activation boundaries for a given state."""
    is_low_power: bool
    heavy_models_allowed: bool
    vision_active: bool
    audio_full_stream: bool
    browser_active: bool
    react_active: bool
    max_cpu_percent_target: float


STATE_RESOURCE_PROFILES: Dict[WiseState, ResourceProfile] = {
    WiseState.BOOT: ResourceProfile(
        is_low_power=False,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=False,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=25.0,
    ),
    WiseState.INITIALIZING: ResourceProfile(
        is_low_power=False,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=False,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=30.0,
    ),
    WiseState.READY: ResourceProfile(
        is_low_power=True,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=False,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=5.0,
    ),
    WiseState.IDLE: ResourceProfile(
        is_low_power=True,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=False,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=1.0,  # Zero/low compute idle
    ),
    WiseState.AWAKENING: ResourceProfile(
        is_low_power=False,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=True,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=15.0,
    ),
    WiseState.OBSERVING: ResourceProfile(
        is_low_power=False,
        heavy_models_allowed=False,
        vision_active=True,
        audio_full_stream=True,
        browser_active=True,
        react_active=False,
        max_cpu_percent_target=40.0,
    ),
    WiseState.REASONING: ResourceProfile(
        is_low_power=False,
        heavy_models_allowed=True,
        vision_active=False,
        audio_full_stream=False,
        browser_active=False,
        react_active=True,
        max_cpu_percent_target=70.0,
    ),
    WiseState.EXECUTING: ResourceProfile(
        is_low_power=False,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=False,
        browser_active=True,
        react_active=True,
        max_cpu_percent_target=50.0,
    ),
    WiseState.VERIFYING: ResourceProfile(
        is_low_power=False,
        heavy_models_allowed=False,
        vision_active=True,
        audio_full_stream=False,
        browser_active=True,
        react_active=False,
        max_cpu_percent_target=35.0,
    ),
    WiseState.WAITING_FOR_USER: ResourceProfile(
        is_low_power=True,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=True,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=3.0,
    ),
    WiseState.WAITING_FOR_PERMISSION: ResourceProfile(
        is_low_power=True,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=False,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=2.0,
    ),
    WiseState.RECOVERING: ResourceProfile(
        is_low_power=False,
        heavy_models_allowed=True,
        vision_active=True,
        audio_full_stream=False,
        browser_active=True,
        react_active=True,
        max_cpu_percent_target=60.0,
    ),
    WiseState.ERROR: ResourceProfile(
        is_low_power=True,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=False,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=5.0,
    ),
    WiseState.SHUTDOWN: ResourceProfile(
        is_low_power=True,
        heavy_models_allowed=False,
        vision_active=False,
        audio_full_stream=False,
        browser_active=False,
        react_active=False,
        max_cpu_percent_target=0.0,
    ),
}


@dataclass
class StateTransitionRecord:
    from_state: WiseState
    to_state: WiseState
    timestamp: float
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class WiseStateMachine:
    """Thread-safe operational state machine with deterministic transitions and hooks."""

    # Explicit allowed transitions graph
    VALID_TRANSITIONS: Dict[WiseState, Set[WiseState]] = {
        WiseState.BOOT: {WiseState.INITIALIZING, WiseState.ERROR, WiseState.SHUTDOWN},
        WiseState.INITIALIZING: {WiseState.READY, WiseState.ERROR, WiseState.SHUTDOWN},
        WiseState.READY: {WiseState.IDLE, WiseState.AWAKENING, WiseState.ERROR, WiseState.SHUTDOWN},
        WiseState.IDLE: {WiseState.AWAKENING, WiseState.OBSERVING, WiseState.REASONING, WiseState.SHUTDOWN, WiseState.ERROR},
        WiseState.AWAKENING: {WiseState.OBSERVING, WiseState.REASONING, WiseState.IDLE, WiseState.ERROR, WiseState.WAITING_FOR_USER},
        WiseState.OBSERVING: {WiseState.REASONING, WiseState.EXECUTING, WiseState.IDLE, WiseState.ERROR},
        WiseState.REASONING: {WiseState.EXECUTING, WiseState.WAITING_FOR_USER, WiseState.WAITING_FOR_PERMISSION, WiseState.RECOVERING, WiseState.IDLE, WiseState.ERROR},
        WiseState.EXECUTING: {WiseState.VERIFYING, WiseState.RECOVERING, WiseState.WAITING_FOR_USER, WiseState.WAITING_FOR_PERMISSION, WiseState.IDLE, WiseState.ERROR},
        WiseState.VERIFYING: {WiseState.IDLE, WiseState.RECOVERING, WiseState.EXECUTING, WiseState.ERROR},
        WiseState.WAITING_FOR_USER: {WiseState.AWAKENING, WiseState.REASONING, WiseState.EXECUTING, WiseState.IDLE, WiseState.SHUTDOWN},
        WiseState.WAITING_FOR_PERMISSION: {WiseState.EXECUTING, WiseState.REASONING, WiseState.IDLE, WiseState.ERROR},
        WiseState.RECOVERING: {WiseState.REASONING, WiseState.EXECUTING, WiseState.IDLE, WiseState.ERROR},
        WiseState.ERROR: {WiseState.READY, WiseState.IDLE, WiseState.RECOVERING, WiseState.SHUTDOWN},
        WiseState.SHUTDOWN: set(),  # Terminal state
    }

    def __init__(self, initial_state: WiseState = WiseState.BOOT):
        self._state: WiseState = initial_state
        self._lock = threading.RLock()
        self._history: List[StateTransitionRecord] = []
        self._listeners: List[Callable[[WiseState, WiseState, str], None]] = []
        self._state_entered_time: float = time.time()

    @property
    def current_state(self) -> WiseState:
        with self._lock:
            return self._state

    @property
    def current_profile(self) -> ResourceProfile:
        with self._lock:
            return STATE_RESOURCE_PROFILES[self._state]

    @property
    def time_in_current_state(self) -> float:
        with self._lock:
            return time.time() - self._state_entered_time

    def can_transition_to(self, target: WiseState) -> bool:
        with self._lock:
            allowed = self.VALID_TRANSITIONS.get(self._state, set())
            return target in allowed

    def transition_to(self, target: WiseState, reason: str = "", metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Transitions state if valid, triggers registered listeners, and records history."""
        with self._lock:
            if target == self._state:
                return True

            allowed = self.VALID_TRANSITIONS.get(self._state, set())
            if target not in allowed:
                LOG.warning(
                    "Invalid state transition rejected: %s -> %s (Reason: %s)",
                    self._state.value, target.value, reason
                )
                return False

            old_state = self._state
            self._state = target
            now = time.time()
            self._state_entered_time = now

            record = StateTransitionRecord(
                from_state=old_state,
                to_state=target,
                timestamp=now,
                reason=reason,
                metadata=metadata or {},
            )
            self._history.append(record)
            if len(self._history) > 200:
                self._history.pop(0)

            LOG.info("WISE State Transition: %s -> %s | Reason: %s", old_state.value, target.value, reason or "None")

            # Fire listeners outside transition block or with copy
            listeners = list(self._listeners)

        for listener in listeners:
            try:
                listener(old_state, target, reason)
            except Exception as e:
                LOG.error("Error in state transition listener: %s", e)

        return True

    def add_listener(self, listener: Callable[[WiseState, WiseState, str], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[WiseState, WiseState, str], None]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "from": r.from_state.value,
                    "to": r.to_state.value,
                    "timestamp": r.timestamp,
                    "reason": r.reason,
                    "metadata": r.metadata,
                }
                for r in self._history[-limit:]
            ]


# Singleton instance
_GLOBAL_STATE_MACHINE: Optional[WiseStateMachine] = None
_INIT_LOCK = threading.Lock()


def get_state_machine() -> WiseStateMachine:
    global _GLOBAL_STATE_MACHINE
    if _GLOBAL_STATE_MACHINE is None:
        with _INIT_LOCK:
            if _GLOBAL_STATE_MACHINE is None:
                _GLOBAL_STATE_MACHINE = WiseStateMachine()
    return _GLOBAL_STATE_MACHINE
