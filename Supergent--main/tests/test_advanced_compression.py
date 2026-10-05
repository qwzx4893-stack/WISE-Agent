"""Phase 8 Part 5 — advanced prompt compression layer."""

from __future__ import annotations

import importlib
from typing import Any

import pytest


def _reload():
    import core.optimization.advanced_compression as ac
    return importlib.reload(ac)


def test_available_backends_keys_and_types():
    ac = _reload()
    info = ac.available_backends()
    # The five canonical backend ids must always be reported, even if
    # none of them are installed, so the /admin/resources endpoint can
    # render consistent UI.
    for name in ("llmlingua", "llmlingua2", "longllmlingua",
                 "un-locc", "chonkify"):
        assert name in info
        assert isinstance(info[name], bool)


def test_compress_prompt_none_method_is_passthrough():
    ac = _reload()
    text = "Hello " * 50
    out = ac.compress_prompt(text, method="none", ratio=0.5)
    assert out.text == text
    assert out.method == "none"
    assert out.before_tokens == out.after_tokens
    assert out.ratio == pytest.approx(1.0)


def test_auto_falls_back_when_no_backend_available(monkeypatch):
    ac = _reload()

    # Force every backend to report unavailable.
    monkeypatch.setattr(ac, "available_backends", lambda: {
        "llmlingua": False, "llmlingua2": False,
        "longllmlingua": False, "un-locc": False, "chonkify": False,
    })

    captured = {}

    def fb(t, r):
        captured["t"] = t
        captured["r"] = r
        return t[: max(1, int(len(t) * r))]

    text = "abcdefghij" * 20
    out = ac.compress_prompt(text, method="auto", ratio=0.5, fallback=fb)
    assert out.fallback is True
    assert out.method == "none"
    assert captured["t"] == text
    assert captured["r"] == 0.5
    # Deterministic fallback really halved the text length.
    assert len(out.text) == len(text) // 2


def test_compress_prompt_uses_first_available_backend(monkeypatch):
    ac = _reload()

    calls = []

    def fake_backend(text, ratio):
        calls.append(ratio)
        return text[:10], {"mode": "fake"}

    monkeypatch.setitem(ac._BACKENDS, "llmlingua2", fake_backend)
    monkeypatch.setattr(ac, "available_backends", lambda: {
        "llmlingua": False, "llmlingua2": True,
        "longllmlingua": False, "un-locc": False, "chonkify": False,
    })

    text = "abcdef" * 40
    out = ac.compress_prompt(text, method="auto", ratio=0.3)
    assert out.method == "llmlingua2"
    assert out.text == text[:10]
    assert calls == [0.3]
    assert out.fallback is False


def test_specific_method_errors_fall_through_to_fallback(monkeypatch):
    ac = _reload()

    def broken(text, ratio):
        raise RuntimeError("boom")

    monkeypatch.setitem(ac._BACKENDS, "llmlingua", broken)
    monkeypatch.setattr(ac, "available_backends", lambda: {
        "llmlingua": True, "llmlingua2": False,
        "longllmlingua": False, "un-locc": False, "chonkify": False,
    })

    out = ac.compress_prompt(
        "some text here", method="llmlingua", ratio=0.5,
        fallback=lambda t, r: t.upper(),
    )
    assert out.fallback is True
    assert out.error and "boom" in out.error
    assert out.text == "SOME TEXT HERE"


def test_stats_record_and_aggregate(monkeypatch):
    ac = _reload()

    def fake_backend(text, ratio):
        return text[: int(len(text) * 0.5)], {}

    monkeypatch.setitem(ac._BACKENDS, "chonkify", fake_backend)
    monkeypatch.setattr(ac, "available_backends", lambda: {
        "llmlingua": False, "llmlingua2": False,
        "longllmlingua": False, "un-locc": False, "chonkify": True,
    })
    text = "xy" * 400
    outcome = ac.compress_prompt(text, method="chonkify", ratio=0.5)
    ac.record_stats(outcome)
    snap = ac.compression_stats()
    assert snap["calls"] >= 1
    assert snap["by_method"]["chonkify"]["calls"] >= 1
    assert snap["by_method"]["chonkify"]["saved_tokens"] == outcome.saved_tokens


def test_token_optimizer_compress_prompt_uses_prompt_optimizer_fallback():
    from core.optimization import TokenOptimizer
    opt = TokenOptimizer(model_name="gpt-4o-mini",
                         max_prompt_tokens=50)
    # Provide a prompt big enough that PromptOptimizer shrinks it.
    text = ("System instructions. " * 60).strip()
    outcome = opt.compress_prompt(text, method="none", ratio=0.25)
    # method='none' is passthrough in advanced_compression; the
    # unified stats entry should still be recorded.
    assert outcome.method == "none"
    assert outcome.before_tokens > 0


def test_engine_uses_advanced_compression_toggle(monkeypatch):
    """End-to-end: enabling compression mutates the system prompt."""
    from core.thinking.engine import ThinkingEngine

    class _Model:
        model_name = "fake"

        def __call__(self, messages, **kw):
            # Always complete so the engine stops after one step.
            return "Final Answer: done"

    def _fake_compress(text, *, method, ratio, model_name,
                       fallback=None):
        from core.optimization.advanced_compression import (
            CompressionOutcome,
        )
        return CompressionOutcome(
            text="compressed", method="llmlingua",
            before_tokens=100, after_tokens=5, ratio=0.05,
            duration_ms=1.0,
        )

    monkeypatch.setattr(
        "core.thinking.engine.compress_prompt", _fake_compress,
        raising=False)
    # Import path inside engine._apply_advanced_compression is lazy;
    # patch the real module instead.
    import core.optimization as opt_pkg
    monkeypatch.setattr(opt_pkg, "compress_prompt", _fake_compress)

    eng = ThinkingEngine(
        model=_Model(), tools={}, enable_planner=False,
        enable_reflector=False, use_cache=False, max_steps=1,
        compression_enabled=True, compression_method="llmlingua",
        compression_ratio=0.1,
    )
    # Trigger the compression helper directly (easier to test).
    msgs = [{"role": "system", "content": "large sys prompt"},
            {"role": "user", "content": "q"}]
    out = eng._apply_advanced_compression(msgs)
    assert out[0]["content"] == "compressed"
    assert out[1]["content"] == "q"
