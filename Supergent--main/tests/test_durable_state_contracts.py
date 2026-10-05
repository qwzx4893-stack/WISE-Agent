"""Fault injection and recovery contracts; every file is under tmp_path."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from core.session_service import SessionService, SessionPersistenceError
from core.brain.task_engine import TaskEngine, TaskPersistenceError, TaskStatus, StepStatus, Subgoal, TaskStep
from core.scheduler import Scheduler, SchedulePersistenceError, CronExpression
from core.durable_io import storage_writer_lock


def fresh_scheduler(tmp_path):
    # Do not mutate the application's process-wide singleton in these tests.
    scheduler = object.__new__(Scheduler)
    scheduler.path = tmp_path / "schedules.json"
    scheduler._schedules = {}
    scheduler._compiled = {}
    scheduler._history = {}
    scheduler._running = set()
    scheduler._runner = None
    scheduler._thread = None
    scheduler._stop_event = threading.Event()
    scheduler._mutex = threading.RLock()
    scheduler._ensure_dispatch_state()
    return scheduler


@pytest.mark.parametrize("expression, day, expected", [
    ("0 9 1 * 1", 1, True),   # Thursday: DOM branch alone matches.
    ("0 9 1 * 1", 5, True),   # Monday: DOW branch alone matches.
    ("0 9 1 * 1", 6, False),  # Neither calendar branch matches.
    ("0 9 * * 1", 1, False),  # DOM wildcard must not bypass Monday.
    ("0 9 * * 1", 5, True),
    ("0 9 1 * *", 1, True),
    ("0 9 1 * *", 5, False),  # DOW wildcard must not bypass DOM.
    ("0 9 * * *", 6, True),
    ("0 9 */2 * 1", 5, True), # Starred DOM step still constrains DOW.
    ("0 9 */2 * 1", 1, False),
])
def test_cron_calendar_restricted_days_are_alternatives_not_wildcards(expression, day, expected):
    assert CronExpression(expression).matches(datetime(2026, 10, day, 9, tzinfo=timezone.utc)) is expected
    assert not CronExpression(expression).matches(datetime(2026, 10, day, 10, tzinfo=timezone.utc))


def test_writer_lock_is_reentrant_and_thread_contention_is_bounded(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def hold():
        with storage_writer_lock(tmp_path):
            with storage_writer_lock(tmp_path):
                entered.set()
                assert release.wait(2)
    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(1)
    try:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            with storage_writer_lock(tmp_path, timeout=.05):
                pytest.fail("Must not acquire another thread's writer lock")
        assert time.monotonic() - start < .3
    finally:
        release.set()
        thread.join(2)
    with storage_writer_lock(tmp_path):
        pass


def task_plan(tmp_path):
    engine = TaskEngine(tmp_path / "tasks.json")
    task = engine.create_task("test", "verified work", [Subgoal(steps=[TaskStep(), TaskStep()])])
    engine.start_task(task.task_id)
    return engine, task


def test_session_identity_mapping_is_injective_and_keeps_valid_names(tmp_path):
    service = SessionService(tmp_path)
    for sid in ("ab", "a/b", "a.b", "CON", "../ab"):
        service.append_messages(sid, [{"role": "user", "content": sid}])
    assert service._get_path("ab").name == "ab.json"
    assert len({service._get_path(sid) for sid in ("ab", "a/b", "a.b", "CON", "../ab")}) == 5
    reopened = SessionService(tmp_path)
    for sid in ("ab", "a/b", "a.b", "CON", "../ab"):
        assert reopened.get_session(sid).history[0]["content"] == sid


def test_session_nested_corruption_uses_known_good_backup(tmp_path):
    service = SessionService(tmp_path)
    service.append_messages("valid", [{"role": "user", "content": "saved"}])
    service.update_session("valid", title="new title")
    path = service._get_path("valid")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["history"] = {"not": "a timeline"}
    path.write_text(json.dumps(data), encoding="utf-8")
    recovered = SessionService(tmp_path).get_session("valid")
    assert recovered.history[0]["content"] == "saved"
    assert recovered.metadata["title"] == "saved"
    assert recovered.metadata["storage_recovery"]["requires_review"]


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_session_failed_write_rolls_back_and_does_not_acknowledge(tmp_path, monkeypatch, failure):
    service = SessionService(tmp_path)
    session = service.append_messages("stable", [{"role": "user", "content": "before"}])
    old = service._get_path("stable").read_bytes()
    def fail(*args, **kwargs):
        raise OSError("simulated storage fault")
    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(SessionPersistenceError):
        service.append_messages("stable", [{"role": "assistant", "content": "not durable"}])
    assert session.history[-1]["content"] == "before"
    assert session.last_response == ""
    assert service._get_path("stable").read_bytes() == old
    assert not list(tmp_path.glob("*.tmp"))


def test_session_failed_create_not_cached(tmp_path, monkeypatch):
    service = SessionService(tmp_path)
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("fault")))
    with pytest.raises(SessionPersistenceError):
        service.get_or_create_session("not-created")
    assert service.get_session("not-created") is None


def test_session_message_and_turn_identity_are_idempotent_not_content(tmp_path):
    service = SessionService(tmp_path)
    batch = [
        {"role": "user", "content": "hello", "turn_id": "turn-1"},
        {"role": "assistant", "content": "hello", "turn_id": "turn-1"},
    ]
    service.append_messages("identity", batch)
    SessionService(tmp_path).append_messages("identity", batch)
    service.append_messages("identity", [{"role": "user", "content": "hello", "turn_id": "turn-2"}])
    service.append_messages("identity", [{"role": "tool", "content": "output", "message_id": "tool-1"}])
    service.append_messages("identity", [{"role": "tool", "content": "output changed", "message_id": "tool-1"}])
    assert len(service.get_session("identity").history) == 4


def test_session_model_context_survives_restart(tmp_path):
    service = SessionService(tmp_path)
    session = service.get_or_create_session("model")
    session.model_context = {"selected_model": "owner-choice", "provider": "configured-provider"}
    service.save_session(session)
    assert SessionService(tmp_path).get_session("model").model_context == session.model_context


def test_session_multiple_instances_do_not_lose_threaded_appends(tmp_path):
    services = [SessionService(tmp_path) for _ in range(4)]
    def append(index):
        services[index % 4].append_messages("shared", [{"role": "user", "content": str(index), "message_id": str(index)}])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(24)))
    history = SessionService(tmp_path).get_session("shared").history
    assert len(history) == 24
    assert {entry["message_id"] for entry in history} == {str(i) for i in range(24)}


def test_cached_metadata_update_keeps_another_writers_messages(tmp_path):
    first = SessionService(tmp_path)
    cached = first.get_or_create_session("shared")
    second = SessionService(tmp_path)
    second.append_messages("shared", [{"role": "user", "content": "newer durable message"}])
    assert first.get_or_create_session("shared", metadata={"owner": "updated"}) is cached
    restored = SessionService(tmp_path).get_session("shared")
    assert restored.history[0]["content"] == "newer durable message"
    assert restored.metadata["owner"] == "updated"


def test_cached_session_never_overwrites_unrecoverable_disk_state(tmp_path):
    service = SessionService(tmp_path)
    service.get_or_create_session("cached")
    path = service._get_path("cached")
    path.write_text("{corrupt", encoding="utf-8")
    for action in (lambda: service.get_session("cached"),
                   lambda: service.get_or_create_session("cached", metadata={"owner": "change"}),
                   lambda: service.append_messages("cached", [{"role": "user", "content": "not saved"}])):
        with pytest.raises(SessionPersistenceError):
            action()
        assert path.read_text(encoding="utf-8") == "{corrupt"
    # Explicit owner deletion remains possible despite corrupt storage.
    assert service.delete_session("cached")
    assert service.get_session("cached") is None


def test_session_external_delete_is_not_resurrected_from_cache(tmp_path):
    first = SessionService(tmp_path)
    first.append_messages("deleted", [{"role": "user", "content": "old"}])
    assert SessionService(tmp_path).delete_session("deleted")
    assert first.get_session("deleted") is None
    replacement = first.get_or_create_session("deleted")
    assert replacement.history == []


def test_failed_backup_delete_keeps_primary_and_does_not_acknowledge(tmp_path, monkeypatch):
    from pathlib import Path
    service = SessionService(tmp_path)
    service.append_messages("kept", [{"role": "user", "content": "original"}])
    path = service._get_path("kept")
    old = path.read_bytes()
    unlink = Path.unlink
    def fail_backup(candidate, *args, **kwargs):
        if candidate.suffix == ".bak":
            raise PermissionError("injected backup deletion denial")
        return unlink(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_backup)
    with pytest.raises(SessionPersistenceError):
        service.delete_session("kept")
    assert path.read_bytes() == old


def test_session_process_writers_use_os_lock(tmp_path):
    SessionService(tmp_path).get_or_create_session("shared")
    code = "from pathlib import Path; from core.session_service import SessionService; import sys; s=SessionService(Path(sys.argv[1])); [s.append_messages('shared',[{'role':'user','content':str(i),'message_id':sys.argv[2]+'-'+str(i)}]) for i in range(5)]"
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
    for process in processes:
        _, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr.decode(errors="replace")
    assert len(SessionService(tmp_path).get_session("shared").history) == 15


def test_session_malformed_batch_and_metadata_do_not_mutate(tmp_path):
    service = SessionService(tmp_path)
    session = service.get_or_create_session("stable")
    with pytest.raises(ValueError):
        service.append_messages("stable", [{"role": "user", "content": "valid"}, None])
    with pytest.raises(TypeError):
        service.append_messages("stable", [{"role": "user", "content": "valid", "metadata": {"bad": object()}}])
    assert session.history == []


def test_unrecoverable_state_is_not_silently_replaced_with_empty_data(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / "broken.json"
    session_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(SessionPersistenceError):
        SessionService(session_dir).get_or_create_session("broken")
    assert session_path.read_text(encoding="utf-8") == "{corrupt"
    task_path = tmp_path / "tasks.json"
    task_path.write_text("{corrupt", encoding="utf-8")
    engine = TaskEngine(task_path)
    with pytest.raises(TaskPersistenceError):
        engine.create_task("new", "not overwrite corrupt state")
    assert engine.list_tasks() == []
    assert task_path.read_text(encoding="utf-8") == "{corrupt"
    scheduler = fresh_scheduler(tmp_path)
    scheduler.path.write_text("{corrupt", encoding="utf-8")
    scheduler._load()
    with pytest.raises(SchedulePersistenceError):
        scheduler.add(name="new", cron="* * * * *", target="test")
    assert scheduler.path.read_text(encoding="utf-8") == "{corrupt"


def test_task_failed_durable_transition_restores_reference(tmp_path, monkeypatch):
    engine, task = task_plan(tmp_path)
    old = (tmp_path / "tasks.json").read_bytes()
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("fault")))
    with pytest.raises(TaskPersistenceError):
        engine.stop_task(task.task_id)
    assert task.status == TaskStatus.RUNNING
    assert engine.get_task(task.task_id) is task
    assert (tmp_path / "tasks.json").read_bytes() == old


@pytest.mark.parametrize("corruption", ["nested", "negative", "overflow", "duplicate"])
def test_task_validates_entire_primary_and_recovers_backup(tmp_path, corruption):
    engine, task = task_plan(tmp_path)
    path = tmp_path / "tasks.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if corruption == "nested":
        raw["tasks"][0]["subgoals"][0]["steps"] = [42]
    elif corruption == "negative":
        raw["tasks"][0]["current_subgoal_idx"] = -1
    elif corruption == "overflow":
        raw["tasks"][0]["subgoals"][0]["current_step_idx"] = 99
    else:
        raw["tasks"].append(raw["tasks"][0])
    path.write_text(json.dumps(raw), encoding="utf-8")
    restored = TaskEngine(path).get_task(task.task_id)
    assert restored is not None
    assert restored.current_subgoal_idx == 0
    assert restored.subgoals[0].current_step_idx == 0


def test_task_only_advances_verified_results_and_never_fabricates_completion(tmp_path):
    engine, task = task_plan(tmp_path)
    first, second = task.subgoals[0].steps
    assert engine.advance_step(task.task_id) == (first, False)
    assert not engine.complete_task(task.task_id)
    engine.record_step_result(task.task_id, first.step_id, False)
    assert engine.advance_step(task.task_id) == (first, False)
    engine.record_step_result(task.task_id, first.step_id, True)
    assert engine.advance_step(task.task_id) == (second, False)
    engine.stop_task(task.task_id)
    assert engine.advance_step(task.task_id) == (second, False)
    assert second.status == StepStatus.PENDING
    engine.resume_task(task.task_id)
    engine.record_step_result(task.task_id, second.step_id, True)
    assert engine.advance_step(task.task_id) == (None, True)
    assert task.completed_steps_count == 2


def test_task_empty_subgoals_are_safe_and_completed_prefix_is_preserved(tmp_path):
    engine = TaskEngine(tmp_path / "tasks.json")
    task = engine.create_task("plan", "work", [Subgoal(), Subgoal(steps=[TaskStep()]), Subgoal()])
    engine.start_task(task.task_id)
    current, complete = engine.advance_step(task.task_id)
    assert current is task.subgoals[1].steps[0] and not complete
    engine.record_step_result(task.task_id, current.step_id, True)
    assert engine.advance_step(task.task_id) == (None, True)


def test_task_checkpoint_preserves_step_and_requires_external_effect_review(tmp_path):
    engine, task = task_plan(tmp_path)
    first = task.subgoals[0].steps[0]
    engine.record_step_result(task.task_id, first.step_id, True)
    engine.advance_step(task.task_id)
    checkpoint = engine.pause_for_human(task.task_id, reason="review")
    assert engine.rollback_to_checkpoint(task.task_id, checkpoint.checkpoint_id)
    assert task.subgoals[0].current_step_idx == 1
    assert first.status == StepStatus.COMPLETED
    assert task.status == TaskStatus.PAUSED_FOR_HUMAN
    assert task.active_intervention["type"] == "CHECKPOINT_REVIEW"


def test_task_recovering_does_not_restart_silently(tmp_path):
    engine, task = task_plan(tmp_path)
    engine.set_task_recovering(task.task_id)
    restored = TaskEngine(tmp_path / "tasks.json").get_task(task.task_id)
    assert restored.status == TaskStatus.PAUSED_FOR_HUMAN
    assert restored.active_intervention["type"] == "RUNTIME_RECOVERY"


def test_artifact_verification_preserves_exact_identity_after_restart(tmp_path):
    from core.contracts import ArtifactVerificationSpec
    engine = TaskEngine(tmp_path / "tasks.json")
    task = engine.create_task("artifacts", "check exact files")
    first = tmp_path / "first" / "report.txt"
    second = tmp_path / "second" / "report.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first file", encoding="utf-8")
    second.write_text("second file", encoding="utf-8")
    engine.register_artifact(task.task_id, str(first))
    restored = TaskEngine(tmp_path / "tasks.json")
    assert restored.verify_artifact(task.task_id, str(second))
    artifacts = restored.get_task(task.task_id).artifacts
    assert len(artifacts) == 2
    assert not artifacts[0]["is_verified"]
    assert restored.verify_artifact(task.task_id, str(first))
    assert artifacts[0]["is_verified"]
    assert len(artifacts) == 2
    old = time.time() - 7200
    os.utime(first, (old, old))
    spec = ArtifactVerificationSpec(str(first), mtime_threshold_seconds=600)
    assert not restored.verify_artifact(task.task_id, str(first), spec)


def test_scheduler_timezone_is_used_for_matching_and_next_run(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    schedule = scheduler.add(name="Baghdad morning", cron="0 9 * * *", target="test", tz="Asia/Baghdad")
    instant = datetime(2026, 10, 2, 5, 30, tzinfo=timezone.utc)
    assert scheduler._next_run(schedule, instant) == datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc).timestamp()
    called = []
    scheduler.set_runner(lambda item: called.append(item.id))
    schedule.next_run_at = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc).timestamp()
    scheduler._tick(datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc))
    deadline = time.monotonic() + 2
    while scheduler.runtime_status()["active_runs"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert called == [schedule.id]


def test_scheduler_rejects_invalid_timezone_before_mutating(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    schedule = scheduler.add(name="valid", cron="* * * * *", target="test")
    with pytest.raises(ValueError):
        scheduler.update(schedule.id, tz="Not/AZone")
    assert schedule.tz == "UTC"
    with pytest.raises(ValueError):
        scheduler.add(name="invalid", cron="* * * * *", target="test", tz="Not/AZone")
    assert len(scheduler.list()) == 1


def test_scheduler_history_survives_restart(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    schedule = scheduler.add(name="test", cron="* * * * *", target="test")
    scheduler.set_runner(lambda item: None)
    assert scheduler.fire_now(schedule.id).status == "success"
    restored = fresh_scheduler(tmp_path)
    restored._load()
    assert restored.history(schedule.id)[-1].status == "success"


def test_scheduler_nested_corrupt_primary_recovers_backup_without_auto_dispatch(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    schedule = scheduler.add(name="first", cron="* * * * *", target="test")
    scheduler.update(schedule.id, name="newer")
    data = json.loads(scheduler.path.read_text(encoding="utf-8"))
    data["history"] = {schedule.id: ["invalid record"]}
    scheduler.path.write_text(json.dumps(data), encoding="utf-8")
    restored = fresh_scheduler(tmp_path)
    restored._load()
    assert restored.get(schedule.id).name == "first"
    assert not restored.get(schedule.id).enabled
    assert restored.get(schedule.id).last_status == "interrupted"
    called = []
    restored.set_runner(lambda item: called.append(item.id))
    restored._tick(datetime.fromtimestamp(schedule.next_run_at, timezone.utc))
    assert called == []


def test_scheduler_restart_marks_uncertain_run_and_skips_missed_work(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    schedule = scheduler.add(name="missed", cron="* * * * *", target="test")
    schedule.next_run_at = time.time() - 600
    scheduler._save()
    restored = fresh_scheduler(tmp_path)
    restored._load()
    assert restored.history(schedule.id)[-1].status == "skipped"
    assert restored._pending == __import__("collections").deque()
    schedule.last_status = "running"
    scheduler._save()
    restored._load()
    assert restored.get(schedule.id).last_status == "interrupted"
    assert "unknown" in restored.get(schedule.id).last_error


def test_scheduler_storage_fault_prevents_runner_and_rolls_back_crud(tmp_path, monkeypatch):
    scheduler = fresh_scheduler(tmp_path)
    schedule = scheduler.add(name="before", cron="* * * * *", target="test")
    called = []
    scheduler.set_runner(lambda item: called.append(item.id))
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("fault")))
    with pytest.raises(SchedulePersistenceError):
        scheduler.update(schedule.id, name="not durable")
    assert scheduler.get(schedule.id).name == "before"
    with pytest.raises(SchedulePersistenceError):
        scheduler.fire_now(schedule.id)
    assert called == []
    assert scheduler.runtime_status()["active_runs"] == 0


def test_manual_scheduler_fire_shares_global_budget_and_stop_drains(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    scheduler._max_concurrent_runs = 1
    first = scheduler.add(name="first", cron="* * * * *", target="test", payload={"id": 1})
    second = scheduler.add(name="second", cron="* * * * *", target="test", payload={"id": 2})
    entered, release = threading.Event(), threading.Event()
    called = []
    def run(item):
        called.append(item.id)
        entered.set()
        assert release.wait(2)
    scheduler.set_runner(run)
    thread = threading.Thread(target=scheduler.fire_now, args=(first.id,))
    thread.start()
    assert entered.wait(1)
    assert scheduler.fire_now(second.id).status == "failed"
    start = time.monotonic()
    scheduler.stop(timeout=.05)
    assert time.monotonic() - start < .3
    assert scheduler.runtime_status()["active_runs"] == 1
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert scheduler.runtime_status()["active_runs"] == 0
    assert called == [first.id]


def test_scheduler_durable_minute_claim_prevents_repeated_and_restart_dispatch(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    schedule = scheduler.add(name="once", cron="* * * * *", target="test")
    due = datetime.fromtimestamp(schedule.next_run_at, timezone.utc)
    called = []
    scheduler.set_runner(lambda item: called.append(item.id))
    scheduler._tick(due)
    deadline = time.monotonic() + 2
    while scheduler.runtime_status()["active_runs"] and time.monotonic() < deadline:
        time.sleep(.01)
    scheduler._tick(due)
    assert called == [schedule.id]
    restored = fresh_scheduler(tmp_path)
    restored._load()
    restored.set_runner(lambda item: called.append(item.id))
    restored._tick(due)
    assert called == [schedule.id]


def test_deleted_queued_schedule_is_not_dispatched(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    scheduler._max_concurrent_runs = 1
    first = scheduler.add(name="first", cron="* * * * *", target="test", payload={"id": 1})
    second = scheduler.add(name="second", cron="* * * * *", target="test", payload={"id": 2})
    entered, release = threading.Event(), threading.Event()
    called = []
    def run(item):
        called.append(item.id)
        entered.set()
        assert release.wait(2)
    scheduler.set_runner(run)
    scheduler._enqueue_scheduled(first, run)
    assert entered.wait(1)
    scheduler._enqueue_scheduled(second, run)
    assert scheduler.runtime_status()["queued_runs"] == 1
    assert scheduler.delete(second.id)
    release.set()
    deadline = time.monotonic() + 2
    while scheduler.runtime_status()["active_runs"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert called == [first.id]
    assert scheduler.runtime_status()["queued_runs"] == 0


def test_schedule_edits_do_not_change_an_inflight_execution_payload(tmp_path):
    scheduler = fresh_scheduler(tmp_path)
    schedule = scheduler.add(name="intent", cron="* * * * *", target="old-target", payload={"value": "old"})
    entered, release = threading.Event(), threading.Event()
    observed = []
    def run(snapshot):
        entered.set()
        assert release.wait(2)
        observed.append((snapshot.target, snapshot.payload["value"]))
    scheduler.set_runner(run)
    thread = threading.Thread(target=scheduler.fire_now, args=(schedule.id,))
    thread.start()
    assert entered.wait(1)
    scheduler.update(schedule.id, target="new-target", payload={"value": "new"})
    release.set()
    thread.join(2)
    assert observed == [("old-target", "old")]
    assert scheduler.get(schedule.id).target == "new-target"
