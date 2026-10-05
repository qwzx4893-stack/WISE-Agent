"""Resident startup should use the same bounded recovery policy as a later crash."""

from __future__ import annotations

import sys
from types import SimpleNamespace


def test_background_initial_failure_enters_recovery(monkeypatch):
    import wise_desktop

    called = []
    monkeypatch.setattr(sys, "argv", ["wise_desktop.py", "--background"])
    monkeypatch.setattr(wise_desktop, "_wise_backend_is_running", lambda host, port: False)
    monkeypatch.setattr(wise_desktop, "_port_is_available", lambda host, port: True)
    monkeypatch.setattr(wise_desktop, "_start_backend", lambda host, port: None)
    monkeypatch.setattr(wise_desktop, "_wait_for_backend", lambda host, port, timeout: False)
    monkeypatch.setattr(wise_desktop, "_keep_background_backend_alive", lambda host, port: called.append((host, port)))
    monkeypatch.setattr(wise_desktop.signal, "signal", lambda *args: None)

    wise_desktop.main()

    assert called == [(wise_desktop.DEFAULT_HOST, wise_desktop.DEFAULT_PORT)]


def test_desktop_backend_waits_for_health_not_a_bare_port(monkeypatch):
    import wise_desktop

    probes = iter([False, False, True])
    monkeypatch.setattr(wise_desktop, "_wise_backend_is_running", lambda *_args: next(probes))
    monkeypatch.setattr(wise_desktop.time, "sleep", lambda *_args: None)

    assert wise_desktop._wait_for_backend("127.0.0.1", 8765, timeout=5) is True
    assert wise_desktop.BACKEND_STARTUP_TIMEOUT >= 60


def test_pythonw_safe_uvicorn_start_disables_console_log_config(monkeypatch):
    import wise_desktop

    captured = {}
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(
            Config=lambda *args, **kwargs: captured.update(kwargs),
            Server=lambda config: SimpleNamespace(run=lambda: None),
        ),
    )
    wise_desktop._server_shutdown_event.clear()
    wise_desktop._start_backend("127.0.0.1", 8765)
    assert wise_desktop._server_thread is not None
    wise_desktop._server_thread.join(timeout=1)

    assert captured["log_config"] is None
    assert captured["access_log"] is False


def test_health_pulse_uses_local_monitoring_not_conversational_core(monkeypatch):
    from api import server

    calls = []
    monkeypatch.setattr(server, "_run_health_pulse", lambda schedule: calls.append(schedule.id))
    schedule = SimpleNamespace(id="pulse", target="health_pulse", name="pulse")

    server._canonical_scheduler_runner(schedule)

    assert calls == ["pulse"]


def test_detailed_health_reports_scheduler_queue_pressure(monkeypatch):
    from api import server
    import core.scheduler as scheduler

    fake = SimpleNamespace(
        _thread=None,
        list=lambda: [],
        runtime_status=lambda: {"active_runs": 1, "queued_runs": 2, "max_concurrent_runs": 2},
    )
    monkeypatch.setattr(scheduler, "get_scheduler", lambda: fake)

    assert server.health_detailed()["scheduler"] == {
        "available": True,
        "schedule_count": 0,
        "running": False,
        "active_runs": 1,
        "queued_runs": 2,
        "max_concurrent_runs": 2,
    }
