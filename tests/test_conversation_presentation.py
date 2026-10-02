"""Model attribution, cheap greetings and live progress contracts (offline)."""
import asyncio
import json
from types import SimpleNamespace


def test_stop_command_cannot_target_another_conversation(tmp_path):
    from core.brain.conversational_core import ConversationalCore, SessionContext
    from core.brain.task_engine import TaskEngine, TaskStatus
    engine = TaskEngine(storage_path=tmp_path / "tasks.json")
    mine = engine.create_capability_task("own goal", "mine")
    other = engine.create_capability_task("other goal", "other")
    core = ConversationalCore.__new__(ConversationalCore)
    core.task_engine = engine
    session = SessionContext("mine")
    session.active_task_id = other.task_id  # Simulate a stale persisted pointer.
    result = core._handle_control_commands("stop", session)
    assert result.task_id == mine.task_id
    assert mine.status == TaskStatus.CANCELLED
    assert other.status == TaskStatus.RUNNING


def test_greeting_uses_one_completion_without_preflight(monkeypatch, tmp_path):
    from core.brain.conversational_core import ConversationalCore
    from core.models.provider_interface import ModelCompletionResponse
    from core.session_service import SessionService
    import core.session_service as sessions
    calls = []

    class Provider:
        def generate(self, request):
            calls.append(request)
            return ModelCompletionResponse(text="وعليكم السلام", model_name="actual-model", is_simulated=False)

    core = ConversationalCore.__new__(ConversationalCore)
    import threading
    core._turn_state = threading.local()
    core._provider = Provider()
    core.decision_engine = SimpleNamespace(preflight_analyze=lambda **_: (_ for _ in ()).throw(AssertionError("unneeded classification")))
    store = SessionService(storage_dir=tmp_path)
    monkeypatch.setattr(sessions, "get_session_service", lambda: store)
    events = []
    result = core.process_turn("السلام عليكم", session_id="greeting", on_milestone=events.append)
    assert len(calls) == 1
    assert result.to_dict()["model_name"] == "actual-model"
    assert result.action_type == "DIRECT_ANSWER"
    assert [e.stage for e in events] == ["Thinking"]
    restored = SessionService(storage_dir=tmp_path).get_or_create_session("greeting")
    assert restored.history[-1]["model_name"] == "actual-model"


def test_live_events_arrive_before_completed_turn_and_replay_is_free(monkeypatch):
    import api.server as server
    import core.brain.conversational_core as conversation
    from core.brain.conversational_core import ConversationMilestone, ConversationalTurnResult
    import threading
    released = threading.Event()
    calls = []

    class Core:
        def process_turn(self, text, **kwargs):
            calls.append(text)
            kwargs["on_milestone"](ConversationMilestone(stage="Tools", title="native.write_file", status="IN_PROGRESS"))
            assert released.wait(5), "milestone was buffered until completion"
            return ConversationalTurnResult(session_id="live", reply_text="done", intent=text, model_name="actual-model")

    monkeypatch.setattr(conversation, "get_conversational_core", lambda: Core())

    async def run():
        request = server.V2ChatRequest(message="build test", session_id="live")
        response = await server.v2_chat_events(request, "test-live-events")
        iterator = response.body_iterator
        first = json.loads((await anext(iterator)).removeprefix("data: "))
        assert first["type"] == "milestone"
        assert not released.is_set()
        released.set()
        done = json.loads((await anext(iterator)).removeprefix("data: "))
        assert done["type"] == "done"
        assert done["response"]["model_name"] == "actual-model"
        replay = await server.v2_chat_events(request, "test-live-events")
        replayed = json.loads((await anext(replay.body_iterator)).removeprefix("data: "))
        assert replayed["response"]["idempotent_replay"]

    asyncio.run(run())
    assert len(calls) == 1
