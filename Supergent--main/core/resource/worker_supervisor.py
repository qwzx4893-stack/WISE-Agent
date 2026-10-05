"""
Worker Supervisor & Self-Healing Watchdog (WorkerSupervisor).
Monitors the lifecycle of ManagedWorker instances governed by WISEResourceManager.
Detects unexpected worker crashes, enforces exponential backoff recovery policies,
and ensures that worker failures never destabilize the Persistent Core.
"""

from __future__ import annotations

import time
import logging
import threading
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

from core.resource.resource_manager import get_resource_manager, ManagedWorker, WorkerStatus
from core.state.state_machine import get_state_machine, WiseState

LOG = logging.getLogger("wise.worker_supervisor")


@dataclass
class WorkerHealthRecord:
    worker_name: str
    status: WorkerStatus
    crash_count: int = 0
    last_crash_time: Optional[float] = None
    last_recovered_time: Optional[float] = None
    recovery_attempts: int = 0
    is_quarantined: bool = False
    last_error: Optional[str] = None
    next_retry_at: Optional[float] = None


class WorkerSupervisor:
    """Watchdog that monitors workers and executes bounded self-healing restarts."""

    def __init__(
        self,
        check_interval_seconds: float = 2.0,
        max_recovery_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
    ):
        self.check_interval = check_interval_seconds
        self.max_recovery_attempts = max_recovery_attempts
        self.backoff_base = backoff_base_seconds
        self._lock = threading.RLock()
        self._records: Dict[str, WorkerHealthRecord] = {}
        self._resource_manager = get_resource_manager()
        self._state_machine = get_state_machine()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start one low-overhead watchdog for the current WISE process."""
        with self._lock:
            if self._running:
                return
            for worker_name in list(self._resource_manager.workers):
                self.register_worker(worker_name)
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="WISE_Worker_Supervisor",
                daemon=True,
            )
            self._thread.start()
            LOG.info("Worker supervisor started (interval %.1fs).", self.check_interval)

    def stop(self) -> None:
        """Stop monitoring before backend shutdown; never restart during exit."""
        with self._lock:
            if not self._running:
                # A recovery thread may have been scheduled by a direct crash
                # report before the monitor loop itself was started.
                self._stop_event.set()
                return
            self._running = False
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.check_interval * 2))

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            try:
                self.check_once()
            except Exception:
                LOG.exception("Worker supervisor monitoring iteration failed")

    def check_once(self) -> None:
        """Perform one bounded health pass; also usable in deterministic tests."""
        with self._lock:
            workers = list(self._resource_manager.workers.items())
        for worker_name, worker in workers:
            self.register_worker(worker_name)
            with self._lock:
                record = self._records[worker_name]
                record.status = worker.status
                quarantined = record.is_quarantined
            # Suspended workers are intentionally idle.  Only a worker that
            # claims to be running can be considered unexpectedly unhealthy.
            if worker.status == WorkerStatus.RUNNING and not quarantined and not worker.is_healthy():
                worker.status = WorkerStatus.STOPPED
                self.report_crash(worker_name, RuntimeError("worker health probe failed"))

    def register_worker(self, worker_name: str) -> None:
        with self._lock:
            if worker_name not in self._records:
                self._records[worker_name] = WorkerHealthRecord(
                    worker_name=worker_name,
                    status=WorkerStatus.STOPPED,
                )

    def report_crash(self, worker_name: str, error: Optional[Exception] = None) -> bool:
        """Reports a worker crash and triggers self-healing recovery if bounded within limits."""
        with self._lock:
            if self._stop_event.is_set():
                # Recovery must never revive a worker while the ASGI process is
                # shutting down.  ``start`` clears this event for a new process
                # lifecycle, so deterministic callers can still use recovery.
                LOG.info("Not scheduling recovery for '%s' during shutdown.", worker_name)
                return False
            if worker_name not in self._records:
                self.register_worker(worker_name)

            rec = self._records[worker_name]
            now = time.time()
            rec.crash_count += 1
            rec.last_crash_time = now
            rec.status = WorkerStatus.STOPPED
            rec.last_error = str(error)[:500] if error else "worker stopped unexpectedly"

            LOG.error(
                "Worker '%s' crashed (Count: %d, Error: %s)",
                worker_name,
                rec.crash_count,
                error,
            )

            # Check if exceeded max recovery attempts
            if rec.recovery_attempts >= self.max_recovery_attempts:
                rec.is_quarantined = True
                LOG.critical(
                    "Worker '%s' exceeded max recovery attempts (%d) and has been QUARANTINED.",
                    worker_name,
                    self.max_recovery_attempts,
                )
                return False

            self._schedule_recovery_locked(worker_name, rec)
            return True

    def _schedule_recovery_locked(self, worker_name: str, rec: WorkerHealthRecord) -> None:
        """Queue exactly one bounded retry while holding ``_lock``.

        A failed restart is treated as another crash by ``_attempt_recovery``;
        this helper therefore gives each retry the same accounting, backoff,
        quarantine boundary and health telemetry as the initial crash.
        """
        backoff = self.backoff_base * (2 ** rec.recovery_attempts)
        rec.recovery_attempts += 1
        rec.next_retry_at = time.time() + backoff
        LOG.info("Scheduling recovery for worker '%s' in %.2fs...", worker_name, backoff)

        def _recover_worker() -> None:
            # ``Event.wait`` is interruptible, unlike ``time.sleep``.  It lets
            # FastAPI shutdown cancel a deferred restart immediately.
            if self._stop_event.wait(backoff):
                return
            self._attempt_recovery(worker_name)

        threading.Thread(target=_recover_worker, name=f"Recover_{worker_name}", daemon=True).start()

    def _attempt_recovery(self, worker_name: str) -> bool:
        """Executes actual recovery of the target worker."""
        with self._lock:
            if self._stop_event.is_set():
                return False
            rec = self._records.get(worker_name)
            if not rec or rec.is_quarantined:
                return False

            rec.next_retry_at = None

            worker = self._resource_manager.workers.get(worker_name)
            if not worker:
                LOG.warning("Cannot recover unregistered worker: %s", worker_name)
                return False

            current_wise_state = self._state_machine.current_state
            # Do not start non-essential workers if core is in IDLE
            if current_wise_state == WiseState.IDLE and not worker.is_essential_in_idle:
                worker.status = WorkerStatus.SUSPENDED
                rec.status = WorkerStatus.SUSPENDED
                rec.last_recovered_time = time.time()
                rec.recovery_attempts = 0
                rec.last_error = None
                LOG.info(
                    "Worker '%s' recovered in SUSPENDED state (Core is IDLE).",
                    worker_name,
                )
                return True

            success = worker.start()
            if success:
                rec.status = WorkerStatus.RUNNING
                rec.last_recovered_time = time.time()
                # A successful restart restores the retry budget.  Otherwise
                # independent, well-separated failures would eventually
                # quarantine a healthy worker forever.
                rec.recovery_attempts = 0
                rec.last_error = None
                LOG.info("Worker '%s' recovered and restarted successfully.", worker_name)
                return True
            LOG.warning("Worker '%s' restart failed during recovery attempt.", worker_name)

        # Do not hold the supervisor lock while recursively scheduling the
        # next bounded retry.  This call records the failure and quarantines
        # after the configured budget is exhausted.
        self.report_crash(worker_name, RuntimeError("worker restart failed"))
        return False

    def reset_worker_health(self, worker_name: str) -> None:
        """Resets crash counters when a worker has been running stably."""
        with self._lock:
            rec = self._records.get(worker_name)
            if rec:
                rec.recovery_attempts = 0
                rec.is_quarantined = False
                rec.last_error = None
                rec.next_retry_at = None

    def get_health_summary(self) -> Dict[str, Any]:
        with self._lock:
            summary: Dict[str, Any] = {
                "_supervisor": {
                    "running": self._running,
                    "check_interval_seconds": self.check_interval,
                },
            }
            summary.update({
                name: {
                    "status": rec.status.value,
                    "crash_count": rec.crash_count,
                    "recovery_attempts": rec.recovery_attempts,
                    "is_quarantined": rec.is_quarantined,
                    "last_crash_time": rec.last_crash_time,
                    "last_recovered_time": rec.last_recovered_time,
                    "last_error": rec.last_error,
                    "next_retry_at": rec.next_retry_at,
                }
                for name, rec in self._records.items()
            })
            return summary


# Singleton
_GLOBAL_SUPERVISOR: Optional[WorkerSupervisor] = None
_SUP_LOCK = threading.Lock()


def get_worker_supervisor() -> WorkerSupervisor:
    global _GLOBAL_SUPERVISOR
    if _GLOBAL_SUPERVISOR is None:
        with _SUP_LOCK:
            if _GLOBAL_SUPERVISOR is None:
                _GLOBAL_SUPERVISOR = WorkerSupervisor()
    return _GLOBAL_SUPERVISOR
