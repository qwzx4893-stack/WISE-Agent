"""Phase 9 Part 1 — public API surface (skills/tools/health) + rate limit.

These exercises the routes added on top of the existing 91-endpoint
admin surface:

* ``GET /tools/{name}``      — alias for ``/tools/manifest/{name}``.
* ``GET /skills``            — public read-only listing with lifecycle.
* ``GET /skills/search``     — semantic search.
* ``GET /health/detailed``   — per-subsystem health snapshot.
* Rate limiter middleware    — 429 after the bucket runs dry.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app_obj(monkeypatch_module):
    """Build the FastAPI app without running lifespan.

    Lifespan would call ``build_runtime`` which instantiates the
    LLM keystore + ``system_awareness``, both of which build a
    ``PlatformManager`` singleton based on whatever ``AGENT_OS_MODE``
    happens to be at process start. That singleton then leaks into
    later tests and breaks ``test_repair_config_resets_resources`` in
    ``tests/test_rag_and_self_healing.py``. We bypass the lifespan
    entirely and rely on the route handlers' own ``_state.get(...)``
    fall-back paths (which return 503 when state is missing — exactly
    what we want when exercising the public surface in isolation).
    """
    monkeypatch_module.delenv("AGENT_API_TOKEN", raising=False)
    monkeypatch_module.setenv("AGENT_RATE_LIMIT_CHAT_CAPACITY", "2")
    monkeypatch_module.setenv("AGENT_RATE_LIMIT_CHAT_REFILL_PER_SEC", "0.001")
    monkeypatch_module.setenv("AGENT_RATE_LIMIT_EXECUTE_CAPACITY", "1")
    monkeypatch_module.setenv("AGENT_RATE_LIMIT_EXECUTE_REFILL_PER_SEC",
                              "0.001")
    # Force fresh limiters that read the env above.
    from core.rate_limit import reset_limiters
    reset_limiters()

    from api.server import app
    # Reset bucket cache once more in case something probed it earlier.
    reset_limiters()
    return app


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped monkeypatch (built-in monkeypatch is function-scoped)."""
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture()
def client(app_obj):
    # Always reset the rate-limiter buckets so tests don't pollute
    # each other. Construct TestClient *without* a context manager so
    # the FastAPI lifespan does not run (see ``app_obj`` for why).
    from core.rate_limit import reset_limiters
    reset_limiters()
    return TestClient(app_obj)


def test_tools_alias_route(client):
    r = client.get("/tools")
    assert r.status_code == 200
    assert "tools" in r.json()


def test_tools_alias_unknown_returns_404(client):
    r = client.get("/tools/this-tool-does-not-exist")
    assert r.status_code in (404, 503)


def test_skills_public_listing(client):
    r = client.get("/skills?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "skills" in body and "total" in body and "count" in body
    assert body["count"] <= 5
    if body["skills"]:
        first = body["skills"][0]
        assert {"name", "state"}.issubset(first.keys())


def test_skills_search_requires_query(client):
    r = client.get("/skills/search?q=")
    assert r.status_code == 400


def test_skills_search_returns_results_or_empty(client):
    r = client.get("/skills/search?q=python&top_k=3")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "python"
    assert "results" in body and "count" in body
    assert body["count"] == len(body["results"])


def test_health_detailed_has_all_subsystems(client):
    r = client.get("/health/detailed")
    assert r.status_code == 200
    body = r.json()
    for k in ("status", "tools_registered", "awareness", "llm",
              "channels", "rag", "scheduler", "sandbox"):
        assert k in body, f"missing key {k}"
    # awareness sub-block has both counts
    assert {"tools", "skills"}.issubset(body["awareness"].keys())


def test_rate_limit_blocks_after_capacity(client, monkeypatch):
    # Capacity 1 means the second /execute call within the refill
    # window MUST be 429-rate-limited regardless of payload validity.
    payloads = [{"command": "echo 1"}, {"command": "echo 2"}]
    statuses = []
    for p in payloads:
        r = client.post("/execute", json=p)
        statuses.append(r.status_code)
    assert 429 in statuses, f"expected one 429 in {statuses}"
    rl = next((s for s in statuses if s == 429), None)
    assert rl == 429


def test_rate_limit_response_shape(client):
    # Burn the chat bucket (capacity=2), then peek at the 429 body.
    for _ in range(3):
        r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 429
    body = r.json()
    assert body["limit"] == "chat"
    assert "retry_after_s" in body
    assert "Retry-After" in r.headers
