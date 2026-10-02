import asyncio

from core.idempotency import IdempotencyStore


def test_store_replays_only_the_same_completed_payload():
    store = IdempotencyStore(ttl_seconds=60)
    payload = {"message": "open report", "session": "s1"}

    assert store.begin("chat", "key-1", payload).state == "EXECUTE"
    assert store.begin("chat", "key-1", payload).state == "IN_PROGRESS"
    assert store.begin("chat", "key-1", {"message": "delete report"}).state == "CONFLICT"
    store.complete("chat", "key-1", {"answer": "done", "nested": {"safe": True}})
    replay = store.begin("chat", "key-1", payload)
    assert replay.state == "REPLAY"
    replay.response["nested"]["safe"] = False
    assert store.begin("chat", "key-1", payload).response["nested"]["safe"] is True


def test_v2_chat_replays_a_completed_turn_without_executing_again(monkeypatch):
    import api.server as server
    import core.brain.conversational_core as conversational

    calls = []

    class Result:
        def to_dict(self):
            return {"reply_text": "done", "session_id": "idempotency-test"}

    class Core:
        def process_turn(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Result()

    monkeypatch.setattr(conversational, "get_conversational_core", lambda: Core())
    request = server.V2ChatRequest(message="safe task", session_id="idempotency-test")
    key = "test-v2-idempotency-unique"
    first = asyncio.run(server.v2_chat(request, idempotency_key=key))
    second = asyncio.run(server.v2_chat(request, idempotency_key=key))

    assert len(calls) == 1
    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert first["trace_id"] == second["trace_id"]
