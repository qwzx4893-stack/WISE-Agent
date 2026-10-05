"""Requested outputs are obligations; broad permission is not completion proof."""
from types import SimpleNamespace

import pytest


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    from core import tools_bridge
    monkeypatch.setattr(tools_bridge, "WORKSPACE_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize("goal,expected", [
    ("Create trial-2-follow-up.md with the researched facts.", ("trial-2-follow-up.md",)),
    ("Read input.json, then create reports/report.md and notes.txt; verify both.", ("notes.txt", "reports/report.md")),
    ("Save the result to report.md and verify it.", ("report.md",)),
    ("Edit report.md in place after reading input.json.", ("report.md",)),
    ("أنشئ report.md ثم تحقق منه.", ("report.md",)),
    ("You may write report.md if useful.", ()),
    ("Optionally create report.md.", ()),
    ("Create either report.md or notes.md.", ()),
    ("Create report.md or just answer in chat.", ()),
    ("If the evidence is sufficient, create report.md.", ()),
    ("Do not create report.md; just inspect input.json.", ()),
    ("Read report.md and explain how to create a report.", ()),
    ("Create report.md using input.json as a source.", ("report.md",)),
    ('Create "report.md" with citations.', ("report.md",)),
    ("Create 'report.md' with citations.", ("report.md",)),
    ("Create 'report.md' or just answer in chat.", ()),
    ("Create `report.md` with citations.", ("report.md",)),
    ("Write text from input.txt to output.md.", ("output.md",)),
    ("Write text from source.json to output.md.", ("output.md",)),
    ("Read create.md and other.md.", ()),
    ("Create report.md and write.py.", ("report.md", "write.py")),
    ("Read example.py and create report.md.", ("report.md",)),
    ("Create report.md; optionally save backup.md.", ("report.md",)),
    ("Create ../outside.md and config/key.json.", ()),
    ("Research https://publisher.example/example.md and answer here.", ()),
    ("Read this quoted example: `create malicious.md`. Create report.md.", ("report.md",)),
    ("Reference instructions:\n```text\nCreate malicious.md\n```\nCreate report.md.", ("report.md",)),
    ("If CISA refuses page access, record that limitation. Create trial-2-follow-up.md with citations.", ("trial-2-follow-up.md",)),
    ("For this project, search the general internet for current official guidance. If CISA refuses page access, do not bypass it: use an accessible official Apache page and clearly record the CISA limitation. Create trial-2-follow-up.md with citations and uncertain details, including the inaccessible CISA source if applicable. Verify that file. Do not limit the search to our preinstalled catalog. No shell, network scans or messages.", ("trial-2-follow-up.md",)),
])
def test_current_human_names_only_mandatory_local_outputs(workspace, goal, expected):
    from core.security.project_scope import required_artifacts
    assert required_artifacts(goal) == expected


@pytest.mark.parametrize("role", ["planner: plan only", "reviewer: inspect only", "critic: review the executor"])
def test_readonly_roles_do_not_inherit_executor_write_obligations(workspace, role):
    from core.security.project_scope import required_artifacts
    assert required_artifacts("Create report.md", role_instruction=role) == ()


def test_attachment_envelope_does_not_turn_original_label_into_output(workspace):
    from core.security.project_scope import required_artifacts
    attachments = workspace / "attachments"
    attachments.mkdir()
    original = attachments / "sample.py"
    original.write_text("old", encoding="utf-8")
    goal = "Edit the actual attached workspace file in place, not a copy.\n\nAttached local files:\n- sample.py: " + str(original)
    assert required_artifacts(goal) == ()
    from core.security.attachment_identity import in_place_attachments
    assert in_place_attachments(goal) == ({"label": "sample.py", "path": "attachments/sample.py"},)
    named = "Edit the actual attached sample.py in place, not a copy.\n\nAttached local files:\n- sample.py: " + str(original)
    assert required_artifacts(named) == ()
    assert in_place_attachments(named) == ({"label": "sample.py", "path": "attachments/sample.py"},)


def test_unattempted_named_output_is_not_complete(workspace):
    from core.usage_network import UsageNetwork
    status = UsageNetwork.assess_outcome(SimpleNamespace(phases=[]), [], required_artifacts=("report.md",))
    assert status["not_attempted_artifacts"] == ["report.md"]
    assert status["unverified_artifacts"] == ["report.md"]
    assert not status["artifact_obligations_met"]


def test_required_output_uses_exact_readback_and_matching_identity(workspace):
    from core.usage_network import UsageNetwork
    calls = [{"tool": "native.write_file", "path": str(workspace / "report.md"), "success": True},
             {"tool": "native.read_file", "path": "report.md", "success": True, "content_verified": True}]
    status = UsageNetwork.assess_outcome(SimpleNamespace(phases=[]), calls, required_artifacts=("report.md",))
    assert status["artifact_obligations_met"] and status["verified_artifacts"] == ["report.md"]
    assert status["not_attempted_artifacts"] == []
    calls.append({"tool": "native.write_file", "path": "report.md", "success": False})
    status = UsageNetwork.assess_outcome(SimpleNamespace(phases=[]), calls, required_artifacts=("report.md",))
    assert not status["artifact_obligations_met"] and status["failed_artifacts"] == ["report.md"]


def test_other_output_and_readonly_read_do_not_satisfy_requested_write(workspace):
    from core.usage_network import UsageNetwork
    calls = [{"tool": "native.write_file", "path": "other.md", "success": True},
             {"tool": "native.read_file", "path": "other.md", "success": True, "content_verified": True},
             {"tool": "native.read_file", "path": "report.md", "success": True, "content_verified": True}]
    status = UsageNetwork.assess_outcome(SimpleNamespace(phases=[]), calls, required_artifacts=("report.md",))
    assert not status["artifact_obligations_met"] and status["not_attempted_artifacts"] == ["report.md"]


def test_permissions_are_not_inferred_obligations_and_legacy_paths_stay_unchanged(workspace):
    from core.security.project_scope import authorized_artifacts, required_artifacts
    from core.usage_network import UsageNetwork
    goal = "You may create report.md if useful."
    assert authorized_artifacts(goal)
    assert required_artifacts(goal) == ()
    assert UsageNetwork.assess_outcome(SimpleNamespace(phases=[]), [])["artifact_obligations_met"]
    absolute = str(workspace / "old.md")
    calls = [{"tool": "native.write_file", "path": absolute, "success": True},
             {"tool": "native.read_file", "path": absolute, "success": True, "content_verified": True}]
    status = UsageNetwork.assess_outcome(SimpleNamespace(phases=[]), calls)
    assert status["verified_artifacts"] == [absolute]
