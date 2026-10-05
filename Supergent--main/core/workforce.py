"""Multi-agent workforce coordinator.

Inspired by Eigent's open-source Cowork desktop architecture
(https://github.com/eigent-ai/eigent — Apache-2.0). All code in this
module is **original**: no source from Eigent (or its CAMEL-AI
dependency) has been copied. We adopt only the high-level design
pattern of a root planner that decomposes a task into subtasks, a
shared async task queue, a pool of workers that consume the queue, and
a recursive-spawn affordance for long-horizon work.

Public surface
--------------

* :class:`Workforce` — singleton-style coordinator. Submit a top-level
  task with :py:meth:`Workforce.run`.
* :class:`RootPlanner` — pluggable strategy that turns a free-form
  task into a list of :class:`SubTask` objects. The default planner
  is heuristic; advanced callers can install an LLM-backed planner.
* :class:`Worker` — async worker; subclass to plug in custom
  execution. The default worker resolves each subtask by calling the
  registered :func:`execute_fn` (typically a thin wrapper around the
  agent's ReAct loop).
* :class:`RetryPolicy` — exponential-backoff retry policy.

Concurrency model
-----------------

Workers run on an ``asyncio.Queue``. The pool size is bounded by the
``max_parallel_tools`` resource setting (so Lite Mode → 1 worker,
Pro Mode → 8 by default). Workers may spawn sub-workers for any
subtask they cannot solve directly, up to ``MAX_RECURSION_DEPTH``.
On the third consecutive failure of any subtask, the failure
escalates to the parent worker; if there is no parent, the failure
propagates to :py:meth:`Workforce.run` and is recorded in the report.

Failure tolerance is intentionally conservative: a subtask that
exhausts its retries marks the parent task ``partial`` (not
``failed``), so the overall ``WorkforceReport`` always returns the
best-effort outcome plus a list of unresolved subtasks the user
can re-run manually.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

LOG = logging.getLogger("agent_os.workforce")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_RECURSION_DEPTH = 3
DEFAULT_RETRY_LIMIT = 3
DEFAULT_BACKOFF_BASE = 1.0      # seconds
DEFAULT_BACKOFF_MAX = 16.0      # seconds


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------
@dataclass
class SubTask:
    """A unit of work produced by the planner and consumed by workers.

    ``deps`` is a list of subtask IDs that must complete (status==done)
    before this subtask becomes ready. The default planner emits
    linear chains for now; richer planners may emit DAGs.
    """
    id: str
    description: str
    deps: List[str] = field(default_factory=list)
    parent_id: Optional[str] = None
    depth: int = 0
    status: str = "pending"          # pending | running | done | failed | partial
    attempts: int = 0
    result: Any = None
    error: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    children: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkforceReport:
    task_id: str
    description: str
    status: str = "pending"          # pending | running | done | partial | failed
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    subtasks: List[SubTask] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "subtasks": [s.to_dict() for s in self.subtasks],
            "failures": list(self.failures),
        }


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------
@dataclass
class RetryPolicy:
    max_attempts: int = DEFAULT_RETRY_LIMIT
    base_delay: float = DEFAULT_BACKOFF_BASE
    max_delay: float = DEFAULT_BACKOFF_MAX

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with cap."""
        return min(self.base_delay * (2 ** max(0, attempt - 1)),
                   self.max_delay)

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_attempts


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
class RootPlanner:
    """Decomposes a free-form task description into ordered subtasks.

    The default implementation is heuristic and intentionally
    deterministic — perfect for unit tests and offline runs. Operators
    that need richer decomposition can subclass and override
    :meth:`plan`. We keep the interface synchronous because planning
    is typically I/O-bound on a single LLM call which the override can
    perform internally.
    """

    def plan(self, description: str) -> List[SubTask]:
        # Heuristic: split on numbered lists, " then ", "; ", or " and ".
        text = description.strip()
        if not text:
            return []
        import re
        # Numbered list: "1. foo 2. bar 3. baz" — split on the marker
        # and discard the empty leading chunk.
        if re.search(r"(?:^|\s)\d+[.)]\s+", text):
            parts = re.split(r"(?:^|\s)\d+[.)]\s+", text)
            items = [p.strip() for p in parts if p.strip()]
        else:
            items = []
        if not items:
            for sep in [" then ", "; ", " and "]:
                if sep in text.lower():
                    items = [s.strip() for s in
                              re.split(sep, text, flags=re.IGNORECASE)]
                    break
        if not items:
            items = [text]
        out: List[SubTask] = []
        prev_id: Optional[str] = None
        for raw in items:
            piece = raw.strip().strip(".,;")
            if not piece:
                continue
            sid = uuid.uuid4().hex[:10]
            deps = [prev_id] if prev_id else []
            out.append(SubTask(id=sid, description=piece, deps=deps))
            prev_id = sid
        return out


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
ExecuteFn = Callable[[SubTask], Awaitable[Any]]


