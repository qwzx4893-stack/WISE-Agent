"""Long-running tasks must survive a resident-runtime restart."""

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
