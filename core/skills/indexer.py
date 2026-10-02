"""
Skills indexer.

Recursive scanner that finds every ``SKILL.md`` under the skills directory.
The previous implementation only inspected ``skills/<name>/SKILL.md`` and
``skills/<name>/<version>/SKILL.md`` which silently dropped 150+ skills
nested inside container folders such as ``skills/n8n/1.0.0/<sub>/SKILL.md``.

The new index entries carry:

- ``name``        - unique skill name (suffix-disambiguated when needed)
- ``version``    - parent dir if it parses as semver, else ``"1.0.0"``
- ``description`` - first markdown heading or first 100 chars
- ``excerpt``    - first 300 chars (collapsed)
- ``path``       - absolute folder containing ``SKILL.md``
- ``last_modified`` - mtime of that folder
- ``category``   - inferred via :mod:`core.skills.categories`
- ``metadata``   - merged ``metadata.json`` (if present)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cache import SkillsCache
from .categories import infer_category
from .config import SkillConfig

_VERSION_RE = __import__("re").compile(r"^\d+(\.\d+){0,3}$")

#: Top-level directory names recognised as categories when they appear at
#: ``skills/<category>/<name>/<version>/SKILL.md``. Anything else is treated
#: as part of the skill name (preserving the legacy ``skills/<name>/...``
#: layout).
_KNOWN_CATEGORIES: frozenset = frozenset({
    "security", "web3", "data", "devops", "frontend", "backend", "database",
    "automation", "seo", "agents", "knowledge", "media", "llm", "testing",
    "business", "infra", "memory", "browser", "design", "general",
})


class SkillIndexer:
    _instance = None
    _lock = threading.RLock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        with self._lock:
            if getattr(self, "_initialized", False):
                return
            from ..paths import SKILLS_DIR

            self.config = SkillConfig()
            self.skills_dir = SKILLS_DIR
            self.cache = SkillsCache(self.skills_dir)
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            self._index: Dict[str, Dict[str, Any]] = {}
            self._last_indexed: Optional[datetime] = None
            self._load_or_rebuild()
            self._initialized = True
            # First-boot self-healing: when no lifecycle file exists yet,
            # run a lightweight health check across every indexed skill so
            # broken/incomplete entries are flagged before the LLM picks
            # them up. Disabled via AGENT_OS_SKIP_FIRST_BOOT_CHECK=1 for
            # tests that want a clean indexer start.
            try:
                import os as _os
                if not _os.environ.get("AGENT_OS_SKIP_FIRST_BOOT_CHECK"):
                    from .lifecycle import LIFECYCLE_FILE, first_boot_health_check
                    if not LIFECYCLE_FILE.exists():
                        first_boot_health_check()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def _classify_skill(self, skill_md: Path) -> tuple[str, str, Path, Optional[str]]:
        """Return ``(name, version, folder, category_hint)`` for a SKILL.md file.

        Supports both layouts:

        - **Legacy**: ``skills/<name>/SKILL.md`` and
          ``skills/<name>/<version>/SKILL.md``.
        - **Nested**: ``skills/<category>/<name>/<version>/SKILL.md`` —
          when the first part matches one of the standard 19 categories we
          treat it as a category hint and strip it from the name.

        Anything deeper collapses into ``__``-joined names with the deepest
        semver folder as the version.
        """
        folder = skill_md.parent
        rel = folder.relative_to(self.skills_dir)
        parts = rel.parts

        if parts and parts[0] == "verified":
            parts = parts[1:]

        category_hint: Optional[str] = None
        if parts and parts[0] in _KNOWN_CATEGORIES:
            category_hint = parts[0]
            parts = parts[1:]

        version = "1.0.0"
        name_parts: List[str] = []
        for p in parts:
            if _VERSION_RE.match(p):
                version = p
                continue
            name_parts.append(p)

        if not name_parts:
            name = (category_hint or (rel.parts[0] if rel.parts else "unknown"))
        elif len(name_parts) == 1:
            name = name_parts[0]
        else:
            name = "__".join(name_parts)

        return name, version, folder, category_hint

    def _read_metadata(self, folder: Path, name: str) -> Dict[str, Any]:
        meta_file = folder / "metadata.json"
        meta: Dict[str, Any] = {}
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f) or {}
            except Exception as e:
                print(f"⚠️ فشل قراءة metadata.json لـ {name}: {e}")
        return meta

    def _read_description(self, skill_md: Path) -> tuple[str, str]:
        try:
            text = skill_md.read_text(encoding="utf-8-sig", errors="replace")
        except Exception as e:
            print(f"⚠️ فشل قراءة محتوى المهارة {skill_md}: {e}")
            return "", ""

        description = ""
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                try:
                    import yaml
                    frontmatter = yaml.safe_load(parts[1])
                    if isinstance(frontmatter, dict):
                        description = str(frontmatter.get("description", ""))
                except Exception:
                    pass
                text = parts[2]
        lines = [l.strip() for l in text.splitlines() if l.strip() and l.strip() not in ("---", "***", "___")]
        if not lines:
            return "", ""
        first = lines[0]
        if description:
            pass
        elif first.startswith("# "):
            description = first[2:].strip()
        else:
            description = first[:100]
        excerpt = " ".join(lines[:6])[:300]
        return description, excerpt

    def _scan_skills(self) -> Dict[str, Dict[str, Any]]:
        index: Dict[str, Dict[str, Any]] = {}
        # Recursive search for every SKILL.md.
        for skill_md in self.skills_dir.rglob("SKILL.md"):
            # Skip the .archive folder created by the dedup tool.
            try:
                rel_first = skill_md.relative_to(self.skills_dir).parts[0]
            except Exception:
                rel_first = ""
            if rel_first.startswith("."):
                continue

            try:
                name, version, folder, category_hint = self._classify_skill(skill_md)
            except Exception:
                continue

            # Disambiguate when the same name exists at multiple paths.
            base_name = name
            suffix = 2
            while name in index and index[name]["path"] != str(folder):
                name = f"{base_name}_{suffix}"
                suffix += 1

            metadata = self._read_metadata(folder, name)
            metadata.setdefault("name", name)
            metadata.setdefault("version", version)

            description, excerpt = self._read_description(skill_md)
            if not str(metadata.get("description") or "").strip("- \t\r\n"):
                metadata["description"] = description

            # Honour the category hint from the nested folder layout, but let
            # explicit metadata.json beat it.
            if category_hint and not metadata.get("category"):
                metadata["category"] = category_hint

            try:
                last_modified = folder.stat().st_mtime
            except Exception:
                last_modified = 0.0

            category = infer_category(
                name=name,
                description=metadata.get("description", description),
                path=folder,
                metadata=metadata,
            )

            index[name] = {
                "name": name,
                "version": version,
                "description": metadata.get("description", description),
                "excerpt": excerpt,
                "path": str(folder),
                "last_modified": last_modified,
                "category": category,
                "metadata": metadata,
            }
        return index

    # ------------------------------------------------------------------
    # Cache wiring
    # ------------------------------------------------------------------
    def _count_skill_files(self) -> int:
        return sum(1 for _ in self.skills_dir.rglob("SKILL.md"))

    def _load_or_rebuild(self) -> None:
        cached = self.cache.load_index()
        ttl = int(self.config.get("index_refresh_interval", 300))
        skills_count = self._count_skill_files()

        if cached and self.cache.index_is_fresh(cached, skills_count, ttl):
            self._index = cached["skills"]
            try:
                self._last_indexed = datetime.fromisoformat(cached["indexed_at"])
            except Exception:
                self._last_indexed = datetime.now()
            return

        self._index = self._scan_skills()
        self._last_indexed = datetime.now()
        self.cache.save_index({
            "indexed_at": self._last_indexed.isoformat(),
            "skills_count": len(self._index),
            "skills": self._index,
        })

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_index(self, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            ttl = timedelta(seconds=int(self.config.get("index_refresh_interval", 300)))
            stale = (
                force_refresh
                or self._last_indexed is None
                or datetime.now() - self._last_indexed > ttl
            )
            if stale:
                self._load_or_rebuild()
            return self._index.copy()

    def list_skills(self) -> List[str]:
        return list(self.get_index().keys())

    def list_categories(self) -> Dict[str, List[str]]:
        from .categories import categorize_index
        return categorize_index(self.get_index())

    def get_skill_info(self, name: str) -> Optional[Dict[str, Any]]:
        return self.get_index().get(name)

    def force_reindex(self) -> int:
        with self._lock:
            self._index = self._scan_skills()
            self._last_indexed = datetime.now()
            self.cache.save_index({
                "indexed_at": self._last_indexed.isoformat(),
                "skills_count": len(self._index),
                "skills": self._index,
            })
            return len(self._index)
