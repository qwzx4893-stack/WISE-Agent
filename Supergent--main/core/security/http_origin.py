"""Strict ingress authorities for the local control service (no DNS lookups)."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


def _origin(value: str):
    """Parse a serialized HTTP origin; reject URL credentials and ambiguity."""
    if not isinstance(value, str) or not value or any(
        ord(char) <= 32 or ord(char) >= 127 or char in "\\%" for char in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or not re.fullmatch(r"(?:\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?", parsed.netloc)
                or parsed.path or parsed.query or parsed.fragment
                or value.endswith(":") or "?" in value or "#" in value):
            return None
        port = parsed.port
        return parsed.scheme, parsed.hostname.lower(), port if port is not None else (
            443 if parsed.scheme == "https" else 80
        )
    except ValueError:
        return None


def _loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ingress_allowed(connection, *, configured_host, allowed_origins, token_configured: bool) -> bool:
    """Validate Host independently of browser Origin and caller credentials.

    Local UI hosts accept their own dynamic port, plus explicit local UI CORS
    origins. Remote authorities/origins require operator configuration AND a
    configured token; the caller must still pass the separate token check.
    Wildcard binds and wildcard CORS never authorize a control authority.
    Origin-less native/MCP clients remain supported. Forwarded headers are
    deliberately not consulted. ``testserver`` is only an in-process fixture.
    """
    hosts = connection.headers.getlist("host")
    origins = connection.headers.getlist("origin")
    fetch_sites = connection.headers.getlist("sec-fetch-site")
    if len(hosts) != 1 or len(origins) > 1 or len(fetch_sites) > 1:
        return False
    raw_scheme = connection.scope.get("scheme", "http")
    scheme = {"ws": "http", "wss": "https"}.get(raw_scheme, raw_scheme)
    authority = _origin(f"{scheme}://{hosts[0]}")
    if authority is None:
        return False
    explicit = {parsed for item in (allowed_origins or []) if (parsed := _origin(item)) is not None}
    host = authority[1]
    local_host = _loopback_host(host)
    test_host = host == "testserver" and connection.client and connection.client.host == "testclient"
    remote_hosts = {item[1] for item in explicit} if token_configured else set()
    if token_configured and isinstance(configured_host, str):
        bind_host = configured_host
        if ":" in bind_host and not bind_host.startswith("["):
            bind_host = f"[{bind_host}]"
        bind_origin = _origin(f"http://{bind_host}")
        if bind_origin and bind_origin[1] not in {"0.0.0.0", "::"}:
            remote_hosts.add(bind_origin[1])
    if not local_host and not test_host and host not in remote_hosts:
        return False
    origin = _origin(origins[0]) if origins else None
    if origins and origin is None:
        return False
    explicit_origin = origin in explicit and (token_configured or _loopback_host(origin[1])) if origin else False
    if origin and not (explicit_origin or ((local_host or test_host) and origin == authority)):
        return False
    # Cross-site browsers may access remote deployments only through an exact
    # operator-listed origin in token mode. No-origin native clients don't emit
    # this browser header; a cross-site value without Origin fails closed.
    if fetch_sites and fetch_sites[0].lower() == "cross-site":
        return bool(token_configured and explicit_origin)
    return True
