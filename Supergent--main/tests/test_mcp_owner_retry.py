"""Offline owner-approved retry contracts; no sockets, models or real accounts."""

import pytest
import copy
from concurrent.futures import ThreadPoolExecutor

from tests.test_mcp_outbound_authority_security import authority  # shared in-memory fixture
from core.mcp.policy import ConfirmationRequired
from core.security.security_gate import SecurityContext
from core.tools_bridge import builtin_security_context


def invoke(wrapper, arguments, session="owner-conversation"):
    with builtin_security_context(SecurityContext(session_id=session), "create_note", arguments):
        return wrapper(**arguments)


def requested(wrapper, arguments, session="owner-conversation"):
    with pytest.raises(ConfirmationRequired) as error:
        invoke(wrapper, arguments, session)
    return error.value.call_id


def test_owner_approved_ordinary_retry_executes_once(authority):
    _, tools = authority.configure()
    wrapper = tools["mcp_fixture_create_note"]
    arguments = {"title": "synthetic-note"}
    call_id = requested(wrapper, arguments)
    assert authority.pending.resolve(call_id, approved=True)
    invoke(wrapper, arguments)
    assert len(authority.wire) == 1
    assert authority.state["notes"] == ["synthetic-note"]
    assert authority.pending.list_pending() == []
    requested(wrapper, arguments)
    assert len(authority.wire) == 1


def test_waiting_ordinary_retry_reuses_same_owner_request(authority):
    _, tools = authority.configure()
    wrapper = tools["mcp_fixture_create_note"]
    arguments = {"title": "synthetic-note"}
    call_id = requested(wrapper, arguments)
    assert requested(wrapper, arguments) == call_id
    assert len(authority.pending.list_pending()) == 1
    assert authority.wire == []


@pytest.mark.parametrize("change", ["session", "arguments", "client", "url", "headers", "policy",
                                    "live-url", "live-headers", "live-session", "tool-definition"])
def test_owner_receipt_requires_unchanged_scope_and_recipient(authority, change):
    registry, tools = authority.configure()
    wrapper = tools["mcp_fixture_create_note"]
    arguments = {"title": "synthetic-note"}
    call_id = requested(wrapper, arguments)
    authority.pending.resolve(call_id, approved=True)
    session = "owner-conversation"
    if change == "session":
        session = "other-conversation"
    elif change == "arguments":
        arguments = {"title": "different-note"}
    elif change == "client":
        registry._clients["fixture"] = copy.copy(registry._clients["fixture"])
    elif change == "url":
        registry._configs[0].url = "https://other-fixture.invalid/mcp"
    elif change == "headers":
        registry._configs[0].headers = {"Authorization": "synthetic-identity-only"}
    elif change == "live-url":
        registry._clients["fixture"].url = "https://other-fixture.invalid/mcp"
    elif change == "live-headers":
        registry._clients["fixture"].headers = {"Authorization": "synthetic-identity-only"}
    elif change == "live-session":
        registry._clients["fixture"]._http_session_id = "other-synthetic-session"
    elif change == "tool-definition":
        registry._clients["fixture"].tools[0]["description"] = "Changed synthetic operation contract"
    else:
        registry._configs[0].allowed_domains = ["changed-fixture.invalid"]
    assert requested(wrapper, arguments, session) != call_id
    assert authority.wire == []


def test_expired_owner_receipt_requests_fresh_consent(authority, monkeypatch):
    from core.mcp import policy
    _, tools = authority.configure()
    wrapper = tools["mcp_fixture_create_note"]
    arguments = {"title": "synthetic-note"}
    call_id = requested(wrapper, arguments)
    authority.pending.resolve(call_id, approved=True)
    now = policy.time.monotonic()
    monkeypatch.setattr(policy.time, "monotonic", lambda: now + 301)
    assert requested(wrapper, arguments) != call_id
    assert authority.wire == []


def test_owner_denial_is_not_a_reusable_approval(authority):
    _, tools = authority.configure()
    wrapper = tools["mcp_fixture_create_note"]
    arguments = {"title": "synthetic-note"}
    call_id = requested(wrapper, arguments)
    assert authority.pending.resolve(call_id, approved=False)
    assert not authority.pending.resolve(call_id, approved=True)
    with pytest.raises(PermissionError, match="denied"):
        invoke(wrapper, arguments)
    assert authority.wire == []


