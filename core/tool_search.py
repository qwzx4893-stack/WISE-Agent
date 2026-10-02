"""Semantic tool search — pick the right tools for a query.

Two-tier ranker:

1. **Embeddings** (preferred) — uses ``sentence-transformers`` if it is
   importable. Indexes tool name + description + use_cases. Cosine
   similarity gives the score.
2. **TF-IDF / keyword overlap** (fallback) — a small in-process
   inverted index. No third-party dependency.

The ranker is rebuilt on demand when the underlying tool catalogue
size changes (cheap signal). Re-build is idempotent and thread-safe.

API:

- :func:`rank_tools(query, k, registry, awareness)` -> list of dicts
  ``[{"name": ..., "score": float, "source": "embed"|"tfidf",
     "description": ...}]``.
- :func:`refresh_index(registry, awareness)` — force rebuild.
- :func:`stats()` — diagnostic.
"""

from __future__ import annotations

import math
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------
try:
    from sentence_transformers import SentenceTransformer  # type: ignore
    import numpy as _np  # type: ignore
    _ST_AVAILABLE = True
except Exception:  # noqa: BLE001
    SentenceTransformer = None  # type: ignore
    _np = None  # type: ignore
    _ST_AVAILABLE = False


# --------------------------------------------------------------------------
@dataclass
class ToolDoc:
    name: str
    description: str = ""
    category: str = ""
    use_cases: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        bits = [self.name.replace("_", " "), self.description, self.category]
        bits.extend(self.use_cases or [])
        return " ".join(b for b in bits if b)


# --------------------------------------------------------------------------
class _ToolIndex:
    """Lazy index over a tool registry."""

    MODEL_NAME = "all-MiniLM-L6-v2"

    def __init__(self) -> None:
        self._docs: List[ToolDoc] = []
        self._model: Any = None
        self._embeddings: Any = None
        self._tfidf_idf: Dict[str, float] = {}
        self._tfidf_doc_vecs: List[Dict[str, float]] = []
        self._signature: Tuple[int, int] = (0, 0)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def maybe_rebuild(self, docs: List[ToolDoc]) -> None:
        sig = (len(docs), sum(hash(d.name) & 0xFFFF for d in docs[:200]))
        if sig == self._signature and self._docs:
            return
        with self._lock:
            self._docs = docs
            self._signature = sig
            self._build_tfidf()
            if _ST_AVAILABLE:
                try:
                    self._build_embeddings()
                except Exception:
                    self._embeddings = None
                    self._model = None

    # ------------------------------------------------------------------
    # TF-IDF
    # ------------------------------------------------------------------
    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return [t for t in re.split(r"[^A-Za-z0-9_\u0600-\u06FF]+", text.lower()) if t]

    def _build_tfidf(self) -> None:
        df: Dict[str, int] = {}
        doc_tokens: List[List[str]] = []
        for d in self._docs:
            toks = self._tokenize(d.text)
            doc_tokens.append(toks)
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n = max(1, len(self._docs))
        self._tfidf_idf = {t: math.log((1.0 + n) / (1.0 + c)) + 1.0 for t, c in df.items()}
        vecs: List[Dict[str, float]] = []
        for toks in doc_tokens:
            tf: Dict[str, float] = {}
            for t in toks:
                tf[t] = tf.get(t, 0.0) + 1.0
            length = max(1.0, float(len(toks)))
            v: Dict[str, float] = {}
            for t, c in tf.items():
                v[t] = (c / length) * self._tfidf_idf.get(t, 0.0)
            vecs.append(v)
        self._tfidf_doc_vecs = vecs

    def _query_tfidf(self, query: str, k: int) -> List[Tuple[int, float]]:
        toks = self._tokenize(query)
        qv: Dict[str, float] = {}
        for t in toks:
            qv[t] = qv.get(t, 0.0) + self._tfidf_idf.get(t, 0.0)
        if not qv:
            return []
        # Cosine
        q_norm = math.sqrt(sum(v * v for v in qv.values())) or 1.0
        scores: List[Tuple[int, float]] = []
        for i, dv in enumerate(self._tfidf_doc_vecs):
            if not dv:
                continue
            dot = 0.0
            for t, v in qv.items():
                if t in dv:
                    dot += v * dv[t]
            if dot == 0.0:
                continue
            d_norm = math.sqrt(sum(v * v for v in dv.values())) or 1.0
            scores.append((i, dot / (q_norm * d_norm)))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------
    def _build_embeddings(self) -> None:
        if not _ST_AVAILABLE:
            return
        if self._model is None:
            self._model = SentenceTransformer(self.MODEL_NAME)
        texts = [d.text for d in self._docs] or [""]
        self._embeddings = self._model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False,
        )

    def _query_embeddings(self, query: str, k: int) -> List[Tuple[int, float]]:
        if not _ST_AVAILABLE or self._model is None or self._embeddings is None:
            return []
        try:
            q = self._model.encode([query], normalize_embeddings=True,
                                    show_progress_bar=False)
            sims = (self._embeddings @ q.T).reshape(-1)
            order = sims.argsort()[::-1][:k]
            return [(int(i), float(sims[i])) for i in order]
        except Exception:
            return []

    # ------------------------------------------------------------------
    def search(self, query: str, k: int = 8) -> Tuple[List[Tuple[int, float]], str]:
        emb = self._query_embeddings(query, k * 2)
        if emb:
            return emb[:k], "embed"
        return self._query_tfidf(query, k), "tfidf"


