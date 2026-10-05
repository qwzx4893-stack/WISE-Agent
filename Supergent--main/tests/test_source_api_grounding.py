"""Structured source data is evidence, not an exemption from claim grounding."""
import json
from types import SimpleNamespace

import pytest

from core.web_research import validate_grounded_answer

URL = "https://api.publisher.example/records?cve=CVE-2021-44228"
REFERENCE = "https://advisory.example/unfetched"
DATA = {"records": [{"cveID": "CVE-2021-44228", "dateAdded": "2021-12-10", "notes": REFERENCE}],
        "provenance": {"source_url": URL, "retrieved_at": "2026-10-03T12:00:00Z"}}


def evidence(data=DATA):
    return [{"url": URL, "evidence_kind": "source_api_data", "page_text": json.dumps(data)}]


def test_actual_json_reference_is_not_promoted_to_fetched_advisory_support():
    assert not validate_grounded_answer(json.dumps(DATA), evidence())
    assert validate_grounded_answer(f"Upgrade to 2.15.0 [advisory]({REFERENCE})", evidence())
    assert validate_grounded_answer(f"Upgrade to 2.15.0 [dataset]({URL})", evidence())


def test_json_quotes_fragments_and_filtered_api_base_citations_are_supported():
    answer = json.dumps({"dateAdded": "2021-12-10", "source": "https://api.publisher.example/records#records"})
    assert not validate_grounded_answer(answer, evidence())
    assert validate_grounded_answer(answer.replace("2021-12-10", "2026-01-01"), evidence())
    assert validate_grounded_answer(answer.replace("api.publisher.example", "invented.example"), evidence())


@pytest.mark.parametrize("unsupported", [True, False])
def test_source_only_agent_checks_write_against_actual_records(monkeypatch, tmp_path, unsupported):
    from core.brain.capability_agent import CapabilityAgent
    from core.capability_router import CapabilityDescriptor
    from core.contracts import CapabilitySource
    from core.models.provider_interface import ModelCompletionResponse
    from core.skills.indexer import SkillIndexer
    monkeypatch.setattr(SkillIndexer, "get_index", lambda self: {})
    tools = [CapabilityDescriptor(name, name, CapabilitySource.NATIVE, name) for name in
             ("source.fixture", "native.write_file", "native.read_file")]
    calls = []
    def execute(tool, args, **kwargs):
        calls.append(tool)
        if tool == "source.fixture":
            output = DATA
        elif tool == "native.write_file":
            (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
            output = "saved"
        else:
            output = (tmp_path / args["path"]).read_text(encoding="utf-8")
        return SimpleNamespace(success=True, to_dict=lambda: {"success": True, "output": output})
    content = {**DATA, **({"patched_version": "2.15.0"} if unsupported else {})}
    actions = iter([{"tool": "source.fixture", "arguments": {}},
                    {"tool": "native.write_file", "arguments": {"path": "report.json", "content": json.dumps(content)}},
                    {"answer": "I created report.json."}])
    def generate(request):
        action = next(actions)
        return ModelCompletionResponse(json.dumps(action), parsed_json=action, model_name="offline")
    outcome = CapabilityAgent(SimpleNamespace(generate=generate),
        router=SimpleNamespace(list_capabilities=lambda: tools, execute=execute), max_steps=3).run(
            "Research with source.fixture and save report.json", session_id="structured-sources")
    assert (tmp_path / "report.json").exists() is not unsupported
    if unsupported:
        assert outcome.error and outcome.metrics["outcome_obligations"]["failed_artifacts"] == ["report.json"]
        assert "I created" not in outcome.answer and calls == ["source.fixture"]
    else:
        assert not outcome.error and outcome.metrics["outcome_obligations"]["artifact_obligations_met"]
        assert calls == ["source.fixture", "native.write_file", "native.read_file"]
