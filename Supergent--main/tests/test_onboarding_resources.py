"""Tests for Phase 5: mandatory onboarding + user-controlled resources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


# --- Resource Settings -------------------------------------------------------
def _reset_singletons(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                      *, mode: str = "pro") -> None:
    import core.platform_manager as pm
    import core.resource_settings as rs
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.setenv("AGENT_OS_MODE", mode)
    monkeypatch.setattr(rs, "SETTINGS_FILE", tmp_path / "resources.json")
    rs.ResourceSettingsStore._instance = None


def test_resources_lite_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    from core.resource_settings import LITE_DEFAULTS, get_store
    s = get_store().load(mode="lite")
    assert s.cpu_seconds == LITE_DEFAULTS["cpu_seconds"]
    assert s.memory_mb == LITE_DEFAULTS["memory_mb"]
    assert s.parallel_tools == LITE_DEFAULTS["parallel_tools"]
    assert s.enable_docker is False
    assert s.enable_wine is False


def test_resources_pro_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="pro")
    from core.resource_settings import get_store, PRO_DEFAULTS
    s = get_store().load(mode="pro")
    assert s.cpu_seconds == PRO_DEFAULTS["cpu_seconds"]   # 0 = unlimited
    assert s.parallel_tools == PRO_DEFAULTS["parallel_tools"]
    assert s.enable_docker is True
    assert s.enable_gpu is True


def test_resources_save_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="pro")
    from core.resource_settings import ResourceSettings, get_store
    store = get_store()
    s = store.load(mode="pro")
    s.cpu_seconds = 300
    s.memory_mb = 8192
    s.enable_docker = False
    store.save(s)

    s2 = get_store().load(mode="pro")
    assert s2.cpu_seconds == 300
    assert s2.memory_mb == 8192
    assert s2.enable_docker is False


def test_resources_lite_forces_pro_only_off(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path):
    """Even if disk somehow has docker=true, Lite Mode must report it as false."""
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    from core.resource_settings import SETTINGS_FILE, get_store
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps({
        "cpu_seconds": 60, "memory_mb": 2048,
        "enable_docker": True, "enable_wine": True,
        "enable_heavy_security": True, "enable_gpu": True,
    }))
    s = get_store().load(mode="lite")
    assert s.enable_docker is False
    assert s.enable_wine is False
    assert s.enable_heavy_security is False
    assert s.enable_gpu is False


def test_resources_diff_for_mode_switch(monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="pro")
    from core.resource_settings import (
        ResourceSettings, diff_for_mode_switch,
    )
    pro = ResourceSettings(
        cpu_seconds=0, memory_mb=0, max_processes=1024,
        parallel_tools=8, enable_docker=True, enable_wine=True,
        enable_heavy_security=True, enable_gpu=True,
        network_timeout_s=120, disk_mb=0, mode_at_save="pro",
    )
    new, warnings = diff_for_mode_switch(pro, target_mode="lite")
    assert new.enable_docker is False
    assert new.enable_wine is False
    assert new.cpu_seconds == 60
    assert new.memory_mb == 2048
    assert new.parallel_tools == 1
    assert any("docker" in w for w in warnings)
    assert any("wine" in w for w in warnings)


def test_platform_manager_uses_user_settings(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path):
    """Custom CPU/memory in resources.json should flow into PlatformManager.limits."""
    _reset_singletons(monkeypatch, tmp_path, mode="pro")
    from core.resource_settings import SETTINGS_FILE
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps({
        "cpu_seconds": 120, "memory_mb": 6144,
        "max_processes": 512, "parallel_tools": 4,
        "mode_at_save": "pro",
    }))
    from core.platform_manager import get_platform_manager
    limits = get_platform_manager().limits
    assert limits["cpu_seconds"] == 120
    assert limits["memory_bytes"] == 6144 * 1024 * 1024
    assert limits["max_processes"] == 512
    assert limits["parallel_tool_calls"] == 4


# --- API endpoints ----------------------------------------------------------
def _client(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient
    from api import server as srv
    monkeypatch.setattr(srv, "_require_auth", lambda *a, **k: None)
    return TestClient(srv.app), srv


def test_api_resources_endpoints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="pro")
    client, _ = _client(monkeypatch)

    resp = client.get("/admin/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "pro"
    assert "lite" in body["defaults"] and "pro" in body["defaults"]
    assert "enable_docker" in body["pro_only_keys"]

    resp = client.post("/admin/resources",
                       json={"cpu_seconds": 240, "memory_mb": 4096,
                              "enable_docker": False})
    assert resp.status_code == 200
    out = resp.json()
    assert out["ok"] is True
    assert out["settings"]["cpu_seconds"] == 240
    assert out["settings"]["enable_docker"] is False


def test_api_resources_lite_rejects_pro_only(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    client, _ = _client(monkeypatch)
    resp = client.post("/admin/resources",
                       json={"enable_docker": True, "enable_wine": True})
    assert resp.status_code == 200
    out = resp.json()
    assert out["settings"]["enable_docker"] is False
    assert out["settings"]["enable_wine"] is False
    assert any("docker" in w for w in out["warnings"])
    assert any("wine" in w for w in out["warnings"])


def test_api_resources_reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    client, _ = _client(monkeypatch)
    client.post("/admin/resources", json={"cpu_seconds": 999})
    resp = client.post("/admin/resources", json={"reset": True})
    assert resp.status_code == 200
    assert resp.json()["settings"]["cpu_seconds"] == 60


# --- Onboarding -------------------------------------------------------------
def test_onboarding_plan_filters_pro_only_in_lite(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    from core import onboarding as ob
    ob.OnboardingManager._instance = None

    class _Status:
        def __init__(self, name: str, found: bool):
            self.name = name
            self.found = found

    fake_scan = {
        "ripgrep": _Status("ripgrep", False),
        "docker": _Status("docker", False),
        "wine": _Status("wine", False),
        "trivy": _Status("trivy", False),
        "found_one": _Status("found_one", True),
    }

    class _FakeInstaller:
        def scan(self, force: bool = False):
            return fake_scan
    import core.tool_installer as ti
    monkeypatch.setattr(ti, "get_installer", lambda: _FakeInstaller())

    mgr = ob.get_onboarding_manager()
    plan = mgr._build_plan(fake_scan, mode="lite")
    names = [c.name for c in plan if c.kind == "tool"]
    assert "ripgrep" in names
    assert "trivy" in names
    assert "docker" not in names      # PRO_ONLY filtered
    assert "wine" not in names         # PRO_ONLY filtered
    assert "found_one" not in names    # already installed


def test_onboarding_plan_includes_all_in_pro(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="pro")
    from core import onboarding as ob
    ob.OnboardingManager._instance = None

    class _Status:
        def __init__(self, name: str, found: bool):
            self.name = name
            self.found = found

    fake_scan = {
        "docker": _Status("docker", False),
        "wine": _Status("wine", False),
        "ripgrep": _Status("ripgrep", False),
    }
    plan = ob.get_onboarding_manager()._build_plan(fake_scan, mode="pro")
    names = {c.name for c in plan if c.kind == "tool"}
    assert {"docker", "wine", "ripgrep"} <= names


def test_onboarding_install_single_skips_pro_only_in_lite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    from core import onboarding as ob
    ob.OnboardingManager._instance = None

    res = ob.get_onboarding_manager().install_single("docker")
    assert res["status"] == "skipped"
    assert "Pro" in res["detail"] or "lite" in res["detail"].lower() or \
           "pro-only" in res["detail"].lower()


def test_onboarding_pipeline_runs_with_mocked_installer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """End-to-end pipeline test — sandbox phase mocked, tools mocked."""
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    from core import onboarding as ob
    ob.OnboardingManager._instance = None

    # Fake rootfs (already exists, so phase 1 is a no-op).
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    (rootfs / "marker").write_text("ok")
    monkeypatch.setattr(ob, "SANDBOX_ROOT", rootfs)

    class _Status:
        def __init__(self, name: str, found: bool):
            self.name = name
            self.found = found

    scanned = {
        "ripgrep": _Status("ripgrep", False),
        "docker": _Status("docker", False),  # filtered out in lite
    }

    install_calls: List[List[str]] = []

    class _FakeInstaller:
        def scan(self, force: bool = False):
            return scanned
        def install_missing(self, names=None):
            install_calls.append(list(names or []))
            return {"installed": [{"tool": n} for n in (names or [])],
                    "failed": [], "skipped": []}

    import core.tool_installer as ti
    monkeypatch.setattr(ti, "get_installer", lambda: _FakeInstaller())

    mgr = ob.get_onboarding_manager()
    task = mgr.start()
    # Wait briefly for the daemon thread.
    import time as _t
    for _ in range(50):
        if task.is_done:
            break
        _t.sleep(0.05)
    assert task.is_done is True
    statuses = {c.name: c.status for c in task.components}
    assert statuses.get("ripgrep") == "installed"
    # docker was filtered before phase 2 even started.
    assert "docker" not in statuses
    # rootfs was already present → either in plan as installed, or absent.
    assert install_calls == [["ripgrep"]]


def test_onboarding_needs_onboarding_returns_shape(monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    from core import onboarding as ob
    ob.OnboardingManager._instance = None

    class _Status:
        def __init__(self, name: str, found: bool):
            self.name = name
            self.found = found

    scanned = {f"tool{i}": _Status(f"tool{i}", False) for i in range(5)}

    class _FakeInstaller:
        def scan(self, force: bool = False):
            return scanned
    import core.tool_installer as ti
    monkeypatch.setattr(ti, "get_installer", lambda: _FakeInstaller())

    info = ob.get_onboarding_manager().needs_onboarding()
    assert "needs_onboarding" in info
    assert info["mode"] == "lite"
    assert info["missing_count"] >= 5


# --- API for onboarding -----------------------------------------------------
def test_api_onboarding_endpoints_registered(monkeypatch: pytest.MonkeyPatch):
    from api.server import app
    paths = set(app.openapi()["paths"])
    assert "/admin/resources" in paths
    assert "/admin/onboarding/required" in paths
    assert "/admin/onboarding/install" in paths
    assert "/admin/onboarding/status/{task_id}" in paths
    assert "/admin/onboarding/install/single" in paths


def test_api_onboarding_required_endpoint(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="lite")
    from core import onboarding as ob
    ob.OnboardingManager._instance = None
    client, _ = _client(monkeypatch)
    resp = client.get("/admin/onboarding/required")
    assert resp.status_code == 200
    body = resp.json()
    assert "needs_onboarding" in body
    assert body["mode"] == "lite"


def test_api_onboarding_status_404_for_unknown(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path):
    _reset_singletons(monkeypatch, tmp_path, mode="pro")
    client, _ = _client(monkeypatch)
    resp = client.get("/admin/onboarding/status/does-not-exist")
    assert resp.status_code == 404
