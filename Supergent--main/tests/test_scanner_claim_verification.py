"""Bounded evidence probes for scanner claims; no live network or secrets.

The remote probes document existing behavior, not a declaration that sending
private data is acceptable. Release decisions must consult the linked review.
"""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def shell_boundary(monkeypatch, tmp_path):
    from core.security.security_gate import WindowsSecurityGate
    from core import tools_bridge, capability_router
    gate = WindowsSecurityGate(audit_log_path=tmp_path / "security.jsonl")
    monkeypatch.setattr("core.security.security_gate.get_security_gate", lambda: gate)
    monkeypatch.setattr(capability_router, "get_security_gate", lambda: gate)
    monkeypatch.setattr(tools_bridge, "WORKSPACE_DIR", tmp_path)
    router = object.__new__(capability_router.CapabilityRouter)
    router._capabilities = {}
    router._load_native_capabilities()
    return tools_bridge, router, gate


def test_b602_direct_unapproved_shell_never_starts_process(shell_boundary, monkeypatch):
    bridge, _, _ = shell_boundary
    invoked = []
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **kw: invoked.append((a, kw)))
    with pytest.raises(PermissionError):
        bridge._run_shell({"command": "echo WISE_SECURITY_PROBE"})
    assert invoked == []


def test_b602_model_cannot_approve_itself_through_arguments(shell_boundary, monkeypatch):
    bridge, router, _ = shell_boundary
    invoked = []
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **kw: invoked.append((a, kw)))
    result = router.execute("native.run_shell", {"command": "echo WISE_SECURITY_PROBE", "confirmed": True})
    assert not result.success
    assert result.metadata["security_evaluation"]["requires_confirmation"]
    assert invoked == []


@pytest.mark.parametrize("confirmed", [False, True])
def test_b602_external_content_cannot_trigger_shell_even_after_confirmation(shell_boundary, monkeypatch, confirmed):
    bridge, router, _ = shell_boundary
    invoked = []
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **kw: invoked.append((a, kw)))
    result = router.execute("native.run_shell", {"command": "echo WISE_SECURITY_PROBE"},
                            confirmed=confirmed, untrusted_content=True)
    assert not result.success
    assert result.metadata["security_evaluation"]["quarantined"]
    assert invoked == []


def test_b602_trusted_confirmed_shell_is_real_host_execution(shell_boundary):
    # Only echo; this proves the intentional trusted execution branch without
    # touching host files/settings, interpreting downloaded input, or UAC.
    _, router, _ = shell_boundary
    result = router.execute("native.run_shell", {"command": "echo WISE_SECURITY_PROBE"}, confirmed=True)
    assert result.success
    assert "WISE_SECURITY_PROBE" in result.output


@pytest.fixture
def remote_wire(monkeypatch):
    import httpx
    from core.mcp import client as module
    requests = []
    def handle(request):
        frame = json.loads(request.content)
        requests.append({"url": str(request.url), "frame": frame, "headers": dict(request.headers)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": frame["id"],
                                        "result": {"content": [{"type": "text", "text": "public docs"}]}})
    factory = httpx.Client
    monkeypatch.setattr(module, "_httpx", SimpleNamespace(Client=lambda **kw: factory(
        transport=httpx.MockTransport(handle), **kw)))
    def create(name, host):
        client = module.MCPClient(name=name, transport="http", url="https://" + host + "/mcp",
                                  allowed_domains=[host])
        client._open_http()  # Enforce actual transport policy before any call.
        client._alive = True  # No handshake network; mock call boundary only.
        return client
    return create, requests


def test_remote_mcp_transmits_only_explicit_arguments_not_ambient_context(remote_wire, monkeypatch):
    create, requests = remote_wire
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic_ambient_provider_secret")
    client = create("cloudflare-docs", "docs.mcp.cloudflare.com")
    client.call_tool("search_cloudflare_documentation", {"query": "public Workers docs"}, kind="read")
    wire = requests[0]["frame"]
    assert wire["params"] == {"name": "search_cloudflare_documentation", "arguments": {"query": "public Workers docs"}}
    assert "synthetic_ambient_provider_secret" not in json.dumps(requests)
    assert "history" not in wire["params"]


@pytest.mark.parametrize("name,host", [("context7", "mcp.context7.com"),
                                      ("cloudflare-docs", "docs.mcp.cloudflare.com"),
                                      ("owner-custom", "example.invalid")])
