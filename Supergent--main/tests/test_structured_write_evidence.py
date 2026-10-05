"""Offline observer scope/noninterference tests; no models or paid dispatch."""
import json
from types import SimpleNamespace

import pytest

from core import web_research
from core.brain import capability_agent
from qa.acceptance import structured_write_evidence as observer

URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "ROOT", tmp_path)
    runtime = tmp_path / ".tooling/project-ui-runtime-fixture"
    runtime.mkdir(parents=True)
    monkeypatch.setenv("WISE_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("WISE_QA_BUDGET_PATH", str(tmp_path / "qa/results/shared-ledger.json"))
    monkeypatch.setattr(web_research, "attach_observed_record_date_citations", web_research.attach_observed_record_date_citations)
    monkeypatch.setattr(web_research, "validate_grounded_answer", web_research.validate_grounded_answer)
    from core.capability_router import CapabilityRouter
    monkeypatch.setattr(CapabilityRouter, "execute", CapabilityRouter.execute)
    return tmp_path / "qa-results/diagnostic/drafts.ndjson"


def source():
    return {"url": URL, "evidence_kind": "source_api_data", "retrieved_at": "2026-10-04T14:00:00+00:00",
            "page_text": json.dumps({"resource_id": "cisa-known-exploited-vulnerabilities", "records": [
                {"cveID": observer.CVE, "dateAdded": "2021-12-10"},
                {"cveID": "CVE-2021-45046", "dateAdded": "2023-05-01"}],
                "secret": "must-not-be-captured"})}


def install_run(monkeypatch, destination, content, path="trial-1-research.md", evidence=None):
    def run(self, goal):
        actual = [source()] if evidence is None else evidence
        bound = web_research.attach_observed_record_date_citations(content, actual, goal, path)
        return bound, web_research.validate_grounded_answer(bound, actual)
    monkeypatch.setattr(capability_agent.CapabilityAgent, "_run", run)
    observer.install(destination)
    return lambda goal: capability_agent.CapabilityAgent._run(None, goal)


def test_actual_raw_bound_and_verdict_preserved(owned, monkeypatch):
    content = "# CVE-2021-44228\n\nKEV dateAdded: 2021-12-10"
    actual = [source()]
    expected_bound = web_research.attach_observed_record_date_citations(content, actual, observer.public_goal(1), "trial-1-research.md")
    expected = expected_bound, web_research.validate_grounded_answer(expected_bound, actual)
    run = install_run(monkeypatch, owned, content)
    assert run(observer.public_goal(1)) == expected
    row = json.loads(owned.read_text(encoding="utf-8"))
    assert row["redacted_raw_draft"] == content and row["redacted_bound_draft"] == expected_bound
    assert row["guard_errors"] == expected[1] == []
    assert row["raw_sha256"] != row["bound_sha256"]
    assert len(row["public_source_records"][0]["records"]) == 1
    assert "must-not-be-captured" not in owned.read_text(encoding="utf-8")


@pytest.mark.parametrize("goal,path,content", [
    ("Private other task", "trial-1-research.md", "CVE-2021-44228"),
    (observer.public_goal(1) + " extra", "trial-1-research.md", "CVE-2021-44228"),
    (observer.public_goal(1), "private.md", "CVE-2021-44228"),
    (observer.public_goal(1), "trial-2-research.md", "CVE-2021-44228"),
    (observer.public_goal(1), "trial-1-research.md", "Unrelated subject"),
])
def test_unrelated_request_path_or_subject_never_captured(owned, monkeypatch, goal, path, content):
    install_run(monkeypatch, owned, content, path)(goal)
    assert not owned.exists()


def test_observer_failure_leaves_guard_verdict_unchanged(owned, monkeypatch):
    run = install_run(monkeypatch, owned, "CVE-2021-44228\n\nObserved date: 2021-12-10")
    monkeypatch.setattr(observer, "write_record", lambda *args: (_ for _ in ()).throw(RuntimeError("observer failed")))
    _, errors = run(observer.public_goal(1))
    assert errors == ["An exact technical claim has no adjacent retrieved-source citation"]
    assert not owned.exists()


def test_bounded_capture_redacts_secret_shape_but_does_not_change_draft(owned, monkeypatch):
    content = "CVE-2021-44228 sk-or-v1-example123 " + "x" * 30000
    run = install_run(monkeypatch, owned, content)
    assert run(observer.public_goal(1))[0] == content
    row = json.loads(owned.read_text(encoding="utf-8"))
    assert row["capture_truncated"] and row["raw_chars"] == len(content)
    assert "sk-or-v1-example123" not in row["redacted_raw_draft"]
    assert len(row["redacted_raw_draft"]) <= 24000


def test_followup_exact_scope_and_actual_public_pages_only(owned, monkeypatch):
    evidence = [source(), {"evidence_kind": "page_excerpt", "url": "https://logging.apache.org/security.html",
                           "page_text": "CVE-2021-44228 observed advisory"},
                {"evidence_kind": "page_excerpt", "url": "http://127.0.0.1/private", "page_text": "private"}]
    run = install_run(monkeypatch, owned, "CVE-2021-44228", "trial-1-follow-up.md", evidence)
    run(observer.follow_up_goal(1))
    row = json.loads(owned.read_text(encoding="utf-8"))
    assert len(row["public_page_records"]) == 1
    assert row["public_page_records"][0]["page_text"] == "CVE-2021-44228 observed advisory"
    assert "127.0.0.1" not in owned.read_text(encoding="utf-8")


def test_exact_public_followup_without_literal_cve_is_observed(owned, monkeypatch):
    run = install_run(monkeypatch, owned, "Log4Shell mitigation guidance", "trial-1-follow-up.md", [])
    run(observer.follow_up_goal(1))
    row = json.loads(owned.read_text(encoding="utf-8"))
    assert row["redacted_raw_draft"] == "Log4Shell mitigation guidance"


def test_followup_scope_not_extended_to_arbitrary_goal_or_path(owned, monkeypatch):
    run = install_run(monkeypatch, owned, "Unrelated", "private.md", [])
    run(observer.follow_up_goal(1))
    assert not owned.exists()


@pytest.mark.parametrize("mutation", [
    {"url": "https://user:password@www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"},
    {"url": URL + "?secret=private"}, {"url": URL.replace("www.cisa.gov", "localhost")},
    {"url": URL.replace("www.cisa.gov", "www.cisa.gov:invalid")},
    {"evidence_kind": "page_excerpt"}, {"page_text": "not json"},
])
def test_unrecognized_or_private_source_not_promoted(mutation):
    assert observer.public_records([{**source(), **mutation}]) == []


@pytest.mark.parametrize("change", ["budget", "runtime", "destination"])
def test_install_requires_owned_opt_in(owned, monkeypatch, change):
    if change == "budget": monkeypatch.delenv("WISE_QA_BUDGET_PATH")
    elif change == "runtime": monkeypatch.setenv("WISE_RUNTIME_ROOT", str(owned.parent / "runtime"))
    else: owned = owned.with_suffix(".json")
    with pytest.raises(ValueError): observer.install(owned)


def test_bandit_capture_uses_owned_fixture_counts_and_hash_only(tmp_path):
    runtime = tmp_path / "project-ui-runtime-fixture"
    target = runtime / "workspace/attachments/1791122451086669600-sample.py"
    target.parent.mkdir(parents=True)
    target.write_text("SENSITIVE_FIXTURE_BODY", encoding="utf-8")
    data = {"success": True, "output": {"scanner": "bandit", "offline": True, "finding_count": 0,
            "files_submitted": 1, "files_skipped": 0, "truncated": False,
            "findings": [{"line": 3, "code": "DO_NOT_CAPTURE", "filename": "PRIVATE_PATH"}]}}
    result = SimpleNamespace(to_dict=lambda: data)
    row = observer.scanner_observation("native.security_scan", {"scanner": "bandit", "path": str(target)}, result, runtime)
    assert row["scan_status"] == "COMPLETED_ZERO_FINDINGS" and row["finding_count"] == 0
    assert len(row["result_sha256"]) == 64
    assert not {"findings", "path", "line", "code"}.intersection(row)
    assert "PRIVATE_PATH" not in json.dumps(row) and "DO_NOT_CAPTURE" not in json.dumps(row)
    assert observer.scanner_observation("native.security_scan", {"scanner": "detect-secrets", "path": str(target)}, result, runtime) is None
    assert observer.scanner_observation("intelligence.bandit", {"path": "."}, result, runtime) is None
    assert observer.scanner_observation("intelligence.bandit", {"path": str(target).replace("sample.py", "private.py")}, result, runtime) is None


def test_bandit_absent_counts_are_unknown_not_fake_clean(tmp_path):
    runtime = tmp_path / "project-ui-runtime-fixture"
    target = runtime / "workspace/attachments/1791122451086669600-sample.py"
    target.parent.mkdir(parents=True)
    target.write_text("synthetic", encoding="utf-8")
    result = SimpleNamespace(to_dict=lambda: {"success": True, "output": {"scanner": "bandit"}})
    row = observer.scanner_observation("intelligence.bandit", {"path": str(target)}, result, runtime)
    assert row["finding_count"] is None and row["scan_status"] == "UNVERIFIED_SCHEMA"
