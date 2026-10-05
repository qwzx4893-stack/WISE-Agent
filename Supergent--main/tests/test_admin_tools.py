"""Tests for the chat-callable admin tools (Part 1 of Phase 10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import admin_tools as at


# ---------------------------------------------------------------------------
# Result shape helpers
# ---------------------------------------------------------------------------
def _parse(s: str) -> dict:
    assert isinstance(s, str)
    return json.loads(s)


# ---------------------------------------------------------------------------
# Confirmation flow
# ---------------------------------------------------------------------------
def test_destructive_tool_refuses_without_confirmation():
    out = _parse(at.admin_remove_api_key(name="anything"))
    assert out["ok"] is False
    assert out["needs_confirmation"] is True
    assert "Re-issue" in out["next_action"]


def test_destructive_tool_passes_through_with_confirmation(monkeypatch):
    called = {}

    class _FakeKS:
        def remove(self, name):
            called["name"] = name
            return True

    monkeypatch.setattr("core.llm.keystore.KeyStore",
                        lambda *a, **k: _FakeKS())
    out = _parse(at.admin_remove_api_key(name="x", confirmed=True))
    assert out["ok"] is True
    assert called["name"] == "x"


# ---------------------------------------------------------------------------
# add_api_key happy + sad paths
# ---------------------------------------------------------------------------
def test_add_api_key_requires_args():
    out = _parse(at.admin_add_api_key(name="", api_key=""))
    assert out["ok"] is False
    assert "required" in out["summary"]


def test_add_api_key_persists_and_masks(tmp_path, monkeypatch):
    from core.llm.keystore import KeyStore
    monkeypatch.setattr(
        "core.llm.keystore.KeyStore",
        lambda *a, **k: KeyStore(path=tmp_path / "keys.json",
                                  secret_path=tmp_path / "secret.bin"))
    out = _parse(at.admin_add_api_key(
        name="prod", api_key="sk-test-abcdefghijklmnop", provider="openai"))
    assert out["ok"] is True
    assert "prod" in out["summary"]
    # The "applied" view must mask the raw key.
    assert "sk-test" not in json.dumps(out.get("applied", {}))


# ---------------------------------------------------------------------------
# list / get tools never raise
# ---------------------------------------------------------------------------
def test_list_tools_never_crash():
    for fn in (at.admin_list_api_keys, at.admin_list_mcp_servers,
                at.admin_list_channels, at.admin_list_schedules,
                at.admin_get_resources):
        out = _parse(fn())
        assert "ok" in out
        # Even when underlying stores are empty, structure stays sane.


# ---------------------------------------------------------------------------
# MCP / channel argument validation
# ---------------------------------------------------------------------------
def test_add_mcp_server_validates_kind():
    out = _parse(at.admin_add_mcp_server(name="x", kind="http"))
    assert out["ok"] is False  # missing url

    out = _parse(at.admin_add_mcp_server(name="x", kind="stdio"))
    assert out["ok"] is False  # missing command


def test_add_channel_validates_args():
    out = _parse(at.admin_add_channel(name="", url=""))
    assert out["ok"] is False


# ---------------------------------------------------------------------------
# Resource limits — recognised keys only
# ---------------------------------------------------------------------------
def test_set_resource_limits_rejects_unknown_keys():
    out = _parse(at.admin_set_resource_limits(unknown=42))
    assert out["ok"] is False
    assert "no recognised" in out["summary"]
    assert "cpu_seconds" in out["allowed"]


def test_set_resource_limits_needs_confirm_then_persists(monkeypatch, tmp_path):
    # Redirect the singleton store to a tmp file so this test can not
    # leak state into ``test_universal_platform.py``.
    import core.resource_settings as rs
    rs.ResourceSettingsStore._instance = None
    monkeypatch.setattr(rs, "SETTINGS_FILE",
                        tmp_path / "resources.json")
    try:
        out = _parse(at.admin_set_resource_limits(cpu_seconds=120))
        assert out["needs_confirmation"] is True
        assert out["preview"] == {"cpu_seconds": 120}

        out2 = _parse(at.admin_set_resource_limits(
            cpu_seconds=120, confirmed=True))
        assert out2["ok"] is True
        assert out2["applied"] == {"cpu_seconds": 120}
    finally:
        rs.ResourceSettingsStore._instance = None


# ---------------------------------------------------------------------------
# Mode switch — only lite/pro
# ---------------------------------------------------------------------------
def test_set_mode_validates():
    out = _parse(at.admin_set_mode(mode="bogus"))
    assert out["ok"] is False
    out = _parse(at.admin_set_mode(mode="pro"))
    assert out["needs_confirmation"] is True


# ---------------------------------------------------------------------------
# Self-test wraps run_self_test
# ---------------------------------------------------------------------------
def test_run_self_test_returns_structured_report():
    out = _parse(at.admin_run_self_test())
    assert "ok" in out
    assert "report" in out or "summary" in out


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
def test_register_admin_tools_idempotent():
    class R:
        def __init__(self): self.t = {}

        def register(self, n, f): self.t[n] = f

        def get(self, n): return self.t.get(n)

    r = R()
    first = at.register_admin_tools(r)
    assert len(first) == len(at.ADMIN_TOOLS)
    second = at.register_admin_tools(r)
    assert second == []  # nothing new registered
    assert all(name in r.t for name in at.ADMIN_TOOLS)
