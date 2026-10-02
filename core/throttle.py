"""Tool throttle — bounds the number of simultaneously-running tools.

Constrained desktop hosts can't sustain dozens of concurrent
subprocess.Popen calls. ``ToolThrottle`` is a thin wrapper around a
``threading.Semaphore`` that every mode (Normal / Workflow / AgentsTeam)
uses to rate-limit tool invocations.

Default: ``AGENT_MAX_CONCURRENT_TOOLS=2`` (i.e. at most 2 tools running
at the same time on the device). Override per-environment.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator

log = logging.getLogger(__name__)


class ToolThrottle:
    def __init__(self, max_concurrent: int | None = None) -> None:
        if max_concurrent is None:
            max_concurrent = int(os.environ.get("AGENT_MAX_CONCURRENT_TOOLS", "2"))
        max_concurrent = max(1, max_concurrent)
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._max = max_concurrent
        self._inflight = 0
        self._lock = threading.Lock()
        self._stats: Dict[str, int] = {"total": 0, "max_observed_inflight": 0}

    @property
    def max_concurrent(self) -> int:
        return self._max

    @contextmanager
    def slot(self, tool_name: str = "?") -> Iterator[None]:
        self._sem.acquire()
        with self._lock:
            self._inflight += 1
            self._stats["total"] += 1
            if self._inflight > self._stats["max_observed_inflight"]:
                self._stats["max_observed_inflight"] = self._inflight
        try:
            yield
        finally:
            with self._lock:
                self._inflight -= 1
            self._sem.release()

    def execute(self, fn: Callable[..., Any], *args: Any,
                tool_name: str = "?", **kwargs: Any) -> Any:
        with self.slot(tool_name):
            t0 = time.time()
            try:
                return fn(*args, **kwargs)
            finally:
                log.debug("throttle: %s took %.2fs", tool_name, time.time() - t0)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "max_concurrent": self._max,
                "inflight": self._inflight,
                **self._stats,
            }


# Module-level singleton so all modes share one limiter.
_THROTTLE = ToolThrottle()


def default_throttle() -> ToolThrottle:
    return _THROTTLE


__all__ = ["ToolThrottle", "default_throttle"]
