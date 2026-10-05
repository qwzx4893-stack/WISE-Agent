"""Contracts for V2 WebSocket trace correlation without a live model."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace


class _Socket:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._received = False

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        if self._received:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        self._received = True
        return json.dumps({"type": "chat", "message": "trace this", "session_id": "stream-test"})

    async def send_json(self, event: dict) -> None:
        self.events.append(event)


def test_v2_stream_uses_one_trace_id_for_every_turn_event(monkeypatch):
    import api.server as server
    from core.observability import Tracer
    import core.brain.conversational_core as conversational_core

    class _Core:
        def process_turn(self, *_args, **_kwargs):
            Tracer.emit("test.stream.turn")
            return SimpleNamespace(
                milestones=[SimpleNamespace(stage="PLAN", title="Plan", status="ok")],
                session_id="stream-test",
                task_id="task-stream",
                reply_text="completed",
                action_type="chat",
                task_status="completed",
                latency_ms=1.2,
            )

    Tracer.clear()
    monkeypatch.setattr(server, "_websocket_is_authorized", lambda _ws: True)
    monkeypatch.setattr(conversational_core, "get_conversational_core", lambda: _Core())
    ws = _Socket()

    asyncio.run(server.v2_stream(ws))

    correlated = [event for event in ws.events if event["type"] in {"status", "milestone", "done"}]
    trace_ids = {event.get("trace_id") for event in correlated}
    assert len(trace_ids) == 1
    trace_id = trace_ids.pop()
    assert trace_id
    assert Tracer.events(trace_id=trace_id, kind="test.stream.turn")

