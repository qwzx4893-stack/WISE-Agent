"""Tests for Phase 6: enhanced RAG layer + self-healing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------------
# RAG: base machinery
# ---------------------------------------------------------------------------
def test_rag_router_singleton_registers_all_default_sources():
    from core.rag import get_router
    from core.rag.sources import all_sources
    router = get_router()
    expected = {s.name for s in all_sources()}
    assert expected.issubset(set(router.sources))


def test_rag_router_search_uses_only_listed_sources(monkeypatch):
    """When sources=[name] is passed, only that source is invoked."""
    from core.rag import get_router
    from core.rag.base import KnowledgeSource, SearchResult
    router = get_router()

    class _FakeSrc(KnowledgeSource):
        name = "_fake"
        category = "test"
        description = "test"

        def search(self, query, *, max_results=5):
            return [SearchResult(title=f"hit:{query}", source=self.name)]

    router.register(_FakeSrc())
    try:
        results = router.search("hello", sources=["_fake"], max_results=3)
        assert len(results) == 1
        assert results[0].title == "hit:hello"
        assert results[0].source == "_fake"
    finally:
        router.unregister("_fake")


def test_rag_router_swallows_source_exceptions(monkeypatch):
    from core.rag import get_router
    from core.rag.base import KnowledgeSource
    router = get_router()

    class _Boom(KnowledgeSource):
        name = "_boom"
        category = "test"
        description = "raises"

        def search(self, query, *, max_results=5):
            raise RuntimeError("kaboom")

    router.register(_Boom())
    try:
        results = router.search("anything", sources=["_boom"])
        assert results == []
        # Status should be cached as unavailable.
        statuses = router.status("_boom", force=False)
        assert statuses[0].available is False
        assert "kaboom" in statuses[0].last_error
    finally:
        router.unregister("_boom")


def test_rag_status_cache(monkeypatch):
    from core.rag import get_router
    from core.rag.base import KnowledgeSource
    router = get_router()

    calls = {"n": 0}

    class _Counter(KnowledgeSource):
        name = "_counter"
        category = "test"
        description = "counts probes"

        def search(self, query, *, max_results=5):
            calls["n"] += 1
            return []

    router.register(_Counter())
    try:
        router.status("_counter", force=True)
        n_after_first = calls["n"]
        # Within TTL → no extra call.
        router.status("_counter", force=False)
        assert calls["n"] == n_after_first
        # Force re-probe.
        router.status("_counter", force=True)
        assert calls["n"] == n_after_first + 1
    finally:
        router.unregister("_counter")


# ---------------------------------------------------------------------------
# RAG: parsing for individual sources (no network calls)
# ---------------------------------------------------------------------------
def test_wikipedia_parse(monkeypatch):
    from core.rag.sources import general
    from core.rag.sources.general import WikipediaSource

    fake_body = {"query": {"search": [
        {"title": "Pytest", "snippet": "<b>pytest</b> is a framework",
         "pageid": 12345},
    ]}}
    monkeypatch.setattr(general, "http_get",
                        lambda *a, **k: (200, fake_body, ""))
    out = WikipediaSource().search("pytest", max_results=3)
    assert len(out) == 1
    assert out[0].title == "Pytest"
    assert "<b>" not in out[0].snippet
    assert "wikipedia.org" in out[0].url


def test_duckduckgo_parse(monkeypatch):
    from core.rag.sources import general
    from core.rag.sources.general import DuckDuckGoSource

    fake = {"AbstractText": "About foo", "Heading": "Foo",
            "AbstractURL": "https://example.com/foo",
            "AbstractSource": "Wikipedia", "RelatedTopics": []}
    monkeypatch.setattr(general, "http_get", lambda *a, **k: (200, fake, ""))
    out = DuckDuckGoSource().search("foo", max_results=3)
    assert len(out) == 1
    assert out[0].title == "Foo"


def test_arxiv_parse(monkeypatch):
    from core.rag.sources import academic
    from core.rag.sources.academic import ArxivSource

    fake_xml = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1234.5678</id>
    <title>Quantum widgets</title>
    <summary>A study of widgets in the quantum domain.</summary>
    <published>2024-01-01T00:00:00Z</published>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
  </entry>
</feed>"""
    monkeypatch.setattr(academic, "http_get",
                        lambda *a, **k: (200, fake_xml, ""))
    out = ArxivSource().search("widgets", max_results=2)
    assert len(out) == 1
    assert "Quantum widgets" in out[0].title
    assert "Alice" in out[0].metadata["authors"]


