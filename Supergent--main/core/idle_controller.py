"""Authoritative activity and idle lifecycle for the resident WISE runtime.

The state machine and resource governor already express how WISE should behave
in IDLE.  This controller is the missing clock: it tracks real work, moves the
runtime into an active state, and transitions it back to IDLE only after the
configured quiet interval.  No screen/audio polling is performed here.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

from core.state.state_machine import WiseState, WiseStateMachine, get_state_machine


LOG = logging.getLogger("wise.idle_controller")


class RuntimeIdleController:
    """Low-overhead lifecycle clock for foreground and scheduled WISE work."""

    def __init__(
        self,
        *,
        state_machine: Optional[WiseStateMachine] = None,
        resource_manager: Optional[Any] = None,
        timeout_provider: Optional[Callable[[], int]] = None,
        check_interval_seconds: float = 1.0,
    ) -> None:
        self._state_machine = state_machine or get_state_machine()
        if resource_manager is None:
            from core.resource.resource_manager import get_resource_manager
            resource_manager = get_resource_manager()
        self._resource_manager = resource_manager
        self._timeout_provider = timeout_provider or self._configured_timeout
        self._check_interval = max(0.2, float(check_interval_seconds))
        self._lock = threading.RLock()
        self._last_activity = time.monotonic()
        self._active_operations = 0
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _configured_timeout() -> int:
        """Read the owner-selected timeout only for enabled resident mode."""
        try:
            from core.system_integration import get_system_integration_settings
            settings = get_system_integration_settings()
            if settings.get("enabled"):
                return max(30, int(settings.get("idle_timeout_seconds", 60)))
        except Exception:
            pass
        # A normal interactive server is less aggressive; the persistent mode
        # explicitly opts into the shorter, energy-conscious timeout.
        return 300

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            state = self._state_machine.current_state
            if state == WiseState.BOOT:
                self._state_machine.transition_to(WiseState.INITIALIZING, "resident runtime starting")
                self._state_machine.transition_to(WiseState.READY, "resident runtime ready")
            self._last_activity = time.monotonic()
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop,
                name="WISE_Idle_Controller",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._check_interval * 2))

    def begin_activity(self, reason: str = "WISE request") -> None:
        with self._lock:
            self._active_operations += 1
            self._last_activity = time.monotonic()
            current = self._state_machine.current_state
            if current in (WiseState.IDLE, WiseState.READY):
                self._state_machine.transition_to(WiseState.AWAKENING, reason)
                self._state_machine.transition_to(WiseState.REASONING, reason)
            elif current in (WiseState.WAITING_FOR_USER, WiseState.WAITING_FOR_PERMISSION):
                self._state_machine.transition_to(WiseState.REASONING, reason)

    def end_activity(self) -> None:
        with self._lock:
            self._active_operations = max(0, self._active_operations - 1)
            self._last_activity = time.monotonic()

    @contextmanager
    def activity(self, reason: str = "WISE request") -> Iterator[None]:
        self.begin_activity(reason)
        try:
            yield
        finally:
            self.end_activity()

    def check_once(self, *, now: Optional[float] = None) -> bool:
        """Transition to IDLE when no operation is active and the timeout elapsed."""
        with self._lock:
            if self._active_operations:
                return False
            elapsed = (time.monotonic() if now is None else now) - self._last_activity
            timeout = max(1, int(self._timeout_provider()))
            current = self._state_machine.current_state
            if elapsed < timeout or current in (WiseState.IDLE, WiseState.SHUTDOWN):
                return False
            transitioned = self._state_machine.transition_to(
                WiseState.IDLE,
                f"no WISE activity for {int(elapsed)}s (timeout {timeout}s)",
            )
            if transitioned:
                LOG.info("WISE entered IDLE after %.1fs without active work.", elapsed)
            return transitioned

    def _loop(self) -> None:
        while not self._stop_event.wait(self._check_interval):
            try:
                self.check_once()
            except Exception:
                LOG.exception("Idle controller iteration failed")

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "active_operations": self._active_operations,
                "idle_timeout_seconds": max(1, int(self._timeout_provider())),
                "seconds_since_activity": round(max(0.0, time.monotonic() - self._last_activity), 2),
                "state": self._state_machine.current_state.value,
            }


_CONTROLLER: Optional[RuntimeIdleController] = None
_CONTROLLER_LOCK = threading.Lock()


def get_idle_controller() -> RuntimeIdleController:
    global _CONTROLLER
    if _CONTROLLER is None:
        with _CONTROLLER_LOCK:
            if _CONTROLLER is None:
                _CONTROLLER = RuntimeIdleController()
    return _CONTROLLER


__all__ = ["RuntimeIdleController", "get_idle_controller"]
