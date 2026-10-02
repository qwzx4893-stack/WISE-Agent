"""Phase 7 — Part 3b: scheduler + dashboard."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect AGENT_OS_ROOT so each test gets its own config dir."""
    root = tmp_path / "agent_os"
    monkeypatch.setenv("AGENT_OS_ROOT", str(root))
    # Force re-import of paths and downstream singletons.
    import importlib
    import core.paths as paths
    importlib.reload(paths)
    yield root


# ---------------------------------------------------------------------------
# CronExpression
# ---------------------------------------------------------------------------
def test_cron_parses_wildcard():
    from core.scheduler import CronExpression
    c = CronExpression("* * * * *")
    assert c.matches(datetime(2025, 1, 1, 12, 30, tzinfo=timezone.utc))


def test_cron_parses_step_minutes():
    from core.scheduler import CronExpression
    c = CronExpression("*/15 * * * *")
    assert c.matches(datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc))
    assert c.matches(datetime(2025, 1, 1, 12, 15, tzinfo=timezone.utc))
    assert c.matches(datetime(2025, 1, 1, 12, 30, tzinfo=timezone.utc))
    assert not c.matches(datetime(2025, 1, 1, 12, 7, tzinfo=timezone.utc))


def test_cron_parses_specific_hour():
    from core.scheduler import CronExpression
    c = CronExpression("0 9 * * *")
    assert c.matches(datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))
    assert not c.matches(datetime(2025, 1, 1, 9, 1, tzinfo=timezone.utc))
    assert not c.matches(datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc))


def test_cron_rejects_invalid_field_count():
    from core.scheduler import CronExpression
    with pytest.raises(ValueError):
        CronExpression("0 9 * *")


def test_cron_rejects_out_of_bounds():
    from core.scheduler import CronExpression
    with pytest.raises(ValueError):
        CronExpression("0 25 * * *")


def test_cron_next_after_advances_one_step():
    from core.scheduler import CronExpression
    c = CronExpression("*/5 * * * *")
    nxt = c.next_after(
        datetime(2025, 1, 1, 12, 30, 30, tzinfo=timezone.utc))
    assert nxt is not None
    assert nxt.minute == 35


# ---------------------------------------------------------------------------
# Scheduler CRUD + persistence
# ---------------------------------------------------------------------------
def _fresh_scheduler(tmp_path):
    """Construct a scheduler with an isolated config path, bypassing the
    process-wide singleton."""
    from core import scheduler as sched_mod
    inst = sched_mod.Scheduler.__new__(sched_mod.Scheduler)
    inst.path = tmp_path / "schedules.json"
    inst._schedules = {}
    inst._compiled = {}
    inst._history = {}
    inst._running = set()
    inst._runner = None
    inst._thread = None
    import threading
    inst._stop_event = threading.Event()
    inst._mutex = threading.RLock()
    return inst


def test_scheduler_add_persists_to_disk(tmp_path):
    sch = _fresh_scheduler(tmp_path)
    s = sch.add(name="hourly", cron="0 * * * *", target="agent.run",
                 payload={"prompt": "ping"})
    assert s.next_run_at is not None
    # Reloading from disk recovers state.
    assert (tmp_path / "schedules.json").exists()
    sch2 = _fresh_scheduler(tmp_path)
    sch2.path = tmp_path / "schedules.json"
    sch2._load()
    assert sch2.get(s.id) is not None
    assert sch2.get(s.id).cron == "0 * * * *"


def test_scheduler_update_changes_cron_and_recomputes_next(tmp_path):
    sch = _fresh_scheduler(tmp_path)
    s = sch.add(name="x", cron="0 0 1 1 *", target="t")
    original_next = s.next_run_at
    s2 = sch.update(s.id, cron="30 * * * *")
    assert s2 is not None
    assert s2.cron == "30 * * * *"
    assert s2.next_run_at != original_next


def test_scheduler_delete(tmp_path):
    sch = _fresh_scheduler(tmp_path)
    s = sch.add(name="x", cron="0 * * * *", target="t")
    assert sch.delete(s.id)
    assert sch.get(s.id) is None
    assert not sch.delete(s.id)


def test_scheduler_rejects_invalid_cron(tmp_path):
    sch = _fresh_scheduler(tmp_path)
    with pytest.raises(ValueError):
        sch.add(name="bad", cron="bogus", target="t")


def test_scheduler_fire_now_invokes_runner(tmp_path):
    sch = _fresh_scheduler(tmp_path)
    s = sch.add(name="x", cron="0 * * * *", target="t")
    seen = []
    sch.set_runner(lambda sched: seen.append(sched.id))
    run = sch.fire_now(s.id)
    assert run is not None
    assert run.status == "success"
    assert seen == [s.id]
    assert sch.get(s.id).last_status == "success"


def test_scheduler_records_failure_on_runner_exception(tmp_path):
    sch = _fresh_scheduler(tmp_path)
    s = sch.add(name="x", cron="0 * * * *", target="t")

    def boom(sched):
        raise RuntimeError("nope")

    sch.set_runner(boom)
    run = sch.fire_now(s.id)
    assert run.status == "failed"
    assert "nope" in run.error
    assert sch.get(s.id).last_status == "failed"


