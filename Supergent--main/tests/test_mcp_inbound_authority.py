"""Inbound MCP approval regression tests; no real settings/OS writes."""

import json
import sys
import threading
from types import SimpleNamespace

import pytest

from core.mcp import server as mcp_server
from core.mcp.policy import ConfirmationGate


APPROVAL_KEY = "agent-os/confirmation_call_id"


@pytest.fixture
def harness(monkeypatch):
    from core.admin_tools import admin_set_resource_limits

    gate = ConfirmationGate()
    saves = []
    store = SimpleNamespace(load=lambda **kw: SimpleNamespace(),
                            save=lambda value: saves.append(vars(value).copy()))
    monkeypatch.setitem(sys.modules, "core.resource_settings",
                        SimpleNamespace(get_store=lambda: store))
    monkeypatch.setitem(sys.modules, "core.platform_manager",
                        SimpleNamespace(get_platform_manager=lambda: SimpleNamespace(mode="lite")))
    monkeypatch.setitem(sys.modules, "agent_core", SimpleNamespace(DANGEROUS_PATTERNS=[]))
    monkeypatch.setattr(mcp_server, "get_gate", lambda: gate)
    registry = SimpleNamespace(tools={"admin_set_resource_limits": admin_set_resource_limits,
                                      "create_note": lambda title: title},
                               get=lambda name: registry.tools.get(name))
    srv = mcp_server.MCPServer(tool_registry=registry)
    return srv, gate, saves, registry


def call(srv, arguments=None, call_id=None, name="admin_set_resource_limits"):
    params = {"name": name, "arguments": arguments or {"cpu_seconds": 120, "confirmed": True}}
    if call_id is not None:
        params["_meta"] = {APPROVAL_KEY: call_id}
    return srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})


def pending(srv, gate):
    result = call(srv)
    assert result["error"]["code"] == -32002
    call_id = result["error"]["data"]["call_id"]
    assert gate.list_pending()[0]["call_id"] == call_id
    return call_id


def test_model_confirm_flags_do_not_authorize_real_settings_save(harness):
    srv, gate, saves, _ = harness
    result = call(srv, {"cpu_seconds": 120, "confirmed": True, "confirm": True})
    assert result["error"]["code"] == -32002
    assert saves == []


def test_owner_resolution_executes_exact_call_once(harness):
    srv, gate, saves, _ = harness
    call_id = pending(srv, gate)
    assert gate.resolve(call_id, approved=True)
    result = call(srv, call_id=call_id)
    assert json.loads(result["result"]["content"][0]["text"])["ok"] is True
    assert saves == [{"cpu_seconds": 120, "mode_at_save": "lite"}]
    assert gate.list_pending() == []
    assert call(srv, call_id=call_id)["error"]["code"] == -32003
    assert len(saves) == 1


def test_unresolved_retry_preserves_owner_pending_request(harness):
    srv, gate, saves, _ = harness
    call_id = pending(srv, gate)
    result = call(srv, call_id=call_id)
    assert result["error"]["code"] == -32002
    assert result["error"]["data"]["call_id"] == call_id
    assert gate.resolve(call_id, approved=True)
    assert "result" in call(srv, call_id=call_id)
    assert len(saves) == 1


@pytest.mark.parametrize("change", ["arguments", "tool", "server", "instance", "argument_type"])
def test_approval_binding_cannot_be_reused_for_different_call(harness, change):
    srv, gate, saves, registry = harness
    call_id = pending(srv, gate)
    assert gate.resolve(call_id, approved=True)
    args, name = None, "admin_set_resource_limits"
    if change == "arguments":
        args = {"cpu_seconds": 999, "confirmed": True}
    elif change == "argument_type":
        args = {"cpu_seconds": 120, "confirmed": 1}
    elif change == "tool":
        name, args = "create_note", {"title": "different"}
    elif change == "server":
        srv.server_name = "different"
    else:
        srv = mcp_server.MCPServer(tool_registry=registry)
    assert call(srv, args, call_id, name)["error"]["code"] == -32003
    assert saves == []


