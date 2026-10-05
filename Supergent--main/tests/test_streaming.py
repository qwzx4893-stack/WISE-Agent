"""Phase 7 — Part 3e: SSE session streaming."""

from __future__ import annotations

import asyncio
import json
import time

import pytest


def _parse_sse_frames(body: str):
    """Parse raw SSE body into a list of (event, payload) pairs."""
    out = []
    for chunk in body.strip().split("\n\n"):
        event = None
        data_lines = []
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if event and data_lines:
            try:
                out.append((event, json.loads("\n".join(data_lines))))
            except Exception:
                out.append((event, {"raw": "\n".join(data_lines)}))
    return out


def test_sse_frame_formatting():
    from core.streaming import _format_sse
    frame = _format_sse("tool_call", {"name": "nmap", "status": "ok"})
    text = frame.decode("utf-8")
    assert text.startswith("event: tool_call\n")
    assert "\"name\": \"nmap\"" in text
    assert text.endswith("\n\n")


def test_stream_events_unknown_session():
    from core.streaming import stream_events

    async def collect():
        out = []
        async for frame in stream_events("does_not_exist",
                                            heartbeat_seconds=0.1):
            out.append(frame.decode("utf-8"))
            if len(out) > 2:
                break
        return out

    frames = asyncio.run(collect())
    assert any("unknown session" in f for f in frames)
    assert any("event: error" in f for f in frames)


def test_start_and_stream_success_flow():
    """End-to-end: start a synthetic session and drain events until complete."""
    from core.streaming import SessionRegistry, stream_events

    async def run():
        # Attach the current loop so background threads can push events.
        loop = asyncio.get_event_loop()
        reg = SessionRegistry()
        reg.attach_loop(loop)

        def runner(session, emitter):
            emitter.emit("progress", {"stage": "thinking"})
            emitter.emit("tool_call", {"name": "nmap",
                                          "status": "start"})
            time.sleep(0.05)
            emitter.emit("tool_call", {"name": "nmap",
                                          "status": "ok"})
            return "final answer"

        session = reg.start(prompt="hi", mode="normal", run_fn=runner)
        frames = []
        async for raw in stream_events(session.id,
                                          heartbeat_seconds=2.0):
            frames.extend(_parse_sse_frames(raw.decode("utf-8")))
            if any(e == "complete" for e, _ in frames):
                break
        return frames, session

    frames, session = asyncio.run(run())
    events = [e for e, _ in frames]
    assert "start" in events
    assert "tool_call" in events
    assert "complete" in events
    complete = next(p for e, p in frames if e == "complete")
    assert complete["answer"] == "final answer"
    assert session.finished is True
    assert session.answer == "final answer"


def test_start_and_stream_error_flow():
    from core.streaming import SessionRegistry, stream_events

    async def run():
        loop = asyncio.get_event_loop()
        reg = SessionRegistry()
        reg.attach_loop(loop)

        def runner(session, emitter):
            raise RuntimeError("boom")

        session = reg.start(prompt="hi", mode="normal", run_fn=runner)
        frames = []
        async for raw in stream_events(session.id,
                                          heartbeat_seconds=2.0):
            frames.extend(_parse_sse_frames(raw.decode("utf-8")))
            if any(e == "error" for e, _ in frames):
                break
        return frames, session

    frames, session = asyncio.run(run())
    events = [e for e, _ in frames]
    assert "error" in events
    err = next(p for e, p in frames if e == "error")
    assert "boom" in err["error"]
    assert session.error is not None


def test_progress_heartbeat_keeps_connection_alive():
    """Long-running session without events still emits progress frames."""
    from core.streaming import SessionRegistry, stream_events

    async def run():
        loop = asyncio.get_event_loop()
        reg = SessionRegistry()
        reg.attach_loop(loop)

        def runner(session, emitter):
            time.sleep(0.5)
            return "done"

        session = reg.start(prompt="x", mode="normal", run_fn=runner)
        frames = []
        async for raw in stream_events(session.id,
                                          heartbeat_seconds=0.1):
            frames.extend(_parse_sse_frames(raw.decode("utf-8")))
            if any(e == "complete" for e, _ in frames):
                break
        return frames

    frames = asyncio.run(run())
    events = [e for e, _ in frames]
    assert "progress" in events
    assert "complete" in events


def test_api_session_get_unknown_returns_404(monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    monkeypatch.setenv("AGENT_OS_API_TOKEN", "secret")
    client = TestClient(app)
    r = client.get("/admin/sessions/nosuchid",
                    headers={"X-Agent-Token": "secret"})
    assert r.status_code == 404
