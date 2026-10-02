"""Phase 7 — Part 1 (RAG cleanup + GitHub PAT) & Part 2 (sandbox auto-recovery)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# secrets_store
# ---------------------------------------------------------------------------
def _fresh_store(tmp_path, monkeypatch):
    """Return a fresh SecretStore pointed at ``tmp_path``."""
    import core.paths as paths
    import core.secrets_store as sst
    monkeypatch.setattr(paths, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(sst, "MEMORY_DIR", tmp_path)
    sst.SecretStore._instance = None
    return sst.SecretStore()


def test_secrets_store_round_trip(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    store.set("github_token", "ghp_test123abc")
    assert store.get("github_token") == "ghp_test123abc"
    listed = store.list()
    assert "github_token" in listed
    # Masked by default.
    assert "ghp_test123abc" not in listed["github_token"]
    assert store.list(reveal=True)["github_token"] == "ghp_test123abc"


def test_secrets_store_delete(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    store.set("foo", "bar")
    assert store.get("foo") == "bar"
    assert store.delete("foo") is True
    assert store.get("foo") is None
    assert store.delete("foo") is False  # already gone


def test_secrets_store_env_overrides_persisted(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    store.set("github_token", "persisted")
    monkeypatch.setenv("GITHUB_TOKEN", "from_env")
    assert store.get("github_token") == "from_env"


def test_secrets_store_persists_across_instances(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    store.set("k", "v")
    # Reset the singleton to simulate a second process.
    import core.secrets_store as sst
    sst.SecretStore._instance = None
    store2 = sst.SecretStore()
    assert store2.get("k") == "v"


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------
def _client(monkeypatch):
    from fastapi.testclient import TestClient
    from api import server as srv
    monkeypatch.setattr(srv, "_require_auth", lambda *a, **k: None)
    return TestClient(srv.app)


def test_api_settings_secrets_endpoints(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.put("/admin/settings/secrets/github_token",
                       json={"value": "ghp_abc"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    resp = client.get("/admin/settings/secrets")
    assert resp.status_code == 200
    secrets = resp.json()["secrets"]
    assert "github_token" in secrets
    assert "ghp_abc" not in secrets["github_token"]  # masked

    resp = client.delete("/admin/settings/secrets/github_token")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.get("/admin/settings/secrets")
    assert "github_token" not in resp.json()["secrets"]


def test_api_settings_secrets_set_rejects_empty(monkeypatch):
    client = _client(monkeypatch)
    resp = client.put("/admin/settings/secrets/x", json={"value": ""})
    assert resp.status_code == 400


def test_github_repos_source_uses_secret_store(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch).set("github_token", "ghp_xyz")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    captured: dict = {}

    def _fake_http_get(url, *, params=None, headers=None, **kw):
        captured["headers"] = headers
        return 200, {"items": []}, ""

    from core.rag.sources import general
    monkeypatch.setattr(general, "http_get", _fake_http_get)

    general.GitHubReposSource().search("test")
    assert captured["headers"]["Authorization"] == "Bearer ghp_xyz"


# ---------------------------------------------------------------------------
# Sandbox auto-recovery
# ---------------------------------------------------------------------------
def test_rootfs_build_succeeds_first_attempt(tmp_path, monkeypatch):
    """Happy path: returncode 0 on first attempt → installed."""
    from core import onboarding as ob

    fake_script = tmp_path / "build.sh"
    fake_script.write_text("#!/bin/bash\nexit 0\n")
    monkeypatch.setattr(ob, "AGENT_OS_ROOT", tmp_path)
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    (sandbox_dir / "build_rootfs.sh").write_text("#!/bin/bash\nexit 0\n")

    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(ob.subprocess, "run", _fake_run)

    ob.OnboardingManager._instance = None
    mgr = ob.OnboardingManager()
    comp = ob.ComponentState(name="rootfs", kind="rootfs")
    mgr._build_rootfs(comp)
    assert comp.status == "installed"
    assert comp.attempts == 1
    assert len(calls) == 1
    assert comp.user_message == ""
    assert comp.manual_steps == []


def test_rootfs_build_retries_then_succeeds(tmp_path, monkeypatch):
    """Two failures, third attempt succeeds → final state installed."""
    from core import onboarding as ob

    monkeypatch.setattr(ob, "AGENT_OS_ROOT", tmp_path)
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    (sandbox_dir / "build_rootfs.sh").write_text("#!/bin/bash\nexit 0\n")

    results = iter([
        MagicMock(returncode=1, stdout="", stderr="Connection refused"),
        MagicMock(returncode=1, stdout="", stderr="timed out"),
        MagicMock(returncode=0, stdout="ok", stderr=""),
    ])

    def _fake_run(cmd, **kw):
        return next(results)

    monkeypatch.setattr(ob.subprocess, "run", _fake_run)
    ob.OnboardingManager._instance = None
    mgr = ob.OnboardingManager()
    comp = ob.ComponentState(name="rootfs", kind="rootfs")
    mgr._build_rootfs(comp)
    assert comp.status == "installed"
    assert comp.attempts == 3


def test_rootfs_build_all_attempts_fail_surfaces_manual_steps(
        tmp_path, monkeypatch):
    """All three attempts fail → status=failed + user_message + manual_steps."""
    from core import onboarding as ob

    monkeypatch.setattr(ob, "AGENT_OS_ROOT", tmp_path)
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    (sandbox_dir / "build_rootfs.sh").write_text("#!/bin/bash\nexit 1\n")

    def _fake_run(cmd, **kw):
        return MagicMock(returncode=1, stdout="",
                          stderr="Connection refused: PyPI unreachable")

    monkeypatch.setattr(ob.subprocess, "run", _fake_run)
    ob.OnboardingManager._instance = None
    mgr = ob.OnboardingManager()
    comp = ob.ComponentState(name="rootfs", kind="rootfs")
    mgr._build_rootfs(comp)
    assert comp.status == "failed"
    assert comp.attempts == 3
    assert comp.repair_category == "network"
    assert "internet" in comp.user_message.lower()
    assert len(comp.manual_steps) >= 2
    assert any("Retry" in s for s in comp.manual_steps)


def test_rootfs_build_missing_script(tmp_path, monkeypatch):
    """Missing script → immediate failure with manual steps."""
    from core import onboarding as ob

    monkeypatch.setattr(ob, "AGENT_OS_ROOT", tmp_path)
    ob.OnboardingManager._instance = None
    mgr = ob.OnboardingManager()
    comp = ob.ComponentState(name="rootfs", kind="rootfs")
    mgr._build_rootfs(comp)
    assert comp.status == "failed"
    assert "missing build script" in comp.detail
    assert comp.manual_steps  # always populated


def test_rootfs_component_state_has_recovery_fields():
    """The new fields are part of the API surface."""
    from core.onboarding import ComponentState
    c = ComponentState(name="rootfs", kind="rootfs")
    d = c.to_dict()
    assert "attempts" in d
    assert "repair_category" in d
    assert "user_message" in d
    assert "manual_steps" in d
