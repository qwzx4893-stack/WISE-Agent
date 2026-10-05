"""Exact consent and provenance across routing/planning/Hands; no OS actions."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest


@pytest.fixture
def computer_boundary(tmp_path, monkeypatch):
    from core.hands import computer_use as module
    from core.security.security_gate import WindowsSecurityGate, SecurityContext
    from core import capability_router
    from core.orchestrator.closed_loop_orchestrator import ClosedLoopOrchestrator
    gate = WindowsSecurityGate(audit_log_path=tmp_path / "security.jsonl")
    metrics = SimpleNamespace(cpu_percent=0, ram_used_mb=0, gpu_memory_used_mb=0)
    monkeypatch.setattr(module, "get_resource_manager", lambda: SimpleNamespace(get_hardware_metrics=lambda: metrics))
    hands = object.__new__(module.WISEHands)
    hands.security_gate = gate
    hands._lock = threading.RLock()
    hands._active = False
    hands.history = []
    hands.window_mgr = SimpleNamespace(find_window=lambda *_: None)
    hands.create_frame_signature = lambda *_: {"signature": "fixture"}
    dispatches = []
    def dispatch(action, params, hwnd):
        dispatches.append((action, dict(params)))
        return {"success": True}
    hands._dispatch_action = dispatch
    monkeypatch.setattr(module, "get_wise_hands", lambda: hands)
    monkeypatch.setattr(capability_router, "get_security_gate", lambda: gate)
    router = object.__new__(capability_router.CapabilityRouter)
    router._capabilities = {}
    router._load_computer_use_capabilities()
    orch = object.__new__(ClosedLoopOrchestrator)
    orch.security_gate = gate
    orch.security_context = SecurityContext(caller="trusted-fixture")
    orch.hands = hands
    orch.state_engine = SimpleNamespace(get_current_world_state=lambda **_: None,
                                       update_task_progress=lambda **_: None)
    orch.fusion_engine = SimpleNamespace(fuse_world_state=lambda *_: "fixture state")
    return gate, hands, router, orch, dispatches


def approved(gate, action, params):
    token = gate.confirmation_manager.create_confirmation_request(action, params, "synthetic owner test")
    assert gate.confirmation_manager.approve_token(token.token_id)[0]
    return token


def test_router_preserves_external_taint_and_blocks_executor(computer_boundary):
    _, _, router, _, dispatches = computer_boundary
    result = router.execute("computer.execute_action", {"action": "open_app",
        "params": {"app_name": "notepad.exe", "post_delay": 0}}, untrusted_content=True, confirmed=True)
    assert not result.success
    assert dispatches == []
    assert result.output["action_result"]["security_evaluation"]["quarantined"]


def test_model_cannot_supply_owner_confirmation_as_nested_argument(computer_boundary):
    _, _, router, _, dispatches = computer_boundary
    result = router.execute("computer.execute_action", {"action": "close_window",
        "params": {"process": "fixture.exe", "force": True, "confirmed": True, "post_delay": 0}})
    assert not result.success
    assert dispatches == []


def test_router_preserves_actual_trusted_confirmation(computer_boundary):
    _, _, router, _, dispatches = computer_boundary
    result = router.execute("computer.execute_action", {"action": "close_window",
        "params": {"process": "fixture.exe", "force": True, "post_delay": 0}}, confirmed=True)
    assert result.success
    assert len(dispatches) == 1


def test_bound_context_is_exact_and_not_read_from_args():
    from core.tools_bridge import bound_security_context, builtin_security_context
    from core.security.security_gate import SecurityContext
    params = {"action": "click", "params": {"x": 1}}
    with builtin_security_context(SecurityContext(confirmed=True), "computer_action", params):
        assert bound_security_context("computer_action", params).confirmed
        assert bound_security_context("computer_action", {"action": "click", "params": {"x": 2}}) is None
        assert bound_security_context("run_shell", params) is None
    assert bound_security_context("computer_action", params) is None


def test_preview_never_consumes_and_actual_execution_rechecks(computer_boundary):
    from core.security.security_gate import SecurityContext
    gate, _, _, _, _ = computer_boundary
    params = {"amount": 5, "recipient": "fixture"}
    token = approved(gate, "transfer_money", params)
    context = SecurityContext(confirmation_token=token.token_id)
    assert gate.preview_action("transfer_money", params, context).allowed
    assert gate.preview_action("transfer_money", params, context).allowed
    assert not token.used
    assert not gate.evaluate("transfer_money", {**params, "amount": 6}, context).allowed
    assert not token.used
    assert gate.evaluate("transfer_money", params, context).allowed
    assert token.used
    assert not gate.evaluate("transfer_money", params, context).allowed


@pytest.mark.parametrize("action_type,annotation,security_action", [
    ("close_window", None, "kill_process"),
    ("click", "transfer_money", "transfer_money"),
])
def test_orchestrator_previews_then_hands_consumes_once(computer_boundary, action_type, annotation, security_action):
    from core.hands.computer_use import ComputerActionType
    from core.orchestrator.closed_loop_orchestrator import OrchestrationStep
    gate, _, _, orch, dispatches = computer_boundary
    params = {"post_delay": 0}
    if action_type == "close_window":
        params.update(process="fixture.exe", force=True)
    if annotation:
        params.update(action_name=annotation, recipient="fixture", amount=5)
    token = approved(gate, security_action, params)
    step = OrchestrationStep(ComputerActionType(action_type), params, confirmation_token=token.token_id)
    result = orch.run_cycle("synthetic owner action", [step])
    assert result.success
    assert len(dispatches) == 1
    assert token.used
    replay = orch.run_cycle("same token replay", [step])
    assert not replay.success
    assert replay.paused_for_human
    assert len(dispatches) == 1


def test_force_close_annotation_cannot_downgrade_risk(computer_boundary):
    from core.hands.computer_use import ComputerActionType, WISEHands
    gate, hands, _, _, dispatches = computer_boundary
    params = {"force": True, "process": "fixture.exe", "action_name": "click"}
    assert WISEHands._security_action_name(ComputerActionType.CLOSE_WINDOW, params, gate) == "kill_process"
    assert not hands.execute_closed_loop_action(ComputerActionType.CLOSE_WINDOW, params).success
    assert dispatches == []


def test_tainted_confirmed_token_never_reaches_executor(computer_boundary):
    from core.security.security_gate import SecurityContext
    from core.hands.computer_use import ComputerActionType
    from core.orchestrator.closed_loop_orchestrator import OrchestrationStep
    gate, _, _, orch, dispatches = computer_boundary
    params = {"process": "fixture.exe", "force": True, "post_delay": 0}
    token = approved(gate, "kill_process", params)
    orch.security_context = SecurityContext(caller="fixture", is_untrusted_content=True, confirmed=True)
    result = orch.run_cycle("tainted action", [OrchestrationStep(ComputerActionType.CLOSE_WINDOW, params,
                             confirmation_token=token.token_id)])
    assert not result.success
    assert dispatches == []
    assert not token.used


def test_financial_verification_failure_cannot_automatically_repeat(computer_boundary):
    from core.security.security_gate import SecurityContext
    from core.hands.computer_use import ComputerActionType
    gate, hands, _, _, dispatches = computer_boundary
    params = {"action_name": "transfer_money", "recipient": "fixture", "amount": 5, "post_delay": 0}
    token = approved(gate, "transfer_money", params)
    hands.verify_condition = lambda *_: False
    hands._attempt_recovery = lambda *_: pytest.fail("High-impact action cannot auto-recover/repeat")
    result = hands.execute_closed_loop_action(ComputerActionType.CLICK, params,
        verification_condition={"type": "always_false"}, max_retries=2,
        security_context=SecurityContext(confirmation_token=token.token_id))
    assert not result.success
    assert not result.recovery_attempted
    assert len(dispatches) == 1


def test_actual_consumption_remains_atomic_across_threads(computer_boundary):
    from core.security.security_gate import SecurityContext
    gate, _, _, _, _ = computer_boundary
    params = {"recipient": "fixture", "amount": 5}
    token = approved(gate, "transfer_money", params)
    context = SecurityContext(confirmation_token=token.token_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: gate.evaluate("transfer_money", params, context).allowed, range(2)))
    assert sorted(outcomes) == [False, True]
