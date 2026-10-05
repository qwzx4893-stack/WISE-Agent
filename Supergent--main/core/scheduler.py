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
persisted with the schedules and surfaced via :meth:`Scheduler.history`.
Missed runs are skipped, and interrupted runs require review; neither is
automatically replayed after a restart.

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
import copy
import math
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

LOG = logging.getLogger("agent_os.scheduler")


class SchedulePersistenceError(OSError):
    """Schedule state was not durably acknowledged."""


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
        # Cron's calendar exception: restricted DOM and DOW are alternatives.
        # A wildcard calendar field (including */N) instead constrains the
        # other field; it must not make a weekday-only schedule fire daily.
        self._calendar_wildcard = parts[2].startswith("*") or parts[4].startswith("*")
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
        day_of_month = dt.day in self.matchers[2]
        day_of_week = ((dt.weekday() + 1) % 7) in self.matchers[4]
        calendar_match = ((day_of_month and day_of_week) if self._calendar_wildcard
                          else (day_of_month or day_of_week))
        return (dt.minute in self.matchers[0]
                and dt.hour in self.matchers[1]
                and dt.month in self.matchers[3]
                and calendar_match)

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
    scheduler invokes it (in a bounded worker thread per run) whenever a
    schedule's cron expression fires. The callback must await/finish its work
    or raise on failure; spawning untracked work and returning early would
    incorrectly acknowledge completion. Long callbacks block overlapping runs.
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
        self._committed_schedules = copy.deepcopy(self._schedules)
        self._committed_history = copy.deepcopy(self._history)
        if getattr(self, "_loaded_recovery_dirty", False):
            self._save()

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
        if not hasattr(self, "_idle_condition") or self._idle_condition._lock is not self._mutex:
            self._idle_condition = threading.Condition(self._mutex)
        if not hasattr(self, "_committed_schedules"):
            self._committed_schedules = copy.deepcopy(self._schedules)
            self._committed_history = copy.deepcopy(self._history)

    @staticmethod
    def _zone(name: str):
        if name == "UTC":
            return timezone.utc
        try:
            return ZoneInfo(name)
        except Exception as exc:
            raise ValueError(f"Unknown or unavailable IANA timezone: {name}") from exc

    def _next_run(self, schedule: Schedule, now: Optional[datetime] = None) -> Optional[float]:
        # Walk real UTC instants and match local calendar fields: spring-forward
        # nonexistent times cannot be invented and fall-back folds stay explicit.
        cron = self._compiled[schedule.id]
        zone = self._zone(schedule.tz)
        candidate = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc) + timedelta(minutes=1)).replace(second=0, microsecond=0)
        for _ in range(60 * 24 * 366):
            if cron.matches(candidate.astimezone(zone)):
                return candidate.timestamp()
            candidate += timedelta(minutes=1)
        return None

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
    def _parse_payload(self, loaded: Any):
        if not isinstance(loaded, dict) or loaded.get("version", 1) != 1 or not isinstance(loaded.get("schedules"), list) or not isinstance(loaded.get("history", {}), dict):
            raise ValueError("invalid schedule persistence shape/version")
        schedules, compiled, history = {}, {}, {}
        for raw in loaded["schedules"]:
            if not isinstance(raw, dict):
                raise ValueError("invalid persisted schedule")
            s = Schedule(**raw)
            if not isinstance(s.id, str) or not s.id or s.id in schedules or not isinstance(s.payload, dict) or not isinstance(s.enabled, bool):
                raise ValueError("invalid/duplicate schedule identity or payload")
            for timestamp in (s.created_at, s.last_run_at, s.next_run_at):
                if timestamp is not None and (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp)):
                    raise ValueError("invalid schedule timestamp")
            self._zone(s.tz)
            schedules[s.id] = s
            compiled[s.id] = CronExpression(s.cron)
        for sid, records in loaded.get("history", {}).items():
            if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                raise ValueError("invalid schedule history")
            history[sid] = [ScheduleRun(**record) for record in records[-50:]]
            for run in history[sid]:
                for timestamp in (run.started_at, run.finished_at, run.duration_ms):
                    if timestamp is not None and (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp)):
                        raise ValueError("invalid schedule history timestamp")
                if run.schedule_id != sid or not isinstance(run.status, str):
                    raise ValueError("invalid schedule history identity")
        return schedules, compiled, history

    def _load(self) -> None:
        data = None
        loaded_from_backup = False
        for candidate in (self.path, self.path.with_suffix(".json.bak")):
            if not candidate.exists():
                continue
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
                schedules, compiled, history = self._parse_payload(loaded)
                data = loaded
                self._schedules, self._compiled, self._history = schedules, compiled, history
                loaded_from_backup = candidate != self.path
                break
            except Exception:
                LOG.warning("Invalid schedule state at %s", candidate, exc_info=True)
        if data is None:
            self._storage_corrupt = self.path.exists() or self.path.with_suffix(".json.bak").exists()
            return
        self._storage_corrupt = False
        self._loaded_recovery_dirty = False
        now = time.time()
        for s in self._schedules.values():
            # Conservative restart policy: no automatic replay of missed or
            # partially executed external actions. Expose uncertainty in history.
            if loaded_from_backup:
                self._loaded_recovery_dirty = True
                s.enabled = False
                s.last_status = "interrupted"
                s.last_error = "Schedule backup restored; newer outcomes may be unknown. Review and re-enable explicitly."
                for run in self._history.get(s.id, []):
                    if run.status == "running":
                        run.status = "interrupted"
                        run.finished_at = now
                        run.error = s.last_error
            elif s.last_status in {"running", "queued"}:
                self._loaded_recovery_dirty = True
                s.last_status = "interrupted"
                s.last_error = "Runtime restarted; previous outcome is unknown. Review before running again."
                for run in self._history.get(s.id, []):
                    if run.status == "running":
                        run.status = "interrupted"
                        run.finished_at = now
                        run.error = s.last_error
            elif s.enabled and s.next_run_at is not None and s.next_run_at < now:
                self._loaded_recovery_dirty = True
                self._record_run(s, ScheduleRun(s.id, now, now, "skipped", error="Missed while runtime was offline; automatic replay is disabled"))

    def _save(self) -> None:
        with self._mutex:
            payload = {"version": 1,
                        "schedules": [s.to_dict()
                                      for s in self._schedules.values()],
                       "history": {sid: [run.to_dict() for run in history[-50:]] for sid, history in self._history.items()},
                       "missed_run_policy": "skip"}
            tmp = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            try:
                if getattr(self, "_storage_corrupt", False):
                    raise ValueError("Unrecoverable schedule storage; restore or explicitly remove it before writing new state")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                content = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
                self._parse_payload(payload)
                with tmp.open("w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.path.exists():
                    try:
                        old = json.loads(self.path.read_text(encoding="utf-8"))
                        self._parse_payload(old)
                        valid = True
                    except (OSError, ValueError, TypeError):
                        valid = False
                    if valid:
                        backup_tmp = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.bak.tmp"
                        try:
                            with backup_tmp.open("wb") as handle:
                                handle.write(self.path.read_bytes())
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.replace(backup_tmp, self.path.with_suffix(".json.bak"))
                        finally:
                            backup_tmp.unlink(missing_ok=True)
                for attempt in range(5):
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05 * (attempt + 1))
                self._committed_schedules = copy.deepcopy(self._schedules)
                self._committed_history = copy.deepcopy(self._history)
            except Exception as exc:
                if hasattr(self, "_committed_schedules"):
                    for sid, snapshot in self._committed_schedules.items():
                        existing = self._schedules.get(sid, copy.deepcopy(snapshot))
                        existing.__dict__.update(copy.deepcopy(snapshot.__dict__))
                        self._schedules[sid] = existing
                    self._schedules = {sid: self._schedules[sid] for sid in self._committed_schedules}
                    self._compiled = {sid: CronExpression(s.cron) for sid, s in self._schedules.items()}
                    self._history = copy.deepcopy(self._committed_history)
                raise SchedulePersistenceError("Could not persist schedule state") from exc
            finally:
                tmp.unlink(missing_ok=True)

    def _recompute_next_runs(self) -> None:
        now = datetime.now(timezone.utc)
        for s in self._schedules.values():
            cron = self._compiled.get(s.id)
            if cron is None:
                continue
            s.next_run_at = self._next_run(s, now)

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
        self._zone(tz)
        sid = uuid.uuid4().hex[:12]
        s = Schedule(id=sid, name=name, cron=cron, target=target,
                      payload=payload or {}, tz=tz, enabled=enabled)
        with self._mutex:
            self._schedules[sid] = s
            self._compiled[sid] = compiled
            s.next_run_at = self._next_run(s)
            self._save()
        return s

    def update(self, sid: str, **fields: Any) -> Optional[Schedule]:
        with self._mutex:
            s = self._schedules.get(sid)
            if s is None:
                return None
            if "tz" in fields:
                self._zone(fields["tz"])
            if "payload" in fields and not isinstance(fields["payload"], dict):
                raise ValueError("schedule payload must be an object")
            if "cron" in fields and fields["cron"] != s.cron:
                self._compiled[sid] = CronExpression(fields["cron"])
            for k, v in fields.items():
                if k in {"name", "cron", "target", "payload", "tz", "enabled"}:
                    setattr(s, k, v)
            cron = self._compiled[sid]
            s.next_run_at = self._next_run(s)
            self._save()
        return s

    def delete(self, sid: str) -> bool:
        with self._mutex:
            if sid not in self._schedules:
                return False
            self._schedules.pop(sid)
            self._compiled.pop(sid, None)
            self._history.pop(sid, None)
            self._ensure_dispatch_state()
            retained = deque()
            for item in self._pending:
                schedule, _, key, _ = item
                if schedule.id == sid:
                    self._pending_ids.discard(sid)
                    self._pending_keys.discard(key)
                else:
                    retained.append(item)
            self._pending = retained
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

    def _invoke(self, s: Schedule, runner: RunFn, *, already_claimed: bool = False,
                execution_snapshot: Optional[Schedule] = None) -> ScheduleRun:
        manual_claim = False
        key = self._coalesce_key(s)
        with self._mutex:
            self._ensure_dispatch_state()
            execution_snapshot = execution_snapshot or copy.deepcopy(s)
            if not already_claimed:
                if s.id in self._running or s.id in self._pending_ids or key in self._active_keys or self._active_run_count >= self._max_concurrent_runs or self._stop_event.is_set():
                    run = ScheduleRun(schedule_id=s.id,
                                      started_at=time.time(),
                                      finished_at=time.time(),
                                      status="failed", duration_ms=0.0,
                                      error="overlap or execution budget exhausted / scheduler stopping")
                    self._record_run(s, run)
                    self._save()
                    return run
                self._running.add(s.id)
                self._active_keys.add(key)
                self._active_run_count += 1
                manual_claim = True
        run = ScheduleRun(schedule_id=s.id, started_at=time.time())
        s.last_status = "running"
        self._record_run(s, run)
        try:
            # Persist the in-flight marker before dispatch. If it fails, do not
            # perform any external effect and do not fall back to another lane.
            self._save()
            # Editing a schedule while it runs changes future work, not the
            # already-dispatched target or payload being used by this callback.
            runner(execution_snapshot)
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
                if s.id in self._compiled:
                    s.next_run_at = self._next_run(s)
                # The run object is already in history; append only if a failed
                # storage attempt restored the committed history snapshot.
                if not any(existing is run for existing in self._history.get(s.id, [])):
                    self._record_run(s, run)
                try:
                    self._save()
                finally:
                    if manual_claim:
                        self._active_run_count = max(0, self._active_run_count - 1)
                        self._active_keys.discard(key)
                        self._idle_condition.notify_all()
                        self._drain_scheduled_queue_locked()
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
            self._pending.append((s, runner, key, copy.deepcopy(s)))
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
            s, runner, key, execution_snapshot = self._pending.popleft()
            self._pending_ids.discard(s.id)
            self._pending_keys.discard(key)
            current = self._schedules.get(s.id)
            if current is None or not current.enabled:
                continue
            self._running.add(s.id)
            self._active_keys.add(key)
            self._active_run_count += 1
            threading.Thread(
                target=self._run_queued_schedule,
                args=(s, runner, key, execution_snapshot),
                name=f"WISE_Schedule_{s.id}",
                daemon=True,
            ).start()

    def _run_queued_schedule(self, s: Schedule, runner: RunFn, key: str, execution_snapshot: Schedule) -> None:
        try:
            self._invoke(s, runner, already_claimed=True, execution_snapshot=execution_snapshot)
        finally:
            with self._mutex:
                self._active_run_count = max(0, self._active_run_count - 1)
                self._active_keys.discard(key)
                self._idle_condition.notify_all()
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
        deadline = time.monotonic() + max(0.0, timeout)
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._mutex:
            self._thread = t if t is not None and t.is_alive() else None
            self._ensure_dispatch_state()
            for schedule, _, _, _ in self._pending:
                schedule.last_status = "skipped"
                schedule.last_error = "Queued run cancelled during shutdown; no action was dispatched"
                now = time.time()
                self._record_run(schedule, ScheduleRun(schedule.id, now, now, "skipped", error=schedule.last_error))
            self._pending.clear()
            self._pending_ids.clear()
            self._pending_keys.clear()
            while self._active_run_count and time.monotonic() < deadline:
                self._idle_condition.wait(timeout=max(0.0, deadline - time.monotonic()))
            self._save()

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
                         and (s.next_run_at is None or s.next_run_at <= now.timestamp())
                         and self._compiled[s.id].matches(now.astimezone(self._zone(s.tz)))]
            runner = self._runner
        if runner is None or not triggers:
            return
        for s in triggers:
            with self._mutex:
                if s.id not in self._schedules or not s.enabled or (s.next_run_at is not None and s.next_run_at > now.timestamp()):
                    continue
                # Claim this minute durably before dispatch; restart or repeated
                # ticks cannot execute a completed trigger a second time.
                s.next_run_at = self._next_run(s, now)
                s.last_status = "queued"
                self._save()
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