class Worker:
    """Single async worker. Pulls subtasks from the queue and runs them."""

    def __init__(self, *, name: str,
                 execute: ExecuteFn,
                 retry: RetryPolicy,
                 workforce: "Workforce") -> None:
        self.name = name
        self.execute = execute
        self.retry = retry
        self.workforce = workforce

    async def run(self) -> None:
        while True:
            subtask = await self.workforce.queue.get()
            try:
                if subtask is None:
                    # Sentinel — pool shutdown.
                    return
                await self._run_subtask(subtask)
            finally:
                self.workforce.queue.task_done()

    async def _run_subtask(self, st: SubTask) -> None:
        st.status = "running"
        st.started_at = time.time()
        for attempt in range(1, self.retry.max_attempts + 1):
            st.attempts = attempt
            try:
                result = await self.execute(st)
                st.result = result
                st.status = "done"
                st.finished_at = time.time()
                LOG.debug("worker=%s subtask=%s done attempts=%d",
                           self.name, st.id, attempt)
                self.workforce._on_subtask_done(st)
                return
            except RecursionRequest as rr:
                # Worker decided to spawn sub-workers.
                if st.depth >= MAX_RECURSION_DEPTH:
                    st.status = "failed"
                    st.error = "max recursion depth reached"
                    st.finished_at = time.time()
                    self.workforce._on_subtask_failed(st)
                    return
                ok = await self.workforce._spawn_recursive(st, rr.subtasks)
                st.status = "done" if ok else "partial"
                st.finished_at = time.time()
                if ok:
                    self.workforce._on_subtask_done(st)
                else:
                    self.workforce._on_subtask_failed(st)
                return
            except Exception as exc:  # noqa: BLE001
                st.error = f"{type(exc).__name__}: {exc}"
                LOG.warning("worker=%s subtask=%s attempt=%d error=%s",
                             self.name, st.id, attempt, st.error)
                if not self.retry.should_retry(attempt):
                    break
                await asyncio.sleep(self.retry.delay_for(attempt))
        st.status = "failed"
        st.finished_at = time.time()
        self.workforce._on_subtask_failed(st)


class RecursionRequest(Exception):
    """Raised by :class:`ExecuteFn` to request recursive spawn.

    The execute function is expected to return a normal value on
    success. To request the workforce spawn sub-workers, raise this
    exception with a list of new :class:`SubTask` objects.
    """

    def __init__(self, subtasks: List[SubTask]) -> None:
        super().__init__(f"recursion request: {len(subtasks)} subtasks")
        self.subtasks = subtasks


