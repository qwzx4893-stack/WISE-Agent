"""Filesystem policy failures must never authorize an adapter or native file sink."""

import json
from types import SimpleNamespace

import pytest

from core.security.security_gate import ActionTier, SecurityContext, WindowsSecurityGate
from core.windows import filesystem_governor as fs


@pytest.fixture
def gate(tmp_path):
    return WindowsSecurityGate(audit_log_path=tmp_path / "audit.jsonl")


def fail_policy(*args, **kwargs):
    raise OSError("synthetic filesystem policy outage")


@pytest.mark.parametrize("action", ["read_file", "write_file", "list_directory", "move_file"])
@pytest.mark.parametrize("failure", ["acquisition", "evaluation"])
def test_filesystem_policy_exception_denies_and_audits(gate, monkeypatch, tmp_path, action, failure):
    if failure == "acquisition":
        monkeypatch.setattr(fs, "get_filesystem_governor", fail_policy)
    else:
        monkeypatch.setattr(fs, "get_filesystem_governor",
                            lambda: SimpleNamespace(evaluate_operation=fail_policy))

    decision = gate.evaluate(action, {"path": str(tmp_path / "synthetic.txt")})

    assert decision.allowed is False
    assert decision.requires_confirmation is False
    assert "filesystem policy" in decision.reason.lower()
    records = [json.loads(line) for line in gate.audit_log_path.read_text().splitlines()]
    assert records[-1]["allowed"] is False
    assert records[-1]["audit_id"] == decision.audit_id


@pytest.mark.parametrize("confirmed", [False, True])
def test_scope_and_confirmation_cannot_override_policy_failure(gate, monkeypatch, tmp_path, confirmed):
    from core import tools_bridge

    monkeypatch.setattr(tools_bridge, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(fs, "get_filesystem_governor", fail_policy)
    target = str(tmp_path / "authorized.txt")
    context = SecurityContext(confirmed=confirmed, elevation_requested=confirmed,
                              is_untrusted_content=True, project_write_scope=(target,))

    decision = gate.evaluate("write_file", {"path": target}, context)

    assert decision.allowed is False
    assert decision.requires_confirmation is False


def test_invalid_policy_decision_fails_closed(gate, monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "get_filesystem_governor",
                        lambda: SimpleNamespace(evaluate_operation=lambda **kwargs: None))

    decision = gate.evaluate("read_file", {"path": str(tmp_path / "synthetic.txt")})

    assert decision.allowed is False


@pytest.mark.parametrize("action", ["read_file", "write_file"])
def test_router_does_not_dispatch_when_filesystem_policy_fails(gate, monkeypatch, tmp_path, action):
    from core import capability_router as router_module
    from core.contracts import CapabilitySource

    monkeypatch.setattr(fs, "get_filesystem_governor", fail_policy)
    monkeypatch.setattr(router_module, "get_security_gate", lambda: gate)
    calls = []
    router = object.__new__(router_module.CapabilityRouter)
    router._capabilities = {}
    router.register(router_module.CapabilityDescriptor(
        id=f"native.{action}", name=action, source=CapabilitySource.NATIVE,
        description="Synthetic file sink", input_schema={"path": {"type": "string"}},
        execution_adapter=lambda params: calls.append(params) or "SYNTHETIC_OUTPUT",
    ))

    result = router.execute(f"native.{action}", {"path": str(tmp_path / "synthetic.txt")})

    assert calls == []
    assert result.success is False
    assert result.metadata["security_evaluation"]["allowed"] is False


@pytest.mark.parametrize("action", ["read_file", "write_file"])
def test_native_control_does_not_reach_file_sink_on_policy_failure(gate, monkeypatch, action):
    from core.windows import computer_control

    monkeypatch.setattr(fs, "get_filesystem_governor", fail_policy)
    sink_calls = []
    monkeypatch.setattr(computer_control, "Path", lambda path: sink_calls.append(path))
    control = object.__new__(computer_control.ComputerControl)
    control.security_gate = gate
    control.security_context = SecurityContext()

    result = (control.read_file("SYNTHETIC_ONLY") if action == "read_file"
              else control.write_file("SYNTHETIC_ONLY", "SYNTHETIC_DATA"))

    assert sink_calls == []
    assert result["success"] is False


def test_healthy_policy_keeps_read_and_write_capabilities(gate, monkeypatch, tmp_path):
    governor = fs.FilesystemGovernor(workspace_root=tmp_path)
    monkeypatch.setattr(fs, "get_filesystem_governor", lambda: governor)
    for action in ("read_file", "write_file"):
        assert gate.evaluate(action, {"path": str(tmp_path / "artifact.txt")}).allowed is True


def test_healthy_policy_keeps_trusted_scoped_write(gate, monkeypatch, tmp_path):
    from core import tools_bridge

    monkeypatch.setattr(tools_bridge, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(fs, "get_filesystem_governor", lambda: fs.FilesystemGovernor(workspace_root=tmp_path))
    target = str(tmp_path / "artifact.txt")
    context = SecurityContext(is_untrusted_content=True, project_write_scope=(target,))

    assert gate.evaluate("write_file", {"path": target}, context).allowed is True
    assert gate.evaluate("write_file", {"path": str(tmp_path / "other.txt")}, context).allowed is False


def test_healthy_policy_keeps_confirmed_system_modification(gate, monkeypatch):
    monkeypatch.setattr(fs, "get_filesystem_governor", lambda: fs.FilesystemGovernor())
    # This is a policy evaluation only. No filesystem adapter is dispatched.
    decision = gate.evaluate("write_file", {"path": r"C:\Windows\SYNTHETIC_TEST_ONLY.txt"},
                             SecurityContext(confirmed=True))

    assert decision.allowed is True
    assert decision.tier == ActionTier.DESTRUCTIVE


def test_unknown_action_default_is_unchanged(gate):
    assert gate.evaluate("synthetic_unregistered_action", {}).tier == ActionTier.MODERATE

