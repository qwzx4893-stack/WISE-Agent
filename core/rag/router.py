"""Knowledge router — registers sources, fans out searches, monitors health."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

from .base import KnowledgeSource, SearchResult, SourceStatus, LOG

# Cache window for status probes — re-probe at most every 60 seconds.
_STATUS_TTL = 60.0


class KnowledgeRouter:
    """Registry + parallel fan-out + cached health status."""

    _instance: Optional["KnowledgeRouter"] = None
    _lock = threading.RLock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "KnowledgeRouter":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialised = False  # type: ignore[attr-defined]
            return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialised", False):
            return
        self._sources: Dict[str, KnowledgeSource] = {}
        self._status_cache: Dict[str, SourceStatus] = {}
        self._initialised = True
        self._register_defaults()

    # ----------------------------------------------------------- Registration
    def _register_defaults(self) -> None:
        from .sources import all_sources
        for src in all_sources():
            self.register(src)

    def register(self, source: KnowledgeSource) -> None:
        self._sources[source.name] = source

    def unregister(self, name: str) -> None:
        self._sources.pop(name, None)
        self._status_cache.pop(name, None)

    @property
    def sources(self) -> Dict[str, KnowledgeSource]:
        return dict(self._sources)

    # ------------------------------------------------------------------ Health
    def cached_status(self) -> List[SourceStatus]:
        """Passive UI snapshot. Opening settings must not launch web probes."""
        return [self._status_cache.get(name) or SourceStatus(name=name,category=source.category,
            available=False,last_checked=0,description=source.description,requires_key=source.requires_key,
            last_error="Not probed; use explicit diagnostics to verify connectivity")
            for name,source in self.sources.items()]

    def status(self, name: Optional[str] = None,
               *, force: bool = False) -> List[SourceStatus]:
        """Return cached status for ``name`` (or every source)."""
        names: Iterable[str] = [name] if name else list(self._sources)
        out: List[SourceStatus] = []
        now = time.time()
        for n in names:
            src = self._sources.get(n)
            if src is None:
                continue
            cached = self._status_cache.get(n)
            if (cached is not None and not force
                    and (now - cached.last_checked) < _STATUS_TTL):
                out.append(cached)
                continue
            st = src.probe()
            self._status_cache[n] = st
            out.append(st)
        return out

    def is_available(self, name: str) -> bool:
        """Cheap check used to skip degraded sources during fan-out."""
        cached = self._status_cache.get(name)
        if cached is None:
            return self._sources[name].available()
        if (time.time() - cached.last_checked) > _STATUS_TTL:
            return self._sources[name].available()
        return cached.available

    # ------------------------------------------------------------------- Search
    def search(self, query: str,
               *, sources: Optional[List[str]] = None,
               max_results: int = 5,
               fail_silently: bool = True,
               include_degraded: bool = False) -> List[SearchResult]:
        names = sources or list(self._sources)
        names = [n for n in names if n in self._sources]
        if not include_degraded:
            names = [n for n in names if self.is_available(n)]
        if not names:
            return []
        try:
            from .. import observability  # type: ignore
            tracer = getattr(observability, "Tracer", None)
        except Exception:
            tracer = None

        results: List[SearchResult] = []
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
            futs = {ex.submit(self._safe_search, n, query, max_results): n
                    for n in names}
            for fut in futs:
                n = futs[fut]
                try:
                    rs = fut.result(timeout=15) or []
                    if tracer is not None:
                        tracer.emit("knowledge.search",
                                     source=n, query=query[:120],
                                     count=len(rs))
                    results.extend(rs)
                except Exception as exc:
                    LOG.warning("source %s failed: %s", n, exc)
                    if not fail_silently:
                        raise
        # Stable sort: prefer newest then largest snippet (tiebreaker).
        return results

    def _safe_search(self, name: str, query: str,
                      max_results: int) -> List[SearchResult]:
        src = self._sources[name]
        try:
            return src.search(query, max_results=max_results) or []
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("source %s raised: %s", name, exc)
            self._status_cache[name] = SourceStatus(
                name=name, category=src.category, available=False,
                last_checked=time.time(),
                last_error=f"{type(exc).__name__}: {exc}",
                description=src.description,
                requires_key=src.requires_key,
            )
            return []


def get_router() -> KnowledgeRouter:
    return KnowledgeRouter()


__all__ = ["KnowledgeRouter", "get_router"]
