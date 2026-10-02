"""Phase 8 — Apprise-backed channel registry."""

from __future__ import annotations

import types

import pytest


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path, monkeypatch):
    """Redirect the SecretStore + channels singleton to a tmp dir.

    Instead of reloading ``core.paths`` (which would leak into every
    later test in the session), we mutate ``MEMORY_DIR`` in place,
    reload the two consumers, then restore at teardown.
    """
    import core.paths as paths_mod
    import sys
    original_apprise_module = sys.modules.get("apprise")
    original = paths_mod.MEMORY_DIR
    monkeypatch.setenv("AGENT_OS_ROOT", str(tmp_path))
    paths_mod.MEMORY_DIR = tmp_path  # type: ignore[assignment]
    (tmp_path).mkdir(parents=True, exist_ok=True)
    import importlib
    import core.secrets_store
    importlib.reload(core.secrets_store)
    import core.channels.unified
    importlib.reload(core.channels.unified)
    import core.channels
    importlib.reload(core.channels)
    try:
        yield
    finally:
        if original_apprise_module is None:
            sys.modules.pop("apprise", None)
        else:
            sys.modules["apprise"] = original_apprise_module
        paths_mod.MEMORY_DIR = original  # type: ignore[assignment]
        importlib.reload(core.secrets_store)
        importlib.reload(core.channels.unified)
        importlib.reload(core.channels)


def _fake_apprise(module_factory):
    """Install a fake apprise module so tests don't touch the network."""
    import importlib
    import sys
    fake = types.ModuleType("apprise")
    module_factory(fake)
    sys.modules["apprise"] = fake
    import core.channels.unified as u
    importlib.reload(u)
    return u


def test_list_channels_without_apprise(monkeypatch):
    """Graceful fallback when apprise is missing."""
    import importlib
    import sys
    sys.modules.pop("apprise", None)
    monkeypatch.setattr("core.channels.unified._apprise_module",
                         lambda: None)
    import core.channels as ch
    importlib.reload(ch)
    out = ch.list_channels()
    assert out["apprise_available"] is False
    assert out["available_schemes"] == []
    assert out["configured"] == []


def test_send_without_url_fails_cleanly(monkeypatch):
    monkeypatch.setattr("core.channels.unified._apprise_module",
                         lambda: None)
    from core.channels import send_message
    res = send_message("slack", "hi")
    assert res.ok is False
    assert "APPRISE_URL_SLACK" in res.detail


def test_send_without_apprise_installed_fails_cleanly(monkeypatch):
    from core.channels import register_channel, send_message
    register_channel("slack", "slack://T/B/x")
    import sys
    sys.modules.pop("apprise", None)
    import importlib
    import core.channels.unified as u
    importlib.reload(u)
    # Removing sys.modules alone does not simulate an absent installed package
    # and could accidentally invoke the real network transport.
    monkeypatch.setattr(u, "_apprise_module", lambda: None)
    res = u.send_message("slack", "hi")
    assert res.ok is False
    assert "apprise" in res.detail.lower()


def test_send_success_with_fake_apprise():
    calls = {"add": [], "notify": []}

    def factory(fake):
        class FakeAp:
            def __init__(self):
                self._urls = []

            def add(self, url):
                calls["add"].append(url)
                self._urls.append(url)
                return True

            def notify(self, **kwargs):
                calls["notify"].append(kwargs)
                return True

            def details(self):
                return {"schemas": [
                    {"protocols": ["slack"],
                     "secure_protocols": ["slacks"]},
                    {"protocols": ["tgram"],
                     "secure_protocols": []},
                ]}
        fake.Apprise = FakeAp

    u = _fake_apprise(factory)
    u.register_channel("slack", "slack://T/B/x")
    res = u.send_message("slack", "hello", title="alert")
    assert res.ok is True
    # Registration validates without sending; send validates its own target.
    assert calls["add"] == ["slack://T/B/x", "slack://T/B/x"]
    assert calls["notify"][0] == {"body": "hello", "title": "alert"}


def test_send_reports_apprise_failure():
    def factory(fake):
        class FakeAp:
            def add(self, url): return True
            def notify(self, **k): return False
            def details(self): return {"schemas": []}
        fake.Apprise = FakeAp
    u = _fake_apprise(factory)
    u.register_channel("slack", "slack://T/B/x")
    res = u.send_message("slack", "hi")
    assert res.ok is False
    assert "failure" in res.detail


