"""Tests for the intelligent tool selection layer.

Covers:
- ranker (capability overlap, success history, MCP penalty,
  category boost, banned filter).
- tool feedback loop (record_failure, ban after threshold,
  suggest_alternatives).
- tool stats aggregation from Tracer events.
- system_awareness dynamic prompt (top-K + collapsed view).
- /admin/tools/audit and /admin/tools/stats endpoints.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

import pytest

from core.observability import Tracer
from core.tool_feedback import ToolFeedback, FAILURE_BAN_THRESHOLD
from core.tool_intelligence.ranker import (
    rank_tools_for_task,
    select_top_tools,
    score_tool,
    tokenize,
    jaccard,
)
from core.tool_stats import compute_tool_stats


# --------------------------------------------------------------------
# Ranker
# --------------------------------------------------------------------
def _toolset() -> Dict[str, Dict]:
    return {
        "search_knowledge": {
            "name": "search_knowledge",
            "description": "Search external knowledge bases for information.",
            "capabilities": ["search information", "rank results", "find answers"],
            "use_cases": ["find information about a topic"],
            "category": "Knowledge",
        },
        "execute_command": {
            "name": "execute_command",
            "description": "Execute an arbitrary shell command in the sandbox.",
            "capabilities": ["run shell command", "execute binary"],
            "use_cases": ["run a one-off shell command"],
            "category": "Execution",
        },
        "nmap_scan": {
            "name": "nmap_scan",
            "description": "Scan a network host for open ports.",
            "capabilities": ["scan network", "detect ports", "fingerprint OS"],
            "use_cases": ["scan a network for open ports"],
            "category": "Security",
        },
        "mcp_remote_search": {
            "name": "mcp_remote_search",
            "description": "Remote MCP search tool, similar to search_knowledge.",
            "capabilities": ["search information", "rank results"],
            "use_cases": ["find information about a topic"],
            "category": "Knowledge",
            "__mcp__": True,
        },
    }


def test_search_information_picks_search_knowledge_over_execute_command():
    ranked = rank_tools_for_task(
        "find information about quantum computing",
        _toolset())
    top = ranked[0].name
    assert top == "search_knowledge"
    assert ranked[0].score > 0


def test_scan_network_picks_specialized_nmap():
    ranked = rank_tools_for_task("scan a network for open ports",
                                  _toolset())
    assert ranked[0].name == "nmap_scan"
    assert ranked[0].score > ranked[1].score


def test_native_outranks_mcp_when_otherwise_tied():
    # search_knowledge and mcp_remote_search are nearly identical;
    # the MCP penalty should keep the native one higher.
    ranked = rank_tools_for_task(
        "find information about a topic", _toolset())
    names = [r.name for r in ranked]
    assert names.index("search_knowledge") < names.index("mcp_remote_search")


def test_mcp_tool_still_appears_in_ranked_list():
    ranked = rank_tools_for_task("find information", _toolset())
    assert any(r.name == "mcp_remote_search" for r in ranked)


def test_history_pulls_score_up():
    history = [
        {"kind": "tool.call.end", "tool": "execute_command",
         "status": "ok"},
        {"kind": "tool.call.end", "tool": "execute_command",
         "status": "ok"},
        {"kind": "tool.call.end", "tool": "execute_command",
         "status": "ok"},
    ]
    no_hist = rank_tools_for_task("run", _toolset())
    with_hist = rank_tools_for_task("run", _toolset(), history=history)
    sc_no = next(r.score for r in no_hist
                 if r.name == "execute_command")
    sc_yes = next(r.score for r in with_hist
                  if r.name == "execute_command")
    assert sc_yes > sc_no


def test_banned_tool_is_excluded_from_top():
    ranked = rank_tools_for_task(
        "scan a network for open ports", _toolset(),
        banned={"nmap_scan"})
    assert ranked[0].name != "nmap_scan"
    assert next(r for r in ranked if r.name == "nmap_scan").score == 0.0


def test_select_top_k():
    top = select_top_tools("information",
                            _toolset(), k=2)
    assert len(top) == 2


def test_jaccard_basic():
    assert jaccard(set(), set()) == 0
    assert jaccard({"a"}, {"a"}) == 1.0
    assert 0 < jaccard({"a", "b"}, {"b", "c"}) < 1


# --------------------------------------------------------------------
# Feedback loop
# --------------------------------------------------------------------
def test_feedback_bans_after_threshold():
    fb = ToolFeedback(threshold=3)
    sid = "s1"
    for _ in range(3):
        info = fb.record_failure(sid, "broken_tool", "fail")
    assert info["banned"] is True
    assert fb.is_banned(sid, "broken_tool")


def test_feedback_does_not_ban_under_threshold():
    fb = ToolFeedback(threshold=3)
    sid = "s2"
    for _ in range(2):
        fb.record_failure(sid, "flaky_tool", "fail")
    assert not fb.is_banned(sid, "flaky_tool")
    assert fb.failure_count(sid, "flaky_tool") == 2


def test_feedback_per_session_isolation():
    fb = ToolFeedback(threshold=3)
    for _ in range(3):
        fb.record_failure("s_a", "t", "x")
    assert fb.is_banned("s_a", "t")
    assert not fb.is_banned("s_b", "t")


def test_feedback_record_success_heals_counter():
    fb = ToolFeedback(threshold=3)
    sid = "s3"
    fb.record_failure(sid, "t", "fail")
    fb.record_failure(sid, "t", "fail")
    fb.record_success(sid, "t")
    assert fb.failure_count(sid, "t") == 1


def test_feedback_suggest_alternatives_excludes_failed_and_banned():
    fb = ToolFeedback(threshold=3)
    sid = "s4"
    for _ in range(3):
        fb.record_failure(sid, "execute_command", "fail")
    alts = fb.suggest_alternatives(
        sid, "execute_command",
        task="find information about a topic",
        tools=_toolset())
    assert "execute_command" not in alts
    assert alts and len(alts) <= 3


# --------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------
def test_stats_aggregates_tool_call_events():
    events = [
        {"kind": "tool.call.start", "tool": "search_knowledge"},
        {"kind": "tool.call.end", "tool": "search_knowledge",
         "status": "ok", "duration_ms": 10},
        {"kind": "tool.call.start", "tool": "search_knowledge"},
        {"kind": "tool.call.end", "tool": "search_knowledge",
         "status": "ok", "duration_ms": 20},
        {"kind": "tool.call.start", "tool": "broken"},
        {"kind": "tool.call.end", "tool": "broken",
         "status": "error", "duration_ms": 5,
         "error": "boom"},
    ]
    stats = compute_tool_stats(events=events)
    assert stats["totals"]["calls"] == 3
    assert stats["totals"]["ok"] == 2
    assert stats["totals"]["error"] == 1
    by = stats["per_tool"]
    assert by["search_knowledge"]["success_rate"] == 1.0
    assert by["search_knowledge"]["avg_ms"] == 15.0
    assert by["broken"]["error_rate"] == 1.0
    assert by["broken"]["last_error"] == "boom"
    assert stats["top_tools"][0]["name"] == "search_knowledge"


# --------------------------------------------------------------------
# system_awareness dynamic prompt
# --------------------------------------------------------------------
def test_awareness_prompt_top_k_is_task_aware(monkeypatch):
    from core.system_awareness import SystemAwareness
    a = SystemAwareness()
    text = a.get_full_system_description(
        task="scan a network for open ports", top_k=5)
    # A CLI absent from this host must not be advertised as executable.
    assert "nmap_scan" not in text.split("### أدوات تحتاج تثبيتاً", 1)[0]
    assert "أدوات تحتاج تثبيتاً" in text
    # The over-reliance guidance must be present.
    assert "execute_command" in text


def test_awareness_static_prompt_used_without_task():
    from core.system_awareness import SystemAwareness
    a = SystemAwareness()
    text = a.get_full_system_description()
    assert "أفضل" not in text  # no top-K block
    assert "الفئة" in text


# --------------------------------------------------------------------
# API endpoints
# --------------------------------------------------------------------
def test_admin_tools_audit_endpoint():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from api.server import app
    client = TestClient(app)
    r = client.get("/admin/tools/audit",
                   headers={"X-Agent-Token": os.environ.get(
                       "AGENT_API_TOKEN", "")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "summary" in body
    assert body["summary"]["total_tools"] >= 100
    assert "by_code" in body["summary"]


def test_admin_tools_stats_endpoint(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from api.server import app
    Tracer.clear()
    Tracer.emit("tool.call.start", tool="zz")
    Tracer.emit("tool.call.end", tool="zz",
                status="ok", duration_ms=4)
    client = TestClient(app)
    r = client.get("/admin/tools/stats",
                   headers={"X-Agent-Token": os.environ.get(
                       "AGENT_API_TOKEN", "")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["calls"] >= 1
    names = [t["name"] for t in body["top_tools"]]
    assert "zz" in names
