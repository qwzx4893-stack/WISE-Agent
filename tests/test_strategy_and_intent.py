"""Tests for the multi-strategy executor and intent classifier."""

from __future__ import annotations

import json

import pytest

from core.thinking.strategy import (
    StrategyChoice, StrategyEvaluator, rank_strategies, run_with_fallback,
)
from core.thinking.intent import classify, format_intent_hint


# ---------------------------------------------------------------------------
# StrategyEvaluator
# ---------------------------------------------------------------------------
def test_propose_default_returns_two_choices():
    ev = StrategyEvaluator()
    out = ev.propose("anything", n=2)
    assert len(out) == 2
    assert all(isinstance(c, StrategyChoice) for c in out)


def test_propose_parses_json_array_from_string_proposer():
    raw = ('Here you go: ['
           '{"name":"a","plan":"first","confidence":0.8,"tools":["x"]},'
           '{"name":"b","plan":"second","confidence":0.6}]')
    ev = StrategyEvaluator(proposer=lambda task, n: raw)
    out = ev.propose("task", 5)
    assert [c.name for c in out] == ["a", "b"]
    assert out[0].confidence == 0.8


def test_rank_orders_by_confidence_when_tied_history():
    a = StrategyChoice(name="a", plan="x", confidence=0.3, cost_estimate=1)
    b = StrategyChoice(name="b", plan="x", confidence=0.9, cost_estimate=1)
    ranked = rank_strategies([a, b])
    assert ranked[0].name == "b"


def test_rank_penalises_redundant_tool_sets():
    a = StrategyChoice(name="a", plan="x", confidence=0.7, tools=["t1", "t2"])
    b = StrategyChoice(name="b", plan="y", confidence=0.7, tools=["t1", "t2"])
    c = StrategyChoice(name="c", plan="z", confidence=0.7, tools=["t3", "t4"])
    ranked = rank_strategies([a, b, c])
    # c should not be last — its diverse tool set keeps it in the top-2.
    assert ranked[-1].name in ("a", "b")


# ---------------------------------------------------------------------------
# run_with_fallback
# ---------------------------------------------------------------------------
def test_fallback_runs_second_when_first_fails():
    a = StrategyChoice(name="bad", plan="x", confidence=0.9)
    b = StrategyChoice(name="good", plan="y", confidence=0.6)

    def exec_fn(c: StrategyChoice):
        if c.name == "bad":
            raise RuntimeError("boom")
        return {"ok": True, "value": 42}

    rep = run_with_fallback([a, b], exec_fn)
    assert rep.final_ok is True
    assert rep.fallback_count == 1
    assert rep.chosen.name == "good"
    assert rep.attempts[0].ok is False
    assert rep.attempts[1].ok is True


def test_fallback_detects_structured_failure_dict():
    a = StrategyChoice(name="a", plan="x", confidence=0.9)
    b = StrategyChoice(name="b", plan="y", confidence=0.5)

    def exec_fn(c: StrategyChoice):
        return {"ok": False, "error": "nope"} if c.name == "a" else \
               {"ok": True}

    rep = run_with_fallback([a, b], exec_fn)
    assert rep.final_ok is True
    assert rep.chosen.name == "b"


def test_fallback_returns_failure_when_all_strategies_fail():
    a = StrategyChoice(name="a", plan="x", confidence=0.9)
    b = StrategyChoice(name="b", plan="y", confidence=0.5)

    def exec_fn(c: StrategyChoice):
        raise RuntimeError("everything is broken")

    rep = run_with_fallback([a, b], exec_fn)
    assert rep.final_ok is False
    assert len(rep.attempts) == 2


def test_fallback_respects_max_attempts():
    items = [StrategyChoice(name=f"s{i}", plan="x", confidence=0.9 - i * 0.1)
             for i in range(5)]
    calls = []

    def exec_fn(c):
        calls.append(c.name)
        raise RuntimeError("fail")

    rep = run_with_fallback(items, exec_fn, max_attempts=2)
    assert len(calls) == 2
    assert rep.final_ok is False


# ---------------------------------------------------------------------------
# Intent classifier
# ---------------------------------------------------------------------------
def test_classify_openai_key():
    m = classify("save my OpenAI key sk-test1234567890abcdef as prod")
    assert m is not None
    assert m.tool == "admin.add_api_key"
    assert m.args["provider"] == "openai"
    assert m.args["api_key"].startswith("sk-test")


def test_classify_anthropic_key():
    m = classify("here's the anthropic api key: sk-ant-abcdefghijklmnop")
    assert m is not None
    assert m.args["provider"] == "anthropic"


def test_classify_mcp_register():
    m = classify("register MCP server named local-mcp at "
                 "http://localhost:9000/mcp please")
    assert m is not None
    assert m.tool == "admin.add_mcp_server"
    assert m.args["url"].startswith("http://localhost:9000")


def test_classify_apprise_channel():
    m = classify("send notifications to discord://abcd/efgh as channel ops")
    assert m is not None
    assert m.tool == "admin.add_channel"
    assert m.args["url"].startswith("discord://")


def test_classify_pro_mode():
    m = classify("Please switch to Pro Mode")
    assert m is not None
    assert m.tool == "admin.set_mode"
    assert m.args["mode"] == "pro"


def test_classify_self_test():
    m = classify("Run a system check please")
    assert m is not None
    assert m.tool == "admin.run_self_test"


def test_classify_repair_target():
    m = classify("repair the sandbox")
    assert m is not None
    assert m.tool == "admin.repair"
    assert m.args["target"] == "sandbox"


def test_classify_schedule_natural_language():
    m = classify("every day at 9 run a self-test")
    assert m is not None
    assert m.tool == "admin.schedule_task"
    assert m.args["cron"] == "0 9 * * *"


def test_classify_resource_limit():
    m = classify("raise the cpu limit to 120 seconds")
    assert m is not None
    assert m.tool == "admin.set_resource_limits"
    assert m.args["cpu_seconds"] == 120


def test_classify_returns_none_for_chitchat():
    assert classify("hi, how are you?") is None
    assert classify("") is None
    assert classify("tell me a joke about apis") is None


def test_format_intent_hint_renders_keys():
    m = classify("save my openai key sk-test1234567890abcdef")
    assert m is not None
    hint = format_intent_hint(m)
    assert "admin.add_api_key" in hint
    assert "api_key" in hint
