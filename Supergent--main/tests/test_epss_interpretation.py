"""Offline contract/guard evidence; not independent model-quality acceptance."""
import copy
import json

import pytest

from core.epss_evidence import interpretation_contract, validate_epss_interpretation
from core.web_research import validate_grounded_answer

URL = "https://api.first.org/data/v1/epss?cve=CVE-2021-44228"
PAYLOAD = {"resource_id": "first-epss", "records": [
    {"cve": "CVE-2021-44228", "epss": "0.999990000", "percentile": "1.000000000", "date": "2026-10-04"}]}


def evidence(payload=PAYLOAD, url=URL):
    return [{"url": url, "evidence_kind": "source_api_data", "page_text": json.dumps(payload)}]


@pytest.mark.parametrize("text", [
    "EPSS score of 0.99999 indicates near-certain exploitation potential.",
    "EPSS means a 99.999% chance that your laptop will be compromised.",
    "EPSS predicts exploitation in the next 365 days.",
    "EPSS estimates the probability that an attack happened yesterday.",
    "EPSS is the probability that an attack happened yesterday.",
    "EPSS يشير إلى احتمال الاستغلال شبه مؤكد.",
    "## FIRST EPSS\n\nPredicted exploitation probability: extremely high, near-certain.",
])
def test_observed_problem_interpretations_are_rejected(text):
    assert validate_epss_interpretation(text, evidence())
    assert validate_grounded_answer(text, evidence())


@pytest.mark.parametrize("text", [
    "EPSS score: 0.999990000; percentile: 1.000000000.",
    "EPSS is an uncertain model estimate of exploitation of the CVE in the wild in the next 30 days, not a guarantee.",
    "EPSS estimates real-world exploitation over a 30-day forecast window; this is not proof of device compromise or a guarantee.",
    "EPSS تقدير لاحتمال استغلال الثغرة في الواقع خلال ٣٠ يوماً، وليس ضماناً لاختراق جهاز معين.",
    "## FIRST EPSS\n\nScore: 0.999990000\n\n## CISA KEV\n\nKnown exploitation indicates observed attacks.",
    "EPSS is not a guarantee of device compromise.",
    "EPSS is an uncertain model estimate of exploitation in the wild in the next 30 days, not a guarantee. Do not describe it as near-certain exploitation.",
    "EPSS is an uncertain model estimate of exploitation in the wild in the next 30 days; exploitation is not guaranteed.",
    "## FIRST EPSS and CISA findings\n\nCISA indicates known exploitation of a CVE.",
])
def test_numeric_reporting_and_qualified_interpretations_remain_allowed(text):
    assert not validate_epss_interpretation(text, evidence())


@pytest.mark.parametrize("url,payload,kind", [
    ("https://api.first.org.evil.example/data/v1/epss", PAYLOAD, "source_api_data"),
    ("http://api.first.org/data/v1/epss", PAYLOAD, "source_api_data"),
    ("https://api.first.org/other", PAYLOAD, "source_api_data"),
    (URL, {**PAYLOAD, "resource_id": "third-party"}, "source_api_data"),
    (URL, {**PAYLOAD, "records": []}, "source_api_data"),
    (URL, PAYLOAD, "page_excerpt"),
])
def test_no_activation_from_request_keywords_or_unrelated_sources(url, payload, kind):
    rows = evidence(payload, url)
    rows[0]["evidence_kind"] = kind
    assert not validate_epss_interpretation("EPSS indicates near-certain exploitation.", rows)
    assert not validate_epss_interpretation("EPSS indicates near-certain exploitation.", [])


def test_adapter_preserves_raw_observations_and_labels_definition_metadata(monkeypatch):
    from core.intelligence import sources
    raw = {"data": copy.deepcopy(PAYLOAD["records"]), "total": 1}
    meta = {"source_url": URL, "content_sha256": "raw-http-digest", "retrieved_at": "actual time"}
    original = copy.deepcopy(raw)
    monkeypatch.setattr(sources, "_json", lambda *args, **kwargs: (raw, meta))
    result = sources.execute_source("first-epss", action="query", query="CVE-2021-44228")
    assert raw == original and result["records"] == original["data"] and result["provenance"] == meta
    assert result["interpretation"]["kind"] == "adapter_contract_not_live_definition_fetch"
    assert result["interpretation"]["forecast_window_days"] == 30
    assert "not the percentile" in result["interpretation"]["score_unit"]
    assert "not exploitation probability" in result["interpretation"]["percentile_unit"]
    changed = interpretation_contract()
    changed["forecast_window_days"] = 99
    assert interpretation_contract()["forecast_window_days"] == 30


def test_ordinary_source_has_no_epss_metadata(monkeypatch):
    from core.intelligence import sources
    monkeypatch.setattr(sources, "_json", lambda *args, **kwargs: ({"search": []}, {}))
    assert "interpretation" not in sources.execute_source("wikidata", action="query", query="fixture")


def test_correct_definition_does_not_launder_wrong_device_claim():
    text = ("EPSS is an uncertain model estimate of CVE exploitation in the wild in the next 30 days, not a guarantee. "
            "This also means a 99.999% chance your laptop gets hacked tomorrow.")
    assert validate_epss_interpretation(text, evidence())


