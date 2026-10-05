import json
from types import SimpleNamespace

import pytest

from core.web_research import (attach_requested_source_receipts, prioritize_evidence,
                              attach_observed_record_date_citations, validate_grounded_answer)


URL = "https://api.publisher.example/records"
TIME = "2026-10-03T12:00:00Z"
EVIDENCE = [{"url": URL, "evidence_kind": "source_api_data", "retrieved_at": TIME}]


def test_requested_dates_use_actual_observation_and_are_idempotent():
    content, errors = attach_requested_source_receipts("Observed facts", EVIDENCE, "Include retrieval dates", "report.md")
    assert not errors and URL in content and TIME in content
    assert "not source publication dates" in content
    assert attach_requested_source_receipts(content, EVIDENCE, "Include retrieval dates", "report.md")[0] == content
    assert attach_requested_source_receipts("Facts", EVIDENCE, "أضف تاريخ الجلب", "report.txt")[0].endswith("\n")


def test_mcp_receipt_uses_actual_observation_without_claiming_independent_fetch():
    records = [{**EVIDENCE[0], "evidence_kind": "mcp_document_excerpt"}]
    content, errors = attach_requested_source_receipts("Observed document", records,
                                                       "Include retrieval dates", "report.md")
    assert not errors and URL in content and TIME in content
    assert "not independently fetched" in content
    assert attach_requested_source_receipts(content, records, "Include retrieval dates", "report.md")[0] == content


@pytest.mark.parametrize("mutation", [
    {"evidence_kind": "search_snippet"}, {"retrieved_at": None}, {"retrieved_at": "not a timestamp"},
    {"retrieved_at": "2026-10-03"}, {"url": "https://user:password@publisher.example"},
    {"url": "javascript:invalid"}, {"url": "https://publisher.example/\nmalformed"},
])
def test_receipt_never_invents_dates_from_snippets_or_invalid_metadata(mutation):
    content, errors = attach_requested_source_receipts("Facts", [{**EVIDENCE[0], **mutation}], "retrieval timestamps", "report.md")
    assert content == "Facts" and errors


def test_receipts_do_not_reformat_code_json_or_unrequested_prose():
    for request, path in (("retrieval dates", "report.json"), ("retrieval dates", "script.py"), ("Facts", "report.md")):
        assert attach_requested_source_receipts("original", EVIDENCE, request, path) == ("original", [])


def test_actual_current_catalog_gets_bounded_fetch_priority_not_snippet_only():
    catalog = {"url": "https://deepmind.google/models/gemini/"}
    historical = {"url": "https://blog.google/news/old-gemini-release/"}
    generic = {"url": "https://community.example/gemini-history"}
    ranked = prioritize_evidence([historical, generic, catalog], "ما أحدث نموذج جيمناي؟")
    assert ranked[0] == catalog and set(row["url"] for row in ranked) == {row["url"] for row in (catalog, historical, generic)}