def test_send_reports_rejected_url():
    def factory(fake):
        class FakeAp:
            def add(self, url): return False
            def notify(self, **k): return True
            def details(self): return {"schemas": []}
        fake.Apprise = FakeAp
    u = _fake_apprise(factory)
    with pytest.raises(ValueError, match="Invalid"):
        u.register_channel("slack", "bogus://…")
    res = u.send_message("slack", "hi", url="bogus://…")
    assert res.ok is False
    assert "rejected" in res.detail.lower()


def test_send_catches_apprise_exception():
    def factory(fake):
        class FakeAp:
            def add(self, url):
                raise RuntimeError("network down")
            def details(self): return {"schemas": []}
        fake.Apprise = FakeAp
    u = _fake_apprise(factory)
    with pytest.raises(ValueError, match="validation"):
        u.register_channel("slack", "slack://T/B/x")
    res = u.send_message("slack", "hi", url="slack://T/B/x")
    assert res.ok is False
    assert "RuntimeError" in res.detail


def test_list_channels_reports_configured_and_schemes():
    def factory(fake):
        class FakeAp:
            def __init__(self): self._urls = []
            def add(self, url): return True
            def notify(self, **k): return True
            def details(self):
                return {"schemas": [
                    {"protocols": ["slack"], "secure_protocols": ["slacks"]},
                    {"protocols": ["tgram"], "secure_protocols": []},
                    {"protocols": ["discord"], "secure_protocols": []},
                ]}
        fake.Apprise = FakeAp
    u = _fake_apprise(factory)
    u.register_channel("slack_prod", "slack://T/B/x")
    u.register_channel("alerts", "tgram://bot/chat")
    out = u.list_channels()
    names = sorted(c["name"] for c in out["configured"])
    assert names == ["alerts", "slack_prod"]
    assert "slack" in out["available_schemes"]
    assert "tgram" in out["available_schemes"]
    assert "discord" in out["available_schemes"]


def test_unregister_removes_stored_url():
    def factory(fake):
        class FakeAp:
            def add(self, url): return True
            def notify(self, **k): return True
            def details(self): return {"schemas": []}
        fake.Apprise = FakeAp
    u = _fake_apprise(factory)
    u.register_channel("slack", "slack://T/B/x")
    assert u.get_registry().get_url("slack") == "slack://T/B/x"
    assert u.unregister_channel("slack") is True
    assert u.get_registry().get_url("slack") is None
    # second delete is a no-op
    assert u.unregister_channel("slack") is False


def test_one_off_url_override_bypasses_stored():
    recorded = []

    def factory(fake):
        class FakeAp:
            def add(self, url):
                recorded.append(url)
                return True
            def notify(self, **k): return True
            def details(self): return {"schemas": []}
        fake.Apprise = FakeAp
    u = _fake_apprise(factory)
    u.register_channel("alerts", "slack://stored")
    res = u.send_message("alerts", "hi",
                           url="discord://override/webhook")
    assert res.ok is True
    assert recorded == ["slack://stored", "discord://override/webhook"]


def test_api_endpoints(monkeypatch):
    """End-to-end: register → list → send → unregister via FastAPI."""
    def factory(fake):
        class FakeAp:
            def add(self, url): return True
            def notify(self, **k): return True
            def details(self):
                return {"schemas": [{"protocols": ["slack"],
                                      "secure_protocols": []}]}
        fake.Apprise = FakeAp
    _fake_apprise(factory)

    from fastapi.testclient import TestClient
    from api.server import app
    monkeypatch.setenv("AGENT_OS_API_TOKEN", "secret")
    c = TestClient(app)
    H = {"X-Agent-Token": "secret"}

    r = c.put("/admin/channels/slack",
                json={"url": "slack://T/B/x"}, headers=H)
    assert r.status_code == 200

    r = c.get("/admin/channels", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert any(x["name"] == "slack" for x in body["configured"])

    r = c.post("/admin/channels/slack/send",
                 json={"message": "deploy done", "title": "CI"},
                 headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = c.delete("/admin/channels/slack", headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_api_send_requires_message(monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    monkeypatch.setenv("AGENT_OS_API_TOKEN", "secret")
    c = TestClient(app)
    r = c.post("/admin/channels/slack/send",
                 json={}, headers={"X-Agent-Token": "secret"})
    assert r.status_code == 400
