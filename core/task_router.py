"""Task router — picks the most relevant tools/skills for a user task.

Used by Workflow and AgentsTeam modes to **curate** a small subset of the
~190 tools and 1817 skills before invoking the model. Loading the full
catalogue into every prompt is wasteful and slow on constrained hosts.

Strategy is intentionally simple (no embeddings):

1. Tokenise the task into lowercase keywords.
2. For each tool, compute a score = matches across:
   - ``name`` (×3)
   - ``description``
   - ``capabilities`` (×2)
   - ``use_cases``
   - ``category`` (×2)
3. For each skill, score by name match.
4. Return the top-k of each, sorted by score descending.

If a task contains no recognisable keywords, fall back to a small,
balanced default selection across categories.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{3,}|[\u0600-\u06FF]{2,}")
_STOPWORDS = {
    "the", "and", "for", "from", "with", "into", "this", "that",
    "هذا", "هذه", "ذلك", "تلك", "الى", "إلى", "على", "عن", "في",
    "كل", "بعد", "قبل", "أن", "ان",
}


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOPWORDS]


def _score_tool(query_tokens: List[str], manifest: Dict[str, Any]) -> int:
    if not query_tokens:
        return 0
    q = set(query_tokens)
    score = 0
    name = manifest.get("name", "")
    desc = manifest.get("description", "")
    cat = manifest.get("category", "")
    caps: Iterable[str] = manifest.get("capabilities", []) or []
    cases: Iterable[str] = manifest.get("use_cases", []) or []

    name_tokens = set(_tokens(name))
    desc_tokens = set(_tokens(desc))
    cat_tokens = set(_tokens(cat))
    cap_tokens = set(_tokens(" ".join(caps)))
    case_tokens = set(_tokens(" ".join(cases)))

    score += 3 * len(q & name_tokens)
    score += 1 * len(q & desc_tokens)
    score += 2 * len(q & cap_tokens)
    score += 1 * len(q & case_tokens)
    score += 2 * len(q & cat_tokens)
    return score


def select_tools_for_task(
    task: str,
    awareness: Any,
    *,
    k: int = 12,
    must_include: Optional[List[str]] = None,
    registry: Any = None,
) -> List[Dict[str, Any]]:
    """Return up to ``k`` tool manifests most relevant to ``task``.

    When the semantic ``tool_search`` index is available we let it
    pre-rank the candidates and then merge with the keyword scorer to
    keep deterministic results for short queries.
    """
    qtoks = _tokens(task)
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for manifest in awareness.tools.values():
        s = _score_tool(qtoks, manifest)
        if s > 0:
            scored.append((s, manifest))
    scored.sort(key=lambda p: (-p[0], p[1].get("name", "")))
    chosen = [m for _, m in scored[:k]]

    # Semantic boost — if available, blend top-K from the embeddings
    # ranker. We give it a small bonus weight to break keyword ties.
    if registry is not None:
        try:
            from core.tool_search import rank_tools  # type: ignore
            sem = rank_tools(task, k=k, registry=registry, awareness=awareness)
        except Exception:
            sem = []
        present = {m.get("name") for m in chosen}
        for row in sem:
            n = row.get("name")
            if not n or n in present:
                continue
            m = awareness.tools.get(n) if hasattr(awareness, "tools") else None
            if m:
                chosen.append(m)
                present.add(n)
            if len(chosen) >= k * 2:
                break
        chosen = chosen[:k]

    # Always include force-listed tools.
    if must_include:
        present = {m.get("name") for m in chosen}
        for name in must_include:
            if name in present:
                continue
            m = awareness.get_tool(name) if hasattr(awareness, "get_tool") else awareness.tools.get(name)
            if m:
                chosen.append(m)

    # Fallback: if nothing matched, take a balanced cross-category sample.
    if not chosen:
        by_cat: Dict[str, List[Dict[str, Any]]] = {}
        for m in awareness.tools.values():
            by_cat.setdefault(m.get("category", "Execution"), []).append(m)
        for cat, items in sorted(by_cat.items()):
            chosen.append(items[0])
            if len(chosen) >= k:
                break
    return chosen[:max(k, len(must_include or []))]


def select_skills_for_task(
    task: str,
    awareness: Any,
    *,
    k: int = 8,
) -> List[str]:
    """Return up to ``k`` skill names most relevant to ``task``."""
    qtoks = set(_tokens(task))
    if not qtoks:
        return list(awareness.skill_names[:k])
    scored: List[Tuple[int, str]] = []
    for name in awareness.skill_names:
        ntoks = set(_tokens(name))
        s = len(qtoks & ntoks)
        if s > 0:
            scored.append((s, name))
    scored.sort(key=lambda p: (-p[0], p[1]))
    return [n for _, n in scored[:k]]


def filter_registry(registry: Any, tool_names: Iterable[str]) -> Dict[str, Any]:
    """Return a ``{name: callable}`` subset of ``registry.tools`` for the model.

    This is what we pass to ``react_loop`` so the model only sees the curated
    tool set instead of the full ~190 entries.
    """
    full = getattr(registry, "tools", {})
    return {name: full[name] for name in tool_names if name in full}


__all__ = [
    "select_tools_for_task",
    "select_skills_for_task",
    "filter_registry",
]
