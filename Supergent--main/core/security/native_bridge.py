"""Explicit native capabilities guarded by the configured backend origin.

pywebview's before_load event is an injection lifecycle notification, not a
cancellable navigation policy. Revoke there; inspect the public current URL
only after loaded because that getter waits for the loaded event itself.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
from typing import Callable
from urllib.parse import urlsplit


LOG = logging.getLogger("WISE.NativeBridge")


def _origin(url: object) -> tuple[str, str, int] | None:
    if not isinstance(url, str) or not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if port <= 0:
            return None
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


def install_native_bridge(window: object, initial_url: str,
                          open_authorization_url: Callable[[str], bool]) -> None:
    """Expose one callable; never attach a reflective js_api object.

    The URL check is a top-level renderer snapshot, not sender-frame
    attestation or an atomic navigation lock. Renderer race/iframe guarantees
    need separate platform acceptance evidence.
    """
    trusted = _origin(initial_url)
    if trusted is None:
        raise ValueError("Native bridge requires a valid local backend origin")
    try:
        local = trusted[1] == "localhost" or ipaddress.ip_address(trusted[1]).is_loopback
    except ValueError:
        local = False
    if not local:
        raise ValueError("Native bridge requires a loopback backend origin")

    ready = threading.Event()
    revoked = threading.Event()

    def current_document_is_trusted() -> bool:
        try:
            return _origin(window.get_current_url()) == trusted
        except Exception:
            return False

    def revoke_before_load() -> None:
        # Do not call get_current_url here: on the initial load its public
        # implementation waits for loaded and would stall bridge injection.
        ready.clear()

    def revoke_permanently() -> None:
        ready.clear()
        revoked.set()

    def verify_loaded_document() -> None:
        if revoked.is_set():
            return
        if current_document_is_trusted():
            if not revoked.is_set():
                ready.set()
            return
        revoke_permanently()
        LOG.warning("Native bridge refused an untrusted or unavailable document; closing window.")
        try:
            window.destroy()
        except Exception:
            LOG.warning("Native window could not close; native bridge remains revoked.")

    def open_external_url(url: str) -> bool:
        if (revoked.is_set() or not ready.is_set() or not current_document_is_trusted()
                or revoked.is_set() or not ready.is_set()):
            raise PermissionError("Native capability requires the trusted WISE backend document")
        return open_authorization_url(url)

    window.events.before_load += revoke_before_load
    window.events.loaded += verify_loaded_document
    window.events.closed += revoke_permanently
    window.expose(open_external_url)


__all__ = ["install_native_bridge"]
