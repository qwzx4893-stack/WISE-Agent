"""Tests for core.oauth (Phase 10 Part 3).

We mock both the user's OAuth provider (to avoid Google/GitHub/Microsoft
network round-trips) and the encrypted KeyStore (so secrets don't leak
into developer machines). The tests cover:

  - PKCE state generation
  - scope resolution
  - end-to-end token exchange via a fake provider
  - refresh-on-expiry
  - revocation
  - scope enforcement on Google tools
  - admin endpoints (start / status / complete / revoke / accounts)
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
import urllib.parse
from typing import Any, Dict

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def isolated_keystore(tmp_path, monkeypatch):
    """Redirect MEMORY_DIR so KeyStore writes to tmp_path."""
    import core.paths as paths
    monkeypatch.setattr(paths, "MEMORY_DIR", tmp_path)
    yield tmp_path


@pytest.fixture()
def fake_provider(monkeypatch):
    """Spin up a fake OAuth provider on 127.0.0.1 and patch endpoints."""
    state: Dict[str, Any] = {"code_seen": None,
                              "verifier_seen": None,
                              "issued": []}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            params = {k: v[0]
                       for k, v in urllib.parse.parse_qs(body).items()}
            path = self.path.rstrip("/")
            if path.endswith("/token"):
                grant = params.get("grant_type")
                if grant == "authorization_code":
                    state["code_seen"] = params.get("code")
                    state["verifier_seen"] = params.get("code_verifier")
                    payload = {
                        "access_token": "AT-1",
                        "refresh_token": "RT-1",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    }
                elif grant == "refresh_token":
                    state["issued"].append(params.get("refresh_token"))
                    payload = {
                        "access_token": "AT-2",
                        "refresh_token": "RT-2",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    }
                else:
                    payload = {"error": "unsupported_grant_type"}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path.endswith("/revoke"):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/userinfo"):
                payload = {"email": "user@example.com",
                           "sub": "x",
                           "name": "User"}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def log_message(self, *a, **kw):  # noqa: N802
            return

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"

    # Replace the GOOGLE provider's endpoints with the fake's.
    import core.oauth.providers as prov_mod
    fake = prov_mod.OAuthProvider(
        id="google",
        display_name="Google",
        auth_endpoint=base + "/auth",
        token_endpoint=base + "/token",
        revoke_endpoint=base + "/revoke",
        userinfo_endpoint=base + "/userinfo",
        client_id_key="oauth.google.client_id",
        client_secret_key="oauth.google.client_secret",
    )
    monkeypatch.setitem(prov_mod.OAUTH_PROVIDERS, "google", fake)

    yield {"base": base, "state": state, "provider": fake}
    srv.shutdown()


@pytest.fixture()
def with_client_creds(isolated_keystore):
    """Pre-populate fake client_id / client_secret in the keystore."""
    from core.llm.keystore import KeyStore
    ks = KeyStore()
    ks.add(name="oauth.google.client_id", api_key="cid-test",
            provider="oauth.client", model="oauth", enabled=True)
    ks.add(name="oauth.google.client_secret", api_key="csec-test",
            provider="oauth.client", model="oauth", enabled=True)
    yield


@pytest.fixture(autouse=True)
def reset_flow_state():
    from core.oauth import flow as f
    with f._LOCK:
        f._FLOWS.clear()
    yield
    with f._LOCK:
        f._FLOWS.clear()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------
def test_scope_resolution_dedup_and_openid_default():
    from core.oauth import resolve_scopes
    out = resolve_scopes("google", ["gmail.send", "gmail.send"])
    assert "openid" in out
    # gmail.send url appears once.
    assert out.count("https://www.googleapis.com/auth/gmail.send") == 1


def test_scope_resolution_unknown_provider():
    from core.oauth import resolve_scopes
    assert resolve_scopes("nope", ["x"]) == []


def test_pkce_pair_unique():
    from core.oauth.flow import _pkce_pair
    a = _pkce_pair()
    b = _pkce_pair()
    assert a != b
    assert len(a["verifier"]) >= 43
    assert len(a["challenge"]) >= 43


def test_start_link_flow_requires_client_creds(isolated_keystore,
                                                  fake_provider):
    from core.oauth import start_link_flow
    with pytest.raises(RuntimeError):
        start_link_flow("google", ["gmail.send"], start_loopback=False,
                         redirect_uri_override="http://x/y")


def test_start_link_flow_unknown_provider(with_client_creds):
    from core.oauth import start_link_flow
    with pytest.raises(ValueError):
        start_link_flow("zzz", ["x"], start_loopback=False,
                         redirect_uri_override="http://x/y")


def test_full_link_flow_persists_account(with_client_creds, fake_provider):
    from core.oauth import (
        start_link_flow,
        complete_link_flow,
        get_account,
        list_accounts,
    )
    flow = start_link_flow(
        "google", ["gmail.send", "calendar.events"],
        start_loopback=False,
        redirect_uri_override="http://127.0.0.1:0/oauth/callback")
    assert "code_challenge" in flow.auth_url
    assert flow.state in flow.auth_url
    acct = complete_link_flow(flow.state, code="DUMMY-CODE")
    assert acct.account == "user@example.com"
    assert acct.access_token == "AT-1"
    assert acct.refresh_token == "RT-1"
    # PKCE was actually delivered.
    assert fake_provider["state"]["verifier_seen"] == flow.code_verifier
    # And the account is now visible in the listing without secrets.
    listed = list_accounts()
    assert any(a["account"] == "user@example.com" for a in listed)
    # And persisted bundle round-trips.
    assert get_account("google", "user@example.com").access_token == "AT-1"


def test_link_flow_status_transitions(with_client_creds, fake_provider):
    from core.oauth import start_link_flow, get_link_status, complete_link_flow
    flow = start_link_flow(
        "google", ["gmail.send"],
        start_loopback=False,
        redirect_uri_override="http://x/y")
    assert get_link_status(flow.state)["status"] == "pending"
    complete_link_flow(flow.state, "C")
    assert get_link_status(flow.state)["status"] == "ok"
    assert get_link_status("missing") is None


def test_refresh_on_expiry(with_client_creds, fake_provider):
    from core.oauth import save_account, refresh_token_if_needed
    from core.oauth.store import OAuthAccount
    save_account(OAuthAccount(
        provider="google",
        account="user@example.com",
        access_token="OLD",
        refresh_token="RT-1",
        expiry=time.time() - 5,
        scopes=["openid"]))
    refreshed = refresh_token_if_needed("google", "user@example.com")
    assert refreshed is not None
    assert refreshed.access_token == "AT-2"


def test_refresh_no_op_when_not_expired(with_client_creds):
    from core.oauth import save_account, refresh_token_if_needed
    from core.oauth.store import OAuthAccount
    save_account(OAuthAccount(
        provider="google",
        account="user@example.com",
        access_token="STILL-VALID",
        refresh_token="RT-1",
        expiry=time.time() + 3600,
        scopes=["openid"]))
    out = refresh_token_if_needed("google", "user@example.com")
    assert out.access_token == "STILL-VALID"


def test_refresh_raises_without_refresh_token(with_client_creds):
    from core.oauth import save_account, refresh_token_if_needed
    from core.oauth.store import OAuthAccount
    save_account(OAuthAccount(
        provider="google",
        account="user@example.com",
        access_token="EXPIRED",
        refresh_token="",
        expiry=time.time() - 5,
        scopes=["openid"]))
    with pytest.raises(RuntimeError):
        refresh_token_if_needed("google", "user@example.com")


def test_revoke_account(with_client_creds, fake_provider):
    from core.oauth import (
        save_account,
        revoke_account,
        get_account,
        list_accounts,
    )
    from core.oauth.store import OAuthAccount
    save_account(OAuthAccount(
        provider="google", account="u@e.com",
        access_token="A", refresh_token="R",
        expiry=time.time() + 3600, scopes=["openid"]))
    assert get_account("google", "u@e.com") is not None
    assert revoke_account("google", "u@e.com") is True
    assert get_account("google", "u@e.com") is None
    assert list_accounts() == []


# ---------------------------------------------------------------------------
# Google tools — scope enforcement (no real network)
# ---------------------------------------------------------------------------
def test_gmail_send_rejects_missing_scope(with_client_creds):
    from core.oauth import save_account
    from core.oauth.store import OAuthAccount
    from core.oauth.google import gmail_send
    save_account(OAuthAccount(
        provider="google", account="u@e.com",
        access_token="A", refresh_token="",
        expiry=time.time() + 3600,
        scopes=["openid"]))   # gmail.send NOT granted
    out = json.loads(gmail_send(to="x@y.com", subject="s", body="b",
                                  account="u@e.com"))
    assert out["ok"] is False
    assert out["error"] == "scope_missing"
    assert "https://www.googleapis.com/auth/gmail.send" in out["needed"]


def test_drive_upload_invalid_b64_returns_error(with_client_creds):
    from core.oauth import save_account
    from core.oauth.store import OAuthAccount
    from core.oauth.google import drive_upload
    save_account(OAuthAccount(
        provider="google", account="u@e.com",
        access_token="A", refresh_token="",
        expiry=time.time() + 3600,
        scopes=["https://www.googleapis.com/auth/drive.file"]))
    out = json.loads(drive_upload(filename="x.bin", data_b64="not-base64!!",
                                    account="u@e.com"))
    assert out["ok"] is False


def test_account_selection_with_no_account_linked():
    """No accounts linked → selection raises a useful error."""
    from core.oauth.google import _select_account
    with pytest.raises(RuntimeError):
        _select_account()


def test_register_google_tools_idempotent():
    from core.oauth.google import register_google_tools, GOOGLE_TOOLS

    class R:
        def __init__(self): self.t = {}

        def register(self, n, f): self.t[n] = f

        def get(self, n): return self.t.get(n)

    r = R()
    first = register_google_tools(r)
    assert set(first) == set(GOOGLE_TOOLS)
    second = register_google_tools(r)
    assert second == []


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------
def test_admin_oauth_endpoints(with_client_creds, fake_provider, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AGENT_API_TOKEN", "test-tok")
    from api.server import app
    headers = {"X-Agent-Token": "test-tok"}
    with TestClient(app) as client:
        # Start.
        r = client.post("/admin/oauth/link/start",
                         json={"provider": "google",
                               "bundles": ["gmail.send"],
                               "start_loopback": False,
                               "redirect_uri": "http://127.0.0.1:0/cb"},
                         headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "auth_url" in data
        state = data["state"]

        # Status — pending.
        r = client.get(f"/admin/oauth/link/status?state={state}",
                        headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "pending"

        # Complete via the manual endpoint (no real callback hit).
        r = client.post("/admin/oauth/link/complete",
                         json={"state": state, "code": "X"},
                         headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["account"] == "user@example.com"

        # Account list shows it.
        r = client.get("/admin/oauth/accounts", headers=headers)
        assert r.json()["count"] == 1

        # Revoke.
        r = client.post("/admin/oauth/revoke",
                         json={"provider": "google",
                               "account": "user@example.com"},
                         headers=headers)
        assert r.status_code == 200, r.text


def test_admin_oauth_start_returns_412_without_client_creds(
        isolated_keystore, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AGENT_API_TOKEN", "test-tok")
    from api.server import app
    with TestClient(app) as client:
        r = client.post("/admin/oauth/link/start",
                         json={"provider": "google",
                               "bundles": ["gmail.send"],
                               "start_loopback": False,
                               "redirect_uri": "http://x/cb"},
                         headers={"X-Agent-Token": "test-tok"})
        assert r.status_code == 412


def test_admin_oauth_start_400_on_unknown_provider(
        with_client_creds, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AGENT_API_TOKEN", "test-tok")
    from api.server import app
    with TestClient(app) as client:
        r = client.post("/admin/oauth/link/start",
                         json={"provider": "nope",
                               "bundles": [],
                               "start_loopback": False,
                               "redirect_uri": "http://x/cb"},
                         headers={"X-Agent-Token": "test-tok"})
        assert r.status_code == 400


def test_admin_oauth_revoke_404_when_no_account(
        with_client_creds, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AGENT_API_TOKEN", "test-tok")
    from api.server import app
    with TestClient(app) as client:
        r = client.post("/admin/oauth/revoke",
                         json={"provider": "google",
                               "account": "nobody@nowhere"},
                         headers={"X-Agent-Token": "test-tok"})
        assert r.status_code == 404
