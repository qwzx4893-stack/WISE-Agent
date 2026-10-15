"""Offline request/handshake contract: no runtime, model or network dispatch."""
from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.websockets import WebSocket


def _scope(kind="http", *, peer="127.0.0.1", path="/api/v2/chat",
           host="127.0.0.1:8765", origin=None, headers=(), query=b""):
    raw = [(b"host", host.encode())] if host is not None else []
    if origin is not None:
        raw.append((b"origin", origin.encode()))
    raw.extend((k.lower().encode(), v.encode()) for k, v in headers)
    return {"type": kind, "asgi": {"version": "3.0"}, "method": "POST",
            "scheme": "ws" if kind == "websocket" else "http", "path": path,
            "root_path": "", "query_string": query, "headers": raw,
            "client": (peer, 43210), "server": ("127.0.0.1", 8765)}


@pytest.fixture(autouse=True)
def _local_config(monkeypatch):
    from api import server
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    monkeypatch.setattr(server, "load_server_config", lambda: {
        "host": "127.0.0.1", "cors_origins": ["http://localhost:8765", "http://127.0.0.1:8765"],
    })


def _http_status(scope):
    from api.server import _api_access_control
    called = []

    async def dispatch(_request):
        called.append(True)
        return Response(status_code=204)

    response = asyncio.run(_api_access_control(Request(scope), dispatch))
    return response.status_code, bool(called)


@pytest.mark.parametrize("path", ["/chat", "/execute", "/admin/reload-tools",
                                      "/api/v2/chat", "/mcp/jsonrpc"])
@pytest.mark.parametrize("headers", [
    {"host": "rebound.attacker.example:8765"},
    {"origin": "https://attacker.example"},
    {"origin": "null"},
    {"headers": (("sec-fetch-site", "cross-site"),)},
])
def test_http_control_rejects_browser_ingress_before_dispatch(path, headers):
    assert _http_status(_scope(path=path, **headers)) == (403, False)


@pytest.mark.parametrize("headers", [
    {"host": None}, {"host": "localhost.attacker.example"},
    {"host": "localhost@attacker.example"}, {"host": "127.0.0.1:bad"},
    {"host": "[::1]attacker.example"}, {"host": "127.0.0.1:8765/path"},
    {"host": "[::1"}, {"host": "127.0.0.1:99999"},
    {"origin": "http://127.0.0.1:8765/path"},
    {"headers": (("host", "attacker.example"),)},
    {"headers": (("origin", "http://127.0.0.1:8765"), ("origin", "https://attacker.example"))},
])
def test_malformed_or_ambiguous_authorities_fail_closed(headers):
    assert _http_status(_scope(**headers)) == (403, False)


@pytest.mark.parametrize("headers", [
    {}, {"origin": "http://127.0.0.1:8765"},
    {"origin": "http://localhost:8765"},
    {"host": "localhost:60219", "origin": "http://localhost:60219"},
    {"host": "[::1]:8765", "origin": "http://[::1]:8765", "peer": "::1"},
    {"host": "testserver", "peer": "testclient"},
    {"host": "testserver", "peer": "testclient", "origin": "http://testserver"},
])
def test_local_browser_native_and_test_transport_controls(headers):
    assert _http_status(_scope(**headers)) == (204, True)


def test_remote_peer_cannot_spoof_loopback_with_forwarding_headers():
    assert _http_status(_scope(peer="198.51.100.10", headers=(
        ("x-forwarded-for", "127.0.0.1"), ("forwarded", "for=127.0.0.1"),
    ))) == (401, False)


def test_real_peer_cannot_use_test_transport_host():
    assert _http_status(_scope(host="testserver")) == (403, False)


@pytest.mark.parametrize("peer", ["198.51.100.10", "10.0.0.5", "unknown"])
def test_non_loopback_peers_never_gain_tokenless_access(peer):
    assert _http_status(_scope(peer=peer)) == (401, False)


@pytest.mark.parametrize("endpoint", ["chat_stream", "v2_stream", "api_v2_stream_endpoint"])
def test_configured_websocket_token_is_required_even_locally(endpoint, monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "synthetic-token")
    for kwargs in ({}, {"query": b"token=incorrect"}, {"headers": (("x-agent-token", "incorrect"),)}):
        assert _handshake(endpoint, _scope("websocket", **kwargs))[0]["type"] == "websocket.close"


def test_public_read_routes_retain_the_existing_boundary():
    scope = _scope(path="/health", host="public.example", origin="https://public.example")
    scope["method"] = "GET"
    assert _http_status(scope) == (204, True)


