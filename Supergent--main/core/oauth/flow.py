"""OAuth 2.0 + PKCE flow with loopback redirect.

Lifecycle::

    1. start_link_flow(provider, bundles)
       → builds a state + PKCE pair
       → spins up a one-shot loopback HTTP server on 127.0.0.1
       → returns {auth_url, state, redirect_uri}

    2. The user opens auth_url in their browser, grants consent,
       provider redirects them to http://127.0.0.1:<port>/oauth/callback
       with ?code=...&state=...

    3. Loopback server captures the code; flow completes asynchronously.
       Tokens are persisted via :func:`save_account`.

    4. get_link_status(state) returns 'pending' / 'ok' / 'error'.

The flow can also be driven manually by calling
:func:`complete_link_flow(state, code)` from an external HTTP handler
— useful when the redirect_uri is hosted by a different service.

Provider client_id / client_secret are loaded from the encrypted
KeyStore at runtime (keys ``oauth.<provider>.client_id`` and
``oauth.<provider>.client_secret``). The repo never ships real
credentials.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Iterable, List, Optional

from .providers import OAUTH_PROVIDERS, OAuthProvider, get_provider
from .scopes import resolve_scopes
from .store import OAuthAccount, save_account


# ---------------------------------------------------------------------------
# State table — flows in flight
# ---------------------------------------------------------------------------
_FLOW_TTL_SECONDS = 600


@dataclass
class LinkFlowState:
    state: str
    provider: str
    bundles: List[str]
    scopes: List[str]
    code_verifier: str
    redirect_uri: str
    auth_url: str
    started_at: float = field(default_factory=lambda: time.time())
    status: str = "pending"           # pending|ok|error
    error: str = ""
    account: Optional[str] = None
    httpd: Optional[Any] = None       # the loopback HTTPServer
    server_port: int = 0
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _claimed: bool = field(default=False, repr=False)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "provider": self.provider,
            "bundles": list(self.bundles),
            "redirect_uri": self.redirect_uri,
            "auth_url": self.auth_url,
            "status": self.status,
            "error": self.error,
            "account": self.account,
            "started_at": self.started_at,
        }


_FLOWS: Dict[str, LinkFlowState] = {}
_LOCK = threading.RLock()


def _put(state: LinkFlowState) -> None:
    with _LOCK:
        _FLOWS[state.state] = state


def _pop(state_id: str) -> Optional[LinkFlowState]:
    with _LOCK:
        return _FLOWS.pop(state_id, None)


def _peek(state_id: str) -> Optional[LinkFlowState]:
    with _LOCK:
        return _FLOWS.get(state_id)


def _list_pending() -> List[LinkFlowState]:
    with _LOCK:
        return list(_FLOWS.values())


def _claim_flow(state_id: str) -> LinkFlowState:
    """Atomically accept a pending response once, before any provider I/O."""
    with _LOCK:
        flow = _FLOWS.get(state_id)
        if flow is None:
            raise RuntimeError("unknown OAuth state")
        if flow.status != "pending" or flow._claimed:
            raise RuntimeError("OAuth flow is no longer pending")
        if _expire_flow(flow):
            raise RuntimeError(flow.error)
        flow._claimed = True
        return flow


def _expire_flow(flow: LinkFlowState) -> bool:
    """Called under _LOCK; an accepted exchange may finish after the deadline."""
    if (flow.status == "pending" and not flow._claimed
            and time.monotonic() - flow._started_monotonic >= _FLOW_TTL_SECONDS):
        flow.status = "error"
        flow.error = "OAuth flow expired"
        return True
    return False


def _shutdown_loopback(httpd: Any) -> None:
    try:
        httpd.shutdown()
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------
def _pkce_pair() -> Dict[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return {"verifier": verifier, "challenge": challenge}


# ---------------------------------------------------------------------------
# Client credential lookup — the user's app, not Devin's
# ---------------------------------------------------------------------------
def _client_credentials(prov: OAuthProvider) -> Dict[str, str]:
    """Look up client_id / client_secret in the encrypted KeyStore.

    We deliberately fail loudly when the credentials are absent — the
    user must register their own OAuth application and store the
    credentials before linking.
    """
    from core.llm.keystore import KeyStore
    ks = KeyStore()
    cid_entry = ks.get(prov.client_id_key, reveal=True)
    csec_entry = ks.get(prov.client_secret_key, reveal=True)
    if not cid_entry:
        raise RuntimeError(
            f"missing OAuth client_id for {prov.id}; store it as "
            f"'{prov.client_id_key}' via /admin/keys")
    cid = cid_entry.get("api_key") or ""
    csec = (csec_entry or {}).get("api_key", "") if csec_entry else ""
    return {"client_id": cid, "client_secret": csec}


# ---------------------------------------------------------------------------
# Loopback callback server
# ---------------------------------------------------------------------------
class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that records the OAuth callback."""

    def do_GET(self) -> None:  # noqa: N802
        consumed = False
        bound = False
        flow = None
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            target = getattr(self.server, "oauth_state_id", "")
            flow = _peek(target)
            if (flow is None or parsed.scheme or parsed.netloc
                    or parsed.params or parsed.fragment):
                raise RuntimeError("invalid OAuth callback")
            if parsed.path != urllib.parse.urlparse(flow.redirect_uri).path:
                raise RuntimeError("invalid OAuth callback path")
            states = qs.get("state", [])
            if (len(states) != 1 or not states[0]
                    or not secrets.compare_digest(states[0].encode(), target.encode())):
                raise RuntimeError("invalid OAuth callback state")
            bound = True
            for key in ("code", "error", "error_description"):
                if key in qs and (len(qs[key]) != 1 or not qs[key][0]):
                    raise RuntimeError("invalid OAuth callback parameters")
            code = qs.get("code", [""])[0]
            err = qs.get("error", [""])[0]
            if bool(code) == bool(err):
                raise RuntimeError("callback requires either code or error")
            if err:
                flow = _claim_flow(target)
                consumed = True
                with _LOCK:
                    flow.status = "error"
                    flow.error = qs.get("error_description", [err])[0]
                body = b"Authorization was declined. You can close this tab."
                self.send_response(400)
            else:
                try:
                    complete_link_flow(target, code)
                    consumed = True
                    body = b"You can close this tab."
                    self.send_response(200)
                except Exception as exc:
                    consumed = flow.status != "pending"
                    body = f"Error: {exc}".encode("utf-8")
                    self.send_response(400)
        except RuntimeError as exc:
            consumed = bound and flow is not None and flow.status != "pending"
            body = f"Error: {exc}".encode("utf-8")
            self.send_response(400)
        except Exception as exc:
            body = f"Server error: {exc}".encode("utf-8")
            self.send_response(500)

        try:
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            # Invalid requests leave the legitimate pending listener usable.
            if consumed:
                threading.Thread(target=lambda: _shutdown_loopback(self.server),
                                 daemon=True).start()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        return  # silence


