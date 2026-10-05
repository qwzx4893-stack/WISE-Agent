"""Multi-strategy executor — pick the best of several paths, fall back on failure.

The agent is sometimes faced with a task that admits more than one
reasonable approach. Rather than committing to the first one the
language model emits, this module:

1.  Asks the model for **N candidate strategies** in a structured form
    (``StrategyChoice`` objects).
2.  Ranks them with a deterministic scorer that combines:
       * the model's own self-reported confidence,
       * a cost estimate (sum of estimated tool calls),
       * `Tracer` history success rate for the tools each strategy
         relies on,
       * a redundancy penalty (avoid picking three near-duplicates).
3.  Executes the top-ranked strategy. If it raises **or** returns a
   structured error, automatically falls back to the next candidate
   and emits a ``strategy.fallback`` Tracer event.

The interface is intentionally small so it can be wired into the
existing :class:`ThinkingEngine` without invasive changes — see
:func:`run_with_fallback` and :class:`StrategyEvaluator`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class StrategyChoice:
    """One candidate plan for solving a task."""

    name: str
    plan: str
    confidence: float = 0.5
    cost_estimate: float = 1.0
    tools: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "plan": self.plan,
            "confidence": round(float(self.confidence), 3),
            "cost_estimate": round(float(self.cost_estimate), 3),
            "tools": list(self.tools),
            "notes": self.notes,
        }


@dataclass
class StrategyResult:
    """Outcome of executing a single strategy."""

    strategy: StrategyChoice
    ok: bool
    output: Any = None
    error: str = ""
    duration_ms: int = 0


@dataclass
class FallbackReport:
    """Summary returned by :func:`run_with_fallback`."""

    chosen: Optional[StrategyChoice]
    attempts: List[StrategyResult]
    final_output: Any = None
    final_ok: bool = False
    fallback_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "attempts": [
                {"strategy": a.strategy.to_dict(), "ok": a.ok,
                 "error": a.error, "duration_ms": a.duration_ms}
                for a in self.attempts
            ],
            "final_ok": self.final_ok,
            "fallback_count": self.fallback_count,
        }


# ---------------------------------------------------------------------------
# Ranker
# ---------------------------------------------------------------------------
def _tracer_history_score(tools: Sequence[str]) -> float:
    """Average success rate across the strategy's tools, in [0, 1]."""
    if not tools:
        return 0.5
    try:
        from core.observability import Tracer  # type: ignore
        events = list(getattr(Tracer, "_events", []) or [])
    except Exception:
        return 0.5
    by_tool: Dict[str, List[bool]] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("name") not in ("tool.call.end",):
            continue
        t = e.get("tool")
        if not isinstance(t, str):
            continue
        ok = (e.get("status") == "ok")
        by_tool.setdefault(t, []).append(ok)
    if not by_tool:
        return 0.5
    rates: List[float] = []
    for t in tools:
        runs = by_tool.get(t)
        if not runs:
            rates.append(0.5)
        else:
            rates.append(sum(1 for v in runs if v) / float(len(runs)))
    return sum(rates) / float(len(rates))


def _redundancy_penalty(c: StrategyChoice,
                         picked: Sequence[StrategyChoice]) -> float:
    """Penalise candidates whose tool set duplicates already-picked ones.

    Returns a multiplier in [0.6, 1.0]; identical tool sets hit 0.6.
    """
    if not picked:
        return 1.0
    cur = set(c.tools)
    if not cur:
        return 1.0
    overlap = 0.0
    for p in picked:
        prev = set(p.tools)
        if not prev:
            continue
        union = cur | prev
        inter = cur & prev
        overlap = max(overlap, len(inter) / max(1, len(union)))
    return max(0.6, 1.0 - 0.4 * overlap)


def rank_strategies(candidates: Sequence[StrategyChoice]) -> List[StrategyChoice]:
    """Stable sort by aggregate score, descending."""
    out: List[StrategyChoice] = []
    scored: List[Dict[str, Any]] = []
    for c in candidates:
        history = _tracer_history_score(c.tools)
        cost_norm = 1.0 / (1.0 + max(0.0, float(c.cost_estimate)))
        red = _redundancy_penalty(c, out)
        score = (
            0.45 * float(c.confidence)
            + 0.30 * history
            + 0.15 * cost_norm
            + 0.10 * red
        )
        scored.append({"choice": c, "score": score})
        out.append(c)  # informs redundancy penalty for later items

    scored.sort(key=lambda r: r["score"], reverse=True)
    return [r["choice"] for r in scored]


# ---------------------------------------------------------------------------
# Candidate proposer (LLM-driven, with regex fallback)
# ---------------------------------------------------------------------------
_PROPOSAL_PROMPT = """You are a planner. The user has asked:

{task}

Propose {n} alternative ways to accomplish this. Output a single JSON
array, no prose. Each element must have:
  name (short label),
  plan (one paragraph),
  confidence (0..1),
  cost_estimate (relative cost, 1 = simple),
  tools (list of tool names you'd call),
  notes (optional).

Return ONLY the JSON array.
"""


