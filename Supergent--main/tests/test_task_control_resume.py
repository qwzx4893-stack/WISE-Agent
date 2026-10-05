"""The task-control API must invoke real recovery execution, not fake a resume."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("action", ["resolve_human", "resume", "continue"])
def test_resolve_human_runs_closed_loop_recovery(monkeypatch, action):
    from api.server import V2TaskControlRequest, v2_task_control

    observed = {}
    monkeypatch.setattr(
        "core.brain.task_engine.get_task_engine",
        lambda: SimpleNamespace(get_task=lambda task_id: SimpleNamespace(context_variables={}) if task_id == "task-1" else None),
    )

    class _Result:
        def to_dict(self):
            return {"success": True, "error": None, "task_id": "task-1"}

    class _Orchestrator:
        def submit_human_intervention_resolution(self, task_id, *, action, resolution_payload):
            observed.update(task_id=task_id, action=action, payload=resolution_payload)
            return _Result()

    monkeypatch.setattr(
        "core.orchestrator.closed_loop_orchestrator.get_closed_loop_orchestrator",
        lambda: _Orchestrator(),
    )

    response = asyncio.run(v2_task_control(
        "task-1",
        V2TaskControlRequest(action=action, payload={"approved_by": "user"}),
    ))

    assert observed == {"task_id": "task-1", "action": "completed", "payload": {"approved_by": "user"}}
    assert response["ok"] is True
    assert response["execution"]["success"] is True


def test_unknown_task_cannot_be_resumed(monkeypatch):
    from api.server import V2TaskControlRequest, v2_task_control
    from fastapi import HTTPException

    monkeypatch.setattr("core.brain.task_engine.get_task_engine", lambda: SimpleNamespace(get_task=lambda _: None))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(v2_task_control("missing", V2TaskControlRequest(action="resume")))
    assert exc.value.status_code == 404


@pytest.mark.parametrize("action", ["resume", "continue", "resolve_human", "modify"])
def test_dynamic_task_resume_does_not_claim_execution(monkeypatch, action):
    from api.server import V2TaskControlRequest, v2_task_control
    from fastapi import HTTPException

    task = SimpleNamespace(context_variables={"execution_kind": "capability"})
    monkeypatch.setattr("core.brain.task_engine.get_task_engine", lambda: SimpleNamespace(get_task=lambda _: task))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(v2_task_control("stopped", V2TaskControlRequest(action=action)))
    assert exc.value.status_code == 409


def test_terminal_task_cannot_be_cancelled_retroactively(monkeypatch):
    from api.server import V2TaskControlRequest, v2_task_control
    from fastapi import HTTPException
    task = SimpleNamespace(status=SimpleNamespace(value="COMPLETED"), context_variables={})
    monkeypatch.setattr("core.brain.task_engine.get_task_engine", lambda: SimpleNamespace(get_task=lambda _: task))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(v2_task_control("done", V2TaskControlRequest(action="cancel")))
    assert exc.value.status_code == 409
