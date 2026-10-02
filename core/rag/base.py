"""Common types for the RAG layer."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

USER_AGENT = "AgentOS-RAG/1.1 (+https://github.com/ZXM878/Supergent-)"
DEFAULT_TIMEOUT = int(os.environ.get("AGENT_OS_RAG_TIMEOUT", "10"))

LOG = logging.getLogger("agent_os.rag")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class SearchResult:
    """One uniform row across every source."""

    title: str = ""
    snippet: str = ""
    url: str = ""
    source: str = ""
    score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SourceStatus:
    name: str
    category: str = "general"
    available: bool = True
    requires_key: bool = False
    last_checked: float = 0.0
    last_error: str = ""
    last_latency_ms: float = 0.0
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------
class _RateLimiter:
    """A simple thread-safe token bucket. Supports per-source quotas."""

    def __init__(self, *, max_calls: int, per_seconds: float) -> None:
        self.max_calls = max_calls
        self.per_seconds = max(0.001, per_seconds)
        self._calls: List[float] = []
        self._lock = threading.Lock()

    def acquire(self, *, block: bool = True) -> bool:
        with self._lock:
            now = time.time()
            cutoff = now - self.per_seconds
            self._calls = [t for t in self._calls if t > cutoff]
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True
            if not block:
                return False
            wait = self.per_seconds - (now - self._calls[0])
        if wait > 0:
            time.sleep(min(wait, 5.0))
        return self.acquire(block=block)


# ---------------------------------------------------------------------------
# Shared HTTP session with retry
# ---------------------------------------------------------------------------
_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()


def _session() -> requests.Session:
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT})
            try:
                from urllib3.util.retry import Retry
                retry = Retry(
                    total=2, backoff_factor=0.5,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset(["GET", "HEAD"]),
                )
                s.mount("https://", HTTPAdapter(max_retries=retry))
                s.mount("http://", HTTPAdapter(max_retries=retry))
            except Exception:
                pass
            _SESSION = s
        return _SESSION


def http_get(url: str, *, params: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None,
             timeout: int = DEFAULT_TIMEOUT) -> Tuple[int, Any, str]:
    """Wrapper that returns (status_code, json-or-text, error). Never raises."""
    try:
        resp = _session().get(url, params=params, headers=headers,
                              timeout=timeout)
        ct = resp.headers.get("content-type", "")
        body: Any
        if "application/json" in ct or url.endswith(".json"):
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
        else:
            body = resp.text
        return resp.status_code, body, ""
    except requests.RequestException as exc:
        return 0, None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Source contract
# ---------------------------------------------------------------------------
class KnowledgeSource:
    """Abstract source. Subclasses implement :meth:`search`.

    Implementations MUST NOT raise — convert exceptions to an empty
    list and let the router record the error via :meth:`probe`.
    """

    name: str = "abstract"
    category: str = "general"
    description: str = ""
    requires_key: bool = False
    rate_limit: Optional[Tuple[int, float]] = None  # (max_calls, per_seconds)

    def __init__(self) -> None:
        self._limiter: Optional[_RateLimiter] = None
        if self.rate_limit:
            self._limiter = _RateLimiter(max_calls=self.rate_limit[0],
                                         per_seconds=self.rate_limit[1])

    # ----- subclass surface -------------------------------------------------
    def available(self) -> bool:
        """Return True when the source can be used. Override for key checks."""
        return True

    def search(self, query: str,
               *, max_results: int = 5) -> List[SearchResult]:
        raise NotImplementedError

    # ----- helpers ----------------------------------------------------------
    def _gate(self) -> None:
        if self._limiter is not None:
            self._limiter.acquire()

    def probe(self) -> SourceStatus:
        """Run a lightweight ping/search to populate the source status."""
        st = SourceStatus(name=self.name, category=self.category,
                           description=self.description,
                           requires_key=self.requires_key)
        if not self.available():
            st.available = False
            st.last_error = "missing api key" if self.requires_key else "unavailable"
            st.last_checked = time.time()
            return st
        started = time.time()
        try:
            results = self.search("hello", max_results=1)
            st.available = True  # zero results is fine, exception is not
            st.last_latency_ms = (time.time() - started) * 1000
        except Exception as exc:
            LOG.warning("rag probe %s failed: %s", self.name, exc)
            st.available = False
            st.last_error = f"{type(exc).__name__}: {exc}"
        st.last_checked = time.time()
        return st


__all__ = [
    "USER_AGENT", "DEFAULT_TIMEOUT", "LOG",
    "SearchResult", "SourceStatus", "KnowledgeSource",
    "http_get",
]