def test_cve_nvd_parse(monkeypatch):
    from core.rag.sources import security
    from core.rag.sources.security import CVESource

    fake = {"vulnerabilities": [{
        "cve": {
            "id": "CVE-2024-12345",
            "descriptions": [{"lang": "en", "value": "Buffer overflow."}],
            "metrics": {"cvssMetricV31": [{
                "cvssData": {"baseScore": 9.8}}]},
            "published": "2024-01-01", "lastModified": "2024-02-01",
        },
    }]}
    monkeypatch.setattr(security, "http_get",
                        lambda *a, **k: (200, fake, ""))
    out = CVESource().search("buffer overflow", max_results=1)
    assert len(out) == 1
    assert out[0].title == "CVE-2024-12345"
    assert out[0].metadata["cvss_v3"] == 9.8


def test_gtfobins_parse(monkeypatch):
    from core.rag.sources import security
    from core.rag.sources.security import GTFOBinsSource

    fake = {"sudo": {}, "vim": {}, "less": {}}
    monkeypatch.setattr(security, "http_get",
                        lambda *a, **k: (200, fake, ""))
    src = GTFOBinsSource()
    src._index = []  # force re-fetch
    out = src.search("vim", max_results=5)
    assert any(r.title == "vim" for r in out)


def test_lolbas_parse(monkeypatch):
    from core.rag.sources import security
    from core.rag.sources.security import LOLBASSource

    fake = [{"Name": "Certutil.exe", "Description": "downloads files",
             "Category": "AwlExecution"}]
    monkeypatch.setattr(security, "http_get",
                        lambda *a, **k: (200, fake, ""))
    src = LOLBASSource()
    src._index = []
    out = src.search("certutil", max_results=5)
    assert out and out[0].title == "Certutil.exe"


def test_openalex_parse(monkeypatch):
    from core.rag.sources import academic
    from core.rag.sources.academic import OpenAlexSource

    fake = {"results": [{
        "id": "https://openalex.org/W123",
        "title": "Distributed Widgets",
        "doi": "10.1000/xyz", "publication_year": 2023,
        "cited_by_count": 42,
    }]}
    monkeypatch.setattr(academic, "http_get",
                        lambda *a, **k: (200, fake, ""))
    out = OpenAlexSource().search("widgets", max_results=1)
    assert out and "Distributed Widgets" == out[0].title
    assert out[0].metadata["doi"] == "10.1000/xyz"


def test_newsapi_and_exploitdb_removed():
    """NewsAPI + Exploit-DB were removed (key-gated). The default registry
    must contain only keyless sources."""
    from core.rag.sources import all_sources
    names = {s.name for s in all_sources()}
    assert "newsapi" not in names
    assert "exploit_db" not in names
    # We expect exactly 15 keyless sources after the cleanup.
    assert len(names) == 15


# ---------------------------------------------------------------------------
# RAG: legacy shim
# ---------------------------------------------------------------------------
def test_legacy_shim_search_knowledge(monkeypatch):
    from core import knowledge_sources
    from core.rag import get_router
    from core.rag.base import KnowledgeSource, SearchResult

    class _Stub(KnowledgeSource):
        name = "wikipedia"
        category = "test"
        description = "stub"

        def search(self, query, *, max_results=5):
            return [SearchResult(title="X", snippet="snippet text",
                                  url="http://x", source=self.name)]

    router = get_router()
    original = router.sources.get("wikipedia")
    router.register(_Stub())
    try:
        out = knowledge_sources.search_knowledge("hello",
                                                   sources="wikipedia")
        assert "[X]" in out
        assert "http://x" in out
    finally:
        if original is not None:
            router.register(original)


# ---------------------------------------------------------------------------
# Self-healing: diagnosis classifier
# ---------------------------------------------------------------------------
def test_diagnose_classifies_missing_dep():
    from core.self_healing import CAT_MISSING_DEP, get_self_healing
    SelfHealing = get_self_healing()
    SelfHealing._tasks.clear()
    diag = SelfHealing.diagnose(target="tool:nmap",
                                 error_text="nmap: command not found")
    assert diag.category == CAT_MISSING_DEP


