"""Tests for the fixed-operation, owner-visible UAC bridge."""

from types import SimpleNamespace


def test_uac_catalogue_never_accepts_a_free_form_command():
    from core.windows.uac import get_uac_broker

    items = get_uac_broker().operations()
    assert items
    assert all(set(item) == {"id", "title", "description"} for item in items)
    assert all("command" not in item for item in items)


def test_uac_request_requires_mode_and_confirmation(monkeypatch):
    import core.windows.uac as uac

    broker = uac.UacBroker()
    monkeypatch.setattr(broker, "supported", lambda: True)
    monkeypatch.setattr(uac, "get_system_integration_settings", lambda: {"enabled": True}) if hasattr(uac, "get_system_integration_settings") else None

    # The import is intentionally inside request(), so patch the source module.
    import core.system_integration as integration
    monkeypatch.setattr(integration, "get_system_integration_settings", lambda: {"enabled": True})
    monkeypatch.setattr(
        uac, "get_security_gate",
        lambda: SimpleNamespace(evaluate_action=lambda *_args, **_kwargs:
                                SimpleNamespace(allowed=False, reason="confirmation required", requires_confirmation=True,
                                                to_dict=lambda: {"allowed": False})),
    )

    result = broker.request("system_file_check", confirmed=False)

    assert result["ok"] is False
    assert result["requires_confirmation"] is True


def test_uac_request_launches_only_after_gate_and_windows_prompt(monkeypatch):
    import core.windows.uac as uac
    import core.system_integration as integration

    broker = uac.UacBroker()
    monkeypatch.setattr(broker, "supported", lambda: True)
    monkeypatch.setattr(integration, "get_system_integration_settings", lambda: {"enabled": True})
    monkeypatch.setattr(
        uac, "get_security_gate",
        lambda: SimpleNamespace(evaluate_action=lambda *_args, **_kwargs:
                                SimpleNamespace(allowed=True, reason="ok", to_dict=lambda: {"allowed": True})),
    )
    launched = []
    monitored = []
    monkeypatch.setattr(
        broker,
        "_launch_runas",
        lambda operation: (launched.append(operation.id) or uac.UacLaunch(process_handle=33, process_id=4321)),
    )
    monkeypatch.setattr(
        broker,
        "_start_monitor",
        lambda request_id, handle: monitored.append((request_id, handle)),
    )

    intent = broker.issue_intent("flush_dns")["intent_token"]
    result = broker.request("flush_dns", confirmed=True, intent_token=intent)

    assert result["ok"] is True
    assert launched == ["flush_dns"]
    assert result["run"]["state"] == "running"
    assert result["run"]["process_id"] == 4321
    assert monitored == [(result["request_id"], 33)]
    assert broker.runs()["active_count"] == 1


def test_uac_run_lifecycle_records_real_completion(monkeypatch):
    import core.windows.uac as uac
    import core.system_integration as integration

    broker = uac.UacBroker()
    monkeypatch.setattr(broker, "supported", lambda: True)
    monkeypatch.setattr(integration, "get_system_integration_settings", lambda: {"enabled": True})
    monkeypatch.setattr(
        uac,
        "get_security_gate",
        lambda: SimpleNamespace(
            evaluate_action=lambda *_args, **_kwargs: SimpleNamespace(allowed=True, reason="ok")
        ),
    )
    monkeypatch.setattr(
        broker,
        "_launch_runas",
        lambda _operation: uac.UacLaunch(process_handle=44, process_id=9876),
    )
    monkeypatch.setattr(broker, "_start_monitor", lambda *_args: None)

    intent = broker.issue_intent("system_file_check")["intent_token"]
    started = broker.request("system_file_check", confirmed=True, intent_token=intent)
    finished = broker._finish_run(started["request_id"], state="completed", exit_code=0)
    snapshot = broker.runs()

    assert finished is not None
    assert snapshot["active_count"] == 0
    assert snapshot["recent"][0]["state"] == "completed"
    assert snapshot["recent"][0]["exit_code"] == 0


def test_uac_duplicate_operation_is_coalesced_while_active(monkeypatch):
    import core.windows.uac as uac
    import core.system_integration as integration

    broker = uac.UacBroker()
    monkeypatch.setattr(broker, "supported", lambda: True)
    monkeypatch.setattr(integration, "get_system_integration_settings", lambda: {"enabled": True})
    monkeypatch.setattr(
        uac,
        "get_security_gate",
        lambda: SimpleNamespace(
            evaluate_action=lambda *_args, **_kwargs: SimpleNamespace(allowed=True, reason="ok")
        ),
    )
    monkeypatch.setattr(
        broker,
        "_launch_runas",
        lambda _operation: uac.UacLaunch(process_handle=55, process_id=2222),
    )
    monkeypatch.setattr(broker, "_start_monitor", lambda *_args: None)

    first_intent = broker.issue_intent("flush_dns")["intent_token"]
    second_intent = broker.issue_intent("flush_dns")["intent_token"]
    first = broker.request("flush_dns", confirmed=True, intent_token=first_intent)
    second = broker.request("flush_dns", confirmed=True, intent_token=second_intent)

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["duplicate"] is True
    assert second["run"]["request_id"] == first["request_id"]
    assert broker.runs()["active_count"] == 1


def test_uac_cancellation_is_recorded_and_releases_reservation(monkeypatch):
    import core.windows.uac as uac
    import core.system_integration as integration

    broker = uac.UacBroker()
    monkeypatch.setattr(broker, "supported", lambda: True)
    monkeypatch.setattr(integration, "get_system_integration_settings", lambda: {"enabled": True})
    monkeypatch.setattr(
        uac,
        "get_security_gate",
        lambda: SimpleNamespace(
            evaluate_action=lambda *_args, **_kwargs: SimpleNamespace(allowed=True, reason="ok")
        ),
    )

    def _cancel(_operation):
        raise OSError(1223, "cancelled")

    monkeypatch.setattr(broker, "_launch_runas", _cancel)

    intent = broker.issue_intent("windows_image_repair")["intent_token"]
    result = broker.request("windows_image_repair", confirmed=True, intent_token=intent)
    snapshot = broker.runs()

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert snapshot["active_count"] == 0
    assert snapshot["recent"][0]["state"] == "cancelled"


def test_uac_requires_fresh_single_use_intent(monkeypatch):
    import core.windows.uac as uac
    import core.system_integration as integration

    broker = uac.UacBroker()
    monkeypatch.setattr(broker, "supported", lambda: True)
    monkeypatch.setattr(integration, "get_system_integration_settings", lambda: {"enabled": True})
    monkeypatch.setattr(
        uac,
        "get_security_gate",
        lambda: SimpleNamespace(
            evaluate_action=lambda *_args, **_kwargs: SimpleNamespace(allowed=True, reason="ok")
        ),
    )
    launches = []
    monkeypatch.setattr(
        broker,
        "_launch_runas",
        lambda operation: (launches.append(operation.id) or uac.UacLaunch(process_handle=66, process_id=3333)),
    )
    monkeypatch.setattr(broker, "_start_monitor", lambda *_args: None)

    missing = broker.request("flush_dns", confirmed=True)
    token = broker.issue_intent("flush_dns")["intent_token"]
    first = broker.request("flush_dns", confirmed=True, intent_token=token)
    broker._finish_run(first["request_id"], state="completed", exit_code=0)
    replay = broker.request("flush_dns", confirmed=True, intent_token=token)

    assert missing["ok"] is False and missing["requires_intent"] is True
    assert first["ok"] is True
    assert replay["ok"] is False and replay["requires_intent"] is True
    assert launches == ["flush_dns"]