def _start_loopback(state_id: str) -> tuple[HTTPServer, int]:
    httpd = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    httpd.oauth_state_id = state_id
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever,
                           name=f"oauth-callback-{port}",
                           daemon=True)
    th.start()
    return httpd, port


# ---------------------------------------------------------------------------
# Public flow API
# ---------------------------------------------------------------------------
def start_link_flow(provider: str,
                     bundles: Iterable[str],
                     *,
                     start_loopback: bool = True,
                     loopback_port: Optional[int] = None,
                     redirect_uri_override: Optional[str] = None,
                     ) -> LinkFlowState:
    """Begin an OAuth link flow; returns the state object.

    ``start_loopback`` defaults to True — the standard desktop flow.
    Tests / non-loopback hosts can pass ``False`` and use
    ``redirect_uri_override``.
    """
    prov = get_provider(provider)
    if prov is None:
        raise ValueError(f"unknown provider '{provider}'")
    bundle_list = [b for b in bundles if b]
    scopes = resolve_scopes(prov.id, bundle_list)
    if not scopes:
        # Fall back to the openid bundle so the userinfo lookup works.
        scopes = resolve_scopes(prov.id, ["openid"])

    creds = _client_credentials(prov)
    pkce = _pkce_pair()
    state_id = secrets.token_urlsafe(24)

    httpd: Optional[HTTPServer] = None
    port = loopback_port or 0
    if start_loopback:
        httpd, port = _start_loopback(state_id)
    redirect_uri = (redirect_uri_override
                     or f"http://127.0.0.1:{port}/oauth/callback")

    params = {
        "response_type": "code",
        "client_id": creds["client_id"],
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state_id,
        "access_type": "offline",
        "prompt": "consent",
    }
    if prov.supports_pkce:
        params["code_challenge"] = pkce["challenge"]
        params["code_challenge_method"] = "S256"

    auth_url = (prov.auth_endpoint + "?"
                + urllib.parse.urlencode(params, safe=":/ "))

    flow = LinkFlowState(
        state=state_id,
        provider=prov.id,
        bundles=bundle_list,
        scopes=scopes,
        code_verifier=pkce["verifier"],
        redirect_uri=redirect_uri,
        auth_url=auth_url,
        httpd=httpd,
        server_port=port,
    )
    _put(flow)

    return flow


def complete_link_flow(state_id: str, code: str) -> OAuthAccount:
    """Exchange ``code`` for tokens, fetch userinfo, persist the account."""
    if not isinstance(code, str) or not code:
        raise RuntimeError("missing OAuth authorization code")
    flow = _claim_flow(state_id)
    try:
        return _exchange_link_flow(flow, code)
    except Exception as exc:
        with _LOCK:
            if _FLOWS.get(state_id) is flow and flow.status == "pending":
                flow.status = "error"
                flow.error = str(exc)
        raise


