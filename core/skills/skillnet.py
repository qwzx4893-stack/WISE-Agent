"""
Skillnet connector — local-first.

Uses the official `skillnet-ai <https://pypi.org/project/skillnet-ai/>`_ SDK
(maintained by zjunlp/SkillNet) to search and install skills from the
SkillNet registry (~400 000 community skills). The SDK ships a default
endpoint, so no environment variable is required: as long as the host has
network access to the SkillNet servers the integration just works.

When the SDK is missing (e.g. in a stripped sandbox) we fall back to a
small ``urllib`` client that still respects the optional
``SKILLNET_BASE_URL`` env var. When neither path is available we degrade
gracefully to an empty result set so callers can treat Skillnet as a
best-effort source.

Two integration points:

1. ``search()`` is invoked when the local index has no hit; results are
   surfaced to the LLM.
2. ``import_skill()`` writes the returned manifest into
   ``skills/<category>/<name>/<version>/`` and registers it with the
   :class:`LifecycleManager`.

A "skill-creator" fallback is exposed via :func:`scaffold_missing_skill`
which uses :class:`ThinkingEngine` (when available) to draft a SKILL.md
when both the local index and Skillnet miss.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..observability import Tracer
from ..paths import SKILLS_DIR

try:  # Optional SDK — preferred path.
    from skillnet_ai import SkillDownloader, SkillNetSearcher  # type: ignore
    _HAS_SDK = True
except Exception:  # pragma: no cover - exercised when SDK is absent.
    _HAS_SDK = False


@dataclass
class SkillnetHit:
    name: str
    description: str
    version: str
    rating: float
    category: str
    raw: Dict[str, Any]

    @property
    def url(self) -> str:
        return str(self.raw.get("skill_url") or self.raw.get("url") or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "rating": self.rating,
            "category": self.category,
            "url": self.url,
        }


# ---------------------------------------------------------------------------
# urllib fallback (used when the SDK can't be imported)
# ---------------------------------------------------------------------------
def _http_get_json(url: str, *, timeout: float = 10.0,
                   headers: Optional[Dict[str, str]] = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "agent-os/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        Tracer.emit("skillnet.http.error", url=url, error=str(exc))
        return None
    except json.JSONDecodeError as exc:
        Tracer.emit("skillnet.http.bad_json", url=url, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class SkillnetClient:
    """Local-first SkillNet client.

    Prefers the official ``skillnet-ai`` SDK; transparently falls back to a
    minimal urllib client when the SDK is not installed. Always enabled
    when the SDK is available — no environment variables required.
    """

    def __init__(self,
                 *,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("SKILLNET_API_KEY", "")
        # Optional override for the urllib fallback path. The SDK uses its
        # own default endpoint when nothing is provided.
        self.base_url = (base_url
                         or os.environ.get("SKILLNET_BASE_URL", "")).rstrip("/")

        self._sdk_searcher = None
        self._sdk_downloader = None
        if _HAS_SDK:
            try:
                if self.base_url:
                    self._sdk_searcher = SkillNetSearcher(skillnet_api_url=self.base_url)
                else:
                    self._sdk_searcher = SkillNetSearcher()
                self._sdk_downloader = SkillDownloader(api_token=self.api_key or None)
            except Exception as exc:  # pragma: no cover
                Tracer.emit("skillnet.sdk.init_failed", error=str(exc))

    @property
    def enabled(self) -> bool:
        # The SDK ships with a default endpoint; the urllib path needs an
        # explicit base URL.
        return bool(self._sdk_searcher) or bool(self.base_url)

    @property
    def backend(self) -> str:
        if self._sdk_searcher:
            return "skillnet-ai"
        if self.base_url:
            return "urllib"
        return "disabled"

    # ------------------------------------------------------------------ API
    def search(self, query: str, *, limit: int = 10,
               category: Optional[str] = None) -> List[SkillnetHit]:
        if not query:
            return []
        if self._sdk_searcher is not None:
            try:
                rows = self._sdk_searcher.search(
                    q=query,
                    category=category,
                    limit=limit,
                )
            except Exception as exc:
                Tracer.emit("skillnet.sdk.search_failed", error=str(exc))
                return []
            return self._parse_sdk_rows(rows)

        if not self.base_url:
            return []
        params: Dict[str, Any] = {"q": query, "limit": limit}
        if category:
            params["category"] = category
        url = f"{self.base_url}/skills/search?{urllib.parse.urlencode(params)}"
        data = _http_get_json(url, timeout=self.timeout, headers=self._headers())
        return self._parse_dict_rows(data)

    def fetch(self, name: str, *, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        if self._sdk_searcher is not None:
            # SDK has no ``fetch by name``; do a 1-result search.
            try:
                rows = self._sdk_searcher.search(q=name, limit=1)
            except Exception as exc:
                Tracer.emit("skillnet.sdk.fetch_failed", error=str(exc))
                rows = []
            hits = self._parse_sdk_rows(rows)
            return hits[0].raw if hits else None

        if not self.base_url:
            return None
        params: Dict[str, Any] = {}
        if version:
            params["version"] = version
        path = f"/skills/{urllib.parse.quote(name)}"
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _http_get_json(url, timeout=self.timeout, headers=self._headers())

    def import_skill(self, name: str, *,
                     category: Optional[str] = None,
                     version: Optional[str] = None,
                     skills_dir: Optional[Path] = None) -> Dict[str, Any]:
        sk_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
        manifest = self.fetch(name, version=version)
        if not isinstance(manifest, dict):
            return {"ok": False, "reason": "skillnet returned no manifest", "name": name}

        cat = (category
               or manifest.get("category")
               or "general").strip().lower() or "general"
        ver = manifest.get("version") or version or "1.0.0"
        target = sk_dir / cat / name / ver
        target.mkdir(parents=True, exist_ok=True)

        # If we have a SkillNet folder URL, try to download the full skill
        # contents via the SDK. Otherwise we just write a stub manifest.
        downloaded = False
        skill_url = manifest.get("skill_url") or manifest.get("url")
        if skill_url and self._sdk_downloader is not None:
            try:
                self._sdk_downloader.download(skill_url, target_dir=str(target))
                downloaded = True
            except Exception as exc:
                Tracer.emit("skillnet.sdk.download_failed",
                            skill=name, url=skill_url, error=str(exc))

        if not (target / "SKILL.md").exists():
            skill_md = manifest.get("skill_md") or manifest.get("readme") or ""
            (target / "SKILL.md").write_text(
                skill_md or f"# {name}\n\n{manifest.get('skill_description') or manifest.get('description', '')}\n",
                encoding="utf-8",
            )
        if not (target / "metadata.json").exists():
            (target / "metadata.json").write_text(
                json.dumps(
                    {
                        "name": name,
                        "version": ver,
                        "category": cat,
                        "description": manifest.get("skill_description")
                        or manifest.get("description", ""),
                        "author": manifest.get("author", "skillnet"),
                        "source": skill_url or "skillnet",
                        "rating": float(manifest.get("rating") or 0.0),
                        "imported_at": time.time(),
                    },
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        Tracer.emit("skillnet.import",
                    skill=name, category=cat, version=ver,
                    path=str(target), downloaded=downloaded)
        return {"ok": True, "name": name, "category": cat,
                "version": ver, "path": str(target),
                "downloaded": downloaded}

    # ------------------------------------------------------------ helpers
    def _headers(self) -> Dict[str, str]:
        h = {"User-Agent": "agent-os/1.0", "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    @staticmethod
    def _parse_sdk_rows(rows: List[Any]) -> List[SkillnetHit]:
        out: List[SkillnetHit] = []
        for row in rows or []:
            data: Dict[str, Any]
            if hasattr(row, "model_dump"):
                try:
                    data = row.model_dump()
                except Exception:
                    data = dict(row.__dict__)
            elif isinstance(row, dict):
                data = row
            else:
                continue
            name = (data.get("skill_name") or data.get("name") or "").strip()
            if not name:
                continue
            stars = float(data.get("stars") or 0)
            # Map stars → rough 0..1 rating so ranker.py can use it.
            rating = min(stars / 1000.0, 1.0)
            out.append(SkillnetHit(
                name=name,
                description=str(data.get("skill_description")
                                or data.get("description") or ""),
                version=str(data.get("version") or "1.0.0"),
                rating=rating,
                category=str(data.get("category") or "general").lower(),
                raw=data,
            ))
        return out

    @staticmethod
    def _parse_dict_rows(data: Any) -> List[SkillnetHit]:
        if not data:
            return []
        items: List[Any]
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("results") or data.get("skills") or []
        else:
            return []
        out: List[SkillnetHit] = []
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or entry.get("skill_name") or "").strip()
            if not name:
                continue
            out.append(SkillnetHit(
                name=name,
                description=(entry.get("description") or "").strip(),
                version=str(entry.get("version") or "1.0.0"),
                rating=float(entry.get("rating") or 0.0),
                category=(entry.get("category") or "general").lower(),
                raw=entry,
            ))
        return out


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------
def search_with_skillnet(query: str,
                         *,
                         local_results: List[Dict[str, Any]],
                         client: Optional[SkillnetClient] = None,
                         min_local: int = 1,
                         limit: int = 10) -> Dict[str, Any]:
    """Combined local + Skillnet search.

    ``local_results`` is whatever the local search produced. If we have at
    least ``min_local`` local hits we skip Skillnet entirely; otherwise we
    augment.
    """
    if len(local_results) >= min_local:
        return {"local": local_results, "skillnet": [],
                "used_skillnet": False, "backend": "local"}

    cli = client or SkillnetClient()
    if not cli.enabled:
        return {"local": local_results, "skillnet": [],
                "used_skillnet": False, "backend": cli.backend,
                "reason": "skillnet-ai SDK is unavailable"}

    hits = cli.search(query, limit=limit)
    return {
        "local": local_results,
        "skillnet": [h.to_dict() for h in hits],
        "used_skillnet": True,
        "backend": cli.backend,
    }


def scaffold_missing_skill(name: str, *,
                           description: Optional[str] = None,
                           category: Optional[str] = None,
                           skills_dir: Optional[Path] = None,
                           use_thinking: bool = True) -> Dict[str, Any]:
    """Last-resort fallback: when neither local nor Skillnet have a skill,
    draft a minimal scaffold so the agent can at least proceed.

    The scaffold is marked ``draft=true`` and the lifecycle state is set to
    ``updating`` so a human can finish it. If the optional ThinkingEngine
    is available we use it to flesh out the SKILL.md; otherwise we emit a
    boilerplate template.
    """
    sk_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
    cat = (category or "general").strip().lower() or "general"
    ver = "0.1.0"
    target = sk_dir / cat / name / ver
    target.mkdir(parents=True, exist_ok=True)

    body = description or f"Skill scaffold for `{name}`. Replace this with the real implementation."
    if use_thinking:
        try:
            from ..thinking.engine import ThinkingEngine  # type: ignore
            engine = ThinkingEngine()
            drafted = engine.draft_skill(name=name, description=description) if hasattr(engine, "draft_skill") else None
            if isinstance(drafted, str) and drafted.strip():
                body = drafted
        except Exception:
            pass

    (target / "SKILL.md").write_text(
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    (target / "metadata.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": ver,
                "category": cat,
                "description": description or body[:200],
                "author": "skill-creator",
                "draft": True,
            },
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    try:
        from .lifecycle import get_manager
        get_manager().mark_updating(
            name, reason="auto-scaffolded by skill-creator",
            actor="skillnet.scaffold",
        )
    except Exception:
        pass

    Tracer.emit("skillnet.scaffold",
                skill=name, category=cat, path=str(target))
    return {"ok": True, "name": name, "category": cat,
            "version": ver, "path": str(target), "drafted": True}


__all__ = [
    "SkillnetClient",
    "SkillnetHit",
    "search_with_skillnet",
    "scaffold_missing_skill",
]
