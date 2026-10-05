"""Failed writes are outcome obligations, even when no artifact was created."""
import json
from types import SimpleNamespace

import pytest

from core.brain.capability_agent import CapabilityAgent
from core.models.provider_interface import ModelCompletionResponse
from core.usage_network import UsageNetwork


def assess(calls):
    return UsageNetwork.assess_outcome(SimpleNamespace(phases=[]), calls)


def write(success, path="report.md"):
    return {"tool": "native.write_file", "path": path, "success": success}


def read(path="report.md"):
    return {"tool": "native.read_file", "path": path, "success": True, "content_verified": True}


def test_latest_attempt_controls_each_path():
    failed = assess([write(False)])
    assert not failed["artifact_obligations_met"] and failed["failed_artifacts"] == ["report.md"]
    later_failure = assess([write(True), read(), write(False), read()])
    assert not later_failure["artifact_obligations_met"] and later_failure["verified_artifacts"] == []
    retried = assess([write(False), write(True), read()])
    assert retried["artifact_obligations_met"] and retried["verified_artifacts"] == ["report.md"]
    assert not assess([write(False), write(True)])["artifact_obligations_met"]
    assert not assess([write(False), write(True, "other.md"), read("other.md")])["artifact_obligations_met"]


def test_cached_duplicate_is_not_a_new_write_attempt():
    cached_success = {**write(True), "duplicate_prevented": True}
    assert assess([write(True), read(), cached_success])["artifact_obligations_met"]
    cached_failure = {**write(False), "duplicate_prevented": True}
    assert not assess([write(False), cached_failure])["artifact_obligations_met"]
    assert not assess([write(False), cached_success])["artifact_obligations_met"]
    assert assess([write(True), read(), cached_failure])["artifact_obligations_met"]


@pytest.mark.parametrize("path", [None, "", {}, []])
def test_unidentified_failure_stays_unresolved_without_exposing_arguments(path):
    result = assess([write(False, path), write(True), read()])
    assert not result["artifact_obligations_met"]
    assert result["unidentified_write_attempts"] == 1
    assert result["failed_artifacts"] == []


