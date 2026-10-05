"""Deterministic resilience contracts for the multi-provider LLM router."""

from __future__ import annotations


class _FakeLLM:
    def __init__(self, name: str, replies: list[object], stream_values: list[str] | None = None):
        self.name = name
        self.model = f"{name}-model"
        self.base_url = "http://provider.invalid/v1"
        self.temperature = 0.0
        self.max_tokens = 32
        self.provider = type("Provider", (), {"id": name})()
        self._replies = list(replies)
        self._stream_values = stream_values
        self.calls = 0

    def __call__(self, _messages, **_kwargs):
        self.calls += 1
        outcome = self._replies.pop(0) if self._replies else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)

    def stream(self, _messages, **_kwargs):
        self.calls += 1
        for value in self._stream_values or []:
            yield value


def test_selected_provider_opens_circuit_without_switching_models(monkeypatch):
    import core.llm.universal as universal

    now = [100.0]
    monkeypatch.setattr(universal.time, "time", lambda: now[0])
    router = universal.LLMRouter(failure_threshold=2, circuit_cooldown_seconds=10)
    failing = _FakeLLM("failing", [RuntimeError("temporary outage")] * 3)
    healthy = _FakeLLM("healthy", ["must-not-run"])
    router.add(failing, priority=10)
    router.add(healthy)

    assert "temporary outage" in router([{"role": "user", "content": "x"}])
    assert "temporary outage" in router([{"role": "user", "content": "x"}])
    assert router.status()[0]["circuit_open"] is True

    # The third request does not spend another attempt or silently switch models.
    assert "متوقف مؤقتاً" in router([{"role": "user", "content": "x"}])
    assert failing.calls == 2
    assert healthy.calls == 0

    now[0] += 11
    assert "temporary outage" in router([{"role": "user", "content": "x"}])
    assert failing.calls == 3


def test_stream_reports_empty_selected_provider_without_switching_models():
    from core.llm.universal import LLMRouter

    router = LLMRouter()
    empty = _FakeLLM("empty", [], stream_values=[])
    healthy = _FakeLLM("healthy", [], stream_values=["provider", "-ok"])
    router.add(empty, priority=10)
    router.add(healthy)

    assert "empty" in "".join(router.stream([{"role": "user", "content": "x"}]))
    assert router.status()[0]["consecutive_failures"] == 1
    assert healthy.calls == 0


def test_active_client_matches_the_selected_backend():
    from core.llm.universal import LLMRouter

    router = LLMRouter(active_name="secondary")
    router.add(_FakeLLM("primary", ["unused"]), priority=10)
    router.add(_FakeLLM("secondary", ["selected"]))

    assert router.active_client["name"] == "secondary"