def test_diagnose_classifies_network():
    from core.self_healing import CAT_NETWORK, get_self_healing
    diag = get_self_healing().diagnose(
        target="all",
        error_text="HTTPSConnectionPool max retries exceeded with url",
    )
    assert diag.category == CAT_NETWORK


def test_diagnose_classifies_corrupted_file():
    from core.self_healing import CAT_CORRUPTED_FILE, get_self_healing
    diag = get_self_healing().diagnose(
        target="config/x.json",
        error_text="JSONDecodeError: Expecting value: line 1 column 1",
    )
    assert diag.category == CAT_CORRUPTED_FILE


def test_diagnose_classifies_sandbox():
    from core.self_healing import CAT_SANDBOX, get_self_healing
    diag = get_self_healing().diagnose(
        target="sandbox",
        error_text="proot: cannot enter rootfs/bin/bash",
    )
    assert diag.category == CAT_SANDBOX


def test_diagnose_target_only_fallback():
    from core.self_healing import CAT_SANDBOX, get_self_healing
    diag = get_self_healing().diagnose(target="sandbox", error_text="")
    assert diag.category == CAT_SANDBOX


# ---------------------------------------------------------------------------
# Self-healing: repair pipeline
# ---------------------------------------------------------------------------
def test_repair_missing_dep_retry_install_succeeds(monkeypatch, tmp_path):
    from core import self_healing as sh

    monkeypatch.setattr(sh, "HISTORY_PATH", tmp_path / "history.json")
    sh.SelfHealing._instance = None

    class _FakeInstaller:
        def install_missing(self, names=None):
            return {"installed": [{"tool": names[0]}],
                    "failed": [], "skipped": []}
        def scan(self, force=False):
            return {}
    import core.tool_installer as ti
    monkeypatch.setattr(ti, "get_installer", lambda: _FakeInstaller())

    sh_mgr = sh.get_self_healing()
    report = sh_mgr.repair(target="tool:nmap",
                            error_text="nmap: command not found")
    assert report.success is True
    assert any(s.name == "retry_install" and s.success
                for s in report.strategies_tried)


def test_repair_missing_dep_falls_through_to_alt_tool(monkeypatch, tmp_path):
    from core import self_healing as sh

    monkeypatch.setattr(sh, "HISTORY_PATH", tmp_path / "history.json")
    sh.SelfHealing._instance = None

    class _FakeInstaller:
        def install_missing(self, names=None):
            return {"installed": [], "failed": [
                {"tool": names[0], "reason": "no candidate"}], "skipped": []}
        def scan(self, force=False):
            class _S:
                def __init__(self, name): self.name = name; self.found = True
                description = "alt"; category = "security"
            return {"masscan": _S("masscan")}
    import core.tool_installer as ti
    monkeypatch.setattr(ti, "get_installer", lambda: _FakeInstaller())

    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("AGENT_OS_APT_MIRROR", raising=False)

    report = sh.get_self_healing().repair(
        target="tool:nmap", error_text="nmap: command not found")
    names = [s.name for s in report.strategies_tried]
    assert "retry_install" in names
    assert "alternative_source" in names
    assert "suggest_alternative_tool" in names
    assert report.success is True  # because alt-tool succeeded
    last = report.strategies_tried[-1]
    assert last.name == "suggest_alternative_tool"
    assert last.success is True


