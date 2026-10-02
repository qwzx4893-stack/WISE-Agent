"""Unified channel layer — Apprise-backed.

Background
----------

In Phase 7 Part 3c we shipped ten native HTTP-webhook / SMTP adapters
alongside an optional ``notifyall`` passthrough. That surface is now
replaced by a thin wrapper around **Apprise**
(https://github.com/caronc/apprise — BSD 2-Clause) which already ships
first-class support for 100+ notification services behind a single
URL-based API. The native adapters are removed; users simply configure
an Apprise URL per channel (e.g. ``slack://…``, ``tgram://…``,
``discord://…``) and the registry dispatches to Apprise.

Design
------

* ``send_message(name, message, title=…, url=…)`` — if an explicit
  ``url`` is passed we use it; otherwise we look up a URL stored in
  :class:`core.secrets_store.SecretStore` under the key
  ``APPRISE_URL_<NAME>`` (uppercased). This lets the desktop Settings
  screen write the URL once and agents refer to it by logical name
  afterwards. Missing URL → ``ok=False`` with a clear detail; no
  exceptions escape to the agent loop.
* ``list_channels()`` returns the aggregated view: every stored name
  from the ``SecretStore`` plus an ``available_schemes`` list pulled
  from Apprise's plugin registry, so the UI can present both
  "configured" and "configurable" sets without hardcoding anything.
* ``register_channel(name, url)`` / ``unregister_channel(name)`` are
  wired through the secrets store so persistence lives in a single
  place.

Fail modes are normalised into :class:`ChannelResult` — the agent loop
doesn't care whether Apprise, the network, or the remote service
rejected the request. Everything is thread-safe behind a lock because
the secrets store is shared with the scheduler and SSE runners.

Licensing note
--------------

Apprise is BSD-2-Clause — copy in ``LICENSES/`` and credited in
README Acknowledgments.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("agent_os.channels")


@dataclass
class ChannelResult:
    """Normalised outcome of a ``send_message`` call."""

    channel: str
    ok: bool
    detail: str = ""
    code: Optional[int] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class Channel:
    """A channel known to the registry.

    Either the URL is stored in secrets (``configured=True``) or only
    the Apprise scheme is recognised (``configured=False``); the UI
    uses that to show "Add URL" affordances.
    """

    name: str
    scheme: str
    configured: bool
    requires_secret: bool = True


def _apprise_module():
    """Lazy import so pure-unit tests don't require the package."""
    try:  # pragma: no cover - trivial
        import apprise  # type: ignore
        return apprise
    except Exception as e:  # noqa: BLE001
        LOG.debug("apprise unavailable: %s", e)
        return None


def _available_schemes() -> List[str]:
    mod = _apprise_module()
    if mod is None:
        return []
    try:
        details = mod.Apprise().details()
        schemas = details.get("schemas") or []
        out: List[str] = []
        for entry in schemas:
            # Each entry has multiple 'protocol' / 'secure_protocol' keys.
            for key in ("protocols", "secure_protocols"):
                for p in entry.get(key) or []:
                    if isinstance(p, str) and p not in out:
                        out.append(p)
        return sorted(out)
    except Exception as e:  # noqa: BLE001
        LOG.warning("apprise scheme enumeration failed: %s", e)
        return []


def _secret_key(name: str) -> str:
    return f"APPRISE_URL_{name.strip().upper().replace('-', '_')}"


def _secrets_store():
    """Imported lazily to avoid circular imports at module load."""
    from core.secrets_store import SecretStore
    return SecretStore()


