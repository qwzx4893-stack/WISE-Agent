"""Remote MCP OAuth: discovery, PKCE, exact loopback state and protected tokens.

Authorization is performed by the human in their browser, never by the model.
No third-party client ID is bundled; DCR or a user-registered client is required.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import socket
import urllib.request
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qs

import httpx

_FLOWS = {}
_LOCK = threading.RLock()
_REFRESH_LOCK = threading.RLock()


def _save_tokens(name, tokens, *, client_id, token_endpoint, resource, previous=None):
    if tokens.get("token_type", "").lower() != "bearer" or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
        raise ValueError("Invalid OAuth token response")
    from core.secrets_store import SecretStore
    store = SecretStore()
    session = {"client_id":client_id, "token_endpoint":token_endpoint, "resource":resource,
        "refresh_token":tokens.get("refresh_token") or (previous or {}).get("refresh_token", "")}
    if tokens.get("expires_in") is not None:
        lifetime = float(tokens["expires_in"])
        if not 0 < lifetime <= 31_536_000:
            raise ValueError("Invalid OAuth token lifetime")
        session["expires_at"] = time.time() + lifetime
    store.set(f"MCP_{name}_oauth_session", json.dumps(session))
    key = f"MCP_{name}_oauth_access_token"
    store.set(key, "Bearer " + tokens["access_token"])
    return "${wise-secret:" + key + "}"


def refresh_if_needed(name, registry):
    """Refresh before connecting/executing; never replay a tool after failure."""
    cfg = registry._find(name)
    if not cfg or cfg.transport != "http" or cfg.headers.get("Authorization") != f"${{wise-secret:MCP_{name}_oauth_access_token}}":
        return False
    from core.secrets_store import SecretStore
    with _REFRESH_LOCK:
        raw = SecretStore().get(f"MCP_{name}_oauth_session")
        if not raw: return False
        try:
            session = json.loads(raw)
            expiry = float(session.get("expires_at", 0))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid stored OAuth session; authorize again") from None
        resource, target = urlsplit(session.get("resource", "")), urlsplit(cfg.url)
        if (resource.scheme,resource.hostname,resource.port or 443) != (target.scheme,target.hostname,target.port or 443):
            raise ValueError("OAuth token resource differs from this server; authorize again")
        base = resource.path.rstrip("/")
        if resource.query or not (target.path == base or target.path.startswith(base+"/")):
            raise ValueError("OAuth token resource differs from this server; authorize again")
        if not expiry or expiry > time.time() + 45: return False
        if not session.get("refresh_token"):
            raise ValueError("OAuth session expired; authorize again")
        try:
            tokens = _json(session["token_endpoint"], form={"grant_type":"refresh_token",
                "refresh_token":session["refresh_token"], "client_id":session["client_id"], "resource":session["resource"]})
            _save_tokens(name, tokens, client_id=session["client_id"], token_endpoint=session["token_endpoint"],
                resource=session["resource"], previous=session)
        except (httpx.HTTPError, ValueError, KeyError):
            raise ValueError("OAuth refresh failed; authorize again") from None
        return True


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("OAuth endpoints must use HTTPS without embedded credentials")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("OAuth discovery cannot access private or local networks")
    return url


def _json(url, *, data=None, headers=None, form=None):
    public_url(url)
    from core.public_http import PublicTransport
    with httpx.Client(timeout=15, follow_redirects=False,trust_env=False,transport=PublicTransport()) as client:
        method = "GET" if data is None and form is None else "POST"
        kwargs = {"headers": headers}
        if data is not None:
            kwargs["json"] = data
        if form is not None:
            kwargs["data"] = form
        with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError(f"OAuth endpoint returned HTTP {response.status_code}")
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 1_000_000:
                    raise ValueError("OAuth metadata exceeds size limit")
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise ValueError("Invalid OAuth metadata")
    return result


def start(name, registry, *, client_id=""):
    cfg = registry._find(name)
    if not cfg or cfg.transport != "http":
        raise ValueError("OAuth requires a registered remote MCP server")
    original_url = cfg.url
    discovery_url = cfg.url
    # Context7 documents a distinct OAuth endpoint. The anonymous /mcp
    # endpoint publishes origin-wide metadata but is not the login transport.
    if cfg.url.rstrip("/") == "https://mcp.context7.com/mcp":
        discovery_url = "https://mcp.context7.com/mcp/oauth"
    # Keep an existing public connection usable while the human logs in.
    public_url(discovery_url)
    parsed = urlsplit(discovery_url)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    # A 401 can point at resource metadata on a different path/domain.
    # Read headers only: successful GETs may be long-lived SSE transports.
    from core.public_http import PublicTransport
    with httpx.Client(timeout=15, follow_redirects=False,trust_env=False,transport=PublicTransport()) as client:
        with client.stream("GET", discovery_url) as challenge_response:
            challenge_header = challenge_response.headers.get("www-authenticate", "")
            if not challenge_header and getattr(challenge_response, "status_code", None) == 200:
                return {"ok": True, "requires_auth": False, "message": "This public MCP endpoint does not require account authorization"}
    challenge_params = {}
    if challenge_header.lower().startswith("bearer "):
        challenge_params = urllib.request.parse_keqv_list(urllib.request.parse_http_list(challenge_header[7:]))
    metadata_url = challenge_params.get("resource_metadata")
    if metadata_url:
        metadata = _json(metadata_url)
    else:
        try:
            metadata = _json(origin + "/.well-known/oauth-protected-resource" + parsed.path.rstrip("/"))
        except ValueError:
            metadata = _json(origin + "/.well-known/oauth-protected-resource")
    resource = metadata.get("resource", "")
    # A protected-resource identifier may cover the origin or a parent path,
    # as Context7's official metadata does. Bind to that advertised identifier
    # ONLY on the exact configured origin and a complete path-segment prefix.
    resource_url = urlsplit(public_url(resource))
    same_origin = (resource_url.scheme.lower(), resource_url.hostname, resource_url.port or 443) == (
        parsed.scheme.lower(), parsed.hostname, parsed.port or 443)
    base_path = resource_url.path.rstrip("/")
    if not same_origin or resource_url.query or not (
        parsed.path == base_path or parsed.path.startswith(base_path + "/")):
        raise ValueError("OAuth resource metadata does not match this MCP server")
    issuers = metadata.get("authorization_servers", [])
    if not isinstance(issuers, list) or not issuers:
        raise ValueError("MCP server did not publish an authorization server")
    auth = None
    for issuer in issuers[:4]:
        try:
            issuer_url = urlsplit(public_url(issuer))
            as_url = urlunsplit((issuer_url.scheme, issuer_url.netloc,
                "/.well-known/oauth-authorization-server" + issuer_url.path, "", ""))
            try:
                candidate = _json(as_url)
            except (ValueError, httpx.HTTPError):
                candidate = _json(issuer.rstrip("/") + "/.well-known/openid-configuration")
            if candidate.get("issuer") != issuer:
                raise ValueError("OAuth issuer mismatch")
            for key in ("authorization_endpoint", "token_endpoint"):
                public_url(candidate.get(key, ""))
            if "S256" not in candidate.get("code_challenge_methods_supported", []):
                continue
            if not client_id and not candidate.get("registration_endpoint"):
                continue
            auth = candidate
            break
        except (ValueError, httpx.HTTPError):
            continue
    if auth is None:
        raise ValueError("No compatible PKCE authorization server; enter a registered public OAuth client ID if required")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    flow = {"name": name, "state": state, "status": "pending", "started": time.time(),
            "registry": registry, "verifier": verifier, "token_endpoint": auth["token_endpoint"], "resource": resource}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log callback codes.

        def do_GET(self):
            values = parse_qs(urlsplit(self.path).query)
            received = values.get("state", [""])[0]
            if urlsplit(self.path).path != "/oauth/callback" or not secrets.compare_digest(received, state):
                self.send_response(400); self.end_headers(); self.wfile.write(b"Invalid OAuth callback")
                return
            if flow["status"] != "pending" or time.time() - flow["started"] > 600:
                self.send_response(410); self.end_headers(); return
            try:
                response_issuer = values.get("iss", [""])[0]
                if (auth.get("authorization_response_iss_parameter_supported") and not response_issuer
                        or response_issuer and response_issuer != issuer):
                    raise ValueError("Authorization response issuer mismatch")
                if values.get("error"):
                    raise ValueError("Authorization was declined")
                code = values.get("code", [""])[0]
                if not code:
                    raise ValueError("Missing authorization code")
                if registry._find(name) is not cfg or cfg.url != original_url:
                    raise ValueError("MCP configuration changed during authorization; start again")
                flow["status"] = "exchanging"
                tokens = _json(flow["token_endpoint"], form={
                    "grant_type": "authorization_code", "code": code,
                    "client_id": flow["client_id"], "redirect_uri": flow["redirect_uri"],
                    "code_verifier": verifier, "resource": resource})
                if registry._find(name) is not cfg or cfg.url != original_url:
                    raise ValueError("MCP configuration changed during authorization; start again")
                cfg.headers["Authorization"] = _save_tokens(name, tokens, client_id=flow["client_id"],
                    token_endpoint=flow["token_endpoint"], resource=resource)
                cfg.url = discovery_url
                registry._save()
                connected = registry.start(name)
                flow["status"] = "ok" if connected.get("alive") else "error"
                flow["error"] = connected.get("error", "")
                self.send_response(200); self.end_headers()
                self.wfile.write(b"WISE: authorization completed. Return to Settings to check the connection.")
            except Exception as exc:
                flow["status"] = "error"
                flow["error"] = str(exc)[:200]
                self.send_response(400); self.end_headers(); self.wfile.write(b"WISE: authorization failed. Return to Settings.")
            finally:
                threading.Thread(target=server.shutdown, daemon=True).start()

    server = HTTPServer(("127.0.0.1", 0), Callback)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth/callback"
    try:
        if not client_id:
            registration = auth.get("registration_endpoint")
            if not registration:
                raise ValueError("This service requires your registered OAuth client ID; enter it in Settings")
            registered = _json(registration, data={"client_name": "WISE", "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"], "response_types": ["code"], "token_endpoint_auth_method": "none"})
            client_id = registered.get("client_id", "")
            if registered.get("token_endpoint_auth_method", "none") != "none":
                raise ValueError("This service requires a confidential OAuth application; public desktop flow unsupported")
        if not client_id:
            raise ValueError("OAuth registration returned no client ID")
        flow.update(client_id=client_id, redirect_uri=redirect_uri)
        scope = challenge_params.get("scope", "")
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                 "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource}
        if scope:
            query["scope"] = scope
        auth_url = auth["authorization_endpoint"] + ("&" if "?" in auth["authorization_endpoint"] else "?") + urlencode(query)
        with _LOCK:
            _FLOWS[state] = flow
        def serve():
            try:
                server.serve_forever(poll_interval=0.25)
            finally:
                server.server_close()
        threading.Thread(target=serve, daemon=True).start()
        def expire():
            if flow["status"] in ("pending", "exchanging"):
                flow.update(status="error", error="Authorization timed out; try again")
                server.shutdown()
            with _LOCK:
                _FLOWS.pop(state, None)
        timer = threading.Timer(600, expire); timer.daemon = True; timer.start()
        return {"ok": True, "auth_url": auth_url, "state": state}
    except Exception:
        server.server_close()
        raise


def status(state):
    with _LOCK:
        flow = _FLOWS.get(state)
        if not flow:
            return {"status": "expired", "error": "Authorization session expired"}
        return {"name": flow["name"], "status": flow["status"], "error": flow.get("error", "")}
