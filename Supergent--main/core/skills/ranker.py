"""
Professional skill selection.

Given a free-form task description, rank the locally indexed skills using
four weak signals and combine them into a single score:

1. **Relevance** — Jaccard similarity between task tokens and the union of
   skill name + description + excerpt + category.
2. **Success history** — counts from :class:`LifecycleManager` (success vs
   failure). Failed/inactive/deprecated skills are pushed to the bottom.
3. **Quality** — explicit ``rating`` from metadata, plus a small bonus for
   skills with rich metadata.
4. **Bundle context** — if the task matches a pre-defined bundle we boost
   that bundle's members.

The output is a list of ``(name, score, breakdown)`` tuples sorted by
descending score. ``recommend_skills`` returns the top-K names plus a
shallow explanation suitable for showing to the user / model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..paths import SKILLS_DIR

#: Lightweight stopword list that mirrors the dedup module so both stay
#: roughly in sync.
_STOPWORDS: Set[str] = {
    "the", "and", "for", "with", "you", "your", "our", "this", "that",
    "from", "into", "about", "are", "was", "were", "been", "has", "have",
    "had", "can", "will", "use", "used", "via", "based", "build", "make",
    "skill", "agent",
}

_TOKEN_RE = re.compile(r"[^\W\d_][\w-]{1,}", re.UNICODE)


def _tokens(text: str) -> Set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")
            if t.lower() not in _STOPWORDS}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / float(len(a | b))


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------
DEFAULT_BUNDLES: Dict[str, Dict[str, Any]] = {
    "web-development": {
        "description": "Building modern websites & web apps (React/Next, Tailwind, shadcn).",
        "skills": [
            "react", "next", "tailwind", "shadcn-ui", "typescript",
            "nextjs", "vercel", "frontend",
        ],
        "keywords": [
            "web", "website", "frontend", "react", "next", "tailwind",
            "ui", "shadcn",
        ],
    },
    "security-audit": {
        "description": "Security/pentest sweeps for web, code and dependencies.",
        "skills": [
            "semgrep", "trufflehog", "gitleaks", "snyk", "burp-suite-testing",
            "metasploit-framework", "binwalk", "radare2",
        ],
        "keywords": [
            "security", "pentest", "vuln", "audit", "exploit", "malware",
            "cve", "scan",
        ],
    },
    "smart-contract-audit": {
        "description": "Solidity/EVM auditing pipeline.",
        "skills": [
            "slither", "mythril", "echidna", "medusa", "aderyn",
            "foundry", "ethers-js",
        ],
        "keywords": [
            "solidity", "evm", "smart-contract", "blockchain", "ethereum",
            "defi", "audit",
        ],
    },
    "data-science": {
        "description": "Data wrangling + analytics + ML modelling.",
        "skills": [
            "pandas", "polars", "scikit-learn", "duckdb", "dbt",
            "snowflake", "data-scientist",
        ],
        "keywords": [
            "data", "dataset", "csv", "ml", "machine-learning", "model",
            "analytics", "feature",
        ],
    },
    "mobile-development": {
        "description": "Cross-platform mobile (Flutter / React Native / Expo).",
        "skills": [
            "flutter-expert", "react-native", "expo", "maestro",
            "shadcn-ui", "webtoapp",
        ],
        "keywords": [
            "mobile", "android", "ios", "flutter", "react-native", "expo",
        ],
    },
    "devops": {
        "description": "Containers, IaC and CI/CD.",
        "skills": [
            "docker", "kubernetes", "terraform", "ansible", "github-actions",
            "kubernetes-architect", "terraform-specialist",
        ],
        "keywords": [
            "devops", "infra", "deploy", "ci", "cd", "kubernetes", "docker",
            "terraform",
        ],
    },
    "research": {
        "description": "Deep web/document research workflows.",
        "skills": [
            "exa", "tavily", "firecrawl", "deep-research", "openalex",
            "search-knowledge",
        ],
        "keywords": [
            "research", "search", "wiki", "paper", "document", "scrape",
        ],
    },
    "automation": {
        "description": "Workflow / integration automation.",
        "skills": [
            "n8n", "zapier", "temporal", "stripe", "twilio", "slack", "discord",
        ],
        "keywords": [
            "automation", "workflow", "webhook", "integration", "schedule",
        ],
    },
}

BUNDLES_FILE = SKILLS_DIR / ".bundles.json"


def load_bundles(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    p = Path(path) if path else BUNDLES_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return DEFAULT_BUNDLES


def save_default_bundles(path: Optional[Path] = None) -> Path:
    p = Path(path) if path else BUNDLES_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(DEFAULT_BUNDLES, indent=2, ensure_ascii=False),
                 encoding="utf-8")
    return p


def detect_bundle(task: str,
                  bundles: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[str]:
    """Pick the bundle whose keywords best overlap the task tokens."""
    bundles = bundles or load_bundles()
    task_tokens = _tokens(task)
    if not task_tokens:
        return None
    best: Tuple[Optional[str], float] = (None, 0.0)
    for name, meta in bundles.items():
        kw = set((meta.get("keywords") or []))
        score = len(task_tokens & kw)
        if score > best[1]:
            best = (name, float(score))
    return best[0] if best[1] > 0 else None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@dataclass
class SkillScore:
    name: str
    score: float
    relevance: float = 0.0
    success: float = 0.0
    quality: float = 0.0
    bundle_boost: float = 0.0
    state: str = "active"
    breakdown: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "relevance": round(self.relevance, 4),
            "success": round(self.success, 4),
            "quality": round(self.quality, 4),
            "bundle_boost": round(self.bundle_boost, 4),
            "state": self.state,
        }


def _success_signal(history: Dict[str, Dict[str, Any]] | None,
                    name: str) -> Tuple[float, str]:
    if not history:
        return 0.5, "active"
    rec = history.get(name)
    if not rec:
        return 0.5, "active"
    state = (rec.get("state") or "active").lower()
    if state == "deprecated":
        return 0.0, state
    if state == "failed":
        return 0.05, state
    if state == "inactive":
        return 0.2, state
    s = float(rec.get("success_count", 0) or 0)
    f = float(rec.get("failure_count", 0) or 0)
    total = s + f
    if total <= 0:
        return 0.5, state
    return s / total, state


def _quality_signal(skill: Dict[str, Any]) -> float:
    metadata = skill.get("metadata") or {}
    rating = float(metadata.get("rating") or 0.0)
    base = min(rating / 5.0, 1.0) if rating else 0.0
    bonus = 0.0
    for key in ("author", "license", "version", "category", "capabilities"):
        if metadata.get(key):
            bonus += 0.05
    return min(base + bonus, 1.0)


def rank_skills(task: str,
                index: Optional[Dict[str, Dict[str, Any]]] = None,
                history: Optional[Dict[str, Dict[str, Any]]] = None,
                bundles: Optional[Dict[str, Dict[str, Any]]] = None,
                *,
                bundle_boost: float = 0.25,
                state_filter: Optional[Iterable[str]] = ("active", "updating")) -> List[SkillScore]:
    if index is None:
        from .indexer import SkillIndexer
        index = SkillIndexer().get_index()
    if history is None:
        try:
            from .lifecycle import get_manager
            history = get_manager().all()
        except Exception:
            history = {}
    bundles = bundles or load_bundles()

    task_tokens = _tokens(task)
    bundle_name = detect_bundle(task, bundles)
    bundle_skills: Set[str] = set()
    if bundle_name:
        bundle_skills = set(bundles.get(bundle_name, {}).get("skills") or [])

    state_filter_set = set(state_filter) if state_filter else None

    out: List[SkillScore] = []
    for name, info in index.items():
        text = " ".join([
            info.get("name") or "",
            info.get("description") or "",
            info.get("excerpt") or "",
            info.get("category") or "",
        ])
        skill_tokens = _tokens(text)
        relevance = _jaccard(task_tokens, skill_tokens)
        success_score, state = _success_signal(history, name)
        if state_filter_set and state not in state_filter_set:
            continue
        quality = _quality_signal(info)
        bb = bundle_boost if name in bundle_skills else 0.0

        score = (relevance * 0.55
                 + success_score * 0.25
                 + quality * 0.15
                 + bb)

        out.append(SkillScore(
            name=name, score=score,
            relevance=relevance, success=success_score,
            quality=quality, bundle_boost=bb, state=state,
        ))

    out.sort(key=lambda s: s.score, reverse=True)
    return out


def recommend_skills(task: str,
                     *,
                     top_k: int = 8,
                     min_score: float = 0.05) -> Dict[str, Any]:
    ranked = rank_skills(task)
    bundle = detect_bundle(task)
    selected = [r for r in ranked if r.score >= min_score][:top_k]
    return {
        "task": task,
        "bundle": bundle,
        "top": [s.to_dict() for s in selected],
        "considered": len(ranked),
    }


__all__ = [
    "SkillScore",
    "rank_skills",
    "recommend_skills",
    "load_bundles",
    "save_default_bundles",
    "detect_bundle",
    "DEFAULT_BUNDLES",
    "BUNDLES_FILE",
]