def test_repair_config_resets_resources(monkeypatch, tmp_path):
    from core import self_healing as sh
    import core.resource_settings as rs
    import core.platform_manager as pm

    monkeypatch.setattr(sh, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(rs, "SETTINGS_FILE", tmp_path / "resources.json")
    saved_rs = rs.ResourceSettingsStore._instance
    saved_pm = pm.PlatformManager._instance
    saved_default = pm._DEFAULT
    sh.SelfHealing._instance = None
    rs.ResourceSettingsStore._instance = None
    pm.PlatformManager._instance = None
    pm._DEFAULT = None
    monkeypatch.setenv("AGENT_OS_MODE", "lite")
    try:
        # Pre-populate with junk values.
        rs.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        rs.SETTINGS_FILE.write_text(json.dumps({"cpu_seconds": 9999}))
        report = sh.get_self_healing().repair(target="config",
                                                error_text="invalid mode")
        assert report.success is True
        fresh = json.loads(rs.SETTINGS_FILE.read_text())
        assert fresh["cpu_seconds"] == 60   # Lite default
    finally:
        rs.ResourceSettingsStore._instance = saved_rs
        pm.PlatformManager._instance = saved_pm
        pm._DEFAULT = saved_default
        sh.SelfHealing._instance = None


def test_repair_history_persisted(monkeypatch, tmp_path):
    from core import self_healing as sh
    monkeypatch.setattr(sh, "HISTORY_PATH", tmp_path / "history.json")
    sh.SelfHealing._instance = None
    mgr = sh.get_self_healing()
    mgr.repair(target="all", error_text="random error nothing matches")
    assert (tmp_path / "history.json").exists()
    body = json.loads((tmp_path / "history.json").read_text())
    assert isinstance(body, list) and len(body) >= 1


def test_repair_unknown_returns_manual_steps(monkeypatch, tmp_path):
    from core import self_healing as sh
    monkeypatch.setattr(sh, "HISTORY_PATH", tmp_path / "history.json")
    sh.SelfHealing._instance = None
    report = sh.get_self_healing().repair(
        target="all", error_text="who knows what this is")
    assert report.success is False
    assert report.manual_steps  # always populated when nothing worked


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
def _client(monkeypatch):
    from fastapi.testclient import TestClient
    from api import server as srv
    monkeypatch.setattr(srv, "_require_auth", lambda *a, **k: None)
    return TestClient(srv.app)


def test_api_phase6_endpoints_registered():
    from api.server import app
    paths = set(app.openapi()["paths"])
    assert "/admin/knowledge/sources" in paths
    assert "/admin/knowledge/test" in paths
    assert "/admin/repair" in paths
    assert "/admin/repair/status/{repair_id}" in paths
    assert "/admin/repair/history" in paths


def test_api_knowledge_sources(monkeypatch):
    from core.rag import get_router
    from core.rag.base import KnowledgeSource, SourceStatus
    # Seed the cache so the endpoint returns immediately.
    router = get_router()
    router._status_cache.clear()
    for name, src in router.sources.items():
        router._status_cache[name] = SourceStatus(
            name=name, category=src.category, available=True,
            last_checked=9999999999.0, description=src.description,
            requires_key=src.requires_key,
        )
    client = _client(monkeypatch)
    resp = client.get("/admin/knowledge/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 10
    names = {s["name"] for s in body["sources"]}
    for must_have in ("wikipedia", "cve_nvd", "gtfobins", "pubmed",
                       "duckduckgo", "openalex", "github_repos"):
        assert must_have in names
    # Removed key-gated sources must be absent.
    assert "newsapi" not in names
    assert "exploit_db" not in names


def test_api_knowledge_test(monkeypatch):
    from core.rag.base import KnowledgeSource, SearchResult
    from core.rag import get_router
    router = get_router()

    class _Stub(KnowledgeSource):
        name = "_stub_for_test"
        category = "test"
        description = "stub"

        def search(self, query, *, max_results=5):
            return [SearchResult(title=f"hit:{query}", source=self.name)]
    router.register(_Stub())
    try:
        client = _client(monkeypatch)
        resp = client.post("/admin/knowledge/test",
                            json={"source": "_stub_for_test", "query": "ping"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["count"] == 1
    finally:
        router.unregister("_stub_for_test")


def test_api_repair_endpoint(monkeypatch, tmp_path):
    from core import self_healing as sh
    monkeypatch.setattr(sh, "HISTORY_PATH", tmp_path / "history.json")
    sh.SelfHealing._instance = None
    client = _client(monkeypatch)
    resp = client.post("/admin/repair",
                        json={"target": "all",
                              "error": "totally unknown nonsense"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "all"
    assert "manual_steps" in body
    repair_id = body["repair_id"]

    resp = client.get(f"/admin/repair/status/{repair_id}")
    assert resp.status_code == 200

    resp = client.get("/admin/repair/history")
    assert resp.status_code == 200
    assert isinstance(resp.json()["history"], list)
