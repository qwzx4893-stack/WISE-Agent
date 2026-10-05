"""Focused actual-page evidence contracts; no live provider/network calls."""
import json
from types import SimpleNamespace

import pytest

from core import web_research

URL = "https://publisher.example/security"
CVE = "CVE-2021-44228"


def source_records(cve=CVE, date="2021-12-10"):
    return [{"url": URL, "evidence_kind": "source_api_data", "page_text": json.dumps({
        "resource_id": "cisa-known-exploited-vulnerabilities",
        "records": [{"cveID": cve, "dateAdded": date}]})}]


def test_request_without_literal_cve_can_bind_one_explicit_body_subject_to_exact_record():
    body = f"## {CVE}\n\ndateAdded: 2021-12-10"
    actual = web_research.attach_observed_record_date_citations(body, source_records(),
                "Research the first CVE from the analysis file", "research.md")
    assert URL in actual
    assert not web_research.validate_grounded_answer(actual, source_records())


@pytest.mark.parametrize("body", [
    "dateAdded: 2021-12-10",  # never infer a CVE from the date alone
    f"## {CVE} and CVE-2021-45046\n\ndateAdded: 2021-12-10",
    f"## {CVE}\n\ndateAdded: 2026-01-01",  # not observed in the actual record
    "## CVE-2021-45046\n\ndateAdded: 2021-12-10",  # same date, other subject
])
def test_no_literal_request_never_binds_ambiguous_missing_or_mismatched_subject(body):
    assert web_research.attach_observed_record_date_citations(body, source_records(),
             "Research the first CVE from the analysis file", "research.md") == body


def test_literal_request_subject_still_overrides_conflicting_body_subject():
    body = "## CVE-2021-45046\n\ndateAdded: 2021-12-10"
    assert web_research.attach_observed_record_date_citations(body, source_records("CVE-2021-45046"),
             f"Research {CVE}", "research.md") == body


def fake_document(monkeypatch, html):
    import httpx
    payload = html.encode()
    class Response:
        is_redirect = False
        headers = {"content-type": "text/html"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_bytes(self): yield payload
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, method, url, **kwargs):
            assert method == "GET" and url == URL
            return Response()
    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(web_research, "_public_url", lambda value: value)


def test_focused_evidence_prefers_actual_later_section_over_table_of_contents(monkeypatch):
    marker = "Observed fixture mitigation tied to the requested section."
    html = (f"<main><p>Contents: {CVE}</p><h2>CVE-2026-12345</h2><p>"
            + "Earlier unrelated fixture text. " * 400
            + f"</p><h2>{CVE}</h2><p>{marker}</p><h2>CVE-2021-45046</h2>"
            + "<p>Different fixture mitigation; preserve its separate subject.</p></main>")
    fake_document(monkeypatch, html)
    baseline = web_research.read_web_evidence(URL)
    assert marker not in baseline["page_text"] and baseline["excerpt_truncated"]
    focused = web_research.read_web_evidence(URL, focus=CVE)
    assert marker in focused["page_text"] and CVE in focused["page_text"]
    assert len(focused["page_text"]) <= 7000
    assert focused["evidence_kind"] == "page_excerpt" and focused["resolved_url"] == URL
    assert focused["retrieved_at"] and focused["excerpt_truncated"]
    assert focused["focus_match"] is True


def test_missing_focus_reports_limit_and_preserves_first_excerpt(monkeypatch):
    fake_document(monkeypatch, "<main><h2>Other subject</h2><p>" + "Fixture text. " * 1000 + "</p></main>")
    baseline = web_research.read_web_evidence(URL)
    focused = web_research.read_web_evidence(URL, focus=CVE)
    assert focused["page_text"] == baseline["page_text"]
    assert focused["focus_match"] is False and focused["excerpt_truncated"]


@pytest.mark.parametrize("focus", ["", " ", "x" * 161, "a\nb", "a\x00b", 123, []])
def test_invalid_focus_is_rejected_before_fetch(monkeypatch, focus):
    def unexpected(_):
        raise AssertionError("No fetch/URL resolution is allowed")
    monkeypatch.setattr(web_research, "_public_url", unexpected)
    with pytest.raises(ValueError, match="focus"):
        web_research.read_web_evidence(URL, focus=focus)


def test_focus_section_excludes_next_peer_subject_and_uses_nested_headings(monkeypatch):
    fake_document(monkeypatch, "<main><h4>Requested subject</h4><p>Actual observation.</p>"
                  "<h5>Subsection</h5><p>Its supported detail.</p>"
                  "<h4>Different subject</h4><p>Not supporting our subject.</p></main>")
    result = web_research.read_web_evidence(URL, focus="Requested subject")
    assert result["focus_match"] is True
    assert "Its supported detail" in result["page_text"]
    assert "Not supporting our subject" not in result["page_text"]


def test_focus_is_literal_not_model_supplied_regex(monkeypatch):
    fake_document(monkeypatch, "<main><h2>Normal subject</h2><p>Actual observation.</p></main>")
    result = web_research.read_web_evidence(URL, focus=".*")
    assert result["focus_match"] is False


def test_duplicate_cve_case_is_one_explicit_subject():
    body = f"## {CVE}\n\ndateAdded: 2021-12-10"
    result = web_research.attach_observed_record_date_citations(body, source_records(),
                f"Research {CVE} and {CVE.lower()}", "research.md")
    assert URL in result
