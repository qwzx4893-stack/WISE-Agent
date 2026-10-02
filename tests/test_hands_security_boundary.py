"""Hands must retain a security boundary even when used outside an orchestrator."""

from core.hands.computer_use import ComputerActionType, WISEHands


def test_direct_forced_process_close_is_blocked_without_confirmation(monkeypatch):
    hands = WISEHands()
    monkeypatch.setattr(hands.window_mgr, "find_window", lambda _name: None)

    record = hands.execute_closed_loop_action(
        ComputerActionType.CLOSE_WINDOW,
        {"process": "notepad.exe", "force": True},
    )

    assert record.success is False
    assert record.failure_type == "SECURITY_BLOCKED"
    assert record.action_result["requires_confirmation"] is True


def test_process_only_close_never_uses_shell_execution(monkeypatch):
    hands = WISEHands()
    monkeypatch.setattr(hands.window_mgr, "find_window", lambda _name: None)

    result = hands._dispatch_action(
        ComputerActionType.CLOSE_WINDOW,
        {"process": "notepad.exe", "force": False},
        None,
    )

    assert result["success"] is False
    assert "force=true" in result["error"]


def test_direct_hands_cannot_force_close_a_protected_windows_process(monkeypatch):
    hands = WISEHands()
    monkeypatch.setattr(hands.window_mgr, "find_window", lambda _name: None)

    record = hands.execute_closed_loop_action(
        ComputerActionType.CLOSE_WINDOW,
        {"app_name": "lsass.exe", "force": True},
    )

    assert record.success is False
    assert "Protected system process" in record.action_result["error"]


def test_hands_honors_confirmed_context_from_authorized_orchestrator(monkeypatch):
    from core.security.security_gate import SecurityContext

    hands = WISEHands()
    monkeypatch.setattr(hands, "_dispatch_action", lambda *_args: {"success": True})
    monkeypatch.setattr(hands, "create_frame_signature", lambda *_args: {"signature": "stable"})

    record = hands.execute_closed_loop_action(
        ComputerActionType.CLOSE_WINDOW,
        {"process": "notepad.exe", "force": True},
        security_context=SecurityContext(caller="authorized-orchestrator", confirmed=True),
    )

    assert record.success is True


def test_force_close_is_destructive_in_every_control_surface():
    from core.security.security_gate import SecurityContext, WindowsSecurityGate

    evaluation = WindowsSecurityGate().evaluate(
        "close_window",
        {"target": "ordinary application", "force": True},
        SecurityContext(caller="computer-control"),
    )

    assert evaluation.allowed is False
    assert evaluation.requires_confirmation is True
