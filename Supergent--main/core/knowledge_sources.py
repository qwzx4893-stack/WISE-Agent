"""Back-compat shim — the real RAG layer lives in :mod:`core.rag`.

Older callers import :func:`search_knowledge` and the four legacy source
classes from this module. Keep that surface, but route everything
through the new :class:`core.rag.KnowledgeRouter`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .rag import KnowledgeRouter, SearchResult, get_router
from .rag.base import KnowledgeSource as _Source
from .rag.sources.academic import (
    ArxivSource as _NewArxiv,
    OpenAlexSource as _NewOpenAlex,
    SemanticScholarSource as _NewSemanticScholar,
)
from .rag.sources.general import WikipediaSource as _NewWikipedia

USER_AGENT = "AgentOS/1.1"
REQUEST_TIMEOUT = 10
MAX_WORKERS = 5

_logger = logging.getLogger("agent_os.knowledge")


# --- Legacy source-class adapters -------------------------------------------
def _legacy_dicts(items: List[SearchResult]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in items:
        d = r.to_dict()
        # Preserve the legacy 'snippet'/'abstract'/'summary' alias map.
        out.append({
            "title": d.get("title", ""),
            "snippet": d.get("snippet", ""),
            "abstract": d.get("snippet", ""),
            "summary": d.get("snippet", ""),
            "url": d.get("url", ""),
            "source": d.get("source", ""),
            "metadata": d.get("metadata", {}),
        })
    return out


class _LegacyAdapter:
    _src: _Source

    def search(self, query: str,
               max_results: int = 5) -> List[Dict[str, Any]]:
        try:
            return _legacy_dicts(self._src.search(
                query, max_results=max_results))
        except Exception as exc:
            return [{"error": str(exc), "source": getattr(self._src, "name", "?")}]


class WikipediaSource(_LegacyAdapter):
    def __init__(self) -> None:
        self._src = _NewWikipedia()


class SemanticScholarSource(_LegacyAdapter):
    def __init__(self) -> None:
        self._src = _NewSemanticScholar()


class ArxivSource(_LegacyAdapter):
    def __init__(self) -> None:
        self._src = _NewArxiv()


class OpenAlexSource(_LegacyAdapter):
    def __init__(self) -> None:
        self._src = _NewOpenAlex()


# --- Legacy router shim -----------------------------------------------------
class _LegacyRouterShim:
    """Wrapper that exposes the legacy ``router.sources`` dict API while
    still delegating real work to :class:`KnowledgeRouter`."""

    def __init__(self) -> None:
        self.sources: Dict[str, _LegacyAdapter] = {
            "wikipedia": WikipediaSource(),
            "semantic_scholar": SemanticScholarSource(),
            "arxiv": ArxivSource(),
            "openalex": OpenAlexSource(),
        }

    def search(self, query: str, sources: str = "wikipedia,arxiv,openalex",
               max_results: int = 5) -> List[Dict[str, Any]]:
        names = [s.strip() for s in (sources or "").split(",") if s.strip()]
        results = get_router().search(
            query, sources=names, max_results=max_results)
        return _legacy_dicts(results)


def search_knowledge(query: str,
                     sources: str = "wikipedia,arxiv,openalex",
                     max_results: int = 5) -> str:
    """Back-compat helper — returns a formatted Markdown string."""
    items = _LegacyRouterShim().search(query, sources=sources,
                                        max_results=max_results)
    if not items:
        return "(no results)"
    out: List[str] = []
    for it in items:
        title = it.get("title", "?")
        url = it.get("url", "")
        body = it.get("snippet") or ""
        line = f"- [{title}]({url})" if url else f"- {title}"
        if body:
            line += f"\n  {body[:400]}"
        out.append(line)
    return "\n".join(out)


def _register_knowledge_tools(registry: Optional[Any] = None) -> None:
    """Legacy hook used by older registry code. No-op by default."""
    return None


# Public re-exports.
__all__ = [
    "USER_AGENT", "REQUEST_TIMEOUT", "MAX_WORKERS",
    "WikipediaSource", "SemanticScholarSource", "ArxivSource",
    "OpenAlexSource", "search_knowledge", "_register_knowledge_tools",
    "KnowledgeRouter", "get_router",
]
