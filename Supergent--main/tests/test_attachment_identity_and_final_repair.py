"""Offline execution contracts for proven real-journey failures, not live QA."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.brain.capability_agent import CapabilityAgent
from core.models.provider_interface import ModelCompletionResponse
from core.security.attachment_identity import in_place_attachments

URL = "https://official.example/current"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import core.capability_network as network
    import core.skills.indexer as skills
    import core.tools_bridge as tools
    monkeypatch.setattr(network, "get_capability_network", lambda: SimpleNamespace(plan=lambda *a, **k: SimpleNamespace(capabilities=[], skills=[])))
    monkeypatch.setattr(skills.SkillIndexer, "get_index", lambda self: {})
    monkeypatch.setattr(tools, "WORKSPACE_DIR", tmp_path)
    attachment = tmp_path / "attachments/1791124849153817100-sample.py"
    attachment.parent.mkdir()
    attachment.write_text("ORIGINAL_FIXTURE", encoding="utf-8")

    class Router:
        def __init__(self):
            self.calls = []
            self.bad_readback = False
        def list_capabilities(self):
            return [SimpleNamespace(id=n, description=n, input_schema={}, availability=True, risk_level="LOW")
                    for n in ("native.read_file", "native.write_file", "native.web_fetch", "native.web_search")]
        def execute(self, tool, args, **kwargs):
            self.calls.append((tool, args.copy()))
            if tool == "native.write_file":
                target = Path(args["path"])
                if not target.is_absolute(): target = tmp_path / target
                target.write_text(args["content"], encoding="utf-8")
                output = "Saved fixture"
            elif tool == "native.read_file":
                target = Path(args["path"])
                if not target.is_absolute(): target = tmp_path / target
                output = "MISMATCH_FIXTURE" if self.bad_readback else target.read_text(encoding="utf-8")
            elif tool == "native.web_fetch":
                output = {"resolved_url": URL, "page_text": "Actual observed protocol only.",
                          "evidence_kind": "page_excerpt", "retrieved_at": "2026-10-04T14:42:17+00:00"}
            else:
                output = []
            return SimpleNamespace(success=True, to_dict=lambda: {"success": True, "output": output})

    class Provider:
        def __init__(self, actions):
            self.actions = iter(actions)
            self.requests = []
        def generate(self, request):
            self.requests.append(request)
            action = next(self.actions)
            return ModelCompletionResponse(json.dumps(action), parsed_json=action, model_name="offline")

    return tmp_path, attachment, Router, Provider


def attachment_goal(attachment):
    return ("Edit the actual attached workspace file in-place, not a new copy. Verify sample.py."
            "\n\nAttached local files:\n- sample.py: " + str(attachment))


def test_original_attachment_cannot_be_replaced_by_copied_basename(harness):
    root, attachment, Router, Provider = harness
    router = Router()
    provider = Provider([{"tool": "native.write_file", "arguments": {"path": "sample.py", "content": "FIXED"}},
                         {"answer": "Completed the original edit"}])
    result = CapabilityAgent(provider, router=router, max_steps=2).run(attachment_goal(attachment), session_id="wrong-copy")
    assert result.error and not router.calls
    assert attachment.read_text(encoding="utf-8") == "ORIGINAL_FIXTURE" and not (root / "sample.py").exists()
    identity = result.metrics["outcome_obligations"]["in_place_attachment_obligations"]
    assert not identity["satisfied"] and identity["rejected_copy_targets"] == ["sample.py"]
    assert "Completed the original edit" not in result.answer


def test_rejected_copy_can_be_corrected_only_at_original_identity(harness):
    root, attachment, Router, Provider = harness
    original = str(attachment.relative_to(root)).replace("\\", "/")
    router = Router()
    provider = Provider([{"tool": "native.write_file", "arguments": {"path": "sample.py", "content": "FIXED"}},
                         {"tool": "native.write_file", "arguments": {"path": str(attachment), "content": "FIXED"}},
                         {"answer": "Original artifact saved and verified"}])
    result = CapabilityAgent(provider, router=router, max_steps=3).run(attachment_goal(attachment), session_id="correct-original")
    assert not result.error and attachment.read_text(encoding="utf-8") == "FIXED"
    assert not (root / "sample.py").exists()
    assert result.metrics["outcome_obligations"]["in_place_attachment_obligations"]["satisfied"]
    assert original in result.metrics["outcome_obligations"]["verified_artifacts"]
    assert len(provider.requests) == 3 and [call[0] for call in router.calls] == ["native.write_file", "native.read_file"]


def test_false_success_without_original_write_is_incomplete(harness):
    _, attachment, Router, Provider = harness
    result = CapabilityAgent(Provider([{"answer": "Finished all edits"}]), router=Router(), max_steps=1).run(
        attachment_goal(attachment), session_id="missing-original-write")
    assert result.error and not result.metrics["outcome_obligations"]["artifact_obligations_met"]
    assert attachment.read_text(encoding="utf-8") == "ORIGINAL_FIXTURE"


@pytest.mark.parametrize("human", ["Read the attached sample.py.", "Do not edit the actual attached sample.py in-place.",
                                   "Create a new copy of the attached sample.py.", "Edit the attached sample.py."])
def test_in_place_obligations_require_explicit_trusted_original_edit(harness, human):
    _, attachment, _, _ = harness
    assert in_place_attachments(human + "\n\nAttached local files:\n- sample.py: " + str(attachment)) == ()


def test_other_explicit_artifact_does_not_substitute_for_original(harness):
    root, attachment, Router, Provider = harness
    goal = attachment_goal(attachment).replace("Verify sample.py.", "Verify sample.py. Create notes.md.")
    router = Router()
    provider = Provider([{"tool": "native.write_file", "arguments": {"path": "notes.md", "content": "Observation note"}},
                         {"answer": "All work finished"}])
    result = CapabilityAgent(provider, router=router, max_steps=2).run(goal, session_id="other-artifact")
    assert (root / "notes.md").read_text(encoding="utf-8") == "Observation note"
    assert result.error and not result.metrics["outcome_obligations"]["in_place_attachment_obligations"]["satisfied"]


@pytest.mark.parametrize("outside", [True, False])
def test_upload_directory_junction_does_not_authorize_or_crash(harness, monkeypatch, outside):
    root, attachment, _, _ = harness
    original = Path.resolve
    destination = root.parent / "unowned" if outside else root / "other-directory"
    def resolve(path, *args, **kwargs):
        return destination if path == root / "attachments" else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert in_place_attachments(attachment_goal(attachment)) == ()


def repair_actions(last):
    return [{"tool": "native.web_fetch", "arguments": {"url": URL}},
            {"tool": "native.write_file", "arguments": {"path": "report.md", "content": f"Unsupported 9.8.7 [Source]({URL})"}}, last]


def test_penultimate_research_write_uses_one_existing_last_slot_no_extra_requests(harness):
    root, _, Router, Provider = harness
    router = Router()
    provider = Provider(repair_actions({"tool": "native.write_file", "arguments": {"path": "report.md", "content": f"Observed protocol only. [Source]({URL})"}}))
    result = CapabilityAgent(provider, router=router, max_steps=3).run("Research official protocol. Create report.md and verify it.", session_id="last-slot-repair")
    assert not result.error and len(provider.requests) == result.metrics["model_requests"] == 3
    assert [call[0] for call in router.calls] == ["native.web_fetch", "native.write_file", "native.read_file"]
    assert "9.8.7" not in (root / "report.md").read_text(encoding="utf-8")
    assert result.metrics["final_slot_artifact_repair"]["verified"]
    assert result.metrics["final_slot_artifact_repair"]["request_budget_increased"] is False
    assert "independent semantic review" in result.answer and "Observed protocol" not in result.answer
    assert "FINAL BOUNDED REPAIR TURN" in provider.requests[-1].system_prompt
    assert "observed resolved source URLs" in json.dumps(provider.requests[-1].messages)


@pytest.mark.parametrize("last", [
    {"tool": "native.read_file", "arguments": {"path": "report.md"}},
    {"tool": "native.web_fetch", "arguments": {"url": URL}},
    {"tool": "native.web_search", "arguments": {"query": "new source"}},
    {"tool": "discover_tools", "arguments": {"query": "new tool"}},
    {"tool": "native.write_file", "arguments": {"path": "other.md", "content": "Unrelated"}},
])
def test_last_slot_repair_cannot_read_search_discover_or_write_other_path(harness, last):
    root, _, Router, Provider = harness
    router = Router()
    provider = Provider(repair_actions(last))
    result = CapabilityAgent(provider, router=router, max_steps=3).run("Research official protocol. Create report.md.", session_id="repair-scope")
    assert result.error and len(provider.requests) == 3
    assert [call[0] for call in router.calls] == ["native.web_fetch"]
    assert not (root / "report.md").exists() and not (root / "other.md").exists()


@pytest.mark.parametrize("bad_readback", [False, True])
def test_repair_still_requires_grounding_and_exact_readback(harness, bad_readback):
    root, _, Router, Provider = harness
    router = Router()
    router.bad_readback = bad_readback
    content = f"Observed protocol only. [Source]({URL})" if bad_readback else f"Unsupported 9.8.7 [Source]({URL}) changed"
    provider = Provider(repair_actions({"tool": "native.write_file", "arguments": {"path": "report.md", "content": content}}))
    result = CapabilityAgent(provider, router=router, max_steps=3).run("Research official protocol. Create report.md.", session_id="repair-fails")
    assert result.error and len(provider.requests) == 3
    assert not result.metrics["outcome_obligations"]["artifact_obligations_met"]
    assert not result.metrics["final_slot_artifact_repair"]["verified"]
    if not bad_readback: assert not (root / "report.md").exists()


def test_observed_native_redirect_retains_requested_and_resolved_citation_identities(harness):
    _, _, Router, Provider = harness
    requested = "https://official.example/previous"
    provider = Provider([{"tool": "native.web_fetch", "arguments": {"url": requested}},
                         {"answer": "Observed protocol only. [Source](" + requested + ")"}])
    result = CapabilityAgent(provider, router=Router(), max_steps=2).run("Research official protocol", session_id="observed-redirect")
    assert not result.error and requested in result.answer


def test_last_slot_repair_does_not_grant_a_read_only_named_artifact_write(harness):
    root, _, Router, Provider = harness
    router = Router()
    provider = Provider(repair_actions({"answer": "No valid artifact was written"}))
    result = CapabilityAgent(provider, router=router, max_steps=3).run("Research official protocol. Read report.md only.", session_id="no-write-grant")
    assert result.error and len(provider.requests) == 3
    assert "final_slot_artifact_repair" not in result.metrics
    assert "FINAL BOUNDED REPAIR TURN" not in provider.requests[-1].system_prompt
    assert not (root / "report.md").exists()


def test_final_correction_prompt_contains_actual_error_and_epSS_scope_guidance(harness):
    _, _, Router, Provider = harness
    provider = Provider([{"tool": "native.web_fetch", "arguments": {"url": URL}},
                         {"answer": "Invented citation https://invented.example/other"},
                         {"answer": "Observed protocol only. [Source](" + URL + ")"}])
    result = CapabilityAgent(provider, router=Router(), max_steps=4).run("Research official protocol", session_id="correction-error")
    assert not result.error and len(provider.requests) == 3
    correction = json.dumps(provider.requests[-1].messages)
    assert "The answer cited an unobserved URL" in correction
    assert "30-day in-the-wild probability" in correction and "particular host" in correction
