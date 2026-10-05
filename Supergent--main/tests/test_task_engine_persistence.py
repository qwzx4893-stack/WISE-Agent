"""Long-running tasks must survive a resident-runtime restart."""

import os
import pytest

from core.brain.task_engine import (
    Subgoal,
    TaskEngine,
    TaskStatus,
    TaskStep,
)
from core.hands import ComputerActionType


def test_active_task_lookup_is_scoped_to_conversation(tmp_path):
    engine = TaskEngine(storage_path=tmp_path / "tasks.json")
    first = engine.create_capability_task("first", "conversation-a")
    second = engine.create_capability_task("second", "conversation-b")
    assert engine.get_active_task().task_id == second.task_id
    assert engine.get_active_task(session_id="conversation-a").task_id == first.task_id
    assert engine.get_active_task(session_id="missing") is None


def test_paused_task_restores_exact_step_and_intervention(tmp_path):
    path = tmp_path / "tasks.json"
    engine = TaskEngine(storage_path=path)
    task = engine.create_task(
        "repair the project",
        "complete repair safely",
        subgoals=[Subgoal(title="repair", steps=[
            TaskStep(action_type=ComputerActionType.WAIT, params={"duration": 0.01}),
            TaskStep(action_type=ComputerActionType.WAIT, params={"duration": 0.01}),
        ])],
        session_id="persistent-session",
    )
    assert engine.start_task(task.task_id)
    # A cursor is an execution acknowledgement, not a substitute for one.
    engine.record_step_result(task.task_id, task.subgoals[0].steps[0].step_id, True)
    engine.advance_step(task.task_id)
    engine.pause_for_human(task.task_id, intervention_type="UAC", reason="Windows approval needed")

    restored = TaskEngine(storage_path=path).get_task(task.task_id)

    assert restored is not None
    assert restored.status == TaskStatus.PAUSED_FOR_HUMAN
    assert restored.current_subgoal_idx == 0
    assert restored.subgoals[0].current_step_idx == 1
    assert restored.active_intervention["type"] == "UAC"


def test_task_modification_persists_redacted_context(tmp_path):
    path = tmp_path / "tasks.json"
    engine = TaskEngine(storage_path=path)
    task = engine.create_task("prepare", "prepare system")

    assert engine.modify_task(
        task.task_id,
        "focus on diagnostics",
        context_update={"api_key": "must-not-persist", "scope": "system health"},
    )
    restored = TaskEngine(storage_path=path).get_task(task.task_id)

    assert restored is not None
    assert restored.context_variables["api_key"] != "must-not-persist"
    assert restored.context_variables["scope"] == "system health"


def test_running_task_becomes_explicit_recovery_pause_after_restart(tmp_path):
    path = tmp_path / "tasks.json"
    engine = TaskEngine(storage_path=path)
    task = engine.create_task("continue work", "finish safely")
    assert engine.start_task(task.task_id)

    restored = TaskEngine(storage_path=path).get_task(task.task_id)

    assert restored.status == TaskStatus.PAUSED_FOR_HUMAN
    assert restored.active_intervention["type"] == "RUNTIME_RECOVERY"


def test_task_state_recovers_from_last_good_backup(tmp_path):
    path = tmp_path / "tasks.json"
    engine = TaskEngine(storage_path=path)
    task = engine.create_task("first", "keep a recoverable state")
    # A second successful write creates the backup of the first valid state.
    engine.create_task("second", "newer state")
    path.write_text("{not valid JSON", encoding="utf-8")

    restored = TaskEngine(storage_path=path)

    assert restored.get_task(task.task_id) is not None


def test_manual_pause_can_resume_and_extended_plan_persists(tmp_path):
    path = tmp_path / "tasks.json"
    engine = TaskEngine(storage_path=path)
    task = engine.create_task(
        "pause safely",
        "resume from the same point",
        subgoals=[Subgoal(title="work", steps=[TaskStep(action_type=ComputerActionType.WAIT)])],
    )
    assert engine.start_task(task.task_id)
    assert engine.stop_task(task.task_id)
    assert engine.resume_task(task.task_id)
    assert engine.extend_plan_with_steps(
        task.task_id,
        task.subgoals[0].subgoal_id,
        [TaskStep(action_type=ComputerActionType.WAIT)],
    )

    restored = TaskEngine(storage_path=path).get_task(task.task_id)

    assert restored.status == TaskStatus.PAUSED_FOR_HUMAN  # RUNNING becomes recovery-safe at restart
    assert len(restored.subgoals[0].steps) == 2


