"""Owner-consent/lifecycle tests without creating tasks, windows or UAC prompts."""
from types import SimpleNamespace
import sys
import pytest


def _desktop(monkeypatch, *, attached=False, resident=False):
    import wise_desktop as desktop
    calls = []
    monkeypatch.setattr(sys, "argv", ["wise_desktop.py", "--native-only"])
    monkeypatch.setattr(desktop.signal, "signal", lambda *a: None)
    monkeypatch.setattr(desktop, "_acquire_native_window_mutex", lambda: True)
    monkeypatch.setattr(desktop, "_release_native_window_mutex", lambda: calls.append("release"))
    monkeypatch.setattr(desktop, "_wise_backend_is_running", lambda *a: attached)
    monkeypatch.setattr(desktop, "_find_free_port", lambda p: p)
    monkeypatch.setattr(desktop, "_start_backend", lambda *a: calls.append("start"))
    monkeypatch.setattr(desktop, "_wait_for_backend", lambda *a, **k: True)
    monkeypatch.setattr(desktop, "_launch_pywebview", lambda *a: True)
    monkeypatch.setattr(desktop, "_background_mode_enabled", lambda: resident)
    monkeypatch.setattr(desktop, "_keep_background_backend_alive", lambda *a: calls.append("resident"))
    monkeypatch.setattr(desktop, "_stop_owned_backend", lambda *a: calls.append("stop"))
    return desktop, calls


@pytest.mark.parametrize("attached,resident,expected", [
    (False, False, ["start", "release", "stop"]),
    (False, True, ["start", "release", "resident", "stop"]),
    (True, True, ["release"]),
    (True, False, ["release"]),
])
def test_native_close_keeps_only_owner_opted_owned_server(monkeypatch, attached, resident, expected):
    desktop, calls = _desktop(monkeypatch, attached=attached, resident=resident)
    desktop.main()
    assert calls == expected


def test_login_task_cannot_start_revoked_mode(monkeypatch):
    desktop, calls = _desktop(monkeypatch)
    import core.system_integration as integration
    monkeypatch.setattr(sys, "argv", ["wise_desktop.py", "--background", "--system-session"])
    monkeypatch.setattr(integration, "get_system_integration_settings", lambda: {**integration._DEFAULTS, "enabled": False})
    desktop.main()
    assert calls == []


def test_graceful_stop_targets_owned_server(monkeypatch):
    import wise_desktop as desktop
    joined = []
    server = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(desktop, "_uvicorn_server", server)
    monkeypatch.setattr(desktop, "_server_thread", SimpleNamespace(join=lambda **kw: joined.append(kw), is_alive=lambda: False))
    desktop._stop_owned_backend(timeout=0.1)
    assert server.should_exit and joined == [{"timeout": 0.1}]
    desktop._shutdown_requested.clear()
    desktop._server_shutdown_event.clear()


def test_failed_registration_does_not_persist_enabled(monkeypatch):
    import core.system_integration as integration
    state = {"system_integration": dict(integration._DEFAULTS)}
    monkeypatch.setattr(integration, "load_server_config", lambda: dict(state))
    monkeypatch.setattr(integration, "save_server_config", lambda cfg: state.update(cfg))
    monkeypatch.setattr(integration, "get_security_gate", lambda: SimpleNamespace(evaluate_action=lambda *a, **kw: SimpleNamespace(allowed=True)))
    manager = integration.SystemIntegrationManager()
    monkeypatch.setattr(manager, "_task_exists", lambda: False)
    monkeypatch.setattr(manager, "_register_login_task", lambda: (False, "denied"))
    result = manager.activate(confirmed=True)
    assert not result["ok"] and not state["system_integration"]["enabled"]


def test_login_command_requires_current_consent():
    from core.system_integration import SystemIntegrationManager
    command = SystemIntegrationManager()._login_task_command()
    assert "--system-session" in command and "--background" in command
    assert "--host 127.0.0.1" in command


def test_health_probe_does_not_attach_foreign_or_other_runtime(monkeypatch):
    import hashlib
    import json
    import io
    import wise_desktop as desktop
    from core.paths import RUNTIME_ROOT
    runtime_id = hashlib.sha256(str(RUNTIME_ROOT).casefold().encode()).hexdigest()[:16]
    for payload, expected in (
        ({"status": "ok"}, False),
        ({"status": "ok", "service": "wise", "runtime_id": "other"}, False),
        ({"status": "ok", "service": "wise", "runtime_id": runtime_id}, True),
    ):
        monkeypatch.setattr(desktop, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
        assert desktop._wise_backend_is_running("127.0.0.1", 12345) is expected


def test_failed_disable_cleanup_still_revokes_runtime(monkeypatch):
    import core.system_integration as integration
    state = {"system_integration": {**integration._DEFAULTS, "enabled": True}}
    monkeypatch.setattr(integration, "load_server_config", lambda: dict(state))
    monkeypatch.setattr(integration, "save_server_config", lambda cfg: state.update(cfg))
    monkeypatch.setattr(integration, "get_security_gate", lambda: SimpleNamespace(evaluate_action=lambda *a, **kw: SimpleNamespace(allowed=True)))
    manager = integration.SystemIntegrationManager()
    monkeypatch.setattr(manager, "_task_exists", lambda: True)
    monkeypatch.setattr(manager, "_remove_login_task", lambda: (False, "denied"))
    result = manager.deactivate(confirmed=True)
    assert not result["ok"] and not state["system_integration"]["enabled"]
