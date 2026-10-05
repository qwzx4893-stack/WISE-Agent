"""Regression tests for owner-controlled Super Computer integration."""

from types import SimpleNamespace


def test_policy_is_off_by_default_and_protective_when_enabled(monkeypatch):
    import core.system_integration as integration

    monkeypatch.setattr(integration, "get_system_integration_settings",
                        lambda: {"enabled": False})
    assert integration.system_integration_policy_prompt() == ""

    monkeypatch.setattr(integration, "get_system_integration_settings",
                        lambda: {"enabled": True})
    prompt = integration.system_integration_policy_prompt()
    assert "UAC" in prompt
    assert "محتوى الويب" in prompt


def test_activation_is_confirmed_and_registers_user_session_autostart(monkeypatch):
    import core.system_integration as integration

    state = {"system_integration": {"enabled": False, "start_at_login": True}}
    monkeypatch.setattr(integration, "load_server_config", lambda: dict(state))
    monkeypatch.setattr(integration, "save_server_config", lambda cfg: state.update(cfg))
    monkeypatch.setattr(
        integration, "get_security_gate",
        lambda: SimpleNamespace(evaluate_action=lambda *_args, **_kwargs:
                                SimpleNamespace(allowed=True, reason="ok")),
    )
    manager = integration.SystemIntegrationManager()
    registered = []
    monkeypatch.setattr(manager, "_register_login_task",
                        lambda: (registered.append(True) or (True, "registered")))

    result = manager.activate(confirmed=True)

    assert result["ok"] is True
    assert state["system_integration"]["enabled"] is True
    assert state["system_integration"]["start_at_login"] is True
    assert registered == [True]


def test_changing_autostart_while_enabled_syncs_task(monkeypatch):
    import core.system_integration as integration

    state = {"system_integration": {"enabled": True, "start_at_login": False}}
    monkeypatch.setattr(integration, "load_server_config", lambda: dict(state))
    monkeypatch.setattr(integration, "save_server_config", lambda cfg: state.update(cfg))
    manager = integration.SystemIntegrationManager()
    registered = []
    monkeypatch.setattr(manager, "_register_login_task",
                        lambda: (registered.append(True) or (True, "registered")))

    manager.configure({"start_at_login": True}, confirmed=True)

    assert state["system_integration"]["start_at_login"] is True
    assert registered == [True]


def test_sensitive_configuration_change_requires_confirmation(monkeypatch):
    import core.system_integration as integration

    state = {"system_integration": {"enabled": True, "start_at_login": False}}
    monkeypatch.setattr(integration, "load_server_config", lambda: dict(state))
    monkeypatch.setattr(integration, "save_server_config", lambda cfg: state.update(cfg))
    monkeypatch.setattr(
        integration, "get_security_gate",
        lambda: SimpleNamespace(evaluate_action=lambda *_args, **_kwargs:
                                SimpleNamespace(allowed=False, reason="confirmation required")),
    )
    manager = integration.SystemIntegrationManager()

    try:
        manager.configure({"start_at_login": True}, confirmed=False)
    except PermissionError as exc:
        assert "confirmation required" in str(exc)
    else:
        raise AssertionError("Sensitive system integration change was not gated")

    assert state["system_integration"]["start_at_login"] is False


def test_status_exposes_canonical_enabled_state(monkeypatch):
    import core.system_integration as integration
    import core.windows.uac as uac

    monkeypatch.setattr(
        integration,
        "get_system_integration_settings",
        lambda: {**integration._DEFAULTS, "enabled": True},
    )
    manager = integration.SystemIntegrationManager()
    monkeypatch.setattr(manager, "_is_windows", lambda: True)
    monkeypatch.setattr(manager, "_task_exists", lambda: False)
    monkeypatch.setattr(manager, "_is_process_elevated", lambda: False)
    monkeypatch.setattr(
        uac,
        "get_uac_broker",
        lambda: SimpleNamespace(
            operations=lambda: [{"id": "flush_dns", "title": "Flush DNS", "description": "test"}],
            runs=lambda limit=10: {
                "supported": True,
                "active_count": 1,
                "active": [{"request_id": "req-1", "state": "running"}],
                "recent": [{"request_id": "req-0", "state": "completed", "exit_code": 0}],
            },
        ),
    )

    status = manager.status()

    assert status["enabled"] is True
    assert status["active"] is True
    assert status["settings"]["enabled"] is True
    assert status["uac"]["active_count"] == 1
    assert status["uac"]["active_runs"][0]["state"] == "running"
    assert status["uac"]["recent_runs"][0]["exit_code"] == 0


def test_activation_refuses_when_security_gate_requires_confirmation(monkeypatch):
    import core.system_integration as integration

    monkeypatch.setattr(
        integration, "get_security_gate",
        lambda: SimpleNamespace(evaluate_action=lambda *_args, **_kwargs:
                                SimpleNamespace(allowed=False, reason="confirmation required")),
    )
    manager = integration.SystemIntegrationManager()
    result = manager.activate(confirmed=False)

    assert result["ok"] is False
    assert result["error"] == "confirmation required"