def test_explicit_remote_origin_and_token_are_both_required(monkeypatch):
    from api import server
    monkeypatch.setenv("AGENT_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(server, "load_server_config", lambda: {
        "host": "0.0.0.0", "cors_origins": ["https://api.example", "https://ui.example"],
    })
    kwargs = dict(peer="198.51.100.10", host="api.example", origin="https://ui.example",
                  headers=(("x-agent-token", "synthetic-token"), ("sec-fetch-site", "cross-site")))
    assert _http_status(_scope(**kwargs)) == (204, True)
    assert _http_status(_scope(**{**kwargs, "headers": ()})) == (401, False)
    assert _http_status(_scope(**{**kwargs, "origin": "https://unlisted.example"})) == (403, False)
    assert _http_status(_scope(**{**kwargs, "host": "unlisted.example"})) == (403, False)


def test_wildcard_cors_and_bind_do_not_enable_arbitrary_authorities(monkeypatch):
    from api import server
    monkeypatch.setenv("AGENT_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(server, "load_server_config", lambda: {"host": "0.0.0.0", "cors_origins": ["*"]})
    assert _http_status(_scope(host="attacker.example", origin="https://attacker.example",
                              headers=(("x-agent-token", "synthetic-token"),))) == (403, False)
    assert _http_status(_scope(origin="https://attacker.example",
                              headers=(("x-agent-token", "synthetic-token"),))) == (403, False)


def test_explicit_remote_bind_supports_native_clients_without_reflecting_origin(monkeypatch):
    from api import server
    monkeypatch.setenv("AGENT_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(server, "load_server_config", lambda: {
        "host": "198.51.100.20", "cors_origins": [],
    })
    kwargs = dict(peer="198.51.100.10", host="198.51.100.20:8765",
                  headers=(("authorization", "Bearer synthetic-token"),))
    assert _http_status(_scope(**kwargs)) == (204, True)
    assert _http_status(_scope(**{**kwargs, "headers": ()})) == (401, False)
    assert _http_status(_scope(**kwargs, origin="http://198.51.100.20:8765")) == (403, False)


def _handshake(endpoint_name, scope):
    from api import server
    sent = []
    incoming = iter([{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}])

    async def receive():
        return next(incoming)

    async def send(message):
        sent.append(message)

    asyncio.run(getattr(server, endpoint_name)(WebSocket(scope, receive, send)))
    return sent


@pytest.mark.parametrize("endpoint", ["chat_stream", "v2_stream", "api_v2_stream_endpoint"])
@pytest.mark.parametrize("kwargs", [
    {"peer": "198.51.100.10"}, {"host": "rebound.attacker.example"},
    {"origin": "https://attacker.example"}, {"origin": "null"},
    {"headers": (("sec-fetch-site", "cross-site"),)},
    {"peer": "198.51.100.10", "headers": (("x-forwarded-for", "127.0.0.1"),)},
])
def test_each_websocket_rejects_before_accept(endpoint, kwargs):
    sent = _handshake(endpoint, _scope("websocket", **kwargs))
    assert sent[0]["type"] == "websocket.close"
    assert not any(message["type"] == "websocket.accept" for message in sent)
    assert sent[0]["code"] in {1008, 4401, 4403}


@pytest.mark.parametrize("endpoint", ["chat_stream", "v2_stream", "api_v2_stream_endpoint"])
@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8765", "http://localhost:8765"])
def test_each_websocket_accepts_local_browser_and_native_clients(endpoint, origin):
    assert _handshake(endpoint, _scope("websocket", origin=origin))[0]["type"] == "websocket.accept"


@pytest.mark.parametrize("endpoint", ["chat_stream", "v2_stream", "api_v2_stream_endpoint"])
def test_valid_websocket_token_does_not_override_foreign_origin(endpoint, monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "synthetic-token")
    assert _handshake(endpoint, _scope("websocket", origin="https://attacker.example",
                                      query=b"token=synthetic-token"))[0]["type"] == "websocket.close"


@pytest.mark.parametrize("endpoint", ["chat_stream", "v2_stream", "api_v2_stream_endpoint"])
@pytest.mark.parametrize("auth", [{"query": b"token=synthetic-token"},
                                  {"headers": (("x-agent-token", "synthetic-token"),)}])
def test_remote_websocket_explicit_configuration_control(endpoint, auth, monkeypatch):
    from api import server
    monkeypatch.setenv("AGENT_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(server, "load_server_config", lambda: {
        "host": "0.0.0.0", "cors_origins": ["https://api.example", "https://ui.example"],
    })
    sent = _handshake(endpoint, _scope("websocket", peer="198.51.100.10", host="api.example",
                                      origin="https://ui.example", **auth))
    assert sent[0]["type"] == "websocket.accept"
