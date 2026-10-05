"""
Skills cache layer.

Centralizes the on-disk caches the skills layer used to manage in two
different files (``.index_cache.json`` and ``.embeddings_cache.npz`` /
``.embeddings_mtime.json``). Treating them as one module makes
invalidation rules consistent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional


class SkillsCache:
    """Backed by JSON for the index and ``.npz`` for embeddings."""

    INDEX_FILE = ".index_cache.json"
    EMB_FILE = ".embeddings_cache.npz"
    EMB_MTIMES = ".embeddings_mtime.json"

    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)

    # ------------------------------------------------------------------
    # Index cache
    # ------------------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.skills_dir / self.INDEX_FILE

    def load_index(self) -> Optional[Dict[str, Any]]:
        if not self.index_path.exists():
            return None
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save_index(self, payload: Dict[str, Any]) -> None:
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"⚠️ فشل حفظ كاش فهرسة المهارات: {e}")

    def index_is_fresh(
        self,
        cached: Dict[str, Any],
        skills_count: int,
        ttl_seconds: int,
        index_now: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> bool:
        """A cache is fresh iff:
        - indexed less than ttl ago
        - skills count matches the directory listing
        - every cached path still exists with the same mtime (best effort)
        """
        try:
            indexed_at = datetime.fromisoformat(cached["indexed_at"])
        except Exception:
            return False
        if datetime.now() - indexed_at > timedelta(seconds=ttl_seconds):
            return False
        if cached.get("skills_count") != skills_count:
            return False

        skills = cached.get("skills") or {}
        # Use cheap stat check — if any skill path's mtime changed, invalidate.
        for name, data in skills.items():
            try:
                p = Path(data["path"])
                if not p.exists() or p.stat().st_mtime != data.get("last_modified"):
                    return False
            except Exception:
                return False
        return True

    # ------------------------------------------------------------------
    # Embeddings cache
    # ------------------------------------------------------------------
    @property
    def embeddings_path(self) -> Path:
        return self.skills_dir / self.EMB_FILE

    @property
    def embeddings_mtime_path(self) -> Path:
        return self.skills_dir / self.EMB_MTIMES

    def load_embeddings_mtimes(self) -> Optional[Dict[str, float]]:
        if not self.embeddings_mtime_path.exists():
            return None
        try:
            with open(self.embeddings_mtime_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save_embeddings_mtimes(self, mtimes: Dict[str, float]) -> None:
        try:
            with open(self.embeddings_mtime_path, "w", encoding="utf-8") as f:
                json.dump(mtimes, f, indent=2)
        except Exception as e:
            print(f"⚠️ فشل حفظ mtimes للتضمينات: {e}")
