"""
Retrieval-Augmented Generation (RAG) layer.

Public surface:
  - :class:`KnowledgeSource` — abstract source contract.
  - :class:`SearchResult`    — uniform result row.
  - :class:`KnowledgeRouter` — registry + parallel fan-out + health monitor.
  - :func:`get_router`        — module-level singleton accessor.

Each source connector lives in :mod:`core.rag.sources` and exposes one
class. Connectors are designed to fail gracefully — a degraded source
produces an empty list and a status the router can surface to callers,
never an exception that crashes the whole search.
"""

from .base import KnowledgeSource, SearchResult, SourceStatus
from .router import KnowledgeRouter, get_router

__all__ = [
    "KnowledgeSource", "SearchResult", "SourceStatus",
    "KnowledgeRouter", "get_router",
]
