"""Tool ranking for task-aware selection.

The ranker scores every available tool against a free-text task
description by combining four signals:

1. **Capability overlap** — Jaccard similarity on tokenised text from
   the tool (``description`` + ``capabilities`` + ``use_cases`` +
   ``category``) against the task tokens.
2. **Success history** — fraction of past ``tool.call`` Tracer events
   that ended with ``status="ok"`` for this tool. Tools without
   history get a neutral 0.5 prior.
3. **Native > MCP** — MCP-bridged tools get a 0.92× multiplier so
   that, all else equal, native tools win. MCP still ranks above
   irrelevant native tools.
4. **Category match** — if the task tokens mention the tool's
   category (case-insensitive substring), score gets a 1.3× boost.

Public API:
    rank_tools_for_task(task: str, tools: dict, *, history=None,
                        banned=None) -> list[ScoredTool]
    select_top_tools(task, tools, k=15) -> list[ScoredTool]

``tools`` is the mapping consumed by :class:`SystemAwareness` —
``{name: manifest_dict}``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set


# --------------------------------------------------------------------
# Tokeniser (simple, deterministic, language-agnostic enough)
# --------------------------------------------------------------------
_WORD_RE = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)
# Stopwords kept tiny: only fillers that hurt Jaccard.
_STOP = frozenset({
    "the", "a", "an", "to", "of", "for", "and", "or", "in", "on",
    "at", "with", "by", "is", "are", "be", "this", "that", "it",
    "do", "use", "using", "into", "from",
    # arabic minimal stoplist
    "في", "من", "على", "إلى", "هذا", "هذه", "أو", "و",
})


def tokenize(text: str) -> Set[str]:
    if not text:
        return set()
    out: Set[str] = set()
    for m in _WORD_RE.finditer(text.lower()):
        w = m.group(0)
        if len(w) <= 1 or w in _STOP:
            continue
        out.add(w)
    return out


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


# --------------------------------------------------------------------
# Result
# --------------------------------------------------------------------
@dataclass
class ScoredTool:
    name: str
    score: float
    capability_overlap: float = 0.0
    success_rate: float = 0.5
    is_mcp: bool = False
    category_match: bool = False
    is_banned: bool = False
    manifest: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "capability_overlap": round(self.capability_overlap, 4),
            "success_rate": round(self.success_rate, 4),
            "is_mcp": self.is_mcp,
            "category_match": self.category_match,
            "is_banned": self.is_banned,
        }


# --------------------------------------------------------------------
# History summarisation
# --------------------------------------------------------------------
def summarise_history(events: Iterable[Dict[str, Any]]) -> Dict[
        str, Dict[str, int]]:
    """Compress ``tool.call.end`` events into ``{tool: {ok, err}}``."""
    out: Dict[str, Dict[str, int]] = {}
    for ev in events:
        if ev.get("kind") != "tool.call.end":
            continue
        tool = ev.get("tool")
        if not tool:
            continue
        bucket = out.setdefault(tool, {"ok": 0, "err": 0})
        if ev.get("status") == "ok":
            bucket["ok"] += 1
        else:
            bucket["err"] += 1
    return out


def _success_rate(stats: Optional[Dict[str, int]]) -> float:
    if not stats:
        return 0.5
    total = stats.get("ok", 0) + stats.get("err", 0)
    if total == 0:
        return 0.5
    # Mild Laplace smoothing toward 0.5 for low-count tools.
    smooth = 2
    return (stats["ok"] + smooth * 0.5) / (total + smooth)


# --------------------------------------------------------------------
# Manifest tokenisation
# --------------------------------------------------------------------
def _manifest_tokens(m: Dict[str, Any]) -> Set[str]:
    parts: List[str] = [str(m.get("description", ""))]
    cap = m.get("capabilities") or []
    if isinstance(cap, list):
        parts.extend(str(x) for x in cap)
    uc = m.get("use_cases") or []
    if isinstance(uc, list):
        parts.extend(str(x) for x in uc)
    parts.append(str(m.get("category", "")))
    parts.append(str(m.get("name", "")))
    return tokenize(" ".join(parts))


def _is_mcp(name: str, manifest: Dict[str, Any]) -> bool:
    if name.startswith("mcp_"):
        return True
    return bool(manifest.get("__mcp__"))


# --------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------
MCP_PENALTY = 0.92
CATEGORY_BOOST = 1.3
HISTORY_WEIGHT = 0.4   # how much history pulls the score
OVERLAP_WEIGHT = 0.6   # how much capability-overlap pulls the score


def score_tool(name: str,
               manifest: Dict[str, Any],
               task_tokens: Set[str],
               *,
               history_summary: Optional[Dict[str, Dict[str, int]]] = None,
               banned: Optional[Set[str]] = None) -> ScoredTool:
    """Score a single tool against ``task_tokens``."""
    tool_tokens = _manifest_tokens(manifest)
    overlap = jaccard(task_tokens, tool_tokens)
    success = _success_rate(
        (history_summary or {}).get(name))
    is_mcp = _is_mcp(name, manifest)
    cat = str(manifest.get("category", "")).lower()
    cat_match = bool(cat and cat in {t.lower() for t in task_tokens})
    base = OVERLAP_WEIGHT * overlap + HISTORY_WEIGHT * success
    if is_mcp:
        base *= MCP_PENALTY
    if cat_match:
        base *= CATEGORY_BOOST
    is_banned = bool(banned and name in banned)
    if is_banned:
        base = 0.0
    return ScoredTool(
        name=name, score=base,
        capability_overlap=overlap, success_rate=success,
        is_mcp=is_mcp, category_match=cat_match,
        is_banned=is_banned, manifest=manifest,
    )


def rank_tools_for_task(task: str,
                        tools: Dict[str, Dict[str, Any]],
                        *,
                        history: Optional[Iterable[Dict[str, Any]]] = None,
                        banned: Optional[Iterable[str]] = None,
                        ) -> List[ScoredTool]:
    """Rank ``tools`` against ``task``, descending score."""
    task_tokens = tokenize(task)
    history_summary = summarise_history(history or [])
    banned_set: Set[str] = set(banned or ())
    scored = [
        score_tool(n, m, task_tokens,
                   history_summary=history_summary,
                   banned=banned_set)
        for n, m in tools.items()
    ]
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def select_top_tools(task: str,
                     tools: Dict[str, Dict[str, Any]],
                     k: int = 15,
                     *,
                     history: Optional[Iterable[Dict[str, Any]]] = None,
                     banned: Optional[Iterable[str]] = None,
                     ) -> List[ScoredTool]:
    return rank_tools_for_task(
        task, tools, history=history, banned=banned)[:max(0, k)]


__all__ = [
    "ScoredTool", "tokenize", "jaccard",
    "rank_tools_for_task", "select_top_tools",
    "score_tool", "summarise_history",
    "MCP_PENALTY", "CATEGORY_BOOST",
]
