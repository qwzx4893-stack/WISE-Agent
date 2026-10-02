"""Tests for §1 (security & control) and §5 (dangerous blocked) of the
MCP spec."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import pytest


FAKE_MCP = str(Path(__file__).parent / "_fake_mcp_server.py")


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    yield


# --------------------------------------------------------------------------
# Tool classification
# --------------------------------------------------------------------------
def test_classifier_marks_destructive_as_dangerous():
    from core.mcp.policy import ToolClassifier
    clf = ToolClassifier()
    cls = clf.classify({"name": "destroy_db",
                         "description": "rm -rf /var/lib/db"})
    assert cls.kind == "dangerous"
    # auto-approve must never apply to dangerous.
    clf2 = ToolClassifier(auto_approve=["destroy_db"])
    cls2 = clf2.classify({"name": "destroy_db",
                           "description": "rm -rf /var/lib/db"})
    assert cls2.auto_approved is False


def test_classifier_read_vs_write_via_verb():
    from core.mcp.policy import ToolClassifier
    clf = ToolClassifier()
    assert clf.classify({"name": "list_users"}).kind == "read"
    assert clf.classify({"name": "create_user"}).kind == "write"
    # Mid-name verb: safe_edit_file.
    assert clf.classify({"name": "safe_edit_file"}).kind == "write"


def test_classifier_explicit_override_wins():
    from core.mcp.policy import ToolClassifier
    clf = ToolClassifier(overrides={"echo": "write"})
    assert clf.classify({"name": "echo"}).kind == "write"


# --------------------------------------------------------------------------
# Confirmation gate
# --------------------------------------------------------------------------
def test_write_requires_confirmation_via_gate():
    from core.mcp import ConfirmationRequired, MCPRegistry, build_mcp_tools
    reg = MCPRegistry()
    reg.add({"name": "f1", "transport": "stdio",
             "command": "python3", "args": [FAKE_MCP],
             "classifications": {"add": "write"}, "sandbox": False})
    tools = build_mcp_tools(reg, auto_start=False)
    with pytest.raises(ConfirmationRequired):
        tools["mcp_f1_add"](a=1, b=2)
    # confirm=True bypasses the gate.
    out = tools["mcp_f1_add"](a=1, b=2, confirm=True)
    assert out.get("value") == 3.0
    reg.stop_all()


def test_dangerous_blocked_even_with_confirm_for_mcp_server_facade():
    """The §5 'dangerous command from MCP tool is blocked' requirement.

    The Agent OS MCP server (§2) sweeps every argument against
    DANGEROUS_PATTERNS regardless of the confirm flag. This proves the
    sweep fires even on a write tool the caller has 'confirmed'."""
    from core.mcp.server import MCPServer

    class FakeReg:
        tools = {"create_note": lambda title, body: f"created {title}/{body}"}
        def get(self, n): return self.tools.get(n)

    srv = MCPServer(tool_registry=FakeReg(), require_confirm=False)
    # Even with no confirmation gate, dangerous patterns reject.
    out = srv.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "create_note",
                                    "arguments": {"title": "ok",
                                                   "body": "rm -rf /"}}})
    assert "error" in out
    assert out["error"]["code"] == -32001
    assert "dangerous" in out["error"]["message"].lower()


def test_dangerous_mcp_tool_blocked_by_classifier():
    """delete_repo on the fake server is classified dangerous and never
    auto-approved through the bridge wrapper."""
    from core.mcp import ConfirmationRequired, MCPRegistry, build_mcp_tools
    reg = MCPRegistry()
    reg.add({"name": "fd", "transport": "stdio",
             "command": "python3", "args": [FAKE_MCP],
             "classifications": {"delete_repo": "dangerous"},
             "auto_approve": ["delete_repo"], "sandbox": False})  # ignored for dangerous
    tools = build_mcp_tools(reg, auto_start=False)
    meta = tools["mcp_fd_delete_repo"].__mcp_meta__
    assert meta["kind"] == "dangerous"
    assert meta["auto_approved"] is False
    with pytest.raises(ConfirmationRequired):
        tools["mcp_fd_delete_repo"](name="prod")
    reg.stop_all()


# --------------------------------------------------------------------------
# Network allowlist
# --------------------------------------------------------------------------
def test_http_transport_rejects_unlisted_domain():
    from core.mcp import MCPRegistry
    reg = MCPRegistry()
    res = reg.add({"name": "ext_bad", "transport": "http",
                   "url": "https://evil.example.com/mcp",
                   "allowed_domains": ["api.good.com"]})
    assert not res.get("alive")
    err = (res.get("error") or "").lower()
    assert "network policy" in err or "domain" in err


def test_http_transport_accepts_wildcard_match():
    from core.mcp.policy import validate_http_url, AllowlistError
    # Should pass — wildcard match.
    validate_http_url("https://api.foo.example.com/mcp",
                      ["*.example.com"])
    with pytest.raises(AllowlistError):
        validate_http_url("https://api.foo.evil.com/mcp",
                          ["*.example.com"])


def test_sandboxed_stdio_server_fails_closed_without_real_isolation(monkeypatch):
    from core.mcp import MCPRegistry

    monkeypatch.setattr("core.mcp.registry.is_sandbox_enforced", lambda: False)
    reg = MCPRegistry()
    result = reg.add({
        "name": "isolated", "transport": "stdio",
        "command": "python3", "args": [FAKE_MCP],
    })

    assert result["alive"] is False
    assert result["started"]["status"] == "ENVIRONMENT_BLOCKED"
    listed = next(row for row in reg.list_servers() if row["name"] == "isolated")
    assert listed["sandbox"] is True
    assert listed["sandbox_enforced"] is False