def _exchange_link_flow(flow: LinkFlowState, code: str) -> OAuthAccount:
    prov = get_provider(flow.provider)
    if prov is None:
        raise RuntimeError(f"provider {flow.provider} no longer registered")
    creds = _client_credentials(prov)

    # 1. Token exchange.
    body_params: Dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow.redirect_uri,
        "client_id": creds["client_id"],
    }
    if prov.sends_secret_in_body and creds.get("client_secret"):
        body_params["client_secret"] = creds["client_secret"]
    if prov.supports_pkce:
        body_params["code_verifier"] = flow.code_verifier

    token_resp = _http_post_form(prov.token_endpoint, body_params)
    access = token_resp.get("access_token") or ""
    if not access:
        raise RuntimeError(f"token exchange failed: {token_resp}")

    # 2. Userinfo (so we can name the account by the user's email).
    account_email = ""
    try:
        info = _http_get_json(
            prov.userinfo_endpoint,
            headers={"Authorization": f"Bearer {access}"})
        account_email = (info.get("email") or info.get("login")
                          or info.get("userPrincipalName") or "user")
    except Exception:
        account_email = "user"

    expires_in = token_resp.get("expires_in")
    expiry = (time.time() + float(expires_in)) if expires_in else 0.0

    acct = OAuthAccount(
        provider=prov.id,
        account=account_email,
        access_token=access,
        refresh_token=token_resp.get("refresh_token", "") or "",
        expiry=expiry,
        scopes=flow.scopes,
        token_type=token_resp.get("token_type", "Bearer"),
    )
    with _LOCK:
        # Cancellation during provider I/O must not persist an account later.
        if _FLOWS.get(flow.state) is not flow:
            raise RuntimeError("OAuth flow was cancelled")
        save_account(acct)
        flow.status = "ok"
        flow.account = account_email
    return acct


def get_link_status(state_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        flow = _FLOWS.get(state_id)
        if flow is None:
            return None
        expired = _expire_flow(flow)
        status = flow.to_public_dict()
    if expired and flow.httpd is not None:
        threading.Thread(target=lambda: _shutdown_loopback(flow.httpd), daemon=True).start()
    return status


def cancel_link_flow(state_id: str) -> bool:
    flow = _pop(state_id)
    if flow is None:
        return False
    if flow.httpd is not None:
        try:
            _shutdown_loopback(flow.httpd)
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Refresh + revocation (called from store.py)
# ---------------------------------------------------------------------------
def refresh_with_provider(provider: str,
                            acct: OAuthAccount) -> OAuthAccount:
    prov = get_provider(provider)
    if prov is None:
        raise RuntimeError(f"unknown provider '{provider}'")
    creds = _client_credentials(prov)
    body_params: Dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": acct.refresh_token,
        "client_id": creds["client_id"],
    }
    if prov.sends_secret_in_body and creds.get("client_secret"):
        body_params["client_secret"] = creds["client_secret"]
    resp = _http_post_form(prov.token_endpoint, body_params)
    access = resp.get("access_token") or ""
    if not access:
        raise RuntimeError(f"refresh failed: {resp}")
    expires_in = resp.get("expires_in")
    new_expiry = (time.time() + float(expires_in)) if expires_in else 0.0
    return OAuthAccount(
        provider=acct.provider,
        account=acct.account,
        access_token=access,
        refresh_token=resp.get("refresh_token") or acct.refresh_token,
        expiry=new_expiry,
        scopes=acct.scopes,
        token_type=resp.get("token_type", "Bearer"),
    )


def revoke_with_provider(provider: str,
                           acct: OAuthAccount) -> bool:
    prov = get_provider(provider)
    if prov is None:
        return False
    try:
        if "{client_id}" in prov.revoke_endpoint:
            return False  # GitHub Apps revocation needs Basic auth + DELETE
        _http_post_form(prov.revoke_endpoint,
                         {"token": acct.refresh_token or acct.access_token})
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tiny HTTP helpers (no requests dep)
# ---------------------------------------------------------------------------
def _http_post_form(url: str, params: Dict[str, str]) -> Dict[str, Any]:
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        # GitHub returns x-www-form-urlencoded by default; parse it.
        parsed = urllib.parse.parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}


def _http_get_json(url: str,
                    headers: Optional[Dict[str, str]] = None
                    ) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


__all__ = [
    "LinkFlowState",
    "start_link_flow",
    "complete_link_flow",
    "get_link_status",
    "cancel_link_flow",
    "refresh_with_provider",
    "revoke_with_provider",
]
