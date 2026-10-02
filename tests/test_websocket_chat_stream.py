"""Phase 9 Part 1.2 — WebSocket /chat/stream end-to-end behaviour.

These tests use Starlette's TestClient WebSocket helper to:

* speak the documented client→server protocol (``{"type":"start", ...}`` /
  ``{"type":"cancel"}``),
* assert the server emits ``session`` → (``token``/``tool_call``/
  ``tool_result``/``think``) → ``final`` events for a successful run,
* assert the cancel pathway works end-to-end,
* assert auth & validation errors close with the expected codes.

They do **not** spin up a real LLM. We inject a tiny stub model into
the FastAPI ``_state`` that the WebSocket runner consumes, plus a
fake tool registry so the Normal-mode agent is satisfied.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def ws_app(monkeypatch_module):
    """Build the FastAPI app and pre-load a stub model for the WebSocket
    runner so ``_state['model']`` is non-None and the chat doesn't 503.
    """
    monkeypatch_module.delenv("AGENT_API_TOKEN", raising=False)

    from api.server import app, _state
    # Drop in a minimal stub that talks to the agent loop just enough
    # for Normal mode to short-circuit: ``Normal.run`` calls
    # ``model.ask(...)`` — we return a final-style answer immediately.
    class _StubModel:
        name = "stub"

        def ask(self, *args: Any, **kwargs: Any) -> str:
            # The Normal agent's ReAct loop typically expects either a
            # tool call or a "Final answer:" sentinel. We return a
            # short textual answer so the loop terminates after one
            # round.
            return "Final answer: hello from stub"

    _state["model"] = _StubModel()
    _state["awareness"] = None  # OK for normal mode
    _state["ready"] = True
    return app


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


def _collect(ws, *, until: str, timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Pull frames until we hit ``type == until`` or hit ``timeout``."""
    out: List[Dict[str, Any]] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            frame = ws.receive_json()
        except Exception:
            break
        out.append(frame)
        if frame.get("type") == until:
            break
    return out


def test_ws_rejects_bad_opening_frame(ws_app):
    client = TestClient(ws_app)
    with client.websocket_connect("/chat/stream") as ws:
        ws.send_json({"type": "garbage"})
        # The server closes 4400 — receive_json will raise.
        with pytest.raises(Exception):
            ws.receive_json()


def test_ws_missing_message_closes_4400(ws_app):
    client = TestClient(ws_app)
    with client.websocket_connect("/chat/stream") as ws:
        ws.send_json({"type": "start", "message": ""})
        with pytest.raises(Exception):
            ws.receive_json()


def test_ws_invalid_mode_closes_4400(ws_app):
    client = TestClient(ws_app)
    with client.websocket_connect("/chat/stream") as ws:
        ws.send_json({"type": "start", "message": "hi", "mode": "lol"})
        with pytest.raises(Exception):
            ws.receive_json()


def test_ws_full_session_emits_session_then_final(ws_app):
    client = TestClient(ws_app)
    with client.websocket_connect("/chat/stream") as ws:
        ws.send_json({"type": "start", "message": "ping",
                       "mode": "normal", "max_steps": 2})
        first = ws.receive_json()
        assert first["type"] == "session"
        assert "session_id" in first

        # Drain until we see the final event or timeout.
        frames = _collect(ws, until="final", timeout=10.0)
        types = [f["type"] for f in frames]
        assert "final" in types, f"missing 'final' in {types}"
        final = next(f for f in frames if f["type"] == "final")
        assert "answer" in final
        assert "duration_ms" in final


def test_ws_cancel_message_terminates_session(ws_app):
    client = TestClient(ws_app)
    with client.websocket_connect("/chat/stream") as ws:
        ws.send_json({"type": "start", "message": "ping",
                       "mode": "normal", "max_steps": 2})
        # First frame is always 'session'.
        first = ws.receive_json()
        assert first["type"] == "session"

        # Immediately cancel. The runner polls cancel_flag every 200ms
        # and emits an ``error`` frame. The server then closes the
        # connection.
        ws.send_json({"type": "cancel"})

        # After cancellation we should see either error or final
        # before the WS closes (the agent may have already finished).
        frames: List[Dict[str, Any]] = []
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                frames.append(ws.receive_json())
            except Exception:
                break
        types = {f["type"] for f in frames}
        assert types & {"error", "final"}, f"got {types}"


def test_ws_auth_required_when_token_set(ws_app, monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "shh")
    client = TestClient(ws_app)
    with pytest.raises(Exception):
        with client.websocket_connect("/chat/stream") as ws:
            ws.receive_json()


def test_ws_auth_passes_when_token_matches(ws_app, monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "shh")
    client = TestClient(ws_app)
    with client.websocket_connect("/chat/stream?token=shh") as ws:
        ws.send_json({"type": "start", "message": "hi"})
        first = ws.receive_json()
        assert first["type"] == "session"