def test_returned_pending_arguments_are_separate_snapshots(authority):
    _, tools = authority.configure()
    wrapper = tools["mcp_fixture_create_note"]
    arguments = {"title": "synthetic-note", "payload": {"items": [1, 2]}}
    call_id = requested(wrapper, arguments)
    arguments["payload"]["items"].append(3)
    displayed = authority.pending.list_pending()[0]
    displayed["arguments"]["payload"]["items"].append(4)
    assert authority.pending.list_pending()[0]["arguments"]["payload"] == {"items": [1, 2]}
    assert authority.pending.list_pending()[0]["session_id"] == "owner-conversation"
    authority.pending.resolve(call_id, approved=True)
    invoke(wrapper, {"title": "synthetic-note", "payload": {"items": [1, 2]}})
    assert authority.wire[0]["params"]["arguments"]["payload"] == {"items": [1, 2]}


def test_concurrent_ordinary_retry_consumes_owner_receipt_once(authority):
    _, tools = authority.configure()
    wrapper = tools["mcp_fixture_create_note"]
    arguments = {"title": "synthetic-note"}
    call_id = requested(wrapper, arguments)
    authority.pending.resolve(call_id, approved=True)
    def retry():
        try:
            invoke(wrapper, arguments)
            return "executed"
        except ConfirmationRequired:
            return "new-consent"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: retry(), range(2)))
    assert sorted(results) == ["executed", "new-consent"]
    assert len(authority.wire) == 1


def test_canonical_router_consumes_exact_owner_retry(authority, monkeypatch):
    from core.capability_router import CapabilityRouter
    registry, _ = authority.configure()
    monkeypatch.setattr("core.mcp.registry.get_mcp_registry", lambda: registry)
    router = object.__new__(CapabilityRouter)
    router._capabilities = {}
    router.refresh_mcp_capabilities()
    arguments = {"title": "synthetic-note"}
    waiting = router.execute("mcp.fixture.create_note", arguments, session_id="owner-conversation")
    assert not waiting.success and waiting.metadata["approval_required"]
    assert waiting.metadata["session_id"] == "owner-conversation"
    authority.pending.resolve(waiting.metadata["approval_call_id"], approved=True)
    assert router.execute("mcp.fixture.create_note", arguments, session_id="owner-conversation").success
    assert len(authority.wire) == 1


def test_legacy_executor_consumes_approval_without_feedback_failure(authority):
    from core.thinking.engine import ThinkingEngine
    from core.thinking.parser import ParsedCall
    from core.thinking.scratchpad import Scratchpad
    _, tools = authority.configure()
    engine = ThinkingEngine(model=lambda messages: "Final Answer: done", tools=tools,
        use_cache=False, enable_planner=False, enable_reflector=False)
    engine._current_session_id = "owner-conversation"
    engine._taint_sources = set()
    engine._untrusted_content_seen = False
    call = ParsedCall(tool="mcp_fixture_create_note", args={"title": "synthetic-note"})
    _, error = engine._execute_call(call, Scratchpad())
    assert error == "approval_required"
    pending = authority.pending.list_pending()[0]
    assert pending["session_id"] == "owner-conversation"
    authority.pending.resolve(pending["call_id"], approved=True)
    _, error = engine._execute_call(call, Scratchpad())
    assert error is None and len(authority.wire) == 1


def test_missing_conversation_identity_cannot_consume_scoped_receipt(authority):
    _, tools = authority.configure()
    wrapper = tools["mcp_fixture_create_note"]
    arguments = {"title": "synthetic-note"}
    call_id = requested(wrapper, arguments)
    authority.pending.resolve(call_id, approved=True)
    assert requested(wrapper, arguments, "default") != call_id
    assert authority.wire == []


def test_gate_enforces_finite_pending_capacity_and_json_size():
    from core.mcp.policy import ConfirmationGate
    gate = ConfirmationGate()
    for _ in range(1024):
        with pytest.raises(ConfirmationRequired):
            gate.request(server="fixture", tool="create_note", arguments={}, kind="write")
    with pytest.raises(PermissionError, match="capacity"):
        gate.request(server="fixture", tool="create_note", arguments={}, kind="write")
    with pytest.raises(ValueError, match="size"):
        gate.request(server="fixture", tool="create_note", arguments={"title": "x" * 1048576}, kind="write")


@pytest.mark.parametrize("approved", [None, "true", "false", 1, 0])
def test_gate_requires_actual_owner_boolean(authority, approved):
    _, tools = authority.configure()
    call_id = requested(tools["mcp_fixture_create_note"], {"title": "synthetic-note"})
    with pytest.raises(ValueError, match="boolean"):
        authority.pending.resolve(call_id, approved=approved)
    assert len(authority.pending.list_pending()) == 1 and authority.wire == []
