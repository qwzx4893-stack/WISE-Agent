"""Cron-style scheduler for autonomous agent execution.

Inspired by OpenFang's open-source Agent OS scheduler
(https://github.com/RightNow-AI/openfang — Apache-2.0 / MIT). All code
in this module is **original** Python; we adopt only the high-level
ideas of (a) schedules persisted to disk, (b) one process driving
many schedules via a single background thread, (c) overlap prevention
per schedule, and (d) configuration write-back so dashboard edits
survive daemon restarts.

Persistence
-----------

Schedules are written to ``CONFIG_DIR/schedules.json`` so they
survive process restarts. Each schedule is identified by a UUID
returned to the caller. Run history (last 50 runs per schedule) is
kept in-memory by default and surfaced via :meth:`Scheduler.history`.

Cron grammar
------------

We accept the standard 5-field cron expression: ``minute hour day
month weekday``. Each field supports:

* ``*``           — any value
* exact integers  — e.g. ``5``
* ``*/N`` step    — e.g. ``*/15``
* comma lists     — e.g. ``0,15,30,45``
* ranges          — e.g. ``9-17``

Timezone defaults to UTC. Pass an explicit ``tz`` (IANA name) per
schedule to override.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("agent_os.scheduler")


# ---------------------------------------------------------------------------
# Cron parsing
# ---------------------------------------------------------------------------
class CronExpression:
    """Minimal cron parser/matcher.

    Supports ``*``, integers, comma lists, ranges, and ``*/N`` steps in
    each of the five standard fields. Validation is strict: any
    unsupported syntax raises :class:`ValueError`.
    """

    FIELDS = (("minute", 0, 59),
               ("hour", 0, 23),
               ("dom", 1, 31),
               ("month", 1, 12),
               ("dow", 0, 6))

    def __init__(self, expr: str) -> None:
        self.expr = expr.strip()
        parts = self.expr.split()
        if len(parts) != 5:
            raise ValueError(
                f"cron expression must have 5 fields, got {len(parts)}: "
                f"{self.expr!r}")
        self.matchers: List[set[int]] = []
        for raw, (_, lo, hi) in zip(parts, self.FIELDS):
            self.matchers.append(self._parse_field(raw, lo, hi))

    @staticmethod
    def _parse_field(raw: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                raise ValueError(f"empty cron chunk in {raw!r}")
            # */N step
            step = 1
            base = chunk
            if "/" in chunk:
                base, step_s = chunk.split("/", 1)
                step = int(step_s)
                if step <= 0:
                    raise ValueError(f"non-positive step {step} in {raw!r}")
            # Range or wildcard or exact
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                a, b = base.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = end = int(base)
            if start < lo or end > hi or start > end:
                raise ValueError(
                    f"cron field {raw!r} out of bounds {lo}-{hi}")
            for v in range(start, end + 1, step):
                out.add(v)
        return out

    def matches(self, dt: datetime) -> bool:
        return (dt.minute in self.matchers[0]
                and dt.hour in self.matchers[1]
                and dt.day in self.matchers[2]
                and dt.month in self.matchers[3]
                # Python: Monday=0; cron: Sunday=0. Translate.
                and ((dt.weekday() + 1) % 7) in self.matchers[4])

    def next_after(self, after: datetime, *,
                    horizon_minutes: int = 60 * 24 * 366
                    ) -> Optional[datetime]:
        """Return the first matching minute strictly after ``after``."""
        # Round up to the next whole minute.
        candidate = (after + timedelta(minutes=1)).replace(
            second=0, microsecond=0)
        for _ in range(horizon_minutes):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return None


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------
@dataclass
class Schedule:
    id: str
    name: str
    cron: str
    target: str                       # opaque identifier for the runner
    payload: Dict[str, Any] = field(default_factory=dict)
    tz: str = "UTC"
    enabled: bool = True
    last_run_at: Optional[float] = None
    next_run_at: Optional[float] = None
    last_status: str = ""             # "" | queued | running | success | failed | skipped
    last_error: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScheduleRun:
    schedule_id: str
    started_at: float
    finished_at: Optional[float] = None
    status: str = "running"           # running | success | failed
    duration_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
RunFn = Callable[[Schedule], None]


class Scheduler:
    """Thread-safe singleton scheduler.

    Operators register a ``RunFn`` callback once per process; the
    scheduler invokes it (in a separate thread per run) whenever a
    schedule's cron expression fires. The callback should be fast or
    spawn its own background work; long-running callbacks block the
    next firing for that schedule (overlap prevention).
    """

    _instance: Optional["Scheduler"] = None
    _lock = threading.RLock()

    def __new__(cls) -> "Scheduler":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()  # type: ignore[attr-defined]
            return cls._instance

    def _init(self) -> None:
        from .paths import CONFIG_DIR
        self.path: Path = CONFIG_DIR / "schedules.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schedules: Dict[str, Schedule] = {}
        self._compiled: Dict[str, CronExpression] = {}
        self._history: Dict[str, List[ScheduleRun]] = {}
        self._running: set[str] = set()
        # Scheduled work is intentionally bounded.  A personal assistant must
        # not turn a minute boundary with many reminders into an unbounded
        # burst of model, voice, or browser work.
        self._pending = deque()
        self._pending_ids: set[str] = set()
        self._pending_keys: set[str] = set()
        self._active_keys: set[str] = set()
        self._active_run_count = 0
        self._max_concurrent_runs = 2
        self._runner: Optional[RunFn] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._mutex = threading.RLock()
        self._load()
        self._recompute_next_runs()

    def _ensure_dispatch_state(self) -> None:
        """Backfill queue fields for old singleton/test scheduler instances."""
        if not hasattr(self, "_pending"):
            self._pending = deque()
        if not hasattr(self, "_pending_ids"):
            self._pending_ids = set()
        if not hasattr(self, "_pending_keys"):
            self._pending_keys = set()
        if not hasattr(self, "_active_keys"):
            self._active_keys = set()
        if not hasattr(self, "_active_run_count"):
            self._active_run_count = 0
        if not hasattr(self, "_max_concurrent_runs"):
            self._max_concurrent_runs = 2

    @staticmethod
    def _coalesce_key(schedule: Schedule) -> str:
        """Stable identity for equivalent scheduled intents.

        Separate schedules with different payloads remain independent; exact
        duplicate triggers are coalesced while one is queued or executing.
        """
        return json.dumps(
            {"target": schedule.target, "payload": schedule.payload},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    # ------------------------------------------------------------------ IO
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            LOG.exception("failed to load schedules.json — starting empty")
            return
        for raw in data.get("schedules", []):
            try:
                s = Schedule(**raw)
                self._schedules[s.id] = s
                self._compiled[s.id] = CronExpression(s.cron)
            except Exception:
                LOG.exception("skipping invalid schedule %r", raw)

    def _save(self) -> None:
        with self._mutex:
            payload = {"version": 1,
                        "schedules": [s.to_dict()
                                      for s in self._schedules.values()]}
        # The runtime tree can be cleaned or remounted while this singleton is
        # alive (notably in test isolation and recovery).  Recreate its parent
        # before staging the atomic replacement instead of crashing a schedule.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        tmp.write_text(content, encoding="utf-8")
        
        # Robust replace for Windows / OneDrive filesystem indexer
        for attempt in range(5):
            try:
                tmp.replace(self.path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
            except Exception:
                break
        try:
            self.path.write_text(content, encoding="utf-8")
            tmp.unlink(missing_ok=True)
        except Exception as e:
            LOG.error("Failed to save schedules to %s: %s", self.path, e)

    def _recompute_next_runs(self) -> None:
        now = datetime.now(timezone.utc)
        for s in self._schedules.values():
            cron = self._compiled.get(s.id)
            if cron is None:
                continue
            nxt = cron.next_after(now)
            s.next_run_at = nxt.timestamp() if nxt else None

    # --------------------------------------------------------- registration
    def set_runner(self, fn: RunFn) -> None:
        """Install the run callback. Must be called before :meth:`start`."""
        with self._mutex:
            self._runner = fn

    # ------------------------------------------------------------------ CRUD
    def list(self) -> List[Schedule]:
        with self._mutex:
            return list(self._schedules.values())

    def get(self, sid: str) -> Optional[Schedule]:
        with self._mutex:
            return self._schedules.get(sid)

    def add(self, *, name: str, cron: str, target: str,
            payload: Optional[Dict[str, Any]] = None,
            tz: str = "UTC", enabled: bool = True) -> Schedule:
        # Validate cron eagerly so invalid expressions never persist.
        compiled = CronExpression(cron)
        sid = uuid.uuid4().hex[:12]
        s = Schedule(id=sid, name=name, cron=cron, target=target,
                      payload=payload or {}, tz=tz, enabled=enabled)
        with self._mutex:
            self._schedules[sid] = s
            self._compiled[sid] = compiled
            nxt = compiled.next_after(datetime.now(timezone.utc))
            s.next_run_at = nxt.timestamp() if nxt else None
        self._save()
        return s

    def update(self, sid: str, **fields: Any) -> Optional[Schedule]:
        with self._mutex:
            s = self._schedules.get(sid)
            if s is None:
                return None
            if "cron" in fields and fields["cron"] != s.cron:
                self._compiled[sid] = CronExpression(fields["cron"])
            for k, v in fields.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            cron = self._compiled[sid]
            nxt = cron.next_after(datetime.now(timezone.utc))
            s.next_run_at = nxt.timestamp() if nxt else None
        self._save()
        return s

    def delete(self, sid: str) -> bool:
        with self._mutex:
            if sid not in self._schedules:
                return False
            self._schedules.pop(sid)
            self._compiled.pop(sid, None)
            self._history.pop(sid, None)
        self._save()
        return True

    def history(self, sid: str) -> List[ScheduleRun]:
        with self._mutex:
            return list(self._history.get(sid, []))

    # ------------------------------------------------------------------ run
    def fire_now(self, sid: str) -> Optional[ScheduleRun]:
        """Run a schedule immediately (out of band). Useful for tests + UI."""
        with self._mutex:
            s = self._schedules.get(sid)
            runner = self._runner
        if s is None or runner is None:
            return None
        return self._invoke(s, runner)

    def _invoke(self, s: Schedule, runner: RunFn, *, already_claimed: bool = False) -> ScheduleRun:
        with self._mutex:
            self._ensure_dispatch_state()
            if not already_claimed:
                if s.id in self._running:
                    run = ScheduleRun(schedule_id=s.id,
                                      started_at=time.time(),
                                      finished_at=time.time(),
                                      status="failed", duration_ms=0.0,
                                      error="overlap with previous run")
                    self._record_run(s, run)
                    return run
                self._running.add(s.id)
        run = ScheduleRun(schedule_id=s.id, started_at=time.time())
        s.last_status = "running"
        try:
            runner(s)
            run.status = "success"
            s.last_status = "success"
            s.last_error = ""
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            s.last_status = "failed"
            s.last_error = run.error
        finally:
            run.finished_at = time.time()
            run.duration_ms = (run.finished_at - run.started_at) * 1000.0
            s.last_run_at = run.finished_at
            with self._mutex:
                self._running.discard(s.id)
            cron = self._compiled.get(s.id)
            if cron is not None:
                nxt = cron.next_after(datetime.now(timezone.utc))
                s.next_run_at = nxt.timestamp() if nxt else None
            self._record_run(s, run)
            self._save()
        return run

    def _record_run(self, s: Schedule, run: ScheduleRun) -> None:
        with self._mutex:
            hist = self._history.setdefault(s.id, [])
            hist.append(run)
            # Keep last 50 runs.
            if len(hist) > 50:
                self._history[s.id] = hist[-50:]

    def _enqueue_scheduled(self, s: Schedule, runner: RunFn) -> None:
        """Queue a cron trigger with bounded concurrency and duplicate coalescing."""
        with self._mutex:
            self._ensure_dispatch_state()
            if self._stop_event.is_set():
                return
            key = self._coalesce_key(s)
            if s.id in self._running or s.id in self._pending_ids:
                LOG.debug("Skipping duplicate trigger for already scheduled %s", s.id)
                return
            if key in self._active_keys or key in self._pending_keys:
                s.last_status = "skipped"
                s.last_error = "coalesced with an equivalent scheduled run"
                LOG.info("Coalesced equivalent scheduled trigger '%s'.", s.name)
                return
            self._pending.append((s, runner, key))
            self._pending_ids.add(s.id)
            self._pending_keys.add(key)
            s.last_status = "queued"
            self._drain_scheduled_queue_locked()

    def _drain_scheduled_queue_locked(self) -> None:
        """Start queued schedule runs until the global execution budget is full."""
        self._ensure_dispatch_state()
        if self._stop_event.is_set():
            self._pending.clear()
            self._pending_ids.clear()
            self._pending_keys.clear()
            return
        while self._pending and self._active_run_count < self._max_concurrent_runs:
            s, runner, key = self._pending.popleft()
            self._pending_ids.discard(s.id)
            self._pending_keys.discard(key)
            self._running.add(s.id)
            self._active_keys.add(key)
            self._active_run_count += 1
            threading.Thread(
                target=self._run_queued_schedule,
                args=(s, runner, key),
                name=f"WISE_Schedule_{s.id}",
                daemon=True,
            ).start()

    def _run_queued_schedule(self, s: Schedule, runner: RunFn, key: str) -> None:
        try:
            self._invoke(s, runner, already_claimed=True)
        finally:
            with self._mutex:
                self._active_run_count = max(0, self._active_run_count - 1)
                self._active_keys.discard(key)
                self._drain_scheduled_queue_locked()

    def runtime_status(self) -> Dict[str, int]:
        """Return non-sensitive scheduler pressure metrics for health surfaces."""
        with self._mutex:
            self._ensure_dispatch_state()
            return {
                "active_runs": self._active_run_count,
                "queued_runs": len(self._pending),
                "max_concurrent_runs": self._max_concurrent_runs,
            }

    # ------------------------------------------------------------------ loop
    def start(self, *, tick_seconds: float = 30.0) -> None:
        with self._mutex:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, args=(tick_seconds,),
                name="agent-os-scheduler", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        with self._mutex:
            self._thread = None
            self._ensure_dispatch_state()
            self._pending.clear()
            self._pending_ids.clear()
            self._pending_keys.clear()

    def _loop(self, tick_seconds: float) -> None:
        LOG.info("scheduler started (tick=%.1fs)", tick_seconds)
        last_minute: Optional[str] = None
        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc).replace(second=0,
                                                       microsecond=0)
            stamp = now.isoformat()
            if stamp != last_minute:
                last_minute = stamp
                self._tick(now)
            self._stop_event.wait(tick_seconds)
        LOG.info("scheduler stopped")

    def _tick(self, now: datetime) -> None:
        with self._mutex:
            triggers = [s for s in self._schedules.values()
                         if s.enabled
                         and s.id in self._compiled
                         and self._compiled[s.id].matches(now)]
            runner = self._runner
        if runner is None or not triggers:
            return
        for s in triggers:
            self._enqueue_scheduled(s, runner)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------
_SCHEDULER_INSTANCE: Optional[Scheduler] = None
_SCHED_LOCK = threading.Lock()


def get_scheduler() -> Scheduler:
    global _SCHEDULER_INSTANCE
    if _SCHEDULER_INSTANCE is None:
        with _SCHED_LOCK:
            if _SCHEDULER_INSTANCE is None:
                _SCHEDULER_INSTANCE = Scheduler()
    return _SCHEDULER_INSTANCE



__all__ = [
    "Scheduler", "Schedule", "ScheduleRun", "CronExpression",
    "get_scheduler",
]
