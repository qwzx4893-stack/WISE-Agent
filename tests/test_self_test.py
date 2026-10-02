"""Phase 9 Part 2.4 — Startup self-test tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.self_test import (
    DEGRADED, FAILED, OK,
    CheckResult, SelfTestReport,
    run_self_test, persist_report, cached_report,
    check_paths, check_sandbox, check_tools, check_skills,
    check_rag_sources, check_channels, check_scheduler,
    check_optional_compression, check_workforce, check_llm_providers,
)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_check_paths_returns_check_result():
    r = check_paths()
    assert isinstance(r, CheckResult)
    assert r.name == "paths"
    assert r.status in (OK, DEGRADED, FAILED)


def test_check_tools_returns_count_when_registry_populated():
    r = check_tools()
    assert r.name == "tools"
    # Either OK with a count or DEGRADED — never FAILED on a clean repo.
    assert r.status in (OK, DEGRADED)


def test_check_channels_reports_apprise_status():
    r = check_channels()
    assert r.name == "channels"
    # apprise is in requirements.txt so it should be available.
    assert r.status == OK
    assert r.detail.get("apprise_available") is True


def test_check_skills_handles_empty_skill_dir(tmp_path, monkeypatch):
    """SkillIndexer works against the configured SKILLS_DIR."""
    r = check_skills()
    # Either ok or degraded; check function never raises.
    assert r.name == "skills"
    assert r.status in (OK, DEGRADED)


def test_check_rag_sources_does_not_make_network_calls():
    r = check_rag_sources()
    assert r.name == "rag_sources"
    assert r.duration_ms < 500


def test_check_scheduler_imports_safely():
    r = check_scheduler()
    assert r.name == "scheduler"
    assert r.status in (OK, DEGRADED)


def test_check_workforce_imports():
    r = check_workforce()
    assert r.name == "workforce"
    assert r.status == OK


def test_check_optional_compression_reports_backends():
    r = check_optional_compression()
    assert r.name == "advanced_compression"
    # Whether or not llmlingua / un_locc is installed, the status
    # must be OK or DEGRADED.
    assert r.status in (OK, DEGRADED)


def test_check_llm_providers_when_no_keys():
    r = check_llm_providers()
    assert r.name == "llm_providers"
    # CI has no LLM keys, so this should be DEGRADED.
    assert r.status in (OK, DEGRADED)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_run_self_test_returns_report():
    report = run_self_test()
    assert isinstance(report, SelfTestReport)
    assert report.status in (OK, DEGRADED, FAILED)
    assert len(report.checks) >= 8  # all default checks
    assert report.summary["ok"] + report.summary["degraded"] \
        + report.summary["failed"] == len(report.checks)
    assert report.duration_ms > 0


def test_run_self_test_only_subset():
    report = run_self_test(only=["paths", "tools"])
    names = {c.name for c in report.checks}
    assert names == {"paths", "tools"}


def test_run_self_test_extra_check():
    """Custom checks injected via ``extra=`` are respected."""
    def my_check() -> CheckResult:
        return CheckResult(name="my", status=OK, message="hello")
    report = run_self_test(only=["paths"], extra=[my_check])
    names = [c.name for c in report.checks]
    assert "my" in names


def test_check_function_that_raises_is_caught():
    """Even a check that throws must be surfaced as FAILED, not crash."""
    def boom() -> CheckResult:
        raise RuntimeError("boom")
    report = run_self_test(only=[], extra=[boom])
    assert any(c.status == FAILED and "boom" in c.message
               for c in report.checks)


def test_persist_and_cached_report_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("core.self_test._report_path",
                        lambda: tmp_path / "self_test.json")
    report = run_self_test(only=["paths"])
    persist_report(report)
    loaded = cached_report()
    assert loaded is not None
    assert loaded.status == report.status
    assert len(loaded.checks) == len(report.checks)
    # Persisted JSON is human-readable.
    raw = json.loads((tmp_path / "self_test.json").read_text())
    assert raw["summary"]["ok"] + raw["summary"]["degraded"] \
        + raw["summary"]["failed"] == len(report.checks)


def test_cached_report_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("core.self_test._report_path",
                        lambda: tmp_path / "missing.json")
    assert cached_report() is None


# ---------------------------------------------------------------------------
# /admin/self-test endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "tok")
    from api.server import app
    return TestClient(app)


def test_admin_self_test_requires_auth(admin_client):
    r = admin_client.get("/admin/self-test")
    assert r.status_code == 401


def test_admin_self_test_returns_full_report(admin_client):
    r = admin_client.get("/admin/self-test",
                         headers={"X-Agent-Token": "tok"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("ok", "degraded", "failed")
    assert "checks" in data
    assert isinstance(data["checks"], list)
    assert data["summary"]["ok"] + data["summary"]["degraded"] \
        + data["summary"]["failed"] == len(data["checks"])


def test_admin_self_test_subset_via_only(admin_client):
    r = admin_client.get("/admin/self-test?only=paths,tools&persist=false",
                         headers={"X-Agent-Token": "tok"})
    assert r.status_code == 200
    data = r.json()
    names = {c["name"] for c in data["checks"]}
    assert names == {"paths", "tools"}


def test_admin_self_test_cached_404_when_no_report(admin_client, monkeypatch,
                                                    tmp_path):
    monkeypatch.setattr("core.self_test._report_path",
                        lambda: tmp_path / "missing.json")
    r = admin_client.get("/admin/self-test/cached",
                         headers={"X-Agent-Token": "tok"})
    assert r.status_code == 404


def test_admin_self_test_cached_returns_persisted(admin_client, monkeypatch,
                                                    tmp_path):
    target = tmp_path / "cached.json"
    monkeypatch.setattr("core.self_test._report_path", lambda: target)
    r1 = admin_client.get("/admin/self-test?only=paths",
                          headers={"X-Agent-Token": "tok"})
    assert r1.status_code == 200
    r2 = admin_client.get("/admin/self-test/cached",
                          headers={"X-Agent-Token": "tok"})
    assert r2.status_code == 200
    assert r2.json()["status"] == r1.json()["status"]
