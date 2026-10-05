"""Aggregate ``tool.call`` events into usage statistics.

The Tracer ring buffer holds the most recent ``tool.call.start`` /
``tool.call.end`` events. We build counts/timings on demand for the
``GET /admin/tools/stats`` endpoint.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from .observability import Tracer


def compute_tool_stats(events: Optional[Iterable[Dict[str, Any]]] = None,
                       *,
                       limit: int = 2048) -> Dict[str, Any]:
    """Roll up tool.call events into per-tool aggregates.

    Returns a dict with keys: ``top_tools`` (ordered list), ``per_tool``
    (mapping name -> stats), ``totals`` (overall counts), and
    ``total_events`` (sample size).
    """
    if events is None:
        events = Tracer.events(kind="tool.call", limit=limit)
    events = list(events)
    starts: Dict[str, int] = defaultdict(int)
    oks: Dict[str, int] = defaultdict(int)
    errs: Dict[str, int] = defaultdict(int)
    durations: Dict[str, List[float]] = defaultdict(list)
    last_error: Dict[str, str] = {}
    for ev in events:
        tool = ev.get("tool")
        if not tool:
            continue
        kind = ev.get("kind", "")
        if kind == "tool.call.start":
            starts[tool] += 1
        elif kind == "tool.call.end":
            if ev.get("status") == "ok":
                oks[tool] += 1
            else:
                errs[tool] += 1
                if ev.get("error"):
                    last_error[tool] = str(ev.get("error"))[:240]
            d = ev.get("duration_ms")
            if isinstance(d, (int, float)):
                durations[tool].append(float(d))
    per_tool: Dict[str, Dict[str, Any]] = {}
    for tool in set(list(starts) + list(oks) + list(errs)):
        ok = oks.get(tool, 0)
        err = errs.get(tool, 0)
        total = ok + err
        ds = durations.get(tool, [])
        avg_ms = (sum(ds) / len(ds)) if ds else 0.0
        max_ms = max(ds) if ds else 0.0
        per_tool[tool] = {
            "calls": total or starts.get(tool, 0),
            "ok": ok,
            "error": err,
            "success_rate": round((ok / total) if total else 0.0, 4),
            "error_rate": round((err / total) if total else 0.0, 4),
            "avg_ms": round(avg_ms, 2),
            "max_ms": round(max_ms, 2),
            "last_error": last_error.get(tool, ""),
        }
    top = sorted(per_tool.items(),
                 key=lambda kv: kv[1]["calls"], reverse=True)
    totals = {
        "calls": sum(s["calls"] for s in per_tool.values()),
        "ok": sum(s["ok"] for s in per_tool.values()),
        "error": sum(s["error"] for s in per_tool.values()),
    }
    if totals["calls"]:
        totals["success_rate"] = round(
            totals["ok"] / totals["calls"], 4)
        totals["error_rate"] = round(
            totals["error"] / totals["calls"], 4)
    else:
        totals["success_rate"] = 0.0
        totals["error_rate"] = 0.0
    return {
        "totals": totals,
        "top_tools": [
            {"name": n, **s} for n, s in top[:25]
        ],
        "per_tool": per_tool,
        "total_events": len(events),
    }


__all__ = ["compute_tool_stats"]
