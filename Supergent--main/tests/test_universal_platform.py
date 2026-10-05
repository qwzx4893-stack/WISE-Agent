"""Tests for Phase 4: universal cross-platform architecture."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# PlatformManager
# ---------------------------------------------------------------------------
def test_platform_manager_detects_capabilities(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_OS_MODE", "pro")
    # Force a fresh singleton.
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None

    mgr = pm.PlatformManager()
    info = mgr.info()
    assert info["mode"] == "pro"
    assert info["capabilities"]["cpu_cores"] >= 1
    assert info["capabilities"]["ram_bytes"] > 0
    assert info["capabilities"]["os"] in ("linux", "darwin", "windows")
    # Pro mode → no parallel cap of 1.
    assert info["limits"]["parallel_tool_calls"] >= 2


def test_platform_manager_lite_mode_caps_resources(monkeypatch: pytest.MonkeyPatch):
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.setenv("AGENT_OS_MODE", "lite")

    mgr = pm.PlatformManager()
    assert mgr.is_lite is True
    limits = mgr.limits
    assert limits["cpu_seconds"] == 60
    assert limits["memory_bytes"] == 2 * 1024 * 1024 * 1024
    assert limits["max_processes"] == 256
    assert limits["parallel_tool_calls"] == 1


def test_platform_manager_set_mode_persists(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch):
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.delenv("AGENT_OS_MODE", raising=False)
    monkeypatch.setattr(pm, "MODE_FILE", tmp_path / "mode.json")

    mgr = pm.PlatformManager()
    initial = mgr.mode
    other = "lite" if initial == "pro" else "pro"
    mgr.set_mode(other)
    saved = json.loads((tmp_path / "mode.json").read_text())
    assert saved["mode"] == other


def test_platform_manager_pro_only_tools_filtered(monkeypatch: pytest.MonkeyPatch):
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.setenv("AGENT_OS_MODE", "lite")

    mgr = pm.PlatformManager()
    tools = {
        "ripgrep": {"name": "ripgrep"},
        "docker": {"name": "docker"},
        "wine": {"name": "wine"},
        "trivy": {"name": "trivy"},
    }
    visible = mgr.filter_tools(tools)
    assert "ripgrep" in visible
    assert "trivy" in visible
    assert "docker" not in visible
    assert "wine" not in visible

    # Pro mode shows everything.
    mgr.set_mode("pro", persist=False)
    assert mgr.filter_tools(tools).keys() == tools.keys()


# ---------------------------------------------------------------------------
# IPython persistent session
# ---------------------------------------------------------------------------
def test_ipython_session_state_persists():
    from core.thinking.ipython_session import IPythonSession
    s = IPythonSession()
    try:
        r1 = s.run("x = 41\nprint('hello')")
        assert r1.exit_code == 0
        assert "hello" in r1.stdout

        r2 = s.run("print(x + 1)")
        assert r2.exit_code == 0
        assert "42" in r2.stdout

        r3 = s.run("raise ValueError('boom')")
        assert r3.exit_code == 1
        assert "ValueError" in r3.stderr or "ValueError" in r3.stdout
    finally:
        s.close()


# ---------------------------------------------------------------------------
# CodeAct strategy
# ---------------------------------------------------------------------------
def test_codeact_runs_python_then_finalises(tmp_path: Path):
    from core.thinking.codeact import CodeActStrategy

    turns = iter([
        "I'll compute 2+2 in Python.\n```python\nprint(2+2)\n```",
        "FINAL: 4",
    ])

    def fake_llm(messages, ctx):
        return next(turns)

    strat = CodeActStrategy(llm_callable=fake_llm,
                            max_steps=5, workdir=tmp_path)
    result = strat.run("Compute 2+2")
    assert result.final == "4"
    assert result.stopped_reason == "final_answer"
    assert len(result.steps) == 1
    assert "4" in result.steps[0].stdout


def test_codeact_handles_bash_block(tmp_path: Path):
    from core.thinking.codeact import CodeActStrategy

    turns = iter([
        "Will inspect the directory.\n```bash\necho universal\n```",
        "FINAL: done",
    ])

    def fake_llm(messages, ctx):
        return next(turns)

    strat = CodeActStrategy(llm_callable=fake_llm, max_steps=3, workdir=tmp_path)
    result = strat.run("show universal")
    assert result.final == "done"
    assert any("universal" in s.stdout for s in result.steps)


# ---------------------------------------------------------------------------
# Universal Executor
# ---------------------------------------------------------------------------
def test_universal_executor_runs_cli(monkeypatch: pytest.MonkeyPatch):
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.setenv("AGENT_OS_MODE", "pro")

    from core.universal_executor import UniversalExecutor
    exec_ = UniversalExecutor(rootfs=None)  # force host fallback
    manifest = {"name": "echo-test", "exec_strategy": "cli",
                "cli_command": "echo hello-from-uexec"}
    res = exec_.execute(manifest)
    assert res.ok
    assert "hello-from-uexec" in res.stdout
    assert res.strategy == "cli"


def test_universal_executor_lite_blocks_heavy_strategies(monkeypatch: pytest.MonkeyPatch):
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.setenv("AGENT_OS_MODE", "lite")

    from core.universal_executor import UniversalExecutor
    res = UniversalExecutor(rootfs=None).execute(
        {"name": "wine", "exec_strategy": "windows-gui",
         "windows_exe": "notepad.exe"},
    )
    assert res.ok is False
    assert "Pro Mode" in res.stderr


def test_universal_executor_unsupported_strategy_returns_error(monkeypatch: pytest.MonkeyPatch):
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.setenv("AGENT_OS_MODE", "pro")
    from core.universal_executor import UniversalExecutor
    res = UniversalExecutor().execute({"name": "?", "exec_strategy": "frob"})
    assert res.ok is False
    assert "frob" in res.stderr


# ---------------------------------------------------------------------------
# Dynamic Tool Acquisition
# ---------------------------------------------------------------------------
def test_dynamic_tool_fetcher_url(tmp_path: Path):
    from core.dynamic_tool_acquisition import ToolFetcher

    # Create a tiny static "tool" file served via file:// — bypasses network.
    fake = tmp_path / "tinytool.bin"
    fake.write_bytes(b"#!/bin/sh\necho ok\n")
    url = fake.as_uri()  # file:// URL handled by urllib.

    f = ToolFetcher(cache_dir=tmp_path / "cache")
    res = f.fetch(url, name="tinytool")
    assert res.ok
    assert res.bytes_downloaded == fake.stat().st_size
    assert Path(res.local_path).exists()


def test_dynamic_tool_analyzer_help_heuristic(tmp_path: Path):
    from core.dynamic_tool_acquisition import ToolAnalyzer

    if os.name == "nt":
        pytest.skip("the fixture is a POSIX bash script")

    # A binary that prints help to stdout.
    binary = tmp_path / "fakebin"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == --help || \"$1\" == -h ]]; then\n"
        "  echo 'fakebin scans network ports and reports findings.'\n"
        "  echo ''\n"
        "  echo 'Usage: fakebin --target HOST'\n"
        "  echo 'Examples: fakebin --target 192.0.2.1'\n"
        "  exit 0\n"
        "fi\n"
    )
    binary.chmod(0o755)
    analyser = ToolAnalyzer()
    res = analyser.analyze(name="fakebin", binary_path=str(binary))
    assert res.exec_strategy == "cli"
    assert "scans network" in res.description.lower() or "scan" in " ".join(res.capabilities).lower()
    assert any(c.lower().startswith("scan") for c in res.capabilities)


def test_dynamic_tool_manifest_generator(tmp_path: Path):
    from core.dynamic_tool_acquisition import (
        AnalysisResult, FetchResult, ManifestGenerator,
    )
    a = AnalysisResult(
        name="demo", description="A demo tool.",
        capabilities=["render scene", "encode video"],
        use_cases=["Render a 3D scene", "Encode a clip"],
        dependencies=["python3"],
        cli_command="demo",
        exec_strategy="cli", confidence=0.7,
    )
    fetch = FetchResult(True, "demo", "https://example.invalid/demo",
                        local_path=str(tmp_path / "demo.bin"),
                        archive_kind="binary", bytes_downloaded=12)
    m = ManifestGenerator().generate(a, source="https://example.invalid/demo",
                                     fetch=fetch, category="media")
    assert m["name"] == "demo"
    assert m["category"] == "media"
    assert m["exec_strategy"] == "cli"
    assert m["auto_generated"] is True
    assert m["confidence"] == 0.7
    out = tmp_path / "demo.json"
    ManifestGenerator.write(m, out)
    assert json.loads(out.read_text())["name"] == "demo"


def test_dynamic_tool_strategy_heuristic_for_exe_url(tmp_path: Path):
    from core.dynamic_tool_acquisition import _heuristic_strategy
    assert _heuristic_strategy(name="notepad", kind="exe", source="",
                               help_text="") == "windows-gui"
    assert _heuristic_strategy(name="x", kind="appimage", source="",
                               help_text="") == "linux-gui"
    assert _heuristic_strategy(name="x", kind="", source="",
                               help_text="DISPLAY env var required") == "linux-gui"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
def test_api_phase4_endpoints_registered():
    from api.server import app
    paths = set(app.openapi()["paths"])
    assert "/admin/platform" in paths
    assert "/admin/mode" in paths
    assert "/admin/tools/acquire" in paths
    assert "/admin/tools/execute" in paths


def test_admin_platform_endpoint(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.setenv("AGENT_OS_MODE", "pro")

    from api import server as srv
    monkeypatch.setattr(srv, "_require_auth", lambda *a, **k: None)
    client = TestClient(srv.app)
    resp = client.get("/admin/platform")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "pro"
    assert "capabilities" in body


def test_admin_mode_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from fastapi.testclient import TestClient
    import core.platform_manager as pm
    pm._DEFAULT = None
    pm.PlatformManager._instance = None
    monkeypatch.delenv("AGENT_OS_MODE", raising=False)
    monkeypatch.setattr(pm, "MODE_FILE", tmp_path / "mode.json")

    from api import server as srv
    monkeypatch.setattr(srv, "_require_auth", lambda *a, **k: None)
    client = TestClient(srv.app)
    resp = client.post("/admin/mode", json={"mode": "lite", "persist": True})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "lite"

    bad = client.post("/admin/mode", json={"mode": "ultra"})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# SWE-Bench workflow
# ---------------------------------------------------------------------------
def test_swe_bench_workflow_clone_failure_returns_error(tmp_path: Path):
    from core.workflows.swe_bench import SWEBenchTask, SWEBenchWorkflow
    wf = SWEBenchWorkflow(workdir=tmp_path)
    task = SWEBenchTask(
        repo_url="https://example.invalid/does/not/exist.git",
        issue="anything",
        timeout_s=5,
    )
    res = wf.run(task)
    assert res.ok is False
    assert "clone" in res.final_message.lower()


def test_swe_bench_workflow_runs_with_stub_llm(tmp_path: Path):
    """End-to-end happy path against a tiny self-built repo (no network)."""
    from core.workflows.swe_bench import SWEBenchTask, SWEBenchWorkflow
    import subprocess

    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "test_thing.py").write_text("def test_ok():\n    assert 1 == 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    # Stub LLM that always returns a final answer (no patches needed).
    def stub_llm(messages, ctx):
        return "FINAL: nothing to fix"

    wf = SWEBenchWorkflow(llm_callable=stub_llm, workdir=tmp_path / "wd")
    task = SWEBenchTask(
        repo_url=str(repo),  # local path, git clone handles it.
        issue="No actual issue, sanity check.",
        test_command="python -m pytest -q test_thing.py",
        timeout_s=30,
    )
    res = wf.run(task)
    # Tests pass on a fresh clone.
    assert res.tests_passed is True
