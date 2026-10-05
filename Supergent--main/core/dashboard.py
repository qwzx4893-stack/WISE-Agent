"""Real-time dashboard data — activity, summary, stats.

Inspired by OpenFang's dashboard (https://github.com/RightNow-AI/openfang
— Apache-2.0 / MIT). Original Python implementation. We expose a tiny
``ActivityLog`` plus a few aggregation helpers; the JSON shapes below
are consumed by both the FastAPI endpoints and the desktop dashboard
screen.

Design notes
------------

* ``ActivityLog`` is in-memory by default (per-process, capacity 200
  events). Operators that need durable history can persist via
  schedules + skill lifecycle stores; we deliberately avoid a third
  on-disk log.
* ``record_event`` is thread-safe so background workers, scheduler
  fires, and chat handlers can all push without coordination.
* Stats blend three sources: in-process activity log, scheduler
  history (last_status / last_run_at), and skills lifecycle counters
  if available. Unavailable sources degrade silently (return zeros).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Event:
    timestamp: float
    kind: str                          # chat | tool | schedule | self_heal | system
    actor: str                         # agent name / "system" / schedule id
    summary: str
    status: str = "info"               # info | success | failed | warning
    duration_ms: Optional[float] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ActivityLog:
    """Bounded in-memory activity log."""

    _instance: Optional["ActivityLog"] = None
    _lock = threading.RLock()

    def __new__(cls) -> "ActivityLog":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()  # type: ignore[attr-defined]
            return cls._instance

    def _init(self) -> None:
        self._events: deque[Event] = deque(maxlen=200)
        self._mutex = threading.RLock()

    def record(self, kind: str, *, actor: str, summary: str,
                status: str = "info",
                duration_ms: Optional[float] = None,
                payload: Optional[Dict[str, Any]] = None) -> Event:
        e = Event(timestamp=time.time(), kind=kind, actor=actor,
                   summary=summary, status=status,
                   duration_ms=duration_ms, payload=payload or {})
        with self._mutex:
            self._events.append(e)
        return e

    def recent(self, limit: int = 50) -> List[Event]:
        with self._mutex:
            n = max(1, min(int(limit), self._events.maxlen or 200))
            return list(self._events)[-n:][::-1]

    def clear(self) -> None:
        with self._mutex:
            self._events.clear()


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
def _scheduler_stats() -> Dict[str, Any]:
    try:
        from .scheduler import get_scheduler
        sch = get_scheduler()
        items = sch.list()
        total = len(items)
        enabled = sum(1 for s in items if s.enabled)
        success = sum(1 for s in items if s.last_status == "success")
        failed = sum(1 for s in items if s.last_status == "failed")
        next_runs = sorted([s.next_run_at for s in items
                              if s.enabled and s.next_run_at])
        next_run = next_runs[0] if next_runs else None
        return {
            "schedules_total": total,
            "schedules_enabled": enabled,
            "schedules_success": success,
            "schedules_failed": failed,
            "next_run_at": next_run,
            **sch.runtime_status(),
        }
    except Exception:
        return {"schedules_total": 0, "schedules_enabled": 0,
                 "schedules_success": 0, "schedules_failed": 0,
                 "next_run_at": None, "active_runs": 0,
                 "queued_runs": 0, "max_concurrent_runs": 0}


def _skills_stats() -> Dict[str, Any]:
    try:
        from .skills.lifecycle import get_manager
        mgr = get_manager()
        snap = mgr.snapshot() if hasattr(mgr, "snapshot") else {}
        return {
            "skills_total": int(snap.get("total", 0) or 0),
            "skills_failing": int(snap.get("failing", 0) or 0),
            "skills_healthy": int(snap.get("healthy", 0) or 0),
        }
    except Exception:
        return {"skills_total": 0, "skills_failing": 0,
                 "skills_healthy": 0}


def summary() -> Dict[str, Any]:
    """Return the dashboard summary card payload."""
    log = ActivityLog()
    recent = log.recent(limit=20)
    last_chat = next((e for e in recent if e.kind == "chat"), None)
    last_failure = next((e for e in recent
                          if e.status == "failed"), None)
    return {
        "last_event_at": recent[0].timestamp if recent else None,
        "last_chat": last_chat.to_dict() if last_chat else None,
        "last_failure": last_failure.to_dict() if last_failure else None,
        "scheduler": _scheduler_stats(),
        "skills": _skills_stats(),
    }


def activity(limit: int = 50) -> Dict[str, Any]:
    log = ActivityLog()
    return {"events": [e.to_dict() for e in log.recent(limit=limit)]}


def stats() -> Dict[str, Any]:
    log = ActivityLog()
    events = log.recent(limit=200)
    by_kind: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    for e in events:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        by_status[e.status] = by_status.get(e.status, 0) + 1
    return {
        "events_total": len(events),
        "by_kind": by_kind,
        "by_status": by_status,
        "scheduler": _scheduler_stats(),
        "skills": _skills_stats(),
    }


__all__ = ["Event", "ActivityLog", "summary", "activity", "stats"]
