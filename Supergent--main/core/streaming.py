"""Server-Sent Events (SSE) streaming for chat sessions.

Inspired by Open Claude Cowork / Agent Cowork
(https://github.com/DevAgentForge/Open-Claude-Cowork — MIT). All code
in this module is **original** Python. We adopt only the pattern of
exposing each chat run as a streamed event feed (start → progress →
tool-call → token → complete) instead of the previous request/response
polling model.

Design
------

A :class:`Session` owns:
* a UUID,
* the user prompt + chosen mode,
* a thread running the agent's ``.run()`` synchronously,
* an :class:`asyncio.Queue` of dict-shaped events.

The consumer (FastAPI ``GET /admin/sessions/{id}/stream``) drains the
queue and writes SSE frames to the HTTP response.

Event types
-----------

* ``start``     — initial frame; payload contains session metadata.
* ``progress``  — periodic heartbeat with ``elapsed_ms``.
* ``trace``     — passthrough of a fresh :mod:`core.observability`
  event (e.g. ``react.tool.start``, ``self_healing.repair.end``).
* ``token``     — per-token delta from the LLM (Phase 8 Part 2). The
  agent runtime pipes ``UniversalLLM.stream()`` deltas through
  :class:`~core.thinking.engine.ThinkingEngine` into this channel, so
  the UI renders the final answer as each chunk arrives. Providers
  that cannot stream (custom stubs, some local backends) fall back to
  a single ``token`` frame carrying the complete response.
* ``tool_call`` — emitted when the agent invokes a tool.
* ``complete``  — terminal frame; payload contains the final answer.
* ``error``     — terminal frame on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

LOG = logging.getLogger("agent_os.streaming")


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------
@dataclass
class Session:
    id: str
    prompt: str
    mode: str
    created_at: float = field(default_factory=time.time)
    finished: bool = False
    answer: Optional[str] = None
    error: Optional[str] = None
    trace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "prompt": self.prompt, "mode": self.mode,
                 "created_at": self.created_at, "finished": self.finished,
                 "answer": self.answer, "error": self.error,
                 "trace_id": self.trace_id}


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------
def _format_sse(event: str, data: Dict[str, Any]) -> bytes:
    """Format one Server-Sent Events frame."""
    payload = json.dumps(data, ensure_ascii=False)
    # Per SSE spec: each line prefixed with "data:"; frames end with \n\n.
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class SessionRegistry:
    _instance: Optional["SessionRegistry"] = None
    _lock = threading.RLock()

    def __new__(cls) -> "SessionRegistry":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()  # type: ignore[attr-defined]
            return cls._instance

    def _init(self) -> None:
        # session_id → (Session, asyncio.Queue, thread)
        self._sessions: Dict[str, Session] = {}
        self._queues: Dict[str, asyncio.Queue] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._mutex = threading.RLock()

    # ------------------------------------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register the FastAPI event loop so background threads can
        push events back into asyncio.Queues safely."""
        self._loop = loop

    def get(self, sid: str) -> Optional[Session]:
        with self._mutex:
            return self._sessions.get(sid)

    def list(self) -> List[Session]:
        with self._mutex:
            return list(self._sessions.values())

    def queue_for(self, sid: str) -> Optional[asyncio.Queue]:
        with self._mutex:
            return self._queues.get(sid)

    # ------------------------------------------------------------------
    def start(self, *, prompt: str, mode: str,
               run_fn: Callable[[Session, "EventEmitter"], str]
               ) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(id=sid, prompt=prompt, mode=mode)
        # Always bind to the *current* running loop. Caching across
        # FastAPI requests breaks tests that spin up a fresh asyncio
        # loop per WebSocket connection (Starlette's TestClient does
        # this) — the cached loop becomes stale and
        # ``call_soon_threadsafe`` then drops every event.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        with self._mutex:
            self._sessions[sid] = session
            self._queues[sid] = q
        emitter = EventEmitter(self._loop, q)
        thread = threading.Thread(
            target=self._run_in_thread,
            args=(session, run_fn, emitter),
            name=f"agent-os-session-{sid}",
            daemon=True)
        thread.start()
        return session

    def _run_in_thread(self, session: Session,
                        run_fn: Callable[[Session, "EventEmitter"], str],
                        emitter: "EventEmitter") -> None:
        emitter.emit("start", session.to_dict())
        t0 = time.time()
        try:
            answer = run_fn(session, emitter)
            session.answer = answer or ""
            session.finished = True
            emitter.emit("complete", {"answer": session.answer,
                                        "duration_ms":
                                            (time.time() - t0) * 1000.0})
        except Exception as exc:  # noqa: BLE001
            session.error = f"{type(exc).__name__}: {exc}"
            session.finished = True
            emitter.emit("error", {"error": session.error})
        finally:
            emitter.close()


class EventEmitter:
    """Thread-side handle that pushes SSE events to the asyncio queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 q: asyncio.Queue) -> None:
        self.loop = loop
        self.queue = q
        self._closed = False

    def emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self._closed:
            return
        frame = (event, payload)
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, frame)
        except Exception:
            # Best-effort; if the loop is gone we silently drop.
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Push sentinel.
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Async iterator for FastAPI StreamingResponse
# ---------------------------------------------------------------------------
async def stream_events(sid: str, *,
                          heartbeat_seconds: float = 5.0
                          ) -> AsyncIterator[bytes]:
    """Async generator that yields SSE frames for ``sid``.

    Emits a ``progress`` heartbeat every ``heartbeat_seconds`` so
    intermediate proxies don't time out the connection. Returns once
    the producer has pushed a sentinel (``None``) or the session has
    been gone for >5 seconds.
    """
    reg = SessionRegistry()
    q = reg.queue_for(sid)
    session = reg.get(sid)
    if q is None or session is None:
        yield _format_sse("error",
                            {"error": f"unknown session: {sid}"})
        return

    t0 = time.time()
    while True:
        try:
            frame = await asyncio.wait_for(q.get(),
                                              timeout=heartbeat_seconds)
        except asyncio.TimeoutError:
            yield _format_sse("progress",
                                {"elapsed_ms":
                                     (time.time() - t0) * 1000.0,
                                  "finished": session.finished})
            if session.finished:
                return
            continue
        if frame is None:
            return
        event, payload = frame
        yield _format_sse(event, payload)
        if event in ("complete", "error"):
            return


def get_registry() -> SessionRegistry:
    return SessionRegistry()


__all__ = [
    "Session", "SessionRegistry", "EventEmitter",
    "stream_events", "get_registry",
]