@pytest.mark.parametrize("unsafe", [False, True])
def test_real_agent_boundary_checks_interpretation_before_write(monkeypatch, tmp_path, unsafe):
    from types import SimpleNamespace
    from core.brain.capability_agent import CapabilityAgent
    from core.capability_router import CapabilityDescriptor
    from core.contracts import CapabilitySource
    from core.models.provider_interface import ModelCompletionResponse
    from core.skills.indexer import SkillIndexer

    monkeypatch.setattr(SkillIndexer, "get_index", lambda self: {})
    payload = {**PAYLOAD, "interpretation": interpretation_contract(), "provenance": {"source_url": URL}}
    tools = [CapabilityDescriptor(name, name, CapabilitySource.NATIVE, name) for name in
             ("source.first-epss", "native.write_file", "native.read_file")]
    calls = []
    text = ("EPSS indicates near-certain exploitation potential." if unsafe else
            "EPSS is an uncertain model estimate of CVE exploitation in the wild in the next 30 days, not a guarantee.")
    actions = iter([{"tool": "source.first-epss", "arguments": {"action": "query", "query": "CVE-2021-44228"}},
                    {"tool": "native.write_file", "arguments": {"path": "report.md", "content": text}},
                    {"answer": "Created report.md"}])
    def execute(tool, args, **kwargs):
        calls.append(tool)
        if tool == "source.first-epss":
            output = payload
        elif tool == "native.write_file":
            (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
            output = "saved"
        else:
            output = (tmp_path / args["path"]).read_text(encoding="utf-8")
        return SimpleNamespace(success=True, to_dict=lambda: {"success": True, "output": output})
    def generate(request):
        action = next(actions)
        return ModelCompletionResponse(json.dumps(action), parsed_json=action, model_name="offline")
    outcome = CapabilityAgent(SimpleNamespace(generate=generate), router=SimpleNamespace(
        list_capabilities=lambda: tools, execute=execute), max_steps=3).run(
            "Research with FIRST EPSS and save report.md", session_id="epss-boundary")
    assert (tmp_path / "report.md").exists() is not unsafe
    if unsafe:
        assert outcome.error and "Created" not in outcome.answer
        assert "native.write_file" not in calls
    else:
        assert not outcome.error and outcome.metrics["outcome_obligations"]["artifact_obligations_met"]
        assert calls == ["source.first-epss", "native.write_file", "native.read_file"]


def test_actual_repaired_draft_explicit_epss_notes_qualify_same_cve():
    # Observed public draft at project-ui-20261004T183437Z, independently
    # reviewed as a formatting false positive. No alternative CVE or new facts.
    text = """# CVE-2021-44228 Analysis

## Exploitation Probability (FIRST EPSS)
- **EPSS Score**: 0.999990000
- **Interpretation**: Model-estimated probability of exploitation within 30 days in the wild (FIRST EPSS forecast window)

## Notes
- The EPSS score represents a model-estimated probability of exploitation within 30 days in the wild, not a guarantee or device-compromise probability
- Known exploitation (KEV) indicates actual exploitation has occurred
- Predicted exploitation probability (EPSS) is a forecast based on machine learning models with uncertainty
"""
    assert not validate_epss_interpretation(text, evidence())


def test_heading_and_numeric_fields_alone_are_not_a_probability_explanation():
    assert not validate_epss_interpretation("## FIRST Exploit Prediction Scoring System (EPSS)\n\n- EPSS Score: 0.999990000", evidence())


def test_forecast_subordinate_will_is_not_a_guarantee():
    text = "EPSS estimates the probability that a CVE will be exploited in the wild within 30 days; this is not a guarantee."
    assert not validate_epss_interpretation(text, evidence())


def test_notes_cannot_qualify_another_cve():
    text = ("## CVE-2021-44228 FIRST EPSS\n\nEPSS is an uncertain model estimate of exploitation in the wild within 30 days, not a guarantee.\n\n"
            "## CVE-2022-12345 FIRST EPSS\n\nEPSS indicates high exploitation probability.")
    assert validate_epss_interpretation(text, evidence())


def test_cve_case_does_not_split_same_subject_notes():
    text = ("## CVE-2021-44228 FIRST EPSS\n\nEPSS indicates high exploitation probability.\n\n"
            "## Notes for cve-2021-44228\n\nEPSS is an uncertain model estimate of exploitation in the wild within 30 days, not a guarantee.")
    assert not validate_epss_interpretation(text, evidence())


def test_unqualified_will_remains_rejected_with_general_notes():
    text = ("EPSS is an uncertain model estimate of exploitation in the wild within 30 days, not a guarantee.\n\n"
            "EPSS indicates that this CVE will be exploited tomorrow.")
    assert validate_epss_interpretation(text, evidence())


@pytest.mark.parametrize("bad", [
    "EPSS is the probability that an attack happened yesterday.",
    "EPSS indicates near-certain exploitation.",
    "EPSS predicts exploitation in the next 365 days.",
    "EPSS means a 99% chance your computer is compromised tomorrow.",
])
def test_correct_general_definition_does_not_qualify_contradictory_claim(bad):
    correct = "EPSS is an uncertain model estimate of CVE exploitation in the wild in the next 30 days, not a guarantee."
    assert validate_epss_interpretation(correct + "\n\n" + bad, evidence())
