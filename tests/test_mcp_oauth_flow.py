"""Offline OAuth protocol tests; no real account login or credential use."""
import io
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, urlencode

import pytest
from core.mcp import oauth


@pytest.fixture
def flow(monkeypatch):
    cfg = SimpleNamespace(transport="http", url="https://mcp.example/mcp", headers={})
    registry = SimpleNamespace(_find=lambda name: cfg, _save=lambda: None,
                               start=lambda name: {"alive": True})
    captured = {"stored": [], "requests": []}
    issuer = "https://auth.example"
    metadata = {"resource": cfg.url, "authorization_servers": [issuer]}
    authorization = {"issuer": issuer, "authorization_endpoint": issuer + "/authorize",
        "token_endpoint": issuer + "/token", "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True}
    def get_json(url, **kwargs):
        captured["requests"].append((url, kwargs))
        if url.endswith("/token"):
            return {"token_type": "Bearer", "access_token": "offline-token"}
        return metadata if "oauth-protected-resource" in url else authorization
    monkeypatch.setattr(oauth, "_json", get_json)
    monkeypatch.setattr(oauth, "public_url", lambda url: url)
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, method, url, **kwargs):
            return nullcontext(SimpleNamespace(headers={"www-authenticate":
                'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource", scope="docs:read"'}))
    monkeypatch.setattr(oauth.httpx, "Client", Client)
    class Server:
        server_port = 54321
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 0)
            captured["handler"] = handler
        def serve_forever(self, **kwargs): pass
        def shutdown(self): pass
        def server_close(self): pass
    monkeypatch.setattr(oauth, "HTTPServer", Server)
    monkeypatch.setattr(oauth.threading, "Timer", lambda *args: SimpleNamespace(daemon=False, start=lambda: None))
    import core.secrets_store as secrets
    monkeypatch.setattr(secrets, "SecretStore", lambda: SimpleNamespace(set=lambda k, v: captured["stored"].append((k, v))))
    oauth._FLOWS.clear()
    captured.update(metadata=metadata, authorization=authorization, cfg=cfg, registry=registry)
    yield captured
    oauth._FLOWS.clear()


def callback(flow, state, **query):
    handler = object.__new__(flow["handler"])
    handler.path = "/oauth/callback?" + urlencode({"state": state, "code": "offline-code", **query})
    handler.wfile = io.BytesIO()
    codes = []
    handler.send_response = codes.append
    handler.end_headers = lambda: None
    handler.do_GET()
    return codes[0]