def test_scheduler_overlap_prevention(tmp_path):
    sch = _fresh_scheduler(tmp_path)
    s = sch.add(name="x", cron="0 * * * *", target="t")
    # Manually mark as running so a second fire is blocked.
    sch._running.add(s.id)
    sch.set_runner(lambda sched: None)
    run = sch.fire_now(s.id)
    assert run.status == "failed"
    assert "overlap" in run.error


def test_scheduler_coalesces_equivalent_triggers_and_bounds_parallelism(tmp_path):
    sch = _fresh_scheduler(tmp_path)
    sch._max_concurrent_runs = 1
    first = sch.add(name="first", cron="* * * * *", target="health_pulse",
                    payload={"task": "health_check"})
    duplicate = sch.add(name="duplicate", cron="* * * * *", target="health_pulse",
                        payload={"task": "health_check"})
    distinct = sch.add(name="distinct", cron="* * * * *", target="agent.run",
                       payload={"task": "other"})
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def runner(schedule):
        seen.append(schedule.id)
        if schedule.id == first.id:
            entered.set()
            assert release.wait(timeout=2)

    sch.set_runner(runner)
    sch._tick(datetime.now(timezone.utc).replace(second=0, microsecond=0))

    assert entered.wait(timeout=1)
    assert duplicate.last_status == "skipped"
    assert sch.runtime_status() == {
        "active_runs": 1,
        "queued_runs": 1,
        "max_concurrent_runs": 1,
    }

    release.set()
    deadline = time.monotonic() + 2
    while distinct.id not in seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [first.id, distinct.id]
    deadline = time.monotonic() + 2
    while sch.runtime_status()["active_runs"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sch.runtime_status()["active_runs"] == 0


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def test_dashboard_records_and_returns_recent():
    from core.dashboard import ActivityLog, activity, summary, stats
    log = ActivityLog()
    log.clear()
    log.record("chat", actor="normal", summary="hello",
                status="success", duration_ms=50)
    log.record("tool", actor="agent", summary="ran nmap",
                status="failed", duration_ms=120)
    a = activity()
    assert len(a["events"]) == 2
    # Most-recent first.
    assert a["events"][0]["summary"] == "ran nmap"
    s = summary()
    assert s["last_chat"] is not None
    assert s["last_failure"] is not None
    st = stats()
    assert st["events_total"] == 2
    assert st["by_kind"]["chat"] == 1
    assert st["by_status"]["failed"] == 1


def test_dashboard_summary_handles_empty_log():
    from core.dashboard import ActivityLog, summary
    ActivityLog().clear()
    s = summary()
    assert s["last_chat"] is None
    assert s["last_failure"] is None
    assert "scheduler" in s
    assert "skills" in s


# ---------------------------------------------------------------------------
# API endpoints (FastAPI)
# ---------------------------------------------------------------------------
def test_api_schedule_crud_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    monkeypatch.setenv("AGENT_OS_API_TOKEN", "secret")
    # Rewire the global scheduler to a sandboxed instance.
    import core.scheduler as sched_mod
    sched_mod.Scheduler._instance = None
    sandbox = _fresh_scheduler(tmp_path)
    monkeypatch.setattr(sched_mod, "get_scheduler", lambda: sandbox)

    client = TestClient(app)
    h = {"X-Agent-Token": "secret"}

    r = client.post("/admin/schedules", headers=h,
                     json={"name": "n", "cron": "0 * * * *", "target": "t"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    r = client.get("/admin/schedules", headers=h)
    assert r.status_code == 200
    assert any(s["id"] == sid for s in r.json()["schedules"])

    r = client.patch(f"/admin/schedules/{sid}", headers=h,
                      json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = client.delete(f"/admin/schedules/{sid}", headers=h)
    assert r.status_code == 200
    r = client.delete(f"/admin/schedules/{sid}", headers=h)
    assert r.status_code == 404


def test_api_schedule_create_rejects_bad_cron(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from api.server import app
    monkeypatch.setenv("AGENT_OS_API_TOKEN", "secret")
    import core.scheduler as sched_mod
    sched_mod.Scheduler._instance = None
    sandbox = _fresh_scheduler(tmp_path)
    monkeypatch.setattr(sched_mod, "get_scheduler", lambda: sandbox)
    client = TestClient(app)
    r = client.post("/admin/schedules",
                     headers={"X-Agent-Token": "secret"},
                     json={"name": "n", "cron": "garbage", "target": "t"})
    assert r.status_code == 400


def test_api_dashboard_endpoints(monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    from core.dashboard import ActivityLog
    monkeypatch.setenv("AGENT_OS_API_TOKEN", "secret")
    ActivityLog().clear()
    ActivityLog().record("chat", actor="normal", summary="hi",
                          status="success")
    client = TestClient(app)
    h = {"X-Agent-Token": "secret"}

    r = client.get("/admin/dashboard/summary", headers=h)
    assert r.status_code == 200
    assert "scheduler" in r.json()

    r = client.get("/admin/dashboard/activity", headers=h)
    assert r.status_code == 200
    assert len(r.json()["events"]) >= 1

    r = client.get("/admin/dashboard/stats", headers=h)
    assert r.status_code == 200
    assert r.json()["events_total"] >= 1
