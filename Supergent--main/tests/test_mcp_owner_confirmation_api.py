"""Synthetic owner decision contracts; handler extraction never boots WISE.

No app imports, sockets, connector/server processes, models, or owner settings.
"""

import ast
import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional

import pytest


class SyntheticHTTPException(Exception):
    def __init__(self, *, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@pytest.fixture
def handler(monkeypatch):
    """Compile only the real route function, with synthetic dependencies."""
    source_path = Path(__file__).parents[1] / "api" / "server.py"
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    function = next(node for node in module.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "mcp_confirm")
    function.decorator_list = []
    extracted = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(extracted)
    state = SimpleNamespace(auth_calls=[], gate_calls=0, decisions=[], denied=False, resolved=True)

    def authenticate(value):
        state.auth_calls.append(value)
        if state.denied:
            raise SyntheticHTTPException(status_code=401, detail="synthetic authentication denied")

    def resolve(call_id, *, approved, reason):
        state.decisions.append((call_id, approved, reason))
        return state.resolved

    def get_gate():
        state.gate_calls += 1
        return SimpleNamespace(resolve=resolve)

    synthetic_mcp = ModuleType("core.mcp")
    synthetic_mcp.get_gate = get_gate
    monkeypatch.setitem(sys.modules, "core.mcp", synthetic_mcp)
    namespace = {"Any": Any, "Dict": Dict, "Optional": Optional,
                 "Header": lambda default=None: default,
                 "HTTPException": SyntheticHTTPException, "_require_auth": authenticate}
    exec(compile(extracted, str(source_path), "exec"), namespace)
    return namespace["mcp_confirm"], state


def invoke(handler, payload):
    function, _ = handler
    return asyncio.run(function("synthetic-call-id", payload=payload, x_agent_token=None))


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("reason", [None, "", "Synthetic owner decision", "x" * 1000])
def test_explicit_boolean_decision_reaches_gate(handler, approved, reason):
    payload = {"approved": approved}
    if reason is not None:  # Omitted optional reason, not JSON null.
        payload["reason"] = reason
    result = invoke(handler, payload)
    _, state = handler
    assert result == {"resolved": True, "approved": approved, "call_id": "synthetic-call-id"}
    assert state.auth_calls == [None]
    assert state.gate_calls == 1
    assert state.decisions == [("synthetic-call-id", approved, reason or "")]
    assert type(state.decisions[0][1]) is bool


@pytest.mark.parametrize("payload", [None, {}, [], "synthetic payload", 1,
    {"approved": None}, {"approved": "true"}, {"approved": "false"},
    {"approved": ""}, {"approved": 0}, {"approved": 1}, {"approved": 1.0},
    {"approved": []}, {"approved": {}}, {"reason": "Synthetic reason only"}])
def test_missing_or_non_boolean_decision_never_resolves(handler, payload):
    with pytest.raises(SyntheticHTTPException) as error:
        invoke(handler, payload)
    _, state = handler
    assert error.value.status_code == 422
    assert state.auth_calls == [None]
    assert state.gate_calls == 0 and state.decisions == []


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("reason", [None, False, True, 0, 1, 1.5, [], {}, "x" * 1001])
def test_invalid_reason_never_resolves(handler, approved, reason):
    with pytest.raises(SyntheticHTTPException) as error:
        invoke(handler, {"approved": approved, "reason": reason})
    _, state = handler
    assert error.value.status_code == 422
    assert state.auth_calls == [None]
    assert state.gate_calls == 0 and state.decisions == []


@pytest.mark.parametrize("payload", [{"approved": True}, {"approved": False}, None,
    {"approved": "true"}, {"approved": True, "reason": "x" * 1001}])
def test_authentication_runs_before_payload_validation(handler, payload):
    _, state = handler
    state.denied = True
    with pytest.raises(SyntheticHTTPException) as error:
        invoke(handler, payload)
    assert error.value.status_code == 401
    assert state.auth_calls == [None]
    assert state.gate_calls == 0 and state.decisions == []


def test_unknown_call_is_reported_without_inventing_resolution(handler):
    _, state = handler
    state.resolved = False
    assert invoke(handler, {"approved": True}) == {
        "resolved": False, "approved": True, "call_id": "synthetic-call-id"}
    assert state.gate_calls == 1 and len(state.decisions) == 1
