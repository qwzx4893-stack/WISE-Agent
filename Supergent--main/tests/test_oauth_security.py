"""Legacy OAuth callback regressions using synthetic state and no sockets/secrets."""

import io
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.oauth import flow as oauth


@pytest.fixture
def synthetic_oauth(monkeypatch):
    with oauth._LOCK:
        oauth._FLOWS.clear()
    saved = []
    exchanged = []
    monkeypatch.setattr(oauth, "_client_credentials", lambda _: {
        "client_id": "synthetic-client", "client_secret": ""})

    def exchange(url, params):
        exchanged.append(dict(params))
        return {"access_token": "synthetic-token", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_http_post_form", exchange)
    monkeypatch.setattr(oauth, "_http_get_json", lambda *a, **kw: {
        "email": "synthetic@example.invalid"})
    monkeypatch.setattr(oauth, "save_account", saved.append)

    def start():
        return oauth.start_link_flow("google", ["openid"], start_loopback=False,
                                     redirect_uri_override="http://127.0.0.1:12345/oauth/callback")

    yield SimpleNamespace(start=start, exchanged=exchanged, saved=saved)
    with oauth._LOCK:
        oauth._FLOWS.clear()


def callback(monkeypatch, flow, path):
    """Run the actual handler against an in-memory HTTP request/response."""
    handler = oauth._CallbackHandler.__new__(oauth._CallbackHandler)
    handler.path = path
    # Legacy handler used a class fallback; set it to show missing-state risk.
    handler.state_id = flow.state
    handler.server = SimpleNamespace(oauth_state_id=flow.state,
                                     shutdown=Mock(), server_close=Mock())
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    monkeypatch.setattr(oauth.threading, "Thread", lambda target, **kw:
                        SimpleNamespace(start=target))
    handler.do_GET()
    return handler


@pytest.mark.parametrize("query", [
    "code=C", "state=&code=C", "state=wrong&code=C",
    "state={state}&state={state}&code=C", "state={state}&code=C&code=D",
    "state={state}&code=C&error=denied", "state={state}&code=",
    "state={state}&error=denied&error=other",
])
def test_invalid_callback_does_not_exchange_consume_or_shutdown(synthetic_oauth, monkeypatch, query):
    flow = synthetic_oauth.start()
    handler = callback(monkeypatch, flow, "/oauth/callback?" + query.format(state=flow.state))
    assert handler.send_response.call_args.args[0] == 400
    assert not synthetic_oauth.exchanged
    assert not synthetic_oauth.saved
    assert flow.status == "pending"
    handler.server.shutdown.assert_not_called()


@pytest.mark.parametrize("path", [
    "/favicon.ico", "/oauth/callback/", "/oauth/../oauth/callback",
    "/oauth/callback;extra", "http://elsewhere.invalid/oauth/callback",
])
def test_callback_requires_exact_registered_path(synthetic_oauth, monkeypatch, path):
    flow = synthetic_oauth.start()
    handler = callback(monkeypatch, flow, f"{path}?state={flow.state}&code=C")
    assert handler.send_response.call_args.args[0] == 400
    assert not synthetic_oauth.exchanged
    assert flow.status == "pending"
    handler.server.shutdown.assert_not_called()


def test_callback_cannot_complete_another_listeners_state(synthetic_oauth, monkeypatch):
    first, second = synthetic_oauth.start(), synthetic_oauth.start()
    handler = callback(monkeypatch, first, f"/oauth/callback?state={second.state}&code=C")
    assert handler.send_response.call_args.args[0] == 400
    assert first.status == second.status == "pending"
    assert not synthetic_oauth.exchanged
    handler.server.shutdown.assert_not_called()


def test_valid_callback_completes_and_releases_listener(synthetic_oauth, monkeypatch):
    flow = synthetic_oauth.start()
    handler = callback(monkeypatch, flow, f"/oauth/callback?state={flow.state}&code=C")
    assert handler.send_response.call_args.args[0] == 200
    assert flow.status == "ok"
    assert len(synthetic_oauth.exchanged) == len(synthetic_oauth.saved) == 1
    assert synthetic_oauth.exchanged[0]["code_verifier"] == flow.code_verifier
    handler.server.shutdown.assert_called_once()
    handler.server.server_close.assert_called_once()


def test_provider_error_requires_matching_state_and_is_terminal(synthetic_oauth, monkeypatch):
    flow = synthetic_oauth.start()
    invalid = callback(monkeypatch, flow, "/oauth/callback?error=denied")
    assert invalid.send_response.call_args.args[0] == 400
    assert flow.status == "pending"
    valid = callback(monkeypatch, flow, f"/oauth/callback?state={flow.state}&error=denied")
    assert flow.status == "error"
    valid.server.shutdown.assert_called_once()
    with pytest.raises(RuntimeError):
        oauth.complete_link_flow(flow.state, "C")
    assert not synthetic_oauth.exchanged


@pytest.mark.parametrize("via_callback", [False, True])
def test_expired_state_rejected_before_exchange(synthetic_oauth, monkeypatch, via_callback):
    flow = synthetic_oauth.start()
    now, wall = time.monotonic(), time.time()
    monkeypatch.setattr(oauth.time, "monotonic", lambda: now + 601)
    monkeypatch.setattr(oauth.time, "time", lambda: wall + 601)
    if via_callback:
        handler = callback(monkeypatch, flow, f"/oauth/callback?state={flow.state}&code=C")
        assert handler.send_response.call_args.args[0] == 400
    else:
        with pytest.raises(RuntimeError):
            oauth.complete_link_flow(flow.state, "C")
    assert flow.status == "error"
    assert not synthetic_oauth.exchanged
    assert not synthetic_oauth.saved


def test_completed_state_cannot_be_replayed_or_changed(synthetic_oauth, monkeypatch):
    flow = synthetic_oauth.start()
    oauth.complete_link_flow(flow.state, "C")
    with pytest.raises(RuntimeError):
        oauth.complete_link_flow(flow.state, "D")
    handler = callback(monkeypatch, flow, f"/oauth/callback?state={flow.state}&error=denied")
    assert handler.send_response.call_args.args[0] == 400
    assert flow.status == "ok" and flow.error == ""
    assert len(synthetic_oauth.exchanged) == len(synthetic_oauth.saved) == 1


def test_exchange_failure_is_terminal(synthetic_oauth, monkeypatch):
    flow = synthetic_oauth.start()
    exchange = Mock(side_effect=RuntimeError("synthetic rejection"))
    monkeypatch.setattr(oauth, "_http_post_form", exchange)
    with pytest.raises(RuntimeError):
        oauth.complete_link_flow(flow.state, "C")
    with pytest.raises(RuntimeError):
        oauth.complete_link_flow(flow.state, "D")
    assert flow.status == "error"
    assert exchange.call_count == 1


def test_concurrent_completion_claims_state_before_exchange(synthetic_oauth, monkeypatch):
    flow = synthetic_oauth.start()
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []

    def exchange(url, params):
        calls.append(params["code"])
        if len(calls) > 1:
            raise RuntimeError("duplicate synthetic exchange")
        entered.set()
        assert release.wait(5)
        return {"access_token": "synthetic-token"}

    def finish():
        try:
            oauth.complete_link_flow(flow.state, "FIRST")
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(oauth, "_http_post_form", exchange)
    worker = threading.Thread(target=finish)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RuntimeError):
            oauth.complete_link_flow(flow.state, "SECOND")
        assert calls == ["FIRST"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert flow.status == "ok" and len(synthetic_oauth.saved) == 1


def test_cancelled_state_and_empty_code_rejected(synthetic_oauth):
    flow = synthetic_oauth.start()
    with pytest.raises(RuntimeError):
        oauth.complete_link_flow(flow.state, "")
    assert flow.status == "pending"
    assert oauth.cancel_link_flow(flow.state)
    with pytest.raises(RuntimeError):
        oauth.complete_link_flow(flow.state, "C")
    assert not synthetic_oauth.exchanged


def test_fresh_manual_flow_retains_pkce_and_public_status(synthetic_oauth):
    flow = synthetic_oauth.start()
    assert oauth.get_link_status(flow.state)["status"] == "pending"
    account = oauth.complete_link_flow(flow.state, "C")
    assert account.account == "synthetic@example.invalid"
    assert oauth.get_link_status(flow.state)["status"] == "ok"
    assert synthetic_oauth.exchanged[0]["redirect_uri"] == flow.redirect_uri
    assert synthetic_oauth.exchanged[0]["code_verifier"] == flow.code_verifier
    assert len(synthetic_oauth.saved) == 1


def test_unknown_cancelled_state_already_rejected(synthetic_oauth):
    flow = synthetic_oauth.start()
    assert oauth.cancel_link_flow(flow.state)
    with pytest.raises(RuntimeError):
        oauth.complete_link_flow(flow.state, "C")
    assert not synthetic_oauth.exchanged and not synthetic_oauth.saved


def test_decline_on_expired_flow_releases_listener(synthetic_oauth, monkeypatch):
    flow = synthetic_oauth.start()
    now = time.monotonic()
    monkeypatch.setattr(oauth.time, "monotonic", lambda: now + 601)
    handler = callback(monkeypatch, flow, f"/oauth/callback?state={flow.state}&error=denied")
    assert handler.send_response.call_args.args[0] == 400
    assert flow.status == "error" and flow.error == "OAuth flow expired"
    handler.server.shutdown.assert_called_once()
    assert not synthetic_oauth.exchanged


def test_loopback_servers_bind_state_independently(synthetic_oauth, monkeypatch):
    servers = []

    def server(address, handler):
        instance = SimpleNamespace(server_address=("127.0.0.1", 12345 + len(servers)),
                                   serve_forever=Mock())
        servers.append(instance)
        return instance

    monkeypatch.setattr(oauth, "HTTPServer", server)
    monkeypatch.setattr(oauth.threading, "Thread", lambda **kw: SimpleNamespace(start=Mock()))
    first = oauth.start_link_flow("google", ["openid"])
    second = oauth.start_link_flow("google", ["openid"])
    assert first.httpd.oauth_state_id == first.state
    assert second.httpd.oauth_state_id == second.state
    assert first.state != second.state


def test_cancellation_during_exchange_prevents_late_persistence(synthetic_oauth, monkeypatch):
    flow = synthetic_oauth.start()

    def exchange(url, params):
        assert oauth.cancel_link_flow(flow.state)
        return {"access_token": "synthetic-token"}

    monkeypatch.setattr(oauth, "_http_post_form", exchange)
    with pytest.raises(RuntimeError, match="cancelled"):
        oauth.complete_link_flow(flow.state, "C")
    assert not synthetic_oauth.saved


def test_polling_expires_pending_flow_and_releases_listener(synthetic_oauth, monkeypatch):
    flow = synthetic_oauth.start()
    flow.httpd = SimpleNamespace(shutdown=Mock(), server_close=Mock())
    now = time.monotonic()
    monkeypatch.setattr(oauth.time, "monotonic", lambda: now + 601)
    monkeypatch.setattr(oauth.threading, "Thread", lambda target, **kw:
                        SimpleNamespace(start=target))
    assert oauth.get_link_status(flow.state)["status"] == "error"
    assert oauth.get_link_status(flow.state)["error"] == "OAuth flow expired"
    flow.httpd.shutdown.assert_called_once()
    flow.httpd.server_close.assert_called_once()
    assert not synthetic_oauth.exchanged