def test_owner_denial_cannot_be_overridden_by_flags(harness):
    srv, gate, saves, _ = harness
    call_id = pending(srv, gate)
    assert gate.resolve(call_id, approved=False)
    result = call(srv, {"cpu_seconds": 120, "confirmed": True, "confirm": True}, call_id)
    assert result["error"]["code"] == -32003
    assert saves == []


def test_expired_approval_cannot_execute(harness, monkeypatch):
    srv, gate, saves, _ = harness
    call_id = pending(srv, gate)
    gate.resolve(call_id, approved=True)
    original = mcp_server.time.monotonic()
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: original + 301)
    assert call(srv, call_id=call_id)["error"]["code"] == -32003
    assert saves == []


def test_forged_and_model_argument_call_ids_fail(harness):
    srv, gate, saves, _ = harness
    assert call(srv, call_id="not-issued")["error"]["code"] == -32003
    call_id = pending(srv, gate)
    gate.resolve(call_id, approved=True)
    result = call(srv, {"cpu_seconds": 120, "confirmed": True,
                        "confirm": True, APPROVAL_KEY: call_id})
    assert result["error"]["code"] == -32002
    assert saves == []


def test_concurrent_approved_retries_execute_at_most_once(harness):
    srv, gate, saves, _ = harness
    call_id = pending(srv, gate)
    gate.resolve(call_id, approved=True)
    barrier = threading.Barrier(2)
    results = []

    def retry():
        barrier.wait(timeout=3)
        results.append(call(srv, call_id=call_id))

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert sum("result" in result for result in results) == 1
    assert len(saves) == 1


def test_gate_failure_never_executes(harness, monkeypatch):
    srv, gate, saves, _ = harness
    monkeypatch.setattr(gate, "request", lambda **kw: None)
    assert call(srv)["error"]["code"] == -32000
    assert saves == []


def test_kernel_policy_import_failure_fails_closed(harness, monkeypatch):
    srv, gate, saves, _ = harness
    monkeypatch.setitem(sys.modules, "agent_core", None)
    result = call(srv, {"cpu_seconds": 120, "confirmed": True, "confirm": True})
    assert result["error"]["code"] == -32001
    assert saves == []


def test_client_keeps_approval_error_and_allows_only_explicit_metadata_retry(harness, monkeypatch):
    from core.mcp.client import MCPClient, MCPError

    srv, gate, saves, _ = harness
    client = MCPClient(name="fixture", transport="http", url="http://127.0.0.1/unused")
    client._alive = True
    frames = []

    def fixture_post(frame):
        frames.append(frame)
        return srv.dispatch(frame)

    monkeypatch.setattr(client, "_http_post", fixture_post)
    args = {"cpu_seconds": 120, "confirmed": True}
    with pytest.raises(MCPError) as error:
        client.call_tool("admin_set_resource_limits", args)
    assert error.value.code == -32002
    assert len(frames) == 1 and saves == []
    call_id = error.value.data["call_id"]
    assert error.value.data["retry_meta_key"] == APPROVAL_KEY
    assert gate.resolve(call_id, approved=True)
    client.call_tool("admin_set_resource_limits", args, metadata={APPROVAL_KEY: call_id})
    assert frames[-1]["params"]["arguments"] == args
    assert frames[-1]["params"]["_meta"] == {APPROVAL_KEY: call_id}
    assert len(saves) == 1


def test_nested_argument_mutation_cannot_change_owner_approved_snapshot(harness):
    srv, gate, saves, registry = harness
    invoked = []
    registry.tools["create_nested"] = lambda payload: invoked.append(payload)
    args = {"payload": {"nested": [1, 2]}}
    result = call(srv, args, name="create_nested")
    call_id = result["error"]["data"]["call_id"]
    args["payload"]["nested"].append(3)
    assert gate.list_pending()[-1]["arguments"] == {"payload": {"nested": [1, 2]}}
    assert gate.resolve(call_id, approved=True)
    assert call(srv, args, call_id, "create_nested")["error"]["code"] == -32003
    assert invoked == []


@pytest.mark.parametrize("arguments", [{"cpu_seconds": float("nan")}, [1, 2]])
def test_non_json_or_non_object_arguments_cannot_create_approval(harness, arguments):
    srv, gate, saves, _ = harness
    assert call(srv, arguments)["error"]["code"] == -32602
    assert gate.list_pending() == [] and saves == []