# ---------------------------------------------------------------------------
# Workforce
# ---------------------------------------------------------------------------
class Workforce:
    """Coordinates planning + worker pool + recursive spawn.

    Use :py:meth:`run` for the simple "submit a task, await a report"
    path. The lower-level :py:meth:`enqueue` is exposed for callers
    that want to stream tasks into an already-running pool.
    """

    def __init__(self, *,
                 planner: Optional[RootPlanner] = None,
                 execute: ExecuteFn,
                 max_workers: int = 1,
                 retry: Optional[RetryPolicy] = None) -> None:
        self.planner = planner or RootPlanner()
        self.execute = execute
        self.max_workers = max(1, int(max_workers))
        self.retry = retry or RetryPolicy()
        self.queue: asyncio.Queue[Optional[SubTask]] = asyncio.Queue()
        self._reports: Dict[str, WorkforceReport] = {}
        self._subtask_index: Dict[str, SubTask] = {}
        self._lock = asyncio.Lock()

    # ---- internal callbacks ----------------------------------------------
    def _on_subtask_done(self, st: SubTask) -> None:
        self._subtask_index[st.id] = st

    def _on_subtask_failed(self, st: SubTask) -> None:
        self._subtask_index[st.id] = st

    async def _spawn_recursive(self, parent: SubTask,
                                children: List[SubTask]) -> bool:
        """Enqueue children + wait for them. Returns True iff all done."""
        for c in children:
            c.parent_id = parent.id
            c.depth = parent.depth + 1
            parent.children.append(c.id)
            self._subtask_index[c.id] = c
        # Run sequentially within the parent worker so we don't deadlock.
        for c in children:
            await self._execute_with_retries(c)
            if c.status not in ("done", "partial"):
                return False
        return True

    async def _execute_with_retries(self, st: SubTask) -> None:
        st.status = "running"
        st.started_at = time.time()
        for attempt in range(1, self.retry.max_attempts + 1):
            st.attempts = attempt
            try:
                st.result = await self.execute(st)
                st.status = "done"
                st.finished_at = time.time()
                return
            except RecursionRequest as rr:
                if st.depth >= MAX_RECURSION_DEPTH:
                    st.status = "failed"
                    st.error = "max recursion depth reached"
                    st.finished_at = time.time()
                    return
                ok = await self._spawn_recursive(st, rr.subtasks)
                st.status = "done" if ok else "partial"
                st.finished_at = time.time()
                return
            except Exception as exc:  # noqa: BLE001
                st.error = f"{type(exc).__name__}: {exc}"
                if not self.retry.should_retry(attempt):
                    break
                await asyncio.sleep(self.retry.delay_for(attempt))
        st.status = "failed"
        st.finished_at = time.time()

    # ---- public API -------------------------------------------------------
    async def run(self, description: str,
                  *, task_id: Optional[str] = None) -> WorkforceReport:
        """Plan, execute, and return a final report."""
        tid = task_id or uuid.uuid4().hex[:12]
        report = WorkforceReport(task_id=tid, description=description,
                                  status="running")
        self._reports[tid] = report

        subtasks = self.planner.plan(description)
        report.subtasks = subtasks
        for st in subtasks:
            self._subtask_index[st.id] = st

        if not subtasks:
            report.status = "done"
            report.finished_at = time.time()
            report.duration_ms = (
                (report.finished_at - report.started_at) * 1000.0)
            return report

        # Drive a topological execution: enqueue ready subtasks, wait,
        # then enqueue the next wave.
        workers = await self._start_workers()
        try:
            remaining = {s.id for s in subtasks}
            while remaining:
                ready = [self._subtask_index[i] for i in remaining
                          if all(self._subtask_index[d].status == "done"
                                  for d in self._subtask_index[i].deps)]
                if not ready:
                    # All remaining are blocked on failed deps.
                    for i in remaining:
                        st = self._subtask_index[i]
                        if st.status == "pending":
                            st.status = "skipped"
                            st.error = "blocked by failed dependency"
                    break
                for st in ready:
                    await self.queue.put(st)
                await self.queue.join()
                remaining = {s.id for s in subtasks
                              if self._subtask_index[s.id].status
                              not in ("done", "failed", "skipped",
                                       "partial")}
                if not remaining:
                    break
        finally:
            await self._stop_workers(workers)

        # Determine final status.
        statuses = {self._subtask_index[s.id].status for s in subtasks}
        if statuses <= {"done"}:
            report.status = "done"
        elif {"done", "partial", "skipped"} & statuses:
            report.status = "partial"
            report.failures = [self._subtask_index[s.id].error
                                for s in subtasks
                                if self._subtask_index[s.id].status
                                in ("failed", "skipped")
                                and self._subtask_index[s.id].error]
        else:
            report.status = "failed"
            report.failures = [self._subtask_index[s.id].error
                                for s in subtasks
                                if self._subtask_index[s.id].error]

        report.finished_at = time.time()
        report.duration_ms = (
            (report.finished_at - report.started_at) * 1000.0)
        return report

    async def _start_workers(self) -> List[asyncio.Task]:
        return [asyncio.create_task(
                    Worker(name=f"w{i}",
                            execute=self.execute,
                            retry=self.retry,
                            workforce=self).run())
                for i in range(self.max_workers)]

    async def _stop_workers(self, workers: List[asyncio.Task]) -> None:
        for _ in workers:
            await self.queue.put(None)
        for w in workers:
            try:
                await w
            except Exception:  # noqa: BLE001
                pass

    def report(self, task_id: str) -> Optional[WorkforceReport]:
        return self._reports.get(task_id)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------
def make_workforce_from_settings(execute: ExecuteFn,
                                  *, planner: Optional[RootPlanner] = None,
                                  ) -> Workforce:
    """Construct a Workforce honouring the current ResourceSettings."""
    try:
        from .resource_settings import get_store
        settings = get_store().load()
        max_workers = max(1, int(getattr(settings,
                                           "max_parallel_tools", 1)))
    except Exception:
        max_workers = 1
    return Workforce(execute=execute, planner=planner,
                      max_workers=max_workers)


__all__ = [
    "Workforce", "Worker", "RootPlanner", "SubTask",
    "WorkforceReport", "RetryPolicy", "RecursionRequest",
    "MAX_RECURSION_DEPTH", "make_workforce_from_settings",
]