_INDEX = _ToolIndex()


# --------------------------------------------------------------------------
def _docs_from_sources(registry: Any, awareness: Any) -> List[ToolDoc]:
    """Merge tools from the runtime registry + awareness manifest."""
    by_name: Dict[str, ToolDoc] = {}
    # Awareness (manifest packs) — has rich descriptions.
    if awareness is not None and hasattr(awareness, "tools"):
        for entry in (getattr(awareness, "tools", {}) or {}).values():
            try:
                name = (entry or {}).get("name", "")
                if not name:
                    continue
                by_name[name] = ToolDoc(
                    name=name,
                    description=str((entry or {}).get("description", "")),
                    category=str((entry or {}).get("category", "")),
                    use_cases=[str(u) for u in (entry or {}).get("use_cases") or []],
                )
            except Exception:
                continue
    # Runtime registry — names that have no manifest still get indexed.
    if registry is not None and hasattr(registry, "tools"):
        for name, fn in (getattr(registry, "tools", {}) or {}).items():
            if name in by_name:
                continue
            doc_str = (getattr(fn, "__doc__", "") or "").strip()
            by_name[name] = ToolDoc(
                name=name, description=doc_str.splitlines()[0][:300] if doc_str else "",
            )
    return list(by_name.values())


def refresh_index(registry: Any, awareness: Optional[Any] = None) -> Dict[str, Any]:
    docs = _docs_from_sources(registry, awareness)
    _INDEX.maybe_rebuild(docs)
    return {"docs": len(docs), "embeddings": _ST_AVAILABLE}


def rank_tools(query: str, *,
               k: int = 8,
               registry: Any,
               awareness: Optional[Any] = None) -> List[Dict[str, Any]]:
    docs = _docs_from_sources(registry, awareness)
    _INDEX.maybe_rebuild(docs)
    if not _INDEX._docs:
        return []
    pairs, source = _INDEX.search(query, k=k)
    out: List[Dict[str, Any]] = []
    for idx, score in pairs:
        if idx < 0 or idx >= len(_INDEX._docs):
            continue
        d = _INDEX._docs[idx]
        out.append({
            "name": d.name,
            "score": round(float(score), 4),
            "source": source,
            "description": d.description[:300],
            "category": d.category,
        })
    return out


def stats() -> Dict[str, Any]:
    return {
        "embeddings_available": _ST_AVAILABLE,
        "indexed_docs": len(_INDEX._docs),
        "model": _ToolIndex.MODEL_NAME if _ST_AVAILABLE else None,
    }


__all__ = ["rank_tools", "refresh_index", "stats", "ToolDoc"]