def test_pkce_resource_binding_and_bearer_secret(flow):
    started = oauth.start("docs", flow["registry"], client_id="public-client")
    query = parse_qs(urlsplit(started["auth_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [flow["cfg"].url]
    assert query["scope"] == ["docs:read"]
    assert len(query["code_challenge"][0]) == 43
    assert callback(flow, started["state"], iss="https://auth.example") == 200
    assert oauth.status(started["state"])["status"] == "ok"
    assert flow["stored"][-1] == ("MCP_docs_oauth_access_token", "Bearer offline-token")
    assert '"resource": "https://mcp.example/mcp"' in flow["stored"][0][1]
    assert "offline-token" not in flow["cfg"].headers["Authorization"]
    token_request = flow["requests"][-1][1]["form"]
    assert token_request["resource"] == flow["cfg"].url
    assert len(token_request["code_verifier"]) >= 43


def test_wrong_state_never_exchanges_tokens(flow):
    started = oauth.start("docs", flow["registry"], client_id="client")
    assert callback(flow, "wrong-state", iss="https://auth.example") == 400
    assert oauth.status(started["state"])["status"] == "pending"
    assert not flow["stored"]


@pytest.mark.parametrize("issuer", ["", "https://other.example"])
def test_missing_or_mismatched_callback_issuer_is_rejected(flow, issuer):
    started = oauth.start("docs", flow["registry"], client_id="client")
    assert callback(flow, started["state"], iss=issuer) == 400
    assert oauth.status(started["state"])["status"] == "error"
    assert not flow["stored"]


def test_resource_metadata_mismatch_is_rejected(flow):
    flow["metadata"]["resource"] = "https://another.example/mcp"
    with pytest.raises(ValueError, match="does not match"):
        oauth.start("docs", flow["registry"], client_id="client")


def test_private_discovery_address_is_rejected(monkeypatch):
    monkeypatch.setattr(oauth.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="private"):
        oauth.public_url("https://public-name.example/mcp")


def test_origin_resource_is_bound_to_auth_and_token_requests(flow):
    flow["metadata"]["resource"] = "https://mcp.example"
    started = oauth.start("docs", flow["registry"], client_id="client")
    assert parse_qs(urlsplit(started["auth_url"]).query)["resource"] == ["https://mcp.example"]
    assert callback(flow, started["state"], iss="https://auth.example") == 200
    assert flow["requests"][-1][1]["form"]["resource"] == "https://mcp.example"


@pytest.mark.parametrize("resource", ["https://mcp.example/m", "https://mcp.example/mcp?other=1", "https://mcp.example/other"])
def test_resource_prefix_must_be_complete_and_not_ambiguous(flow, resource):
    flow["metadata"]["resource"] = resource
    with pytest.raises(ValueError, match="does not match"):
        oauth.start("docs", flow["registry"], client_id="client")


def test_context7_authorize_switches_to_documented_oauth_transport(flow):
    flow["cfg"].url = "https://mcp.context7.com/mcp"
    flow["metadata"]["resource"] = "https://mcp.context7.com"
    stopped = []
    flow["registry"].stop = stopped.append
    result = oauth.start("docs", flow["registry"], client_id="client")
    assert stopped == []
    assert flow["cfg"].url == "https://mcp.context7.com/mcp"
    assert parse_qs(urlsplit(result["auth_url"]).query)["resource"] == ["https://mcp.context7.com"]
    assert callback(flow,result["state"],iss="https://auth.example") == 200
    assert flow["cfg"].url == "https://mcp.context7.com/mcp/oauth"


def test_public_server_does_not_invent_oauth(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, *args): return nullcontext(SimpleNamespace(status_code=200, headers={}))
    monkeypatch.setattr(oauth.httpx, "Client", Client)
    monkeypatch.setattr(oauth, "public_url", lambda url: url)
    registry = SimpleNamespace(_find=lambda name: SimpleNamespace(transport="http",url="https://public.example/mcp"))
    assert oauth.start("public", registry)["requires_auth"] is False


def test_refresh_rotation_is_protected_and_not_replayed(monkeypatch):
    import json
    import core.secrets_store as secrets
    stored = {"MCP_docs_oauth_session": json.dumps({"expires_at":1, "client_id":"client",
        "refresh_token":"offline-refresh", "token_endpoint":"https://auth.example/token", "resource":"https://mcp.example"})}
    monkeypatch.setattr(secrets, "SecretStore", lambda: SimpleNamespace(get=stored.get, set=stored.__setitem__))
    calls = []
    def exchange(url, **kwargs):
        calls.append(kwargs["form"])
        return {"token_type":"Bearer", "access_token":"new-offline", "refresh_token":"rotated-offline", "expires_in":3600}
    monkeypatch.setattr(oauth, "_json", exchange)
    cfg = SimpleNamespace(transport="http",url="https://mcp.example/mcp",headers={"Authorization":"${wise-secret:MCP_docs_oauth_access_token}"})
    registry = SimpleNamespace(_find=lambda name: cfg)
    assert oauth.refresh_if_needed("docs", registry) is True
    assert oauth.refresh_if_needed("docs", registry) is False
    assert len(calls) == 1 and calls[0]["grant_type"] == "refresh_token"
    assert json.loads(stored["MCP_docs_oauth_session"])["refresh_token"] == "rotated-offline"
    assert stored["MCP_docs_oauth_access_token"] == "Bearer new-offline"


def test_expired_session_without_refresh_requires_human(monkeypatch):
    import core.secrets_store as secrets
    monkeypatch.setattr(secrets, "SecretStore", lambda: SimpleNamespace(get=lambda key:'{"expires_at":1,"resource":"https://mcp.example"}'))
    cfg = SimpleNamespace(transport="http",url="https://mcp.example/mcp",headers={"Authorization":"${wise-secret:MCP_docs_oauth_access_token}"})
    with pytest.raises(ValueError, match="authorize again"):
        oauth.refresh_if_needed("docs", SimpleNamespace(_find=lambda name:cfg))


def test_changed_configuration_during_login_rejects_exchange(flow):
    started = oauth.start("docs",flow["registry"],client_id="client")
    flow["cfg"].url = "https://another.example/mcp"
    assert callback(flow,started["state"],iss="https://auth.example") == 400
    assert not flow["stored"]


def test_changed_configuration_during_token_exchange_is_not_saved(flow, monkeypatch):
    started = oauth.start("docs", flow["registry"], client_id="client")
    original_json = oauth._json
    def exchange(url, **kwargs):
        response = original_json(url, **kwargs)
        flow["cfg"].url = "https://another.example/mcp"
        return response
    monkeypatch.setattr(oauth, "_json", exchange)
    assert callback(flow, started["state"], iss="https://auth.example") == 400
    assert not flow["stored"]
    assert not flow["cfg"].headers


def test_managed_token_is_never_reused_on_another_origin(monkeypatch):
    import core.secrets_store as secrets
    monkeypatch.setattr(secrets,"SecretStore",lambda:SimpleNamespace(get=lambda key:'{"resource":"https://mcp.example"}'))
    cfg = SimpleNamespace(transport="http",url="https://another.example/mcp",headers={"Authorization":"${wise-secret:MCP_docs_oauth_access_token}"})
    with pytest.raises(ValueError,match="resource differs"):
        oauth.refresh_if_needed("docs",SimpleNamespace(_find=lambda name:cfg))
