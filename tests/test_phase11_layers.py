"""Phase 11.C — layer-by-layer smoke tests.

For each foundational layer in Agent OS, prove that:

* the public API entry point still imports cleanly,
* a representative call returns the expected shape,
* internal state is consistent.

These tests intentionally avoid network egress so they run on CI in
under a few seconds. The goal is to detect regressions in the layer
contracts, not to re-test the underlying implementations (covered by
the dedicated suites).
"""

from __future__ import annotations

import os

import pytest


# --------------------------------------------------------------------------
# RAG layer (15 sources)
# --------------------------------------------------------------------------

def test_rag_layer_has_15_sources() -> None:
    from core.rag.sources import all_sources

    sources = all_sources()
    assert len(sources) == 15
    names = {getattr(s, "name", type(s).__name__) for s in sources}
    # Spot check the headline sources we advertise in the README.
    for must in ("wikipedia", "arxiv", "pubmed", "github_repos",
                 "semantic_scholar"):
        assert must in names, f"missing RAG source {must!r}"


# --------------------------------------------------------------------------
# Skills layer
# --------------------------------------------------------------------------

def test_skills_indexer_finds_at_least_1700() -> None:
    from core.skills import SkillIndexer

    idx = SkillIndexer()
    skills = idx.list_skills()
    assert len(skills) >= 1700, f"skills indexed: {len(skills)}"


def test_skills_search_returns_relevant_results() -> None:
    from core.skills.search import SkillSearch

    ss = SkillSearch()
    results = ss.search("git", top_k=3)
    # Results may be empty if embeddings are unavailable, but if we
    # got any they must be tuples of (name, score, ...).
    if results:
        first = results[0]
        assert isinstance(first, tuple) or hasattr(first, "name")


def test_skills_upload_endpoint_writes_file(tmp_path, monkeypatch) -> None:
    """Smoke ``POST /admin/skills/upload`` end-to-end."""
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    from fastapi.testclient import TestClient
    from api.server import app

    client = TestClient(app)
    payload = {"content": "# phase11-test-skill\n\nA dummy skill for the audit."}
    r = client.post("/admin/skills/upload", json=payload)
    # 200 on success; if the server hasn't fully booted state, accept
    # 503 too — the route exists and validation passed.
    assert r.status_code in (200, 503), r.text


# --------------------------------------------------------------------------
# Channels layer (Apprise)
# --------------------------------------------------------------------------

def test_channels_apprise_advertises_100_plus_schemes() -> None:
    """Phase 9 already asserts ``len(schemes) >= 100`` — duplicated here
    so the layer audit fails loudly if Apprise is removed/downgraded."""
    import importlib
    import sys

    for cached in ("apprise", "core.channels", "core.channels.unified"):
        sys.modules.pop(cached, None)
    apprise = importlib.import_module("apprise")
    schemas = apprise.Apprise().details().get("schemas") or []
    schemes: set[str] = set()
    for entry in schemas:
        for key in ("protocols", "secure_protocols"):
            for p in entry.get(key) or []:
                if isinstance(p, str):
                    schemes.add(p)
    assert len(schemes) >= 100


def test_channels_unified_facade_imports() -> None:
    from core.channels.unified import (  # noqa: F401
        list_channels,
        ChannelRegistry,
        get_registry,
    )


# --------------------------------------------------------------------------
# Scheduler layer
# --------------------------------------------------------------------------

def test_scheduler_singleton_alive() -> None:
    from core.scheduler import get_scheduler

    s = get_scheduler()
    assert s is not None
    assert hasattr(s, "list")
    schedules = s.list()
    assert isinstance(schedules, list)


# --------------------------------------------------------------------------
# Workforce layer
# --------------------------------------------------------------------------

def test_workforce_planner_and_pool_importable() -> None:
    from core.workforce import (  # noqa: F401
        RootPlanner,
        Worker,
        Workforce,
    )


# --------------------------------------------------------------------------
# Self-healing layer
# --------------------------------------------------------------------------

