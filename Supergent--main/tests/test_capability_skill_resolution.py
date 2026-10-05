"""Offline resolution and correction contracts; no paid model requests."""
import json
from types import SimpleNamespace

import pytest

from core.brain.capability_agent import CapabilityAgent, _resolve_indexed_skill
from core.capability_network import CapabilityNetwork
from core.models.provider_interface import ModelCompletionResponse


@pytest.fixture
def loop(monkeypatch, tmp_path):
    import core.capability_network as network
    import core.paths as paths
    import core.skills.indexer as skills
    import core.skills.lifecycle as lifecycle

    root = tmp_path / "skills"
    root.mkdir()
    folder = root / "code-review-checklist" / "1.0.0"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("Inspect authorized code and verify edits.", encoding="utf-8")
    index = {"code-review-checklist": {"path": str(folder), "category": "general"}}
    monkeypatch.setattr(paths, "SKILLS_DIR", root)
    monkeypatch.setattr(skills.SkillIndexer, "get_index", lambda self: index)
    monkeypatch.setattr(lifecycle, "LIFECYCLE_FILE", tmp_path / "lifecycle.json")
    candidate = CapabilityNetwork._score_skill(set(), [], "code-review-checklist", index["code-review-checklist"])
    plan = SimpleNamespace(capabilities=[], skills=[candidate], to_dict=lambda: {"skills": [candidate.to_dict()]})
    monkeypatch.setattr(network, "get_capability_network", lambda: SimpleNamespace(plan=lambda *a, **k: plan))

    class Router:
        def __init__(self):
            self.calls = []
            self.capabilities = [SimpleNamespace(id=name, name=name, description=name,
                input_schema={}, availability=True) for name in ("native.web_fetch", "native.read_file")]

        def list_capabilities(self):
            return self.capabilities

        def execute(self, tool, args, **kwargs):
            self.calls.append(tool)
            output = {"resolved_url": "https://example.org/advisory", "text": "Observed source text."}
            return SimpleNamespace(success=True, to_dict=lambda: {"success": True, "output": output})

    class Provider:
        def __init__(self, actions):
            self.actions = iter(actions)
            self.requests = []

        def generate(self, request):
            self.requests.append(request)
            action = next(self.actions)
            return ModelCompletionResponse(text=json.dumps(action), parsed_json=action, model_name="offline")

    return SimpleNamespace(root=root, folder=folder, index=index, candidate=candidate,
                           Router=Router, Provider=Provider, lifecycle=tmp_path / "lifecycle.json")


@pytest.mark.parametrize("reference", ["code-review-checklist", "skill.code-review-checklist"])
def test_indexed_name_and_advertised_id_load_same_canonical_procedure(loop, reference):
    assert loop.candidate.id == "skill.code-review-checklist"
    assert loop.candidate.name == "code-review-checklist"
    # Reproduces the former index.get(name) protocol mismatch: the emitted
    # discovery ID was never an index key, although its canonical name was.
    assert loop.candidate.id not in loop.index
    assert loop.candidate.name in loop.index
    provider = loop.Provider([{"tool": "discover_tools", "arguments": {"query": "code review"}},
                             {"tool": "read_skill", "arguments": {"name": reference}},
                             {"answer": "Read the actual procedure."}])
    result = CapabilityAgent(provider, router=loop.Router(), max_steps=3).run(
        "Read the indexed code-review-checklist skill", session_id="skill-id")
    assert not result.error
    assert result.metrics["skills_read"] == ["code-review-checklist"]
    assert result.metrics["skill_obligations"]["satisfied"]
    assert set(result.metrics["skill_provenance"]) == {"code-review-checklist"}
    assert '"id": "skill.code-review-checklist"' in provider.requests[1].messages[-1]["content"]


def test_indexer_classifies_category_version_layout_as_exact_name(loop):
    from core.skills.indexer import SkillIndexer
    indexer = object.__new__(SkillIndexer)
    indexer.skills_dir = loop.root
    name, version, folder, category = indexer._classify_skill(loop.folder / "SKILL.md")
    assert (name, version, folder) == ("code-review-checklist", "1.0.0", loop.folder)


def test_literal_prefixed_name_wins_over_discovery_alias():
    index = {"foo": {"path": "a"}, "skill.foo": {"path": "b"}}
    assert _resolve_indexed_skill("skill.foo", index) == ("skill.foo", index["skill.foo"])


