"""Phase 8 Part 2 — token-level streaming through ThinkingEngine."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

import pytest


class _StreamingFakeModel:
    """A model that exposes both ``__call__`` and ``stream()``.

    ``stream`` yields the answer in small chunks; ``__call__`` joins
    them so the non-streaming path still works.
    """

    model_name = "fake-stream"

    def __init__(self, response: str, chunk_size: int = 4):
        self.response = response
        self.chunk_size = chunk_size
        self.stream_calls = 0
        self.sync_calls = 0

    def __call__(self, messages: List[Dict[str, str]], **kw: Any) -> str:
        self.sync_calls += 1
        return self.response

    def stream(self, messages: List[Dict[str, str]],
               **kw: Any) -> Iterable[str]:
        self.stream_calls += 1
        r = self.response
        for i in range(0, len(r), self.chunk_size):
            yield r[i:i + self.chunk_size]


class _NonStreamingModel:
    model_name = "fake-sync"

    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def __call__(self, messages, **kw):
        self.calls += 1
        return self.response


def _make_engine(model, on_token):
    from core.thinking.engine import ThinkingEngine
    return ThinkingEngine(
        model=model,
        tools={},  # no tools needed for final-answer streaming
        enable_planner=False,
        enable_reflector=False,
        use_cache=False,
        max_steps=2,
        on_token=on_token,
    )


def test_engine_forwards_only_final_answer_tokens():
    """Tokens before 'Final Answer:' must not leak to the callback."""
    tokens: List[str] = []
    response = ("Thought: I should think carefully.\n"
                "Final Answer: The answer is 42.")
    model = _StreamingFakeModel(response, chunk_size=6)
    engine = _make_engine(model, on_token=tokens.append)
    result = engine.run("what is 6x7?")
    assert result.answer.strip() == "The answer is 42."
    # The streamed text should equal what comes after 'Final Answer:'.
    forwarded = "".join(tokens)
    assert forwarded.startswith("The answer is 42")
    # And must not contain the reasoning preamble.
    assert "Thought:" not in forwarded
    assert model.stream_calls == 1


def test_engine_falls_back_to_sync_without_stream_method():
    tokens: List[str] = []
    response = "Final Answer: hello"
    model = _NonStreamingModel(response)
    engine = _make_engine(model, on_token=tokens.append)
    result = engine.run("hi")
    assert result.answer.strip() == "hello"
    assert model.calls == 1
    # No callback fires when the model can't stream.
    assert tokens == []


def test_engine_ignores_callback_exceptions():
    bad_tokens: List[str] = []

    def broken(delta):
        bad_tokens.append(delta)
        raise RuntimeError("boom")

    response = "Final Answer: ok"
    model = _StreamingFakeModel(response, chunk_size=2)
    engine = _make_engine(model, on_token=broken)
    result = engine.run("anything")
    assert result.answer.strip() == "ok"
    # Despite every callback raising, we still collected every delta.
    assert "".join(bad_tokens).startswith("ok")


def test_engine_without_callback_uses_sync_call():
    response = "Final Answer: sync-path"
    model = _StreamingFakeModel(response, chunk_size=3)
    engine = _make_engine(model, on_token=None)
    result = engine.run("hi")
    assert result.answer.strip() == "sync-path"
    # With no on_token the engine must not call stream().
    assert model.stream_calls == 0
    assert model.sync_calls == 1


def test_universal_llm_stream_error_never_dispatches_hidden_second_request(monkeypatch):
    """A failed paid stream must propagate, not silently generate again."""
    from core.llm.universal import UniversalLLM

    llm = UniversalLLM(api_key="sk-test", model="gpt-4o-mini")

    calls = []
    def bad_stream(*a, **k):
        raise RuntimeError("rate limited")

    def ok_call(msgs, **k):
        calls.append(msgs)
        return "full answer"

    monkeypatch.setattr(llm, "_stream_openai_compat", bad_stream)
    monkeypatch.setattr(llm, "_call_openai_compat", ok_call)
    with pytest.raises(RuntimeError, match="retry explicitly") as captured:
        list(llm.stream([{"role": "user", "content": "hi"}]))
    assert str(captured.value.__cause__) == "rate limited"
    assert calls == []


def test_universal_llm_stream_openai_compat_yields_deltas(monkeypatch):
    from core.llm.universal import UniversalLLM

    llm = UniversalLLM(api_key="sk-test", model="gpt-4o-mini")

    class _Delta:
        def __init__(self, c): self.content = c

    class _Choice:
        def __init__(self, c): self.delta = _Delta(c)

    class _Chunk:
        def __init__(self, c): self.choices = [_Choice(c)]

    class _FakeCompletions:
        def create(self, **kw):
            assert kw["stream"] is True
            return iter([_Chunk("Hel"), _Chunk("lo "), _Chunk("world")])

    class _FakeChat:
        def __init__(self): self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self): self.chat = _FakeChat()

    monkeypatch.setattr(llm, "_ensure_openai", lambda: _FakeClient())
    out = list(llm.stream([{"role": "user", "content": "hi"}]))
    assert out == ["Hel", "lo ", "world"]