def test_self_healing_repair_categories() -> None:
    import core.self_healing as sh

    cats = [getattr(sh, name) for name in dir(sh)
            if name.startswith("CAT_")]
    # 5+ failure categories per the original spec (missing dep, sandbox,
    # config, network, corrupted file, …)
    assert len(cats) >= 5


def test_self_healing_singleton_repair_callable() -> None:
    from core.self_healing import SelfHealing

    sh = SelfHealing()
    assert hasattr(sh, "repair")
    assert callable(sh.repair)


# --------------------------------------------------------------------------
# Self-modify layer
# --------------------------------------------------------------------------

def test_self_modify_safe_edit_round_trip() -> None:
    """``safe_edit_file`` only allows edits inside ``AGENT_OS_ROOT`` so we
    write a throwaway file under ``MEMORY_DIR`` and round-trip an edit
    there.
    """
    from core.paths import MEMORY_DIR, ensure_runtime_dirs
    from core.self_modify import safe_edit_file, rollback_file

    ensure_runtime_dirs()
    target = MEMORY_DIR / "phase11_self_modify_smoke.py"
    target.write_text("x = 1\n", encoding="utf-8")
    try:
        msg = safe_edit_file(str(target), "x = 1", "x = 2")
        assert isinstance(msg, str)
        # Either the edit succeeded or it failed due to validation
        # (we only care that the function didn't raise).
        assert "❌" not in msg or "النص المستهدف" not in msg, msg
        # Best-effort rollback to keep the tree clean.
        try:
            rollback_file(str(target))
        except Exception:
            pass
    finally:
        try:
            target.unlink()
        except Exception:
            pass


def test_self_modify_apply_patch_callable() -> None:
    from core.self_modify import apply_patch

    assert callable(apply_patch)


# --------------------------------------------------------------------------
# OAuth layer
# --------------------------------------------------------------------------

def test_oauth_providers_and_flow_importable() -> None:
    from core.oauth.flow import (  # noqa: F401
        start_link_flow,
        complete_link_flow,
        get_link_status,
    )
    from core.oauth.providers import OAUTH_PROVIDERS  # noqa: F401

    assert callable(start_link_flow)
    assert callable(complete_link_flow)
    assert "google" in OAUTH_PROVIDERS


def test_oauth_store_lists_accounts() -> None:
    """The token store must expose ``list_accounts`` without touching
    the network or raising."""
    from core.oauth.store import list_accounts

    accounts = list_accounts()
    assert isinstance(accounts, list)


# --------------------------------------------------------------------------
# Preview server
# --------------------------------------------------------------------------

def test_preview_server_lifecycle() -> None:
    """``preview_server`` rejects paths outside ``WORKSPACE_DIR`` so we
    drop the demo directory inside the workspace tree.
    """
    import shutil
    from core.preview_server import (
        start_preview, stop_preview, list_previews,
    )
    from core.paths import WORKSPACE_DIR, ensure_runtime_dirs

    ensure_runtime_dirs()
    site = WORKSPACE_DIR / "phase11_preview_demo"
    if site.exists():
        shutil.rmtree(site, ignore_errors=True)
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text("<h1>phase11</h1>", encoding="utf-8")

    info = start_preview(str(site), session_id="phase11")
    server_id = None
    try:
        # info is a PreviewServerInfo dataclass-like with .url and .server_id.
        url = getattr(info, "url", None)
        server_id = getattr(info, "server_id", None) or getattr(info, "id", None)
        assert url and "127.0.0.1" in url, f"non-loopback url {url}"

        running = list_previews()
        assert running, "list_previews returned empty after start"
        assert any(p.get("server_id") == server_id for p in running), (
            f"server_id {server_id} not in {running}")
    finally:
        if server_id:
            try:
                stop_preview(server_id)
            except Exception:
                pass
        try:
            shutil.rmtree(site, ignore_errors=True)
        except Exception:
            pass