@pytest.fixture
def loop(monkeypatch, tmp_path):
    import core.capability_network as network
    import core.skills.indexer as skills
    import core.web_research as research
    plan = SimpleNamespace(capabilities=[], skills=[])
    monkeypatch.setattr(network, "get_capability_network", lambda: SimpleNamespace(plan=lambda *a, **k: plan))
    monkeypatch.setattr(skills.SkillIndexer, "get_index", lambda self: {})
    monkeypatch.setattr(research, "validate_grounded_answer",
                        lambda text, evidence: ["unsupported research claim"] if text == "unsupported" else [])

    class Router:
        def __init__(self):
            self.calls = []
            self.capabilities = [SimpleNamespace(id=name, name=name, description=name,
                input_schema={}, availability=True) for name in
                ("native.web_fetch", "native.write_file", "native.read_file")]

        def list_capabilities(self):
            return self.capabilities

        def execute(self, tool, args, **kwargs):
            self.calls.append((tool, args))
            if tool == "native.web_fetch":
                output = {"resolved_url": "https://example.org/advisory", "text": "Observed facts"}
            elif tool == "native.write_file":
                (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
                output = "saved"
            else:
                output = (tmp_path / args["path"]).read_text(encoding="utf-8")
            return SimpleNamespace(success=True, to_dict=lambda: {"success": True, "output": output})

    class Provider:
        def __init__(self, actions):
            self.actions = iter(actions)
            self.requests = []

        def generate(self, request):
            self.requests.append(request)
            action = next(self.actions)
            return ModelCompletionResponse(text=json.dumps(action), parsed_json=action, model_name="offline")

    fetch = {"tool": "native.web_fetch", "arguments": {}}
    def save(content, path="report.md"):
        return {"tool": "native.write_file", "arguments": {"path": path, "content": content}}
    return SimpleNamespace(Router=Router, Provider=Provider, root=tmp_path, fetch=fetch, save=save)


def test_rejected_research_artifact_never_written_cannot_claim_creation(loop, tmp_path):
    from core.brain.task_engine import TaskEngine, TaskStatus
    provider = loop.Provider([loop.fetch, loop.save("unsupported"), {"answer": "I created report.md"}])
    router = loop.Router()
    engine = TaskEngine(storage_path=tmp_path / "tasks.json")
    result = CapabilityAgent(provider, router=router, max_steps=3).run(
        "Research then write report.md", session_id="actual-failure", task_engine=engine)
    assert not (loop.root / "report.md").exists()
    assert result.error and result.task_status == TaskStatus.FAILED.value
    assert result.metrics["outcome_obligations"]["failed_artifacts"] == ["report.md"]
    assert not result.metrics["outcome_obligations"]["artifact_obligations_met"]
    assert "I created" not in result.answer and "Task incomplete" in result.answer
    assert not result.artifacts
    assert [tool for tool, args in router.calls] == ["native.web_fetch"]


def test_later_rejection_preserves_existing_file_but_task_is_partial(loop):
    provider = loop.Provider([loop.fetch, loop.save("Observed facts"), loop.save("unsupported"),
                              {"answer": "Created the updated file"}])
    result = CapabilityAgent(provider, router=loop.Router(), max_steps=4).run(
        "Research then write report.md", session_id="later-failure")
    assert (loop.root / "report.md").read_text(encoding="utf-8") == "Observed facts"
    assert result.error and "Created the updated" not in result.answer
    assert result.artifacts == ["report.md"]
    assert not result.metrics["outcome_obligations"]["artifact_obligations_met"]


def test_one_corrected_write_resolves_failure_and_readback(loop):
    provider = loop.Provider([loop.fetch, loop.save("unsupported"), {"answer": "I created report.md"},
                              loop.save("Observed facts"), {"answer": "Saved and verified report.md"}])
    result = CapabilityAgent(provider, router=loop.Router(), max_steps=7).run(
        "Research then write report.md", session_id="repair")
    assert not result.error
    assert result.metrics["outcome_obligations"]["artifact_obligations_met"]
    assert result.metrics["outcome_obligations"]["verified_artifacts"] == ["report.md"]
    assert len(provider.requests) == 5
    assert provider.requests[-1].json_schema["required"] == ["answer"]


@pytest.mark.parametrize("repair", ["failed", "other-path", "wrong-tool"])
def test_repair_is_one_opportunity_without_expanding_scope(loop, repair):
    action = loop.save("unsupported") if repair == "failed" else (
        loop.save("Observed facts", "other.md") if repair == "other-path" else loop.fetch)
    actions = [loop.fetch, loop.save("unsupported"), {"answer": "I created report.md"}, action]
    if repair == "failed":
        actions.append({"answer": "Everything was saved"})
    provider = loop.Provider(actions)
    router = loop.Router()
    result = CapabilityAgent(provider, router=router, max_steps=8).run(
        "Research then write report.md", session_id="repair-budget")
    assert result.error and "Task incomplete" in result.answer
    assert not (loop.root / "report.md").exists() and not (loop.root / "other.md").exists()
    assert len(provider.requests) <= 5
    assert [tool for tool, args in router.calls] == ["native.web_fetch"]


def test_read_only_team_role_has_no_inferred_artifact_obligation(loop):
    provider = loop.Provider([{"answer": "Execution belongs to the executor"}])
    result = CapabilityAgent(provider, router=loop.Router(), max_steps=1)._run(
        "Build report.md", session_id="planner", role_instruction="planner: plan only")
    assert not result.error and result.metrics["outcome_obligations"]["artifact_obligations_met"]


def test_failed_write_with_unknown_path_is_partial_without_repair(loop):
    provider = loop.Provider([loop.fetch, {"tool": "native.write_file", "arguments": {"content": "unsupported"}},
                              {"answer": "Created the report"}])
    result = CapabilityAgent(provider, router=loop.Router(), max_steps=7).run(
        "Research then write report.md", session_id="unknown")
    assert result.error and "Created the report" not in result.answer
    assert result.metrics["outcome_obligations"]["unidentified_write_attempts"] == 1
    assert len(provider.requests) == 3
