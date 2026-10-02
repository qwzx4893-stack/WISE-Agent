"""Tests for §2 (Agent OS as MCP server) and §4 (bridge into
system_awareness + ThinkingEngine)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest


FAKE_MCP = str(Path(__file__).parent / "_fake_mcp_server.py")


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    # Force every test to start with a clean classifier/gate state.
    yield


# --------------------------------------------------------------------------
# §2 — MCPServer (expose Agent OS)
# --------------------------------------------------------------------------
class _FakeRegistry:
    def __init__(self, tools):
        self.tools = tools
    def get(self, name):
        return self.tools.get(name)


def test_server_lists_tools_with_kind():
    from core.mcp.server import MCPServer
    srv = MCPServer(tool_registry=_FakeRegistry({
        "list_users": lambda: ["a", "b"],
        "create_user": lambda name: f"user/{name}",
    }))
    out = srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    by_name = {t["name"]: t for t in out["result"]["tools"]}
    assert by_name["list_users"]["x-agent-os"]["kind"] == "read"
    assert by_name["create_user"]["x-agent-os"]["kind"] == "write"


def test_server_denylist_hides_execute_command():
    from core.mcp.server import MCPServer, DEFAULT_DENY
    srv = MCPServer(tool_registry=_FakeRegistry({
        "execute_command": lambda command: command,
        "ok_tool": lambda: "ok",
    }))
    out = srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in out["result"]["tools"]}
    assert "execute_command" not in names
    assert "execute_command" in DEFAULT_DENY
    assert "ok_tool" in names


def test_server_write_requires_confirm():
    from core.mcp.server import MCPServer
    srv = MCPServer(tool_registry=_FakeRegistry({
        "create_note": lambda title, body: f"created {title}",
    }))
    # Without confirm — gate raises -32002 with call_id.
    out = srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "create_note",
                                     "arguments": {"title": "t", "body": "b"}}})
    assert out["error"]["code"] == -32002
    assert "call_id" in out["error"]["data"]
    # With confirm: passes.
    out = srv.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "create_note",
                                     "arguments": {"title": "t", "body": "b",
                                                    "confirm": True}}})
    assert "result" in out


def test_server_stdio_entrypoint_speaks_jsonrpc():
    """`python -m core.mcp.server --transport stdio` answers initialize +
    tools/list with the same shape Claude Desktop / Codex CLI expects."""
    proc = subprocess.Popen(
        ["python3", "-m", "core.mcp.server", "--transport", "stdio",
         "--allow", "verify_python"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=0,
    )
    try:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1,
                                       "method": "initialize",
                                       "params": {}}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline().strip()
        init = json.loads(line)
        assert init["result"]["protocolVersion"] == "2024-11-05"

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2,
                                       "method": "tools/list"}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline().strip()
        listed = json.loads(line)
        names = [t["name"] for t in listed["result"]["tools"]]
        assert "verify_python" in names
    finally:
        proc.stdin.close()
        proc.wait(timeout=4)


# --------------------------------------------------------------------------
# §4 — bridge into system_awareness + ThinkingEngine surface
# --------------------------------------------------------------------------
def test_register_mcp_tools_populates_awareness():
    from core.mcp import MCPRegistry
    from core.system_awareness import SystemAwareness

    class Reg:
        def __init__(self): self.tools = {}
        def register(self, n, f): self.tools[n] = f
        def get(self, n): return self.tools.get(n)

    mcp_reg = MCPRegistry()
    mcp_reg.add({"name": "br", "transport": "stdio",
                 "command": "python3", "args": [FAKE_MCP],
                 "enabled": True, "sandbox": False})
    aw = SystemAwareness()
    n = aw.register_mcp_tools(Reg(), mcp_reg)
    try:
        assert n >= 2  # echo + add at least
        keys = [k for k in aw.tools if k.startswith("mcp_br_")]
        assert keys
        for k in keys:
            assert aw.tools[k]["__mcp__"] is True
            assert aw.tools[k]["category"] == "MCP"
            assert "kind" in aw.tools[k]["security"]
        desc = aw.get_full_system_description()
        assert "MCP" in desc
        assert "mcp_br_echo" in desc
    finally:
        mcp_reg.stop_all()


def test_tracer_emits_mcp_call_for_bridged_tools():
    from core.mcp import MCPRegistry
    from core.observability import Tracer
    from core.system_awareness import SystemAwareness

    class Reg:
        def __init__(self): self.tools = {}
        def register(self, n, f): self.tools[n] = f
        def get(self, n): return self.tools.get(n)

    mcp_reg = MCPRegistry()
    mcp_reg.add({"name": "tx", "transport": "stdio",
                 "command": "python3", "args": [FAKE_MCP],
                 "enabled": True, "auto_approve": ["add"], "sandbox": False})
    aw = SystemAwareness()
    reg = Reg()
    aw.register_mcp_tools(reg, mcp_reg)
    try:
        reg.tools["mcp_tx_echo"](text="hi")
        reg.tools["mcp_tx_add"](a=2, b=3)
        evts = Tracer.events(kind="mcp.call")
        kinds = {(e.get("tool"), e.get("tool_kind")) for e in evts
                 if e.get("kind") == "mcp.call.start"}
        assert ("echo", "read") in kinds
        assert ("add", "write") in kinds
    finally:
        mcp_reg.stop_all()


def test_thinking_parser_resolves_mcp_tools_with_fuzz():
    """ThinkingEngine reaches MCP tools through the same resolver native
    tools use, so kebab/camel variants still hit the right name."""
    from core.thinking.parser import ToolCallParser
    p = ToolCallParser(["mcp_alpha_echo", "search_knowledge"])
    calls = p.extract_calls(
        "Action: mcp-alpha-echo\nAction Input: {\"text\": \"hi\"}",
    )
    assert calls and calls[0].tool == "mcp_alpha_echo"
    assert calls[0].fuzzy_from == "mcp-alpha-echo"