@pytest.mark.parametrize("reference", [None, [], {}, 1, "load_skill", "skill.absent", "../code-review-checklist", "CODE-REVIEW-CHECKLIST"])
def test_invalid_or_unindexed_reference_fails_without_loading(loop, reference):
    result = CapabilityAgent(loop.Provider([{"tool": "read_skill", "arguments": {"name": reference}},
        {"answer": "Unable to load."}]), router=loop.Router(), max_steps=2).run("Inspect code", session_id="invalid")
    assert result.calls[0]["success"] is False
    assert result.metrics["skills_read"] == []


@pytest.mark.parametrize("state", ["disabled", "deprecated", "quarantined"])
@pytest.mark.parametrize("reference", ["code-review-checklist", "skill.code-review-checklist"])
def test_alias_cannot_load_ineligible_skill(loop, state, reference):
    loop.lifecycle.write_text(json.dumps({"code-review-checklist": {"state": state}}), encoding="utf-8")
    result = CapabilityAgent(loop.Provider([{"tool": "read_skill", "arguments": {"name": reference}},
        {"answer": "Unable to load."}]), router=loop.Router(), max_steps=2).run("Inspect code", session_id="eligibility")
    assert result.calls[0]["success"] is False
    assert "Skill not indexed" in result.calls[0]["error"]
    assert result.metrics["skills_read"] == []


@pytest.mark.parametrize("lifecycle_text", ["invalid JSON", "[]", '{"code-review-checklist": "active"}'])
def test_alias_cannot_bypass_corrupt_eligibility_data(loop, lifecycle_text):
    loop.lifecycle.write_text(lifecycle_text, encoding="utf-8")
    result = CapabilityAgent(loop.Provider([{"tool": "read_skill", "arguments": {"name": loop.candidate.id}},
        {"answer": "Unable to load."}]), router=loop.Router(), max_steps=2).run("Inspect code", session_id="corrupt")
    assert "Skill not indexed" in result.calls[0]["error"]
    assert result.metrics["skills_read"] == []


@pytest.mark.parametrize("boundary", ["outside", "oversized"])
def test_alias_preserves_skill_file_boundary(loop, tmp_path, boundary):
    if boundary == "outside":
        folder = tmp_path / "outside"
        folder.mkdir()
        (folder / "SKILL.md").write_text("Unapproved outside content", encoding="utf-8")
        loop.index["code-review-checklist"]["path"] = str(folder)
        expected = "outside the indexed skill directory"
    else:
        (loop.folder / "SKILL.md").write_text("x" * 80001, encoding="utf-8")
        expected = "exceeds context limit"
    result = CapabilityAgent(loop.Provider([{"tool": "read_skill", "arguments": {"name": loop.candidate.id}},
        {"answer": "Unable to load."}]), router=loop.Router(), max_steps=2).run("Inspect code", session_id="boundary")
    assert expected in result.calls[0]["error"]
    assert result.metrics["skills_read"] == []


@pytest.mark.parametrize("replacement", [{"answer": "Grounded correction"}, {"answer": "Unsafe claim"},
                                       {"tool": "native.read_file", "arguments": {"path": "x"}}, []])
def test_answer_correction_is_one_answer_only_turn_within_budget(loop, monkeypatch, replacement):
    import core.web_research as research
    monkeypatch.setattr(research, "validate_grounded_answer",
                        lambda answer, evidence: ["unsupported"] if answer == "Unsafe claim" else [])
    router = loop.Router()
    provider = loop.Provider([{"tool": "native.web_fetch", "arguments": {"url": "https://example.org/advisory"}},
                              {"answer": "Unsafe claim"}, replacement])
    result = CapabilityAgent(provider, router=router, max_steps=5).run("Research advisory", session_id="correct")
    assert len(provider.requests) == 3
    assert result.metrics["model_requests"] == 3
    assert router.calls == ["native.web_fetch"]
    assert provider.requests[-1].json_schema["required"] == ["answer"]
    assert "Available tools:" not in provider.requests[-1].system_prompt
    if replacement == {"answer": "Grounded correction"}:
        assert not result.error and result.answer == "Grounded correction"
    else:
        assert result.error


def test_unsupported_answer_on_last_turn_does_not_exceed_budget(loop, monkeypatch):
    import core.web_research as research
    monkeypatch.setattr(research, "validate_grounded_answer", lambda answer, evidence: ["unsupported"])
    provider = loop.Provider([{"tool": "native.web_fetch", "arguments": {}}, {"answer": "Unsafe claim"}])
    result = CapabilityAgent(provider, router=loop.Router(), max_steps=2).run("Research advisory", session_id="last")
    assert len(provider.requests) == 2
    assert result.error == "unsupported"
