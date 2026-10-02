"""Shared fixtures for E2E scenario tests.

These fixtures stub out every external boundary so the scenarios can
be validated in CI without a single network packet:

* ``mock_llm_router``      — replaces ``TeamModel.ask`` with a tiny
  scripted dispatcher driven by the test (default: echoes the prompt).
* ``mock_apprise``          — replaces apprise.Apprise so channel
  notifications are captured into a list instead of sent.
* ``mock_rag_source``       — installs a synthetic KnowledgeSource into
  the global router for the duration of a test.
* ``mock_subprocess_ok``    — replaces ``subprocess.run`` with a stub
  that records each call and returns a successful CompletedProcess.

All fixtures are scoped per-function and tear down cleanly.
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import pytest


# ---------------------------------------------------------------------------
# LLM router stub
# ---------------------------------------------------------------------------


@dataclass
class _StubLLM:
    """Mimics the duck-typed model object that ``Normal`` consumes.

    ``responder`` is called with the textual prompt; whatever it
    returns is forwarded back as if the LLM had answered. Defaults to
    a "Final answer:" wrapper so the ReAct loop terminates after one
    round.
    """

    responder: Callable[[str], str]
    name: str = "stub"
    calls: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def ask(self, *args: Any, **kwargs: Any) -> str:
        # The Normal agent calls ``model.ask(messages, ...)`` where
        # ``messages`` is a list of {"role","content"} dicts. We keep
        # the contract loose because some helpers pass strings too.
        prompt = ""
        if args:
            arg0 = args[0]
            if isinstance(arg0, str):
                prompt = arg0
            elif isinstance(arg0, list):
                prompt = " ".join(m.get("content", "") for m in arg0
                                  if isinstance(m, dict))
        self.calls.append(prompt)
        return self.responder(prompt)


@pytest.fixture()
def mock_llm_router(monkeypatch):
    """Returns a factory ``make(responder)`` that yields a stub LLM."""

    def make(responder: Optional[Callable[[str], str]] = None) -> _StubLLM:
        if responder is None:
            responder = lambda p: f"Final answer: {p[:64]}"
        return _StubLLM(responder=responder)

    return make


# ---------------------------------------------------------------------------
# Apprise stub
# ---------------------------------------------------------------------------


class _CapturedNotify:
    """Captures calls to ``apprise.Apprise.notify`` into ``sent``."""

    def __init__(self) -> None:
        self.urls: List[str] = []
        self.sent: List[Dict[str, Any]] = []

    def add(self, url: str) -> bool:
        self.urls.append(url)
        return True

    def notify(self, body: str, title: str = "", **kwargs: Any) -> bool:
        self.sent.append({"body": body, "title": title, **kwargs})
        return True

    def details(self) -> Dict[str, Any]:
        return {"schemas": [{"service_name": "stub"}]}


@pytest.fixture()
def mock_apprise(monkeypatch):
    """Replace ``apprise.Apprise`` with a capturing double."""
    captured = _CapturedNotify()

    class _ApprisePatch:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def add(self, url: str, *args: Any, **kwargs: Any) -> bool:
            return captured.add(url)

        def notify(self, body: str = "", title: str = "",
                   **kwargs: Any) -> bool:
            return captured.notify(body=body, title=title, **kwargs)

        def details(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            return captured.details()

    try:
        import apprise
        monkeypatch.setattr(apprise, "Apprise", _ApprisePatch, raising=True)
    except Exception:
        pytest.skip("apprise not installed")
    return captured


# ---------------------------------------------------------------------------
# RAG source stub
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_rag_source():
    """Register a synthetic source on the global KnowledgeRouter for
    the duration of the test.
    """
    from core.rag.base import KnowledgeSource, SearchResult
    from core.rag.router import KnowledgeRouter

    class _StubSource(KnowledgeSource):
        name = "stub_source"
        category = "test"
        description = "synthetic test source"

        def search(self, query: str, *,
                   max_results: int = 5) -> List[SearchResult]:
            return [
                SearchResult(
                    title=f"Stub result for: {query}",
                    snippet=f"This is fake context about {query}",
                    url=f"https://example.test/?q={query}",
                    source=self.name,
                    score=0.95,
                ),
            ][:max_results]

    router = KnowledgeRouter()
    src = _StubSource()
    router.register(src)
    yield src
    router.unregister(src.name)


# ---------------------------------------------------------------------------
# subprocess stub
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_subprocess_ok(monkeypatch):
    """Replace ``subprocess.run`` with a stub that records and succeeds."""
    calls: List[Dict[str, Any]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="ok\n", stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fastapi_client():
    """Build a TestClient with a stub model so /chat works."""
    from fastapi.testclient import TestClient
    from api.server import app, _state

    class _Stub:
        name = "stub"

        def ask(self, *a, **kw):
            return "Final answer: stub-reply"

    _state["model"] = _Stub()
    _state["awareness"] = None
    _state["ready"] = True
    return TestClient(app)