def test_remote_known_credentials_blocked_for_every_remote_server(remote_wire, name, host):
    from core.mcp.client import MCPError
    create, requests = remote_wire
    secret = "sk-" + "SYNTHETIC_ONLY_" * 3
    client = create(name, host)
    with pytest.raises(MCPError, match="credential arguments blocked"):
        client.call_tool("query-docs", {"query": secret}, kind="read")
    assert requests == []


@pytest.mark.parametrize("arguments", [
    {"password": "fake-password"}, {"nested": [{"refresh_token": "fake-token"}]},
    {"query": "-----BEGIN RSA " + "PRIVATE KEY-----"},
    {"query": "Bearer " + "SYNTHETIC_ONLY_" * 3},
    {"query": "ghp_" + "SYNTHETICONLY" * 3},
    {"query": "https://test-user:test-password@example.invalid"},
    {"authorization": "fake-value"}, {"private_key": "fake-value"},
    {"accessToken": "fake-value"}, {"privateKey": "fake-value"},
])
def test_remote_sensitive_payload_blocked_before_wire(remote_wire, arguments):
    from core.mcp.client import MCPError
    create, requests = remote_wire
    client = create("cloudflare-docs", "docs.mcp.cloudflare.com")
    with pytest.raises(MCPError, match="credential arguments blocked"):
        client.call_tool("search_cloudflare_documentation", arguments, kind="read")
    assert requests == []


def test_remote_public_osint_input_and_scoped_auth_headers_are_not_blocked(remote_wire):
    create, requests = remote_wire
    client = create("owner-custom", "example.invalid")
    client.headers = {"Authorization": "Bearer " + "SYNTHETIC_ONLY_" * 3}
    client.call_tool("search", {"email": "public@example.invalid", "query": "public token pricing", "shipping": "public"}, kind="read")
    assert requests[0]["headers"]["authorization"] == client.headers["Authorization"]
    assert requests[0]["frame"]["params"]["arguments"]["email"] == "public@example.invalid"


def test_remote_read_wrapper_has_no_per_call_human_approval(remote_wire, shell_boundary, monkeypatch):
    from core.mcp.registry import MCPRegistry, MCPServerConfig, build_mcp_tools
    from core.mcp.policy import ToolClassification
    create, requests = remote_wire
    client = create("cloudflare-docs", "docs.mcp.cloudflare.com")
    client.tools = [{"name": "search_cloudflare_documentation", "description": "Search public docs"}]
    config = MCPServerConfig(name="cloudflare-docs", transport="http", url=client.url,
                             allowed_domains=["docs.mcp.cloudflare.com"], require_confirmation=True)
    registry = object.__new__(MCPRegistry)
    registry._configs = [config]
    registry._clients = {config.name: client}
    registry._classifications = {config.name: {client.tools[0]["name"]: ToolClassification(
        name=client.tools[0]["name"], kind="read")}}
    monkeypatch.setattr("core.mcp.oauth.refresh_if_needed", lambda *a: False)
    tools = build_mcp_tools(registry, auto_start=False)
    result = tools["mcp_cloudflare-docs_search_cloudflare_documentation"](query="PRIVATE_FIXTURE_PROSE")
    assert result["content"][0]["text"] == "public docs"
    assert "PRIVATE_FIXTURE_PROSE" in json.dumps(requests)
    # Exercise the central router too: a read-class remote call can carry an
    # external-content taint; the firewall blocks host commands, not prose
    # egress. This is the remaining owner/privacy boundary, not a sandbox.
    _, router, _ = shell_boundary
    monkeypatch.setattr("core.mcp.registry.get_mcp_registry", lambda: registry)
    router.refresh_mcp_capabilities()
    call = router.execute("mcp.cloudflare-docs.search_cloudflare_documentation",
                          {"query": "PRIVATE_FIXTURE_PROSE"}, untrusted_content=True)
    assert call.success
    assert len(requests) == 2


def test_remote_transport_negative_control_blocks_unapproved_domain():
    from core.mcp.client import MCPClient, MCPError
    client = MCPClient(name="probe", transport="http", url="https://example.invalid/mcp",
                        allowed_domains=["docs.mcp.cloudflare.com"])
    with pytest.raises(MCPError, match="network policy"):
        client._open_http()


def test_remote_credential_guard_rejects_uninspectable_nesting():
    from core.mcp.policy import validate_remote_arguments, SensitiveRemoteArgumentsError
    value = "public fixture"
    for _ in range(23):
        value = {"nested": value}
    with pytest.raises(SensitiveRemoteArgumentsError, match="inspection limits"):
        validate_remote_arguments(value)
