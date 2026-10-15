"""Offline model-control rejection contracts, not live server acceptance.

Only an HTTP MockTransport with disposable in-memory state is used.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from core.mcp import client as client_module
from core.mcp.policy import ConfirmationGate, ConfirmationRequired, ToolClassifier
from core.mcp.registry import MCPRegistry, MCPServerConfig, build_mcp_tools
from core.security.security_gate import ActionTier, WindowsSecurityGate
from core.thinking.engine import ThinkingEngine


@pytest.fixture
def authority(tmp_path, monkeypatch):
    from core import capability_router
    from core.security import security_gate
    from core.mcp import registry as registry_module

    wire = []
    state = {"notes": [], "repository_exists": True}
    pending = ConfirmationGate()
    gate = WindowsSecurityGate(audit_log_path=tmp_path / "audit.jsonl")
    monkeypatch.setattr(security_gate, "get_security_gate", lambda: gate)
    monkeypatch.setattr(capability_router, "get_security_gate", lambda: gate)
    monkeypatch.setattr(registry_module, "get_gate", lambda: pending)
    monkeypatch.setattr("core.mcp.oauth.refresh_if_needed", lambda *args: False)
    factory = httpx.Client

    def accepting_fixture(request):
        frame = json.loads(request.content)
        assert request.url.host == "authority-fixture.invalid"
        wire.append(frame)
        tool = frame["params"]["name"]
        arguments = frame["params"]["arguments"]
        if tool == "create_note":
            state["notes"].append(arguments["title"])
        elif tool == "delete_repo":
            assert arguments["name"] == "synthetic-repository"
            state["repository_exists"] = False
        else:
            raise AssertionError("Unexpected synthetic tool")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": frame["id"],
            "result": {"content": [{"type": "text", "text": "synthetic operation accepted"}]}})

    def offline_client(**options):
        # Production supplies its checked public transport. Replace only the
        # transport in this fixture; no socket/DNS/model/account is contacted.
        options.pop("transport", None)
        return factory(transport=httpx.MockTransport(accepting_fixture), **options)

    monkeypatch.setattr(client_module, "_httpx", SimpleNamespace(Client=offline_client))

    def configure(*, auto=(), required=True):
        client = client_module.MCPClient(name="fixture", transport="http",
            url="https://authority-fixture.invalid/mcp", allowed_domains=["authority-fixture.invalid"])
        client._open_http()  # Actual allowlist validation, no handshake or connection.
        client._alive = True
        client.tools = [{"name": "create_note", "description": "Create a synthetic note"},
                        {"name": "delete_repo", "description": "Delete a synthetic repository"}]
        config = MCPServerConfig(name="fixture", transport="http", url=client.url,
            allowed_domains=client.allowed_domains, auto_approve=list(auto), require_confirmation=required)
        registry = object.__new__(MCPRegistry)
        registry._configs = [config]
        registry._clients = {config.name: client}
        classifier = ToolClassifier(auto_approve=list(auto))
        registry._classifications = {config.name: {
            tool["name"]: classifier.classify(tool) for tool in client.tools}}
        return registry, build_mcp_tools(registry, auto_start=False)

    return SimpleNamespace(gate=gate, pending=pending, wire=wire, state=state, configure=configure)


def arguments_for(tool):
    return {"title": "synthetic-note"} if tool == "create_note" else {"name": "synthetic-repository"}


def legacy_run(wrappers, tool, arguments, *, tainted=False):
    replies = []
    tools = dict(wrappers)
    if tainted:
        tools["web_search"] = lambda **args: "SYNTHETIC_EXTERNAL_INSTRUCTION"
        replies.append('Action: web_search\nAction Input: {"query":"synthetic query"}')
    replies.extend(["Action: mcp_fixture_" + tool + "\nAction Input: " + json.dumps(arguments),
                    "Final Answer: synthetic verification complete"])
    responses = iter(replies)
    engine = ThinkingEngine(model=lambda messages: next(responses), tools=tools, max_steps=len(replies),
        use_cache=False, enable_planner=False, enable_reflector=False)
    return engine, engine.run("Inspect the synthetic fixture; no owner approval record exists.")


@pytest.mark.parametrize("tool", ["create_note", "delete_repo"])
@pytest.mark.parametrize("tainted", [False, True])
def test_prefixed_legacy_model_confirm_is_rejected_before_transport(authority, tool, tainted):
    _, wrappers = authority.configure()
    assert wrappers["mcp_fixture_" + tool].__mcp_meta__["require_confirmation"]
    assert not wrappers["mcp_fixture_" + tool].__mcp_meta__["auto_approved"]
    engine, result = legacy_run(wrappers, tool, {**arguments_for(tool), "confirm": True}, tainted=tainted)
    assert result.answer == "synthetic verification complete"
    assert authority.wire == []
    assert authority.pending.list_pending() == []
    assert authority.gate.get_action_tier("mcp_fixture_" + tool) == ActionTier.MODERATE
    if tainted:
        assert engine._untrusted_content_seen
    assert authority.state == {"notes": [], "repository_exists": True}


@pytest.mark.parametrize("tool", ["create_note", "delete_repo"])
@pytest.mark.parametrize("flag", [None, False])
def test_required_operation_without_true_flag_stops_before_transport(authority, tool, flag):
    _, wrappers = authority.configure()
    arguments = arguments_for(tool)
    if flag is not None:
        arguments["confirm"] = flag
    legacy_run(wrappers, tool, arguments)
    assert authority.wire == []
    assert authority.state == {"notes": [], "repository_exists": True}
    assert len(authority.pending.list_pending()) == (1 if flag is None else 0)


@pytest.mark.parametrize("tool", ["create_note", "delete_repo"])
def test_canonical_model_confirm_flag_is_rejected_before_transport(authority, monkeypatch, tool):
    from core import capability_router
    registry, _ = authority.configure()
    monkeypatch.setattr("core.mcp.registry.get_mcp_registry", lambda: registry)
    router = object.__new__(capability_router.CapabilityRouter)
    router._capabilities = {}
    router.refresh_mcp_capabilities()
    result = router.execute("mcp.fixture." + tool, {**arguments_for(tool), "confirm": True})
    assert not result.success
    assert "approval" in result.error.lower()
    assert authority.wire == [] and authority.pending.list_pending() == []


def test_owner_configured_automatic_write_remains_intentional(authority):
    _, wrappers = authority.configure(auto=["create_note"])
    legacy_run(wrappers, "create_note", arguments_for("create_note"), tainted=True)
    assert authority.state["notes"] == ["synthetic-note"]
    assert len(authority.wire) == 1 and authority.pending.list_pending() == []


def test_explicit_owner_disable_of_operation_confirmation_is_not_bypass(authority):
    _, wrappers = authority.configure(required=False)
    legacy_run(wrappers, "create_note", arguments_for("create_note"))
    assert len(authority.wire) == 1 and authority.pending.list_pending() == []


def test_dangerous_cannot_autoapprove_without_flag(authority):
    _, wrappers = authority.configure(auto=["delete_repo"])
    assert wrappers["mcp_fixture_delete_repo"].__mcp_meta__["kind"] == "dangerous"
    assert not wrappers["mcp_fixture_delete_repo"].__mcp_meta__["auto_approved"]
    legacy_run(wrappers, "delete_repo", arguments_for("delete_repo"), tainted=True)
    assert authority.wire == [] and authority.state["repository_exists"]
    assert len(authority.pending.list_pending()) == 1


def test_exact_privileged_action_rule_blocks_tainted_dangerous_even_with_flag(authority):
    _, wrappers = authority.configure()
    authority.gate.register_action_tier("mcp_fixture_delete_repo", ActionTier.DESTRUCTIVE)
    legacy_run(wrappers, "delete_repo", {**arguments_for("delete_repo"), "confirm": True}, tainted=True)
    assert authority.wire == [] and authority.state["repository_exists"]
    assert authority.pending.list_pending() == []


def test_http_credential_guard_still_blocks_confirmed_business_payload(authority):
    _, wrappers = authority.configure()
    legacy_run(wrappers, "create_note", {"title": "synthetic-note", "password": "synthetic-only", "confirm": True})
    assert authority.wire == [] and authority.state["notes"] == []


def test_trusted_direct_wrapper_skip_is_current_documented_programmatic_behavior(authority):
    _, wrappers = authority.configure()
    with pytest.raises(ConfirmationRequired):
        wrappers["mcp_fixture_create_note"](**arguments_for("create_note"))
    wrappers["mcp_fixture_create_note"](**arguments_for("create_note"), confirm=True)
    assert len(authority.wire) == 1 and authority.state["notes"] == ["synthetic-note"]
    # The unresolved earlier request did not approve this invocation.
    assert len(authority.pending.list_pending()) == 1
    assert not authority.pending._pending[next(iter(authority.pending._pending))].approved


@pytest.mark.parametrize("tool", ["create_note", "delete_repo"])
def test_actual_normal_mode_preserves_mcp_identity_and_rejects_model_confirmation(authority, monkeypatch, tool):
    from modes.normal import Normal, _throttle_wrap
    from core.throttle import ToolThrottle
    # Keep the actual Normal->ReAct->ThinkingEngine route, only turn off its
    # optional response cache. Never contact a model or launch a workflow.
    monkeypatch.setattr("core.thinking.engine.get_plan_cache", lambda: None)
    _, wrappers = authority.configure()
    wrapper = wrappers["mcp_fixture_" + tool]
    throttled = _throttle_wrap("mcp_fixture_" + tool, wrapper, ToolThrottle(1))
    assert throttled.__mcp_meta__ == wrapper.__mcp_meta__
    assert throttled.__wrapped__ is wrapper
    replies = iter(["Action: mcp_fixture_" + tool + "\nAction Input: " +
                    json.dumps({**arguments_for(tool), "confirm": True}),
                    "Final Answer: normal fixture done"])
    normal = Normal(model=lambda messages: next(replies), tool_registry=SimpleNamespace(tools=wrappers),
                    max_steps=2, throttle=ToolThrottle(1))
    normal.run("Synthetic Normal authority verification: " + tool)
    assert authority.wire == [] and authority.pending.list_pending() == []
    assert authority.state == {"notes": [], "repository_exists": True}


@pytest.mark.parametrize("control", [{"confirm": False}, {"confirm": True}, {"_wait": 0}, {"_wait": 1}])
@pytest.mark.parametrize("metadata", [False, True])
def test_reserved_controls_are_blocked_for_aliases_and_metadata_free_wrappers(control, metadata):
    calls = []
    def tool(**kwargs):
        calls.append(kwargs)
    name = "registered_alias" if metadata else "mcp_fixture_custom"
    if metadata:
        tool.__mcp_meta__ = {"server": "fixture", "tool": "custom"}
    engine = ThinkingEngine(model=lambda messages: "Final Answer: done", tools={name: tool},
                            use_cache=False, enable_planner=False, enable_reflector=False)
    from core.thinking.parser import ParsedCall
    from core.thinking.scratchpad import Scratchpad
    _, error = engine._execute_call(ParsedCall(tool=name, args=control), Scratchpad())
    assert error == "security" and calls == []


def test_dead_connection_stops_confirmed_model_call(authority):
    registry, wrappers = authority.configure()
    registry._clients["fixture"]._alive = False
    legacy_run(wrappers, "create_note", {**arguments_for("create_note"), "confirm": True})
    assert authority.wire == [] and authority.state["notes"] == []


def test_changed_classification_stops_confirmed_model_call(authority):
    registry, wrappers = authority.configure()
    registry._classifications["fixture"]["create_note"].kind = "dangerous"
    legacy_run(wrappers, "create_note", {**arguments_for("create_note"), "confirm": True})
    assert authority.wire == [] and authority.state["notes"] == []


def test_unapproved_destination_cannot_open_even_with_configured_client(authority):
    client = client_module.MCPClient(name="unapproved", transport="http",
        url="https://other-fixture.invalid/mcp", allowed_domains=["authority-fixture.invalid"])
    with pytest.raises(client_module.MCPError, match="network policy"):
        client._open_http()
    assert authority.wire == []
