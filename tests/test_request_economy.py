import threading
import time

from core.optimization.plan_cache import InMemoryCache, PlanCache, RequestCoalescer


def test_cache_key_accepts_whitespace_only_retries():
    assert PlanCache.derive_key("  explain   caching\n", tools=["read"]) == PlanCache.derive_key(
        "explain caching", tools=["read"]
    )


def test_singleflight_runs_identical_request_once():
    coalescer = RequestCoalescer()
    calls = []
    values = []

    def call():
        calls.append(True)
        time.sleep(0.05)
        return "shared"

    workers = [threading.Thread(target=lambda: values.append(coalescer.run("same", call))) for _ in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert calls == [True]
    assert values == ["shared", "shared", "shared"]
    assert coalescer.stats()["coalesced_requests"] == 2


def test_engine_uses_free_planning_by_default_and_records_cache_tokens(monkeypatch):
    import core.thinking.engine as engine_module
    from core.thinking.engine import ThinkingEngine

    cache = InMemoryCache()
    monkeypatch.setattr(engine_module, "get_plan_cache", lambda: cache)
    calls = []

    class Model:
        model_name = "request-economy-test"

        def __call__(self, _messages):
            calls.append(True)
            return "Final Answer: complete"

    result = ThinkingEngine(
        model=Model(), tools={}, max_steps=1, enable_planner=True,
        enable_reflector=False, use_cache=True,
    ).run("Write a detailed but straightforward explanation of request caching behavior.")

    assert result.answer == "complete"
    assert calls == [True]  # no separate LLM planning request
    entry = next(iter(cache.entries.values()))
    assert entry["tokens_in"] > 0
    assert entry["tokens_out"] > 0


def test_tool_index_respects_its_token_budget_with_many_categories():
    from core.optimization.prompt_optimizer import PromptOptimizer

    tools = [(f"tool_{index}", "description " * 8, f"category_{index}") for index in range(80)]
    optimizer = PromptOptimizer(max_tokens=100)
    result = optimizer.select_tools_for_prompt(tools, keep=80, budget_tokens=80)

    assert optimizer.counter.count(result) <= 80
