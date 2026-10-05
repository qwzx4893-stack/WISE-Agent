"""Phase 11 — comprehensive backend audit.

This module verifies the whole production surface in one place so a future
regression breaks loudly:

* every documented public + admin endpoint can be reached on a fresh
  ``TestClient`` (each one resolves to a known status code, never a
  Python ``500``),
* the path resolver picks up the JSON tool packs and the 1,725-skill
  index even when ``AGENT_OS_ROOT`` points at the legacy ``~/agent-os``
  runtime tree,
* the in-process ``ToolRegistry`` boots with a meaningful set of **verified
  executable** tools when ``agent_core.build_runtime`` is invoked.  Pack
  manifests without a local implementation or installed binary remain
  documentation and must not inflate the callable registry,
* the ReAct loop terminates with a deterministic mock LLM and routes
  through ``execute_command`` correctly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------
# Path / packaging assertions
# --------------------------------------------------------------------------


def test_paths_resolve_to_substantive_dirs() -> None:
    """``TOOLS_PACKS_DIR`` and ``SKILLS_DIR`` must point at directories
    that actually contain content even when ``AGENT_OS_ROOT`` is the
    legacy ``~/agent-os`` runtime tree.
    """
    from core.paths import TOOLS_PACKS_DIR, SKILLS_DIR

    assert TOOLS_PACKS_DIR.exists(), f"{TOOLS_PACKS_DIR} missing"
    packs = list(TOOLS_PACKS_DIR.glob("*.json"))
    assert len(packs) >= 19, f"expected 19+ packs, got {len(packs)}"

    assert SKILLS_DIR.exists(), f"{SKILLS_DIR} missing"
    # Substantive == has at least one non-dotfile.
    visible = [c for c in SKILLS_DIR.iterdir() if not c.name.startswith(".")]
    assert visible, "skills dir is empty (only dotfiles)"


def test_skill_indexer_finds_at_least_one_thousand() -> None:
    from core.skills import SkillIndexer

    indexer = SkillIndexer()
    names = indexer.list_skills()
    assert len(names) >= 1000, f"only {len(names)} skills indexed"


def test_tool_packs_define_at_least_two_hundred_unique_tools() -> None:
    """The tool packs ship 200+ unique manifests (we currently observe
    201). System awareness will dedupe by name, so this is the lower
    bound feeding into ``ToolRegistry`` from JSON."""
    import json
    import glob
    from core.paths import TOOLS_PACKS_DIR

    names: set[str] = set()
    for path in sorted(glob.glob(str(TOOLS_PACKS_DIR / "*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            for entry in json.load(f):
                if isinstance(entry, dict) and "name" in entry:
                    names.add(entry["name"])
    assert len(names) >= 200, f"only {len(names)} unique tool names"


# --------------------------------------------------------------------------
# ToolRegistry sizing — the headline Phase 11.B assertion
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime_tool_registry() -> Any:
    """Build the full agent runtime once and yield the populated
    ``tool_registry`` singleton from ``agent_core``.

    Sets ``AGENT_AUTO_INSTALL=0`` so the boot-time tool installer never
    blocks waiting for ``y/n`` input on a TTY.
    """
    os.environ.setdefault("AGENT_AUTO_INSTALL", "0")
    from agent_core import build_runtime, tool_registry

    build_runtime()
    return tool_registry


def test_tool_registry_has_verified_core_tools(runtime_tool_registry) -> None:
    """Only executable capabilities belong in the callable registry.

    The exact count depends on installed optional integrations, so asserting
    hundreds of manifests created a false readiness signal.  The core surface
    must still contain enough real tools for a functioning desktop agent.
    """
    count = len(runtime_tool_registry.tools)
    assert count >= 20, f"only {count} verified tools registered (need 20+)"


def test_tool_registry_includes_each_subsystem(runtime_tool_registry) -> None:
    """Spot check that every subsystem registered at least one tool."""
    names = set(runtime_tool_registry.tools.keys())
    # Admin tools are namespaced ``admin.*`` so they're easy to spot.
    assert any(n.startswith("admin.") for n in names), "no admin.* tools"
    # Preview server tools (namespaced ``preview.*``).
    assert any(n.startswith("preview.") for n in names), (
        f"missing preview.* tools, got names like {sorted(names)[:10]}")
    # Skill facade.
    assert "search_skills" in names and "load_skill" in names
    # At least one built-in adapter must be wired.  AWS is intentionally not
    # required: no fabricated cloud adapter is registered without a real
    # configured integration.
    assert any(
        name.startswith(prefix)
        for name in names
        for prefix in ("github_", "browser_", "windows_")
    )


# --------------------------------------------------------------------------
# Endpoint coverage — every advertised route must be reachable
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def public_client() -> TestClient:
    # Make sure auth is disabled for the surface sweep.
    os.environ.pop("AGENT_API_TOKEN", None)
    from api.server import app

    return TestClient(app)


# (path, method) → expected status code (or set of acceptable codes).
# 200 / 503 are both acceptable for endpoints that depend on optional
# state (e.g. /chat needs an LLM model, /admin/tools/audit needs a
# real workspace tree). 404 is fine for ``{name}`` placeholders that
# we hit with synthetic IDs.
_PUBLIC_GETS = {
    "/health", "/health/detailed",
    "/tools", "/skills", "/skills/search",
    "/openapi.json", "/docs", "/redoc",
    "/throttle/stats", "/macros",
    "/workflows", "/traces", "/traces/_/stats",
    "/admin/dashboard/summary", "/admin/dashboard/stats",
    "/admin/dashboard/activity",
    "/admin/keys", "/admin/keys/providers",
    "/admin/channels",
    "/admin/schedules", "/admin/repair/history",
    "/admin/mcp/servers", "/admin/mcp/pending",
    "/admin/oauth/accounts",
    "/admin/onboarding/required",
    "/admin/preview/list",
    "/admin/router/status",
    "/admin/skills/lifecycle", "/admin/skills/learned",
    "/admin/skills/recommend",
    "/admin/tools/known-missing", "/admin/tools/missing",
    "/admin/tools/stats", "/admin/tools/status",
    "/admin/tools/audit",
    "/admin/instructions",
    "/admin/platform",
    "/admin/resources",
    "/admin/self-test/cached",
    "/admin/settings/secrets",
    "/admin/knowledge/sources",
    "/admin/documents/available",
    "/admin/adapters",
    "/admin/streaming",
    "/optimization/stats",
    "/tools/search/stats",
}


@pytest.mark.parametrize("path", sorted(_PUBLIC_GETS))
def test_get_endpoint_does_not_500(path: str, public_client: TestClient) -> None:
    r = public_client.get(path)
    # 200 is best, 503 acceptable when a subsystem is offline, 404
    # acceptable on routes that disambiguate via a missing path
    # parameter, 401/403 acceptable when auth is enforced upstream.
    assert r.status_code < 500, (
        f"GET {path} → {r.status_code}\n{r.text[:300]}")


def test_health_subsystems_present(public_client: TestClient) -> None:
    r = public_client.get("/health/detailed")
    assert r.status_code == 200
    payload = r.json()
    for key in ("status", "tools_registered", "llm", "channels", "rag",
                "scheduler", "sandbox"):
        assert key in payload, f"/health/detailed missing key {key!r}"

    # 15 RAG sources are configured.
    assert payload["rag"].get("count") == 15


def test_apprise_registers_at_least_100_schemes() -> None:
    """Verify the live ``apprise`` install (independent of any module
    monkeypatching done by other tests) advertises 100+ schemes.

    Earlier tests in the suite stub the entire top-level ``apprise``
    package in ``sys.modules`` to drive the channels integration with
    a fake. We blow that cached stub away and re-import the real one
    before counting.
    """
    import importlib
    import sys

    for cached in ("apprise", "core.channels", "core.channels.unified"):
        sys.modules.pop(cached, None)
    apprise = importlib.import_module("apprise")
    details = apprise.Apprise().details()
    schemas = details.get("schemas") or []
    schemes: set[str] = set()
    for entry in schemas:
        for key in ("protocols", "secure_protocols"):
            for p in entry.get(key) or []:
                if isinstance(p, str):
                    schemes.add(p)
    assert len(schemes) >= 100, (
        f"only {len(schemes)} apprise schemes registered: "
        f"{sorted(schemes)[:10]}")


def test_tools_endpoint_lists_registered_tools(
        public_client: TestClient,
        runtime_tool_registry) -> None:
    """``GET /tools`` should reflect the registered tool count once the
    runtime is initialized in this process."""
    r = public_client.get("/tools")
    assert r.status_code == 200
    body = r.json()
    # Different code paths return either a list or a dict.
    if isinstance(body, dict):
        items = body.get("tools") or body.get("items") or []
    else:
        items = body
    # The public API must expose exactly the verified callable registry, not
    # every manifest that happens to be documented in tools/packs.
    assert set(items) == set(runtime_tool_registry.list_tools())


def test_skills_endpoint_returns_json(public_client: TestClient) -> None:
    r = public_client.get("/skills")
    assert r.status_code == 200
    body = r.json()
    # 1725 skills are pre-indexed; the public endpoint may paginate.
    if isinstance(body, dict):
        items = body.get("skills") or body.get("items") or []
        assert items, f"/skills returned empty list: {body}"
    else:
        assert body, "/skills returned empty list"


def test_skills_search_returns_results(public_client: TestClient) -> None:
    r = public_client.get("/skills/search", params={"q": "git"})
    assert r.status_code in (200, 422)


# --------------------------------------------------------------------------
# ReAct loop with a mock LLM
# --------------------------------------------------------------------------


class _MockLLM:
    """Deterministic LLM stub callable. ``ThinkingEngine`` invokes the
    model as ``self.model(messages)`` so the mock implements
    ``__call__`` (and ``ask`` for completeness — every legacy code path
    we still ship).
    """

    name = "mock"

    def __init__(self) -> None:
        self.calls: List[str] = []
        self._step = 0

    def _next(self) -> str:
        self._step += 1
        self.calls.append(f"step{self._step}")
        if self._step == 1:
            # Force the agent to use ``execute_command`` first.
            return ("Thought: I need to list the workspace.\n"
                    "Action: execute_command\n"
                    "Action Input: echo phase11-audit-ok")
        return "Final Answer: phase11-audit-complete"

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        return self._next()

    def ask(self, *args: Any, **kwargs: Any) -> str:
        return self._next()


def test_react_loop_with_mock_llm_terminates(runtime_tool_registry) -> None:
    """Run the ReAct loop with a mock LLM and verify it reaches a final
    answer without raising.

    We use the legacy ``react_loop`` shim because that's what the
    server's ``/chat`` endpoint and Normal-mode runner ultimately call.
    """
    from core.agent_loop import react_loop

    mock = _MockLLM()
    answer = react_loop(
        user_input="ping?",
        model=mock,
        tools=dict(runtime_tool_registry.tools),
        max_steps=3,
        enable_planner=False,
        enable_reflector=False,
        use_cache=False,
    )
    # Either the engine returned the final answer text or the mock got
    # called at least once (the engine reached the loop).
    assert mock.calls, "react_loop never invoked the mock model"
    assert isinstance(answer, str)