class ChannelRegistry:
    """Process-wide Apprise-backed registry.

    Thread-safe; all reads/writes go through the secrets store so
    configuration survives restart and participates in the same
    protected secret store (Windows DPAPI when available).
    """

    _instance: Optional["ChannelRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @classmethod
    def instance(cls) -> "ChannelRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _all_stored(self) -> Dict[str, str]:
        """Map of name → Apprise URL, read from the secrets store."""
        store = _secrets_store()
        out: Dict[str, str] = {}
        prefix = "APPRISE_URL_"
        for key in store.list(reveal=False).keys():
            if key.startswith(prefix):
                url = store.get(key)
                if url:
                    name = key[len(prefix):].lower()
                    out[name] = url
        return out

    def get_url(self, name: str) -> Optional[str]:
        return _secrets_store().get(_secret_key(name))

    def set_url(self, name: str, url: str) -> None:
        with self._lock:
            _secrets_store().set(_secret_key(name), url)

    def delete_url(self, name: str) -> bool:
        with self._lock:
            return _secrets_store().delete(_secret_key(name))

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------
    def list_channels(self) -> Dict[str, Any]:
        stored = self._all_stored()
        channels = [
            Channel(name=n, scheme=_scheme_of(url),
                    configured=True, requires_secret=True).__dict__
            for n, url in sorted(stored.items())
        ]
        return {
            "configured": channels,
            "available_schemes": _available_schemes(),
            "apprise_available": _apprise_module() is not None,
        }

    def send(self, name: str, message: str, *, title: str = "",
             url: Optional[str] = None,
             attach: Optional[List[str]] = None) -> ChannelResult:
        target = url or self.get_url(name)
        if not target:
            return ChannelResult(
                channel=name, ok=False,
                detail=("no Apprise URL configured for this channel — "
                         "store one via PUT /admin/settings/secrets/"
                         f"{_secret_key(name)}"))
        mod = _apprise_module()
        if mod is None:
            return ChannelResult(
                channel=name, ok=False,
                detail="apprise package not installed "
                         "(pip install apprise)")
        try:
            ap = mod.Apprise()
            if not ap.add(target):
                return ChannelResult(
                    channel=name, ok=False,
                    detail="apprise rejected the URL (unsupported "
                             "scheme or malformed credentials)")
            kwargs: Dict[str, Any] = {"body": message}
            if title:
                kwargs["title"] = title
            if attach:
                kwargs["attach"] = attach
            ok = bool(ap.notify(**kwargs))
            return ChannelResult(
                channel=name, ok=ok,
                detail="sent" if ok else "apprise reported failure",
                raw={"scheme": _scheme_of(target)})
        except Exception as e:  # noqa: BLE001
            LOG.warning("channel %s send failed (%s)", name, type(e).__name__)
            return ChannelResult(
                channel=name, ok=False,
                detail=f"{type(e).__name__}: notification failed; check the channel configuration")


# ---------------------------------------------------------------------------
# Module-level conveniences (used by api/server.py and agent tools)
# ---------------------------------------------------------------------------
def _scheme_of(url: str) -> str:
    idx = url.find("://")
    return url[:idx] if idx > 0 else "unknown"


def get_registry() -> ChannelRegistry:
    return ChannelRegistry.instance()


def list_channels() -> Dict[str, Any]:
    return get_registry().list_channels()


def send_message(name: str, message: str, *, title: str = "",
                 url: Optional[str] = None,
                 attach: Optional[List[str]] = None,
                 **_ignored: Any) -> ChannelResult:
    """Send ``message`` via the channel registered under ``name``.

    ``_ignored`` silently absorbs extra kwargs the old native adapters
    understood (e.g. ``priority``), so calling code that was written
    against the pre-Apprise API keeps working. Service-specific knobs
    should now be encoded in the Apprise URL itself (e.g.
    ``pover://user_key@token/?priority=high``).
    """
    return get_registry().send(
        name, message, title=title, url=url, attach=attach)


def validate_channel(name: str, url: str) -> None:
    import re
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name):
        raise ValueError("Channel name must use letters, numbers and underscores")
    if len(url) > 4096:
        raise ValueError("Channel configuration is too long")
    mod = _apprise_module()
    if mod is None:
        raise ValueError("Apprise is not installed; install messaging dependencies first")
    try:
        accepted = mod.Apprise().add(url)
    except Exception as exc:
        raise ValueError("Channel validation failed; check the service configuration") from None
    if not accepted:
        raise ValueError("Invalid service URL or credentials; check the service format")


def register_channel(name: str, url: str) -> None:
    validate_channel(name, url)
    get_registry().set_url(name, url)


def unregister_channel(name: str) -> bool:
    return get_registry().delete_url(name)


__all__ = [
    "Channel", "ChannelResult", "ChannelRegistry",
    "get_registry", "list_channels", "send_message",
    "register_channel", "unregister_channel",
]
