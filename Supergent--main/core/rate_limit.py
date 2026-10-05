"""Per-client request rate limiter for the public HTTP surface.

This is **distinct** from :mod:`core.throttle`, which gates concurrent
tool invocations. Here we cap how many ``/chat`` and ``/execute``
requests a single caller (identified by ``X-Agent-Token`` header when
present, otherwise the source IP) can issue per minute.

The limiter is a token-bucket per identity:

* ``capacity``  — burst allowance (default 30 for ``/chat``, 10 for
  ``/execute``).
* ``refill_per_sec`` — sustained rate (default 0.5 r/s for ``/chat``,
  0.16 r/s for ``/execute``, i.e. 30/min and ~10/min respectively).

Buckets are kept in-memory in a ``dict`` guarded by a lock. Idle
buckets older than 10 minutes are evicted on each call so memory
doesn't grow unbounded under spiky traffic.

All numbers are configurable via environment variables and the
``config/server.json`` payload exposed by :mod:`core.server_config`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass
class _Bucket:
    tokens: float
    last_refill: float
    last_seen: float = field(default_factory=time.time)


class TokenBucketLimiter:
    """Token-bucket limiter keyed by an arbitrary identity string."""

    def __init__(self, capacity: int, refill_per_sec: float,
                 *, idle_evict_seconds: float = 600.0) -> None:
        self.capacity = max(1, int(capacity))
        self.refill_per_sec = max(0.001, float(refill_per_sec))
        self.idle_evict_seconds = max(60.0, float(idle_evict_seconds))
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _evict_locked(self, now: float) -> None:
        cutoff = now - self.idle_evict_seconds
        stale = [k for k, b in self._buckets.items() if b.last_seen < cutoff]
        for k in stale:
            self._buckets.pop(k, None)

    def acquire(self, identity: str) -> Tuple[bool, float]:
        """Try to consume one token for ``identity``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds``
        is 0 when the request is allowed, otherwise an estimate of how
        long the client should wait before retrying.
        """
        now = time.time()
        with self._lock:
            self._evict_locked(now)
            b = self._buckets.get(identity)
            if b is None:
                b = _Bucket(tokens=float(self.capacity), last_refill=now)
                self._buckets[identity] = b
            elapsed = max(0.0, now - b.last_refill)
            b.tokens = min(float(self.capacity),
                           b.tokens + elapsed * self.refill_per_sec)
            b.last_refill = now
            b.last_seen = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0.0
            deficit = 1.0 - b.tokens
            retry_after = deficit / self.refill_per_sec
            return False, retry_after

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return {k: b.tokens for k, b in self._buckets.items()}


# ---------------------------------------------------------------------------
# Default limiters wired by the FastAPI middleware. Read configuration on
# first use so test fixtures can override env before they fire.
# ---------------------------------------------------------------------------
_DEFAULTS: Dict[str, TokenBucketLimiter] = {}
_DEFAULTS_LOCK = threading.Lock()


def _config_for(name: str) -> Tuple[int, float]:
    from .server_config import load_server_config
    cfg = load_server_config()
    block = (cfg.get("rate_limit") or {}).get(name) or {}
    capacity = int(block.get("capacity", 30 if name == "chat" else 10))
    refill = float(block.get("refill_per_sec",
                             0.5 if name == "chat" else 0.16))
    return capacity, refill


def get_limiter(name: str) -> TokenBucketLimiter:
    with _DEFAULTS_LOCK:
        existing = _DEFAULTS.get(name)
        if existing is not None:
            return existing
        cap, refill = _config_for(name)
        limiter = TokenBucketLimiter(cap, refill)
        _DEFAULTS[name] = limiter
        return limiter


def reset_limiters() -> None:
    """Test hook — drop all configured limiters."""
    with _DEFAULTS_LOCK:
        _DEFAULTS.clear()


def identity_for_request(headers: Dict[str, str], client_host: Optional[str]
                         ) -> str:
    """Stable identity string for a request.

    Prefers the agent token (per-user) but falls back to the source IP
    when auth is disabled. Anything that hits us without an obvious
    identifier is rate-limited as ``anonymous`` (one shared bucket).
    """
    token = (headers.get("x-agent-token")
             or headers.get("X-Agent-Token") or "").strip()
    if token:
        return f"token:{token[:32]}"
    if client_host:
        return f"ip:{client_host}"
    return "anonymous"


__all__ = [
    "TokenBucketLimiter",
    "get_limiter",
    "reset_limiters",
    "identity_for_request",
]
