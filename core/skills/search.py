"""Semantic + lexical skill search."""

from __future__ import annotations

import threading
from typing import Dict, List, Tuple

import numpy as np

from .cache import SkillsCache
from .config import SkillConfig
from .indexer import SkillIndexer


class SkillSearch:
    def __init__(self):
        from ..paths import SKILLS_DIR

        self.indexer = SkillIndexer()
        self.config = SkillConfig()
        self.cache = SkillsCache(SKILLS_DIR)
        self._encoder = None
        self._embeddings: Dict[str, np.ndarray] = {}
        self._lock = threading.RLock()

    @property
    def encoder(self):
        if not self.config.get("semantic_search", False):
            return False
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
                model_name = self.config.get("embedding_model", "all-MiniLM-L6-v2")
                self._encoder = SentenceTransformer(model_name)
            except Exception:
                self._encoder = False
        return self._encoder

    def _skill_text(self, name: str, info: dict) -> str:
        return (
            f"{name}: {info.get('description', '')} "
            f"category={info.get('category', '')} "
            f"{info.get('excerpt', '')}"
        )

    # ------------------------------------------------------------------
    # Embeddings cache
    # ------------------------------------------------------------------
    def _load_or_build_embeddings(self) -> None:
        with self._lock:
            if self._embeddings:
                return

            index = self.indexer.get_index()
            cached_mtimes = self.cache.load_embeddings_mtimes()

            if (
                self.config.get("cache_embeddings")
                and self.cache.embeddings_path.exists()
                and cached_mtimes is not None
            ):
                fresh = all(
                    name in cached_mtimes
                    and info["last_modified"] == cached_mtimes.get(name)
                    for name, info in index.items()
                )
                if fresh and len(cached_mtimes) == len(index):
                    try:
                        data = np.load(self.cache.embeddings_path)
                        names = data["names"].tolist()
                        matrix = data["matrix"]
                        for i, name in enumerate(names):
                            self._embeddings[name] = matrix[i]
                        return
                    except Exception as e:
                        print(f"⚠️ فشل تحميل تضمينات المهارات: {e}")

            mtimes: Dict[str, float] = {}
            names: List[str] = []
            embeddings_list: List[np.ndarray] = []
            for name, info in index.items():
                emb = self.encoder.encode(self._skill_text(name, info))
                self._embeddings[name] = emb
                names.append(name)
                embeddings_list.append(emb)
                mtimes[name] = info["last_modified"]

            if self.config.get("cache_embeddings"):
                try:
                    matrix = np.array(embeddings_list)
                    np.savez_compressed(
                        self.cache.embeddings_path,
                        names=np.array(names),
                        matrix=matrix,
                    )
                    self.cache.save_embeddings_mtimes(mtimes)
                except Exception as e:
                    print(f"⚠️ فشل حفظ تضمينات المهارات: {e}")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 5,
               category: str | None = None) -> List[Tuple[str, float, str]]:
        if not self.encoder:
            from .ranker import rank_skills
            ranked = rank_skills(query)
            index = self.indexer.get_index()
            results: List[Tuple[str, float, str]] = []
            for item in ranked:
                # Quality/recency bonuses alone do not establish relevance.
                # Without this guard an unrelated skill always wins a query
                # even when none of its instructions match the task.
                if item.relevance <= 0 and item.bundle_boost <= 0:
                    continue
                if category and index.get(item.name, {}).get("category") != category:
                    continue
                info = index.get(item.name, {})
                results.append((item.name, float(item.score), info.get("description", "")))
                if len(results) >= top_k:
                    break
            return results

        self._load_or_build_embeddings()
        if not self._embeddings:
            return []

        query_emb = self.encoder.encode(query)
        index = self.indexer.get_index()
        scores: List[Tuple[str, float]] = []
        with self._lock:
            for name, emb in self._embeddings.items():
                if category and index.get(name, {}).get("category") != category:
                    continue
                sim = float(
                    np.dot(query_emb, emb)
                    / (np.linalg.norm(query_emb) * np.linalg.norm(emb) + 1e-9)
                )
                scores.append((name, sim))

        scores.sort(key=lambda x: x[1], reverse=True)
        results: List[Tuple[str, float, str]] = []
        for name, sim in scores[:top_k]:
            info = index.get(name, {})
            results.append((name, sim, info.get("description", "")))
        return results
