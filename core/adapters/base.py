"""Base classes shared by every adapter.

A :class:`BaseAdapter` resolves credentials in this order:

1. The universal :class:`core.llm.keystore.KeyStore` (named entries).
2. Environment variables listed in ``ENV_KEYS``.
3. The platform's CLI subprocess (e.g. ``gh``, ``aws``, ``glab``) when
   the adapter declares ``CLI_FALLBACK = True``.

Every public method should be wrapped with :meth:`_call` so that:

- Retries with exponential backoff are applied (configurable per
  adapter).
- A token-bucket rate limiter prevents bursts.
- Each call emits a ``Tracer.span("adapter.<name>.<method>")`` event.
- HTTP errors are normalised into :class:`AdapterError`.

The class deliberately avoids any heavy SDK imports at module load —
imports happen lazily inside the methods that need them so a missing
SDK doesn't crash the registry.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    import httpx as _httpx  # type: ignore
except Exception:  # noqa: BLE001
    _httpx = None  # type: ignore

from core.observability import Tracer


# --------------------------------------------------------------------------
class AdapterError(Exception):
    """Raised by adapters for any user-visible failure."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


@dataclass
class AdapterStatus:
    name: str
    ready: bool
    reason: str = ""
    capabilities: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
class _RateLimiter:
    """Simple token bucket — refill ``rate`` tokens/sec, bursts up to ``capacity``."""

    def __init__(self, rate: float, capacity: int):
        self.rate = max(0.01, float(rate))
        self.capacity = max(1, int(capacity))
        self._tokens = float(self.capacity)
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._ts
            self._tokens = min(
                float(self.capacity), self._tokens + elapsed * self.rate,
            )
            self._ts = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self.rate
        time.sleep(max(0.0, wait))
        # Re-acquire by recursion-by-design (bounded by sleep above).
        return self.acquire()


# --------------------------------------------------------------------------
class BaseAdapter:
    """Abstract base — concrete adapters override the class attributes
    below and implement their methods using :meth:`_call` / :meth:`_http`.
    """

    # Identity
    NAME: str = "base"
    KEYSTORE_NAMES: List[str] = []     # logical names looked up in KeyStore
    ENV_KEYS: List[str] = []           # fallback env var names
    REQUIRED: List[str] = []           # all of these must resolve to credentials
    CLI_FALLBACK: bool = False         # may we shell out when API creds missing
    CLI_BIN: str = ""                  # binary name when falling back

    # Limits
    RATE_PER_SEC: float = 5.0
    RATE_BURST: int = 10
    DEFAULT_TIMEOUT: float = 30.0
    MAX_RETRIES: int = 2
    RETRY_BACKOFF: float = 1.5

    # ------------------------------------------------------------------
    def __init__(self) -> None:
        self._creds: Dict[str, str] = {}
        self._cli_ok: bool = False
        self._limiter = _RateLimiter(self.RATE_PER_SEC, self.RATE_BURST)
        self._resolve_credentials()

    # ------------------------------------------------------------------
    # Credential resolution
    # ------------------------------------------------------------------
    def _resolve_credentials(self) -> None:
        # KeyStore first.
        try:
            from core.llm.keystore import KeyStore
            ks = KeyStore()
            for name in self.KEYSTORE_NAMES:
                entry = ks.get(name, reveal=True) if hasattr(ks, "get") else None
                if not entry:
                    continue
                key = entry.get("api_key") or entry.get("token") or ""
                if key:
                    self._creds[name] = key
        except Exception:
            pass
        # Env fallback.
        for env in self.ENV_KEYS:
            if env in os.environ and os.environ[env]:
                self._creds.setdefault(env, os.environ[env])
        # CLI fallback detection.
        if self.CLI_FALLBACK and self.CLI_BIN:
            self._cli_ok = _which(self.CLI_BIN) is not None

    # ------------------------------------------------------------------
    def cred(self, *names: str) -> Optional[str]:
        for n in names:
            if n in self._creds and self._creds[n]:
                return self._creds[n]
        return None

    def has_creds(self) -> bool:
        if not self.REQUIRED:
            return bool(self._creds) or self._cli_ok
        return all(self.cred(n) for n in self.REQUIRED)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        return self.has_creds() or self._cli_ok

    def status(self) -> AdapterStatus:
        if self.has_creds():
            reason = "ok (api)"
        elif self._cli_ok:
            reason = f"ok (cli fallback: {self.CLI_BIN})"
        else:
            missing = [n for n in self.REQUIRED if not self.cred(n)]
            reason = (
                "missing credentials: " + ", ".join(missing)
                if missing else "no credentials configured"
            )
        return AdapterStatus(
            name=self.NAME,
            ready=self.is_ready(),
            reason=reason,
            capabilities=list(self.tools_for().keys()),
        )

    def tools_for(self) -> Dict[str, Callable[..., Any]]:
        """Override — return ``{tool_name: bound_method}``."""
        return {}

    # ------------------------------------------------------------------
    # Core helpers for subclasses
    # ------------------------------------------------------------------
    def _call(self, method_name: str, fn: Callable[..., Any],
              *args: Any, **kwargs: Any) -> Any:
        """Wrap a call with rate-limit + retry + tracing."""
        self._limiter.acquire()
        attempts = 0
        last_exc: Optional[BaseException] = None
        with Tracer.span(f"adapter.{self.NAME}.{method_name}"):
            while attempts <= self.MAX_RETRIES:
                try:
                    return fn(*args, **kwargs)
                except AdapterError as exc:
                    if exc.status and 400 <= exc.status < 500 and exc.status not in (408, 429):
                        raise
                    last_exc = exc
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                attempts += 1
                if attempts > self.MAX_RETRIES:
                    break
                time.sleep(self.RETRY_BACKOFF ** attempts)
        if isinstance(last_exc, AdapterError):
            raise last_exc
        raise AdapterError(f"{self.NAME}.{method_name} failed: {last_exc}")

    # ------------------------------------------------------------------
    def _http(self, method: str, url: str,
              *, headers: Optional[Dict[str, str]] = None,
              params: Optional[Dict[str, Any]] = None,
              json: Any = None, data: Any = None,
              timeout: Optional[float] = None) -> Any:
        """Generic HTTP via httpx. Raises :class:`AdapterError` on !=2xx."""
        if _httpx is None:
            raise AdapterError(
                f"{self.NAME}: httpx غير مثبّت — لا يمكن تنفيذ مكالمة HTTP"
            )
        try:
            with _httpx.Client(timeout=timeout or self.DEFAULT_TIMEOUT) as cli:
                resp = cli.request(
                    method.upper(), url,
                    headers=headers or {}, params=params,
                    json=json, data=data,
                )
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"network error: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text[:500]
            raise AdapterError(
                f"{method.upper()} {url} -> {resp.status_code}",
                status=resp.status_code, details=detail,
            )
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"text": resp.text}


def _which(cmd: str) -> Optional[str]:
    import shutil
    return shutil.which(cmd)


__all__ = ["BaseAdapter", "AdapterError", "AdapterStatus"]
