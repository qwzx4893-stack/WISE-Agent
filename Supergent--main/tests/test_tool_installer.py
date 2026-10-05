"""Tests for the Tool Installer (auto-install missing tool deps)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

import pytest


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _make_packs(tmp_path: Path) -> Path:
    packs = tmp_path / "tools" / "packs"
    packs.mkdir(parents=True)
    (packs / "demo.json").write_text(json.dumps([
        # CLI tool whose binary exists everywhere (`python3`).
        {"name": "py_runner",
         "version": "1",
         "category": "Execution",
         "implementation_type": "cli",
         "cli_command": "python3 -V",
         "dependencies": ["python3"]},
        # CLI tool that needs an obviously absent binary.
        {"name": "missing_cli",
         "version": "1",
         "category": "Execution",
         "implementation_type": "cli",
         "cli_command": "definitely-not-on-path-xyzzy {arg}",
         "dependencies": []},
        # Python tool that needs a non-installed package.
        {"name": "needs_pkg",
         "version": "1",
         "category": "Memory",
         "implementation_type": "python",
         "dependencies": ["definitely-not-a-real-pkg-xyzzy"]},
        # Tool already shipped by the kernel — should always be available.
        {"name": "execute_command",
         "version": "1",
         "category": "Execution",
         "implementation_type": "python"},
    ]), encoding="utf-8")
    return packs


@pytest.fixture
def installer(tmp_path):
    from core.tool_installer import ToolInstaller
    packs = _make_packs(tmp_path)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    return ToolInstaller(packs_dir=packs, rootfs=rootfs,
                          cache_ttl_s=0.0)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def test_scan_classifies_tools_correctly(installer):
    scanned = installer.scan()
    assert {"py_runner", "missing_cli", "needs_pkg",
             "execute_command"}.issubset(scanned)
    # python3 is on every Linux dev box.
    assert scanned["py_runner"].found is True
    # bogus binary missing.
    assert scanned["missing_cli"].found is False
    assert any(m["type"] == "binary"
                and "definitely-not-on-path-xyzzy" in m["name"]
                for m in scanned["missing_cli"].missing)
    # bogus python pkg missing.
    assert scanned["needs_pkg"].found is False
    assert any(m["type"] == "python" for m in scanned["needs_pkg"].missing)
    # kernel builtin always reported as found.
    assert scanned["execute_command"].found is True
    assert scanned["execute_command"].note == "kernel builtin"


def test_missing_report_summary(installer):
    rep = installer.missing_report()
    names = {m["name"] for m in rep["missing"]}
    assert {"missing_cli", "needs_pkg"} <= names
    assert "py_runner" not in names
    assert rep["total"] >= 4
    assert rep["available"] >= 2
    assert rep["missing_count"] >= 2


def test_install_hint_includes_pip_install(installer):
    st = installer.scan()["needs_pkg"]
    assert "pip_install" in st.install_hint
    assert "definitely-not-a-real-pkg-xyzzy" in st.install_hint


def test_extract_binary_from_command():
    from core.tool_installer import ToolInstaller
    extract = ToolInstaller._extract_binary_from_command
    assert extract("npx create-next-app@latest --example {type}") == "npx"
    assert extract("python3 -c 'import django; print(1)'") == "python3"
    assert extract("kubectl get pods -n {ns}") == "kubectl"
    assert extract("FOO=bar npx run") == "npx"
    assert extract("") is None


def test_classify_dependency_paths():
    from core.tool_installer import ToolInstaller
    cls = ToolInstaller._classify_dependency
    assert cls("django")["type"] == "python"
    assert cls("@sveltejs/kit")["type"] == "npm"
    assert cls("nodejs")["type"] == "apt"
    assert cls("python:numpy") == {"type": "python", "name": "numpy"}
    assert cls("apt:curl") == {"type": "apt", "name": "curl"}


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------
def test_install_unknown_tool_is_skipped(installer):
    out = installer.install_missing(["does_not_exist"])
    assert any(s["tool"] == "does_not_exist" for s in out["skipped"])
    assert out["installed"] == []


def test_install_routes_through_self_install(monkeypatch, installer):
    """The installer must NEVER duplicate self_install logic — it must
    call pip_install / apt_install."""
    calls = []
    def fake_pip(pkg, *extra):
        calls.append(("pip", pkg, extra))
        return f"✅ تم تثبيت {pkg}"
    def fake_apt(pkg):
        calls.append(("apt", pkg))
        return f"✅ تم تثبيت {pkg}"
    from core import self_install
    monkeypatch.setattr(self_install, "pip_install", fake_pip)
    monkeypatch.setattr(self_install, "apt_install", fake_apt)
    out = installer.install_missing(["needs_pkg"])
    assert out["installed"], out
    assert calls and calls[0][0] == "pip"
    assert calls[0][1] == "definitely-not-a-real-pkg-xyzzy"


def test_install_rate_limited_after_ten_calls(monkeypatch, tmp_path):
    """Eleventh package within one minute is rejected with retry-after."""
    from core import self_install
    from core.tool_installer import ToolInstaller
    packs = tmp_path / "tools" / "packs"
    packs.mkdir(parents=True)
    # Build 12 tools each needing a distinct python package.
    items = []
    for i in range(12):
        items.append({
            "name": f"t{i}",
            "version": "1",
            "category": "X",
            "implementation_type": "python",
            "dependencies": [f"missing-pkg-{i}"],
        })
    (packs / "many.json").write_text(json.dumps(items), encoding="utf-8")

    rootfs = tmp_path / "rootfs"; rootfs.mkdir()
    inst = ToolInstaller(packs_dir=packs, rootfs=rootfs,
                          cache_ttl_s=0.0, rate_max_per_min=10)

    monkeypatch.setattr(self_install, "pip_install",
                         lambda pkg, *e: f"✅ تم تثبيت {pkg}")
    out = inst.install_missing()
    installed = len(out["installed"])
    rate_failed = sum(1 for f in out["failed"]
                      if "rate limited" in f.get("reason", ""))
    assert installed == 10
    assert rate_failed == 2


def test_tracer_emits_install_span(monkeypatch, installer):
    from core import self_install
    from core.observability import Tracer

    monkeypatch.setattr(self_install, "pip_install",
                         lambda pkg, *e: f"✅ تم تثبيت {pkg}")
    Tracer._buffer.clear()  # type: ignore[attr-defined]
    installer.install_missing(["needs_pkg"])
    starts = Tracer.events(kind="tool.install.start")
    assert any(e.get("package") == "definitely-not-a-real-pkg-xyzzy"
                for e in starts), f"got {starts}"


# --------------------------------------------------------------------------
# Awareness + registry guards
# --------------------------------------------------------------------------
def test_annotate_awareness_marks_missing_tools(installer):
    """`annotate_awareness` flips `__install_status__` on every manifest
    so the prompt knows which tools are unusable."""
    from core.tool_installer import annotate_awareness

    class FakeAwareness:
        tools = {
            "missing_cli": {"name": "missing_cli", "category": "X"},
            "needs_pkg":  {"name": "needs_pkg", "category": "X"},
            "py_runner":  {"name": "py_runner", "category": "X"},
        }

    aw = FakeAwareness()
    n = annotate_awareness(aw, installer)
    assert n >= 2
    assert aw.tools["missing_cli"]["__install_status__"]["found"] is False
    assert aw.tools["needs_pkg"]["__install_status__"]["found"] is False
    assert aw.tools["py_runner"]["__install_status__"]["found"] is True


def test_registry_guard_returns_helpful_message(installer):
    """A missing tool wrapped by the install-guard must return the
    'not installed' message instead of executing."""
    from core.tool_installer import wrap_registry_with_install_check

    class FakeRegistry:
        def __init__(self):
            self.tools = {}
        def register(self, name, fn):
            self.tools[name] = fn
        def get(self, name):
            return self.tools.get(name)

    reg = FakeRegistry()
    # Original missing-tool wrapper that would crash if called.
    def boom(**kw):
        raise RuntimeError("should never execute")
    reg.register("missing_cli", boom)
    reg.register("needs_pkg", boom)
    reg.register("py_runner", lambda **kw: "ran")
    wrapped = wrap_registry_with_install_check(reg, installer)
    assert wrapped >= 2
    msg = reg.get("missing_cli")(arg="x")
    assert "غير مثبَّتة" in msg or "not installed" in msg.lower() \
            or "POST /admin/tools/install" in msg
    # Available tool stays untouched.
    assert reg.get("py_runner")() == "ran"


# --------------------------------------------------------------------------
# API endpoints
# --------------------------------------------------------------------------
def test_admin_tools_missing_endpoint(monkeypatch, tmp_path):
    packs = _make_packs(tmp_path)
    rootfs = tmp_path / "rootfs"; rootfs.mkdir()

    from core.tool_installer import ToolInstaller
    import core.tool_installer as ti
    monkeypatch.setattr(ti, "_default_installer",
                         ToolInstaller(packs_dir=packs, rootfs=rootfs,
                                        cache_ttl_s=0.0))

    from fastapi.testclient import TestClient
    from api.server import app
    c = TestClient(app)
    r = c.get("/admin/tools/missing")
    assert r.status_code == 200
    body = r.json()
    names = {m["name"] for m in body["missing"]}
    assert "needs_pkg" in names
    assert "missing_cli" in names


def test_curated_map_skill_entries_count_as_available(tmp_path):
    """Tools tagged ``skill`` / ``skip`` in the curated map are
    considered fulfilled by Agent OS layers and should NOT show as
    missing."""
    from core.tool_installer import ToolInstaller

    packs = tmp_path / "tools" / "packs"
    packs.mkdir(parents=True)
    # Manifest that would otherwise look 'missing' (bogus dependency).
    (packs / "p.json").write_text(json.dumps([{
        "name": "agent-orchestrator", "version": "1",
        "category": "Orchestration",
        "implementation_type": "python",
        "dependencies": ["definitely-not-real-pkg"],
    }]), encoding="utf-8")
    # Curated map says it's a skill.
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps({
        "agent-orchestrator": {"type": "skill", "note": "fulfilled by AgentsTeam"}
    }), encoding="utf-8")

    rootfs = tmp_path / "rootfs"; rootfs.mkdir()
    inst = ToolInstaller(packs_dir=packs, rootfs=rootfs,
                          cache_ttl_s=0.0, curated_map_path=curated)
    st = inst.scan()["agent-orchestrator"]
    assert st.found is True
    assert "skill" in st.note
    assert st.missing == []


def test_known_missing_writer(tmp_path):
    from core.tool_installer import ToolInstaller

    packs = tmp_path / "tools" / "packs"; packs.mkdir(parents=True)
    # ``kubectl`` happens to be pre-installed on GitHub Actions'
    # ``ubuntu-latest`` image, which would make this test pass in a
    # developer laptop and fail in CI. Use a binary name that is
    # guaranteed absent from every realistic PATH so we're testing the
    # installer's bookkeeping rather than the runner environment.
    missing_cli = "agentos-nonexistent-devops-cli"
    # Add a tool whose curated entry marks it as a 'skill' (no real
    # install channel) so it lands in known_missing, plus the python
    # one above which is auto-installable.
    (packs / "p.json").write_text(json.dumps([
        {"name": missing_cli, "version": "1", "category": "DevOps",
         "implementation_type": "cli",
         "cli_command": f"{missing_cli} list",
         "dependencies": []},
        {"name": "needs_pkg", "version": "1", "category": "X",
         "implementation_type": "python",
         "dependencies": ["definitely-not-real-pkg"]},
        {"name": "fictional-persona", "version": "1", "category": "X",
         "implementation_type": "cli",
         "cli_command": "fictional-persona run", "dependencies": []},
    ]), encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps({
        # github_release / binary_zip are now auto-installable so we use
        # ``skill`` to place a tool in known_missing.
        "fictional-persona": {"type": "skill",
                                "note": "persona only"},
    }), encoding="utf-8")
    rootfs = tmp_path / "rootfs"; rootfs.mkdir()
    inst = ToolInstaller(packs_dir=packs, rootfs=rootfs,
                          cache_ttl_s=0.0, curated_map_path=curated)
    out = tmp_path / "known_missing.json"
    payload = inst.write_known_missing(out)
    names = {row["name"] for row in payload["tools"]}
    # Skill entries count as 'found' so they're NOT in known_missing
    # either. needs_pkg is pip-auto-installable. kubectl has no curated
    # entry but its binary requirement is also non-auto. → kubectl lands
    # in known_missing.
    assert missing_cli in names
    assert "needs_pkg" not in names
    assert "fictional-persona" not in names
    assert out.exists()
    written = json.loads(out.read_text())
    assert written["count"] == payload["count"]


def test_rootfs_info_shape(tmp_path):
    from core.tool_installer import ToolInstaller

    rootfs = tmp_path / "rootfs"; rootfs.mkdir()
    (rootfs / "etc").mkdir()
    (rootfs / "etc" / "debian_version").write_text("12.5\n", encoding="utf-8")
    (rootfs / ".agent-os-stamp").write_text(
        "release=bookworm\narch=amd64\nbuilt_at=2026-04-27T15:00:00Z\nmethod=debootstrap\n",
        encoding="utf-8")
    inst = ToolInstaller(packs_dir=tmp_path, rootfs=rootfs,
                          cache_ttl_s=0.0)
    info = inst.rootfs_info()
    assert info["exists"] is True
    assert info["debian_version"].startswith("12")
    assert info["stamp"]["release"] == "bookworm"
    assert info["stamp"]["method"] == "debootstrap"


def test_interactive_install_prompt_unattended(monkeypatch, tmp_path):
    """AGENT_AUTO_INSTALL=0 keeps installs skipped without blocking."""
    from core import self_install
    from core.tool_installer import (ToolInstaller,
                                       interactive_install_prompt)
    packs = _make_packs(tmp_path)
    rootfs = tmp_path / "rootfs"; rootfs.mkdir()
    inst = ToolInstaller(packs_dir=packs, rootfs=rootfs,
                          cache_ttl_s=0.0)

    monkeypatch.setenv("AGENT_AUTO_INSTALL", "0")
    out = interactive_install_prompt(inst)
    assert out.startswith("skipped")

    calls = []
    monkeypatch.setattr(self_install, "pip_install",
                         lambda pkg, *e: (calls.append(pkg)
                                            or f"✅ تم تثبيت {pkg}"))
    monkeypatch.setenv("AGENT_AUTO_INSTALL", "1")
    out = interactive_install_prompt(inst)
    assert "installed=" in out
    # When pip is forced, the curated/auto path should at least try.
    assert calls or "installed=0" in out


def test_github_release_install_uses_fast_path(monkeypatch, tmp_path):
    """Install path uses the stable ``/releases/latest/download`` URL
    when the asset name has no wildcards (no API quota required)."""
    from core import tool_installer as ti

    packs = tmp_path / "tools" / "packs"; packs.mkdir(parents=True)
    (packs / "p.json").write_text(json.dumps([
        {"name": "fakebin", "version": "1", "category": "X",
         "implementation_type": "cli", "cli_command": "fakebin --x",
         "dependencies": []},
    ]), encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps({
        "fakebin": {"type": "github_release",
                     "repo": "acme/fakebin",
                     "asset": "fakebin-linux.bin",
                     "binary": "fakebin"}
    }), encoding="utf-8")
    rootfs = tmp_path / "rootfs"
    (rootfs / "usr" / "local" / "bin").mkdir(parents=True)

    captured: Dict[str, Any] = {}
    def fake_download(url, dest, timeout=120.0):
        captured["url"] = url
        Path(dest).write_bytes(b"#!/bin/sh\necho fakebin v1\n")
    api_calls: Dict[str, Any] = {"count": 0}
    def fake_api(url, timeout=15.0):
        api_calls["count"] += 1
        return {"assets": []}

    monkeypatch.setenv("AGENT_BINARY_INSTALL", "1")
    monkeypatch.setattr(ti.ToolInstaller, "_http_download",
                         staticmethod(fake_download))
    monkeypatch.setattr(ti.ToolInstaller, "_http_get_json",
                         staticmethod(fake_api))

    inst = ti.ToolInstaller(packs_dir=packs, rootfs=rootfs,
                              cache_ttl_s=0.0,
                              curated_map_path=curated)
    out = inst.install_missing(["fakebin"])
    assert out["installed"], out
    assert "/releases/latest/download/fakebin-linux.bin" in captured["url"]
    assert api_calls["count"] == 0  # fast path skipped the API
    placed = rootfs / "usr" / "local" / "bin" / "fakebin"
    assert placed.exists()
    assert os.access(placed, os.X_OK)


def test_github_release_uses_api_when_pattern_has_wildcard(
        monkeypatch, tmp_path):
    from core import tool_installer as ti

    packs = tmp_path / "tools" / "packs"; packs.mkdir(parents=True)
    (packs / "p.json").write_text(json.dumps([
        {"name": "verbin", "version": "1", "category": "X",
         "implementation_type": "cli", "cli_command": "verbin",
         "dependencies": []},
    ]), encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps({
        "verbin": {"type": "github_release",
                    "repo": "acme/verbin",
                    "asset": "verbin_*_linux_amd64.tar.gz",
                    "binary": "verbin"}
    }), encoding="utf-8")
    rootfs = tmp_path / "rootfs"
    (rootfs / "usr" / "local" / "bin").mkdir(parents=True)

    api_calls: Dict[str, Any] = {"count": 0}
    def fake_api(url, timeout=15.0):
        api_calls["count"] += 1
        return {"assets": [{
            "name": "verbin_2.5.0_linux_amd64.tar.gz",
            "browser_download_url": "https://example/v2/verbin.tgz"}]}
    import io, tarfile as tf
    def fake_download(url, dest, timeout=120.0):
        # Build a tiny tarball containing the binary.
        buf = io.BytesIO()
        with tf.open(fileobj=buf, mode="w:gz") as t:
            data = b"#!/bin/sh\necho verbin\n"
            info = tf.TarInfo(name="verbin")
            info.size = len(data)
            info.mode = 0o755
            t.addfile(info, io.BytesIO(data))
        Path(dest).write_bytes(buf.getvalue())

    monkeypatch.setenv("AGENT_BINARY_INSTALL", "1")
    monkeypatch.setattr(ti.ToolInstaller, "_http_get_json",
                         staticmethod(fake_api))
    monkeypatch.setattr(ti.ToolInstaller, "_http_download",
                         staticmethod(fake_download))

    inst = ti.ToolInstaller(packs_dir=packs, rootfs=rootfs,
                              cache_ttl_s=0.0,
                              curated_map_path=curated)
    out = inst.install_missing(["verbin"])
    assert out["installed"], out
    assert api_calls["count"] == 1
    assert (rootfs / "usr" / "local" / "bin" / "verbin").exists()


def test_github_release_blocked_without_env(monkeypatch, tmp_path):
    from core import tool_installer as ti

    packs = tmp_path / "tools" / "packs"; packs.mkdir(parents=True)
    (packs / "p.json").write_text(json.dumps([
        {"name": "noenvbin", "version": "1", "category": "X",
         "implementation_type": "cli", "cli_command": "noenvbin",
         "dependencies": []},
    ]), encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps({
        "noenvbin": {"type": "github_release", "repo": "acme/x",
                       "asset": "x.bin", "binary": "noenvbin"}}),
                        encoding="utf-8")
    rootfs = tmp_path / "rootfs"; rootfs.mkdir()
    monkeypatch.delenv("AGENT_BINARY_INSTALL", raising=False)
    inst = ti.ToolInstaller(packs_dir=packs, rootfs=rootfs,
                              cache_ttl_s=0.0,
                              curated_map_path=curated)
    out = inst.install_missing(["noenvbin"])
    # Blocked installs are reported as 'failed' so the operator sees
    # the env-toggle hint, not silently skipped.
    assert out["failed"], out
    assert "AGENT_BINARY_INSTALL" in out["failed"][0]["result"]


def test_prerequisites_run_before_primary(monkeypatch, tmp_path):
    from core import tool_installer as ti
    from core import self_install

    packs = tmp_path / "tools" / "packs"; packs.mkdir(parents=True)
    (packs / "p.json").write_text(json.dumps([
        {"name": "slither", "version": "1", "category": "Security",
         "implementation_type": "python",
         "dependencies": ["slither-analyzer"]},
    ]), encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps({
        "slither": {"type": "pip", "package": "slither-analyzer",
                      "prerequisites": [
                          {"type": "pip", "package": "solc-select"},
                          {"type": "pip", "package": "crytic-compile"}]}
    }), encoding="utf-8")
    rootfs = tmp_path / "rootfs"; rootfs.mkdir()
    order: list = []
    def fake_pip(pkg, *extra):
        order.append(pkg)
        return f"✅ {pkg}"
    monkeypatch.setattr(self_install, "pip_install", fake_pip)

    inst = ti.ToolInstaller(packs_dir=packs, rootfs=rootfs,
                              cache_ttl_s=0.0,
                              curated_map_path=curated)
    out = inst.install_missing(["slither"])
    assert out["installed"], out
    # Prerequisites must precede the primary package.
    assert order == ["solc-select", "crytic-compile", "slither-analyzer"]


def test_admin_tools_install_endpoint(monkeypatch, tmp_path):
    packs = _make_packs(tmp_path)
    rootfs = tmp_path / "rootfs"; rootfs.mkdir()

    from core import self_install
    from core.tool_installer import ToolInstaller
    import core.tool_installer as ti
    monkeypatch.setattr(self_install, "pip_install",
                         lambda pkg, *e: f"✅ تم تثبيت {pkg}")
    monkeypatch.setattr(ti, "_default_installer",
                         ToolInstaller(packs_dir=packs, rootfs=rootfs,
                                        cache_ttl_s=0.0))

    from fastapi.testclient import TestClient
    from api.server import app
    c = TestClient(app)
    r = c.post("/admin/tools/install", json={"tools": ["needs_pkg"]})
    assert r.status_code == 200
    body = r.json()
    assert any(i["tool"] == "needs_pkg" for i in body["installed"])
