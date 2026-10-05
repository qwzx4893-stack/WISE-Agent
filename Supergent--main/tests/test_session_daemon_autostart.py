"""The legacy session daemon must never create an always-elevated task."""

from types import SimpleNamespace


def test_scheduler_autostart_is_limited_interactive_user_task(monkeypatch, tmp_path):
    import core.windows.session_daemon as daemon_module

    daemon = daemon_module.WindowsSessionDaemon()
    calls = []
    monkeypatch.setattr(daemon, "_audit_onboarding", lambda: None)
    monkeypatch.setattr(daemon_module.os, "environ", {"USERNAME": "owner"})
    monkeypatch.setattr(
        daemon_module.subprocess,
        "run",
        lambda args, **kwargs: (calls.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout="ok", stderr="")),
    )

    result = daemon.register_autostart(str(tmp_path / "wise.py"), use_scheduler=True)

    assert result["success"] is True
    args, kwargs = calls[0]
    lowered = [str(value).lower() for value in args]
    assert "limited" in lowered
    assert "highest" not in lowered
    assert "/it" in lowered
    assert "shell" not in kwargs
