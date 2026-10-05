"""Regression coverage for the active worker watchdog."""

import time
from types import SimpleNamespace

from core.resource.resource_manager import ManagedWorker, WorkerStatus
from core.resource.worker_supervisor import WorkerSupervisor
from core.state.state_machine import WiseState


def test_supervisor_detects_unhealthy_running_worker_and_schedules_recovery(monkeypatch):
    worker = ManagedWorker(
        "test_runtime",
        start_fn=lambda: None,
        health_check_fn=lambda: False,
    )
    worker.status = WorkerStatus.RUNNING
    supervisor = WorkerSupervisor(check_interval_seconds=60, max_recovery_attempts=2)
    monkeypatch.setattr(supervisor._resource_manager, "workers", {worker.name: worker})

    supervisor.check_once()

    health = supervisor.get_health_summary()[worker.name]
    assert health["crash_count"] == 1
    assert health["recovery_attempts"] == 1


def test_supervisor_ignores_intentionally_suspended_worker(monkeypatch):
    worker = ManagedWorker("sleeping_runtime", health_check_fn=lambda: False)
    worker.status = WorkerStatus.SUSPENDED
    supervisor = WorkerSupervisor(check_interval_seconds=60)
    monkeypatch.setattr(supervisor._resource_manager, "workers", {worker.name: worker})

    supervisor.check_once()

    assert supervisor.get_health_summary()[worker.name]["crash_count"] == 0


def test_failed_restart_is_recorded_as_a_new_bounded_recovery(monkeypatch):
    def fail_start():
        raise RuntimeError("runtime still unavailable")

    worker = ManagedWorker("flaky_runtime", start_fn=fail_start)
    supervisor = WorkerSupervisor(check_interval_seconds=60)
    supervisor._state_machine = SimpleNamespace(current_state=WiseState.EXECUTING)
    monkeypatch.setattr(supervisor._resource_manager, "workers", {worker.name: worker})
    supervisor.register_worker(worker.name)

    recorded = []
    monkeypatch.setattr(
        supervisor,
        "report_crash",
        lambda name, error=None: recorded.append((name, str(error))) or True,
    )

    assert supervisor._attempt_recovery(worker.name) is False
    assert recorded == [(worker.name, "worker restart failed")]


def test_recovered_worker_gets_a_fresh_retry_budget(monkeypatch):
    worker = ManagedWorker("recoverable_runtime", start_fn=lambda: None)
    supervisor = WorkerSupervisor(check_interval_seconds=60)
    supervisor._state_machine = SimpleNamespace(current_state=WiseState.EXECUTING)
    monkeypatch.setattr(supervisor._resource_manager, "workers", {worker.name: worker})
    supervisor.register_worker(worker.name)
    supervisor._records[worker.name].recovery_attempts = 2
    supervisor._records[worker.name].last_error = "old error"

    assert supervisor._attempt_recovery(worker.name) is True
    health = supervisor.get_health_summary()[worker.name]
    assert health["status"] == WorkerStatus.RUNNING.value
    assert health["recovery_attempts"] == 0
    assert health["last_error"] is None


def test_failed_restarts_quarantine_only_after_bounded_retries(monkeypatch):
    def fail_start():
        raise RuntimeError("still failing")

    worker = ManagedWorker("persistently_broken", start_fn=fail_start)
    supervisor = WorkerSupervisor(
        check_interval_seconds=60,
        max_recovery_attempts=2,
        backoff_base_seconds=0.001,
    )
    supervisor._state_machine = SimpleNamespace(current_state=WiseState.EXECUTING)
    monkeypatch.setattr(supervisor._resource_manager, "workers", {worker.name: worker})

    assert supervisor.report_crash(worker.name, RuntimeError("initial failure")) is True
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if supervisor.get_health_summary()[worker.name]["is_quarantined"]:
            break
        time.sleep(0.01)

    health = supervisor.get_health_summary()[worker.name]
    assert health["is_quarantined"] is True
    assert health["recovery_attempts"] == 2
    assert health["crash_count"] == 3
    assert health["last_error"] == "worker restart failed"
