"""Lightweight tracing for Agent OS modes.

We intentionally do NOT use OpenTelemetry — too heavy for a phone-class
deployment. Instead we keep a process-local, thread-safe ring buffer of
trace events:

    Tracer.emit("workflow.step.start", workflow_id="…", step_id="…", tool="…")
    Tracer.emit("workflow.step.end",   …, status="ok", duration_ms=12.3)

Events are JSON-serialisable dicts with at minimum:

    { "ts": 1738012345.6, "kind": "workflow.step.start", "trace_id": "…", … }

Public API:

- ``Tracer`` — module-level singleton; ``Tracer.emit(kind, **fields)``.
- ``Tracer.span(kind, **fields)`` — context-manager that emits ``.start``
  / ``.end`` automatically and times the body.
- ``Tracer.events(trace_id=None, kind=None, limit=200)`` — query.
- ``new_trace_id()`` — generate a stable id for a logical task.

The buffer size is bounded by ``AGENT_TRACE_BUFFER`` (default 2048).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterable, Iterator, List, Optional


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


class _Tracer:
    def __init__(self, capacity: int | None = None) -> None:
        if capacity is None:
            capacity = int(os.environ.get("AGENT_TRACE_BUFFER", "2048"))
        self.capacity = max(64, capacity)
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=self.capacity)
        self._lock = threading.Lock()
        # ``trace_id`` defaults to the per-thread current trace.
        self._tls = threading.local()

    # ------------------------------------------------------------------
    def current_trace_id(self) -> Optional[str]:
        return getattr(self._tls, "trace_id", None)

    @contextmanager
    def trace(self, trace_id: str | None = None) -> Iterator[str]:
        tid = trace_id or new_trace_id()
        prev = getattr(self._tls, "trace_id", None)
        self._tls.trace_id = tid
        try:
            yield tid
        finally:
            self._tls.trace_id = prev

    def emit(self, kind: str, **fields: Any) -> Dict[str, Any]:
        ev = {
            "ts": time.time(),
            "kind": kind,
            "trace_id": fields.pop("trace_id", None) or self.current_trace_id(),
            **fields,
        }
        with self._lock:
            self._buffer.append(ev)
        return ev

    @contextmanager
    def span(self, kind: str, **fields: Any) -> Iterator[Dict[str, Any]]:
        start = time.time()
        ctx = {**fields}
        self.emit(f"{kind}.start", **ctx)
        ok = True
        err: Optional[str] = None
        try:
            yield ctx
        except Exception as e:  # noqa: BLE001
            ok = False
            err = str(e)
            raise
        finally:
            duration_ms = (time.time() - start) * 1000
            end_payload = {
                **ctx,
                "status": "ok" if ok else "error",
                "duration_ms": round(duration_ms, 2),
            }
            if err:
                end_payload["error"] = err
            self.emit(f"{kind}.end", **end_payload)

    # ------------------------------------------------------------------
    def events(self, *, trace_id: Optional[str] = None,
               kind: Optional[str] = None,
               limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            snapshot = list(self._buffer)
        if trace_id:
            snapshot = [e for e in snapshot if e.get("trace_id") == trace_id]
        if kind:
            snapshot = [e for e in snapshot if e.get("kind", "").startswith(kind)]
        return snapshot[-limit:]

    def trace_ids(self, *, limit: int = 50) -> List[str]:
        with self._lock:
            seen: List[str] = []
            seen_set: set[str] = set()
            for e in reversed(self._buffer):
                tid = e.get("trace_id")
                if tid and tid not in seen_set:
                    seen.append(tid)
                    seen_set.add(tid)
                if len(seen) >= limit:
                    break
        return seen

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            kinds: Dict[str, int] = {}
            for e in self._buffer:
                kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
            return {
                "capacity": self.capacity,
                "size": len(self._buffer),
                "by_kind": kinds,
            }


Tracer = _Tracer()


__all__ = ["Tracer", "new_trace_id"]