def _coerce_choice(raw: Dict[str, Any]) -> Optional[StrategyChoice]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip() or "strategy"
    plan = str(raw.get("plan") or "").strip()
    if not plan:
        return None
    try:
        conf = float(raw.get("confidence", 0.5))
    except Exception:
        conf = 0.5
    try:
        cost = float(raw.get("cost_estimate", 1.0))
    except Exception:
        cost = 1.0
    tools = raw.get("tools") or []
    if not isinstance(tools, list):
        tools = []
    return StrategyChoice(
        name=name, plan=plan,
        confidence=max(0.0, min(1.0, conf)),
        cost_estimate=max(0.0, cost),
        tools=[str(t) for t in tools],
        notes=str(raw.get("notes") or ""),
    )


def _parse_json_array(text: str) -> List[Dict[str, Any]]:
    """Extract the first JSON array we can find inside ``text``."""
    if not text:
        return []
    m = re.search(r"\[\s*\{.*?\}\s*\]", text, re.DOTALL)
    blob = m.group(0) if m else text
    try:
        data = json.loads(blob)
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Public evaluator
# ---------------------------------------------------------------------------
class StrategyEvaluator:
    """Propose -> rank -> execute helper.

    The class deliberately knows nothing about the LLM's API surface;
    callers pass in a ``proposer`` callable that, given a task and a
    requested count, returns either a list of ``StrategyChoice`` or a
    raw model string that we'll parse. This keeps the unit tests
    deterministic and lets production callers wire whatever model
    they like.
    """

    def __init__(self,
                 *,
                 proposer: Optional[Callable[[str, int], Any]] = None,
                 default_n: int = 3) -> None:
        self.proposer = proposer
        self.default_n = max(1, int(default_n))

    def propose(self, task: str, n: Optional[int] = None) -> List[StrategyChoice]:
        if not isinstance(task, str) or not task.strip():
            return []
        count = self.default_n if n is None else max(1, int(n))
        if self.proposer is None:
            # Last-resort placeholder — produces one obvious strategy
            # and one fallback so the executor still has something to
            # rank when no model is wired in.
            return [
                StrategyChoice(name="direct",
                                plan=f"Answer '{task}' directly with the "
                                "most relevant single tool.",
                                confidence=0.6, cost_estimate=1.0),
                StrategyChoice(name="search-then-answer",
                                plan=f"Search the knowledge sources for "
                                f"'{task}' first, then answer.",
                                confidence=0.4, cost_estimate=2.0,
                                tools=["search"]),
            ][:count]

        raw = self.proposer(task, count)
        if isinstance(raw, list):
            items: List[StrategyChoice] = []
            for entry in raw:
                if isinstance(entry, StrategyChoice):
                    items.append(entry)
                else:
                    coerced = _coerce_choice(entry if isinstance(entry, dict)
                                              else {"plan": str(entry)})
                    if coerced:
                        items.append(coerced)
            return items[:count]
        if isinstance(raw, str):
            parsed = _parse_json_array(raw)
            return [c for c in (_coerce_choice(d) for d in parsed) if c][:count]
        return []

    def rank(self, candidates: Sequence[StrategyChoice]) -> List[StrategyChoice]:
        return rank_strategies(candidates)


# ---------------------------------------------------------------------------
# Fallback executor
# ---------------------------------------------------------------------------
def run_with_fallback(
        candidates: Sequence[StrategyChoice],
        executor: Callable[[StrategyChoice], Any],
        *,
        is_failure: Optional[Callable[[Any], bool]] = None,
        max_attempts: Optional[int] = None) -> FallbackReport:
    """Run ranked candidates one after another until one succeeds.

    ``executor`` is invoked with each candidate; whatever it returns is
    fed to ``is_failure``. The default failure detector treats any
    raised exception or any value with ``ok=False`` / ``ok is False``
    as a failure and falls back. ``max_attempts`` limits the walk
    (default = len(candidates)).
    """
    ranked = rank_strategies(candidates)
    if not ranked:
        return FallbackReport(chosen=None, attempts=[], final_ok=False)

    cap = max_attempts if max_attempts is not None else len(ranked)
    cap = max(1, min(int(cap), len(ranked)))

    def _default_failure(out: Any) -> bool:
        if isinstance(out, dict):
            ok = out.get("ok")
            if ok is False:
                return True
        if isinstance(out, str):
            try:
                parsed = json.loads(out)
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    return True
            except Exception:
                pass
        return False

    detector = is_failure or _default_failure
    attempts: List[StrategyResult] = []
    final_output: Any = None
    chosen: Optional[StrategyChoice] = None

    for i in range(cap):
        c = ranked[i]
        t0 = time.time()
        try:
            out = executor(c)
            duration = int((time.time() - t0) * 1000)
            failed = detector(out)
            attempts.append(StrategyResult(
                strategy=c, ok=not failed, output=out,
                duration_ms=duration,
                error="" if not failed else "failure detected"))
            if not failed:
                chosen = c
                final_output = out
                break
        except Exception as exc:
            duration = int((time.time() - t0) * 1000)
            attempts.append(StrategyResult(
                strategy=c, ok=False, output=None,
                duration_ms=duration, error=f"{type(exc).__name__}: {exc}"))
            try:
                from core.observability import Tracer  # type: ignore
                Tracer.emit("strategy.fallback",
                             from_strategy=c.name,
                             reason=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass

    return FallbackReport(
        chosen=chosen,
        attempts=attempts,
        final_output=final_output,
        final_ok=chosen is not None,
        fallback_count=max(0, len(attempts) - 1),
    )


__all__ = [
    "StrategyChoice",
    "StrategyResult",
    "FallbackReport",
    "StrategyEvaluator",
    "rank_strategies",
    "run_with_fallback",
]