def test_agent_saves_and_verifies_actual_receipts_not_just_response(monkeypatch, tmp_path):
    from core.brain.capability_agent import CapabilityAgent
    from core.capability_router import CapabilityDescriptor
    from core.contracts import CapabilitySource
    from core.models.provider_interface import ModelCompletionResponse
    from core.skills.indexer import SkillIndexer
    monkeypatch.setattr(SkillIndexer, "get_index", lambda self: {})
    tools = [CapabilityDescriptor(name, name, CapabilitySource.NATIVE, name) for name in
             ("source.fixture", "native.write_file", "native.read_file")]
    captured = []
    def execute(tool, args, **kwargs):
        if tool == "source.fixture":
            output = {"records": [{"name": "observed"}], "provenance": {"source_url": URL, "retrieved_at": TIME}}
        elif tool == "native.write_file":
            captured.append(args["content"])
            (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
            output = "saved"
        else:
            output = (tmp_path / args["path"]).read_text(encoding="utf-8")
        return SimpleNamespace(success=True, to_dict=lambda: {"success": True, "output": output})
    actions = iter([{"tool": "source.fixture", "arguments": {}},
                    {"tool": "native.write_file", "arguments": {"path": "report.md", "content": f"Observed facts [source]({URL})"}},
                    {"answer": "Created report.md."}])
    def generate(request):
        action = next(actions)
        return ModelCompletionResponse(json.dumps(action), parsed_json=action, model_name="offline")
    outcome = CapabilityAgent(SimpleNamespace(generate=generate),
        router=SimpleNamespace(list_capabilities=lambda: tools, execute=execute), max_steps=3).run(
            "Research with source.fixture and create report.md including retrieval dates", session_id="receipts")
    assert not outcome.error and TIME in captured[0]
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == captured[0]
    assert outcome.metrics["outcome_obligations"]["artifact_obligations_met"]


def record_evidence(cve="CVE-2021-44228", resource="cisa-known-exploited-vulnerabilities", date="2021-12-10"):
    payload = {"resource_id": resource, "records": [{"cveID": cve, "dateAdded": date, "date": date}]}
    return [{"url": URL, "evidence_kind": "source_api_data", "page_text": json.dumps(payload)}]


def test_actual_rejected_draft_format_gets_named_record_date_citation():
    content = "## Status\n- **CISA Added Date**: 2021-12-10\n\n## Sources\n" + URL
    bound = attach_observed_record_date_citations(content, record_evidence(), "Research CVE-2021-44228", "report.md")
    assert f"2021-12-10 [Source](<{URL}>)" in bound
    assert not validate_grounded_answer(bound, record_evidence())
    assert attach_observed_record_date_citations(bound, record_evidence(), "Research CVE-2021-44228", "report.md") == bound


@pytest.mark.parametrize("goal,body,evidence", [
    ("CVE-2021-44228", "Released 2021-12-10", record_evidence()),
    ("CVE-2021-44228", "CISA Added Date: 2021-12-11", record_evidence()),
    ("CVE-2021-44228", "CISA Added Date: 2021-12-10", record_evidence(cve="CVE-2021-45046")),
    ("CVE-2021-44228 and CVE-2021-45046", "CISA Added Date: 2021-12-10", record_evidence()),
    ("CVE-2021-44228", "CISA Added Date: 2021-12-10", record_evidence(resource="unknown")),
    ("CVE-2021-44228", "CVE-2021-45046 CISA Added Date: 2021-12-10", record_evidence()),
    ("CVE-2021-44228", "## CVE-2021-45046\n\n- CISA Added Date: 2021-12-10", record_evidence()),
])
def test_record_citation_binding_cannot_launder_other_subjects_or_claims(goal, body, evidence):
    assert attach_observed_record_date_citations(body, evidence, goal, "report.md") == body


def test_record_date_citation_does_not_make_unsupported_upgrade_pass():
    content = "CISA Added Date: 2021-12-10. Upgrade to 2.15.0."
    bound = attach_observed_record_date_citations(content, record_evidence(), "CVE-2021-44228", "report.md")
    assert validate_grounded_answer(bound, record_evidence())


def test_first_score_date_uses_actual_first_record_only():
    content = "- **Score Date**: 2026-10-03"
    data = record_evidence(resource="first-epss", date="2026-10-03")
    assert URL in attach_observed_record_date_citations(content, data, "CVE-2021-44228", "report.md")
    assert attach_observed_record_date_citations(content, record_evidence(), "CVE-2021-44228", "report.md") == content


@pytest.mark.parametrize("unsupported", [False, True])
def test_final_answer_uses_same_actual_field_binding_without_extra_calls(monkeypatch, unsupported):
    from core.brain.capability_agent import CapabilityAgent
    from core.capability_router import CapabilityDescriptor
    from core.contracts import CapabilitySource
    from core.models.provider_interface import ModelCompletionResponse
    from core.skills.indexer import SkillIndexer
    monkeypatch.setattr(SkillIndexer, "get_index", lambda self: {})
    tools = [CapabilityDescriptor("source.fixture", "source.fixture", CapabilitySource.NATIVE, "source.fixture")]
    payload = json.loads(record_evidence()[0]["page_text"])
    payload["provenance"] = {"source_url": URL, "retrieved_at": TIME}
    calls = []
    def execute(tool, args, **kwargs):
        return SimpleNamespace(success=True, to_dict=lambda: {"success": True, "output": payload})
    answer = "CISA Date Added: 2021-12-10" + (". Upgrade to 2.15.0." if unsupported else "")
    actions = iter([{"tool": "source.fixture", "arguments": {}}, {"answer": answer}])
    def generate(request):
        calls.append(request)
        action = next(actions)
        return ModelCompletionResponse(json.dumps(action), parsed_json=action, model_name="offline")
    outcome = CapabilityAgent(SimpleNamespace(generate=generate),
        router=SimpleNamespace(list_capabilities=lambda: tools, execute=execute), max_steps=2).run(
            "Research CVE-2021-44228 with source.fixture", session_id="final-record-citation")
    assert len(calls) == 2 and bool(outcome.error) is unsupported
    if not unsupported: assert URL in outcome.answer
