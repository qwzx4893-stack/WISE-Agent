"""Phase 9 Part 2.3 — End-to-end scenario tests.

Eight scenarios, each one a self-contained simulation of a real user
workflow with every external boundary stubbed:

1. Research          — RAG router + synthetic knowledge source
2. Code generation   — IPython sandbox via direct Python
3. Multi-agent task  — Workforce planner + worker pool
4. Scheduled task    — Scheduler.add + fire_now
5. Channel notify    — Apprise wrapper with captured notify()
6. Self-healing      — SelfHealing.repair on a faked broken file
7. MCP tool          — MCPServerStore round-trip + manifest
8. Streaming WS      — /chat/stream with stub model end-to-end

The shared fixtures in conftest.py are responsible for the boundary
stubs; each scenario only orchestrates the domain logic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------------
# 1. Research — RAG search returns synthetic results
# ---------------------------------------------------------------------------
def test_e2e_research_task(mock_rag_source):
    from core.rag.router import KnowledgeRouter

    router = KnowledgeRouter()
    results = router.search("quantum computing",
                             sources=["stub_source"], max_results=3)
    assert len(results) >= 1
    titles = [r.title for r in results]
    assert any("quantum computing" in t for t in titles)
    assert results[0].source == "stub_source"


# ---------------------------------------------------------------------------
# 2. Code generation — IPython runs Python in-process
# ---------------------------------------------------------------------------
def test_e2e_code_generation_executes_python(tmp_path):
    """Verify the agent's IPython session can execute generated code."""
    from core.thinking.ipython_session import IPythonSession

    session = IPythonSession(workdir=tmp_path)
    try:
        result = session.run(
            "def sort_list(xs): return sorted(xs)\n"
            "print(sort_list([3, 1, 2]))",
            timeout=10,
        )
    finally:
        session.close()
    text = (result.stdout or "") + (result.stderr or "")
    assert "1" in text and "2" in text and "3" in text
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# 3. Multi-agent — Workforce decomposes + executes subtasks
# ---------------------------------------------------------------------------
def test_e2e_multi_agent_workforce_runs_subtasks():
    import asyncio
    from core.workforce import Workforce, RootPlanner, SubTask

    completed: List[str] = []

    async def execute(st: SubTask) -> str:
        completed.append(st.description)
        return f"done:{st.description}"

    planner = RootPlanner()
    wf = Workforce(planner=planner, execute=execute, max_workers=2)

    async def run() -> Dict[str, Any]:
        return await wf.run("Analyze this dataset and write a summary")

    report = asyncio.run(run())
    assert report.subtasks
    assert any(st.status == "done" for st in report.subtasks)
    # Every successful subtask should have run through ``execute``.
    assert len(completed) >= 1


# ---------------------------------------------------------------------------
# 4. Scheduled task — fire_now runs the registered runner
# ---------------------------------------------------------------------------
def test_e2e_scheduler_fires_runner(monkeypatch, tmp_path):
    from core import scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "Scheduler", sched_mod.Scheduler)
    # Force a per-test instance so we don't pollute the global one.
    sched_mod.Scheduler._instance = None
    sched = sched_mod.Scheduler()
    monkeypatch.setattr(sched, "path", tmp_path / "schedules.json")

    runs: List[str] = []

    def runner(s):
        runs.append(s.name)
        return {"ok": True, "summary": f"ran {s.name}"}

    sched.set_runner(runner)
    s = sched.add(name="nightly_health",
                   cron="0 3 * * *",
                   target="self_test",
                   payload={"only": ["paths"]})

    assert s.id is not None
    run = sched.fire_now(s.id)
    assert run is not None
    assert runs == ["nightly_health"]


# ---------------------------------------------------------------------------
# 5. Channel notification — Apprise URL fan-out is captured
# ---------------------------------------------------------------------------
def test_e2e_channel_notification_captured(mock_apprise):
    from core.channels.unified import register_channel, send_message

    register_channel(name="ops_e2e",
                     url="json://example.test/webhook")
    res = send_message(name="ops_e2e", title="Job done",
                       message="Report ready at /tmp/r.docx")
    assert res.ok is True
    assert "json://example.test/webhook" in mock_apprise.urls
    assert any(s["title"] == "Job done" for s in mock_apprise.sent)
    assert any("Report ready" in s["body"] for s in mock_apprise.sent)


# ---------------------------------------------------------------------------
# 6. Self-healing — repair a broken config and verify recovery
# ---------------------------------------------------------------------------
def test_e2e_self_healing_repairs_corrupted_config(monkeypatch, tmp_path):
    from core import self_healing as sh
    from core import resource_settings as rs

    monkeypatch.setattr(sh, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(rs, "SETTINGS_FILE", tmp_path / "resources.json")
    sh.SelfHealing._instance = None

    # Simulate operator corruption.
    rs.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    rs.SETTINGS_FILE.write_text("not-json{{{")

    report = sh.get_self_healing().repair(
        target="config",
        error_text="JSONDecodeError")
    assert report.success is True

    # File should now be parseable JSON again.
    parsed = json.loads(rs.SETTINGS_FILE.read_text())
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# 7. MCP tool registration — store round-trip via API
# ---------------------------------------------------------------------------
def test_e2e_mcp_tool_round_trip(monkeypatch, tmp_path):
    from core.mcp.registry import MCPRegistry

    reg = MCPRegistry(root=tmp_path)
    out = reg.add({
        "name": "weather",
        "kind": "stdio",
        "command": "/bin/true",
        "enabled": False,  # don't actually start a process
    })
    assert out["added"] is True
    assert out["name"] == "weather"

    # Listed servers include our entry.
    servers = reg.list_servers()
    assert any(s["name"] == "weather" for s in servers)

    # Remove cleans up.
    assert reg.remove("weather") is True
    assert not any(s["name"] == "weather" for s in reg.list_servers())


# ---------------------------------------------------------------------------
# 8. Streaming WS — Flutter-style chat round-trip with token frames
# ---------------------------------------------------------------------------
def test_e2e_streaming_websocket_round_trip(fastapi_client):
    with fastapi_client.websocket_connect("/chat/stream") as ws:
        ws.send_json({"type": "start",
                       "message": "say hi",
                       "mode": "normal", "max_steps": 1})
        first = ws.receive_json()
        assert first["type"] == "session"

        # Drain until we see a final or error.
        deadline = time.time() + 5.0
        types: List[str] = [first["type"]]
        while time.time() < deadline:
            try:
                f = ws.receive_json()
            except Exception:
                break
            types.append(f["type"])
            if f["type"] in ("final", "error"):
                break

        assert "session" in types
        assert any(t in ("final", "error") for t in types), types