def test_transient_windows_rename_denial_retries_without_repeating_task_action(tmp_path, monkeypatch):
    import os
    import core.brain.task_engine as task_module
    engine = TaskEngine(tmp_path / "tasks.json")
    task = engine.create_capability_task("observe", "isolated")
    replace = os.replace
    attempts = {"primary": 0, "backup": 0}
    delays = []
    def briefly_blocked(source, destination):
        kind = "backup" if destination.suffix == ".bak" else "primary"
        attempts[kind] += 1
        if attempts[kind] <= 2:
            error = PermissionError("synthetic Windows sharing denial")
            error.winerror = 5 if kind == "primary" else 32
            raise error
        replace(source, destination)
    monkeypatch.setattr(os, "replace", briefly_blocked)
    monkeypatch.setattr(task_module.time, "sleep", delays.append)
    engine.record_capability_event(task.task_id, "native.read_file", "COMPLETED")
    assert attempts == {"primary": 3, "backup": 3}
    assert delays == [.025, .05, .025, .05]
    restored = TaskEngine(tmp_path / "tasks.json").get_task(task.task_id)
    assert len(restored.history) == 1
    assert restored.history[0]["status"] == "COMPLETED"
    assert len(restored.milestones) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_persistent_windows_rename_denial_remains_failed_and_rolls_back(tmp_path, monkeypatch):
    import os
    import pytest
    import core.brain.task_engine as task_module
    from core.brain.task_engine import TaskPersistenceError
    engine = TaskEngine(tmp_path / "tasks.json")
    task = engine.create_capability_task("observe", "isolated")
    original = (tmp_path / "tasks.json").read_bytes()
    replace = os.replace
    attempts, delays = [], []
    def deny_primary(source, destination):
        if destination.suffix == ".bak":
            return replace(source, destination)
        attempts.append(str(source))
        error = PermissionError("synthetic permanent access denial")
        error.winerror = 5
        raise error
    monkeypatch.setattr(os, "replace", deny_primary)
    monkeypatch.setattr(task_module.time, "sleep", delays.append)
    with pytest.raises(TaskPersistenceError):
        engine.record_capability_event(task.task_id, "native.read_file", "COMPLETED")
    assert len(attempts) == 5 and len(set(attempts)) == 1
    assert sum(delays) == .375
    assert task.history == [] and task.milestones == []
    assert (tmp_path / "tasks.json").read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")
def test_real_windows_reader_release_allows_atomic_task_transition(tmp_path, monkeypatch):
    import ctypes
    import threading
    from ctypes import wintypes
    engine = TaskEngine(tmp_path / "tasks.json")
    task = engine.create_capability_task("observe", "isolated")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # FILE_SHARE_READ but not FILE_SHARE_DELETE: a real temporary reader
    # blocks replacing this test file until its handle is closed.
    handle = kernel.CreateFileW(str(tmp_path / "tasks.json"), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError(ctypes.get_last_error())
    release = threading.Event()
    first_denial = threading.Event()
    observed_errors = []
    replace = os.replace
    def observed_replace(source, destination):
        try:
            return replace(source, destination)
        except OSError as exc:
            if destination == tmp_path / "tasks.json":
                observed_errors.append(getattr(exc, "winerror", None))
                first_denial.set()
            raise
    monkeypatch.setattr(os, "replace", observed_replace)
    def close_reader():
        first_denial.wait(2)
        release.wait(.1)
        kernel.CloseHandle(handle)
    thread = threading.Thread(target=close_reader)
    thread.start()
    try:
        engine.record_capability_event(task.task_id, "native.read_file", "COMPLETED")
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert observed_errors and all(code in {5, 32, 33} for code in observed_errors)
    restored = TaskEngine(tmp_path / "tasks.json").get_task(task.task_id)
    assert len(restored.history) == 1
    assert restored.history[0]["status"] == "COMPLETED"
