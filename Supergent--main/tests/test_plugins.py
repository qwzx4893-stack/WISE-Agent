"""MCP-first extensions: actual process lifecycle, persistence and boundaries.

Cloud OAuth is tested at the protocol boundary separately. Directory metadata
and fixture tools are never evidence that every public service works.
"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.plugins.catalog import from_registry, identifier, public_https
from core.plugins.manager import PluginManager, PluginError
from core.mcp.registry import MCPRegistry


@pytest.fixture
def manager(tmp_path, monkeypatch):
    import core.secrets_store as secrets
    monkeypatch.setattr(secrets, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(secrets.SecretStore, "_instance", None)
    registry = MCPRegistry(tmp_path)
    service = PluginManager(tmp_path, registry)
    monkeypatch.setattr(service, "refresh", lambda *args: None)
    monkeypatch.setattr(service, "_included", lambda: [])
    yield service
    for row in registry.list_servers(): registry.stop(row["name"])


def local(manager, name="QA_process"):
    doc = {"mcpServers":{name:{"command":sys.executable, "args":[str(Path(__file__).with_name("_fake_mcp_server.py"))]}}}
    return manager.import_config(json.dumps(doc))["items"][0]["id"]


def test_import_is_not_execution_and_local_trust_is_mandatory(manager):
    key = local(manager)
    assert not manager.registry.list_servers() and not manager.detail(key)["installed"]
    with pytest.raises(PluginError, match="explicit trust"): manager.connect(key)
    assert not manager.registry.list_servers()


def test_real_stdio_handshake_call_disable_restart_uninstall(manager):
    key = local(manager)
    result = manager.connect(key, trusted_local=True)
    assert result["status"] == "CONNECTED" and len(result["tools"]) == 4
    name = result["server"]
    from core.mcp.registry import build_mcp_tools
    answer = build_mcp_tools(manager.registry, auto_start=False)[f"mcp_{name}_echo"](text="Arabic العربية and English")
    assert answer["content"][0]["text"] == "Arabic العربية and English"
    manager.disable(key)
    assert manager.detail(key)["status"] == "DISABLED" and not manager.detail(key)["tools"]
    reopened = PluginManager(manager.root, manager.registry)
    reopened._included = lambda: []
    assert reopened.detail(key)["status"] == "DISABLED"
    assert reopened.connect(key, trusted_local=True)["status"] == "CONNECTED"
    reopened.disable(key, remove=True)
    assert not manager.registry.list_servers()
    assert not PluginManager(manager.root, manager.registry).installed


def test_direct_service_needs_no_broker_or_fake_tools(manager):
    data = manager.catalog(source="featured")
    assert data["total"] >= 12
    assert all(r["can_connect"] and not r["tools"] and r["status"] == "AVAILABLE" for r in data["items"])
    assert "Nango" not in json.dumps(data)


def test_imported_credentials_never_return_to_renderer_or_plain_config(manager):
    from core.secrets_store import SecretStore
    key = manager.import_config(json.dumps({"mcpServers":{"owned":{"url":"https://mcp.example.com/mcp",
        "headers":{"Authorization":"Bearer fixture_only_credential"}}}}))["items"][0]["id"]
    assert "fixture_only_credential" not in json.dumps(manager.detail(key))
    assert "fixture_only_credential" not in (manager.root/"plugin_catalog.json").read_text()
    manager.registry.start = lambda name: {"alive":False,"error":"http 401: fixture_only_credential"}
    result = manager.connect(key)
    assert result["status"] == "AUTH_REQUIRED" and not result["tools"]
    assert "fixture_only_credential" not in json.dumps(result)
    assert "fixture_only_credential" not in (manager.root/"mcp_servers.json").read_text()
    assert any(name.startswith("MCP_wise_") for name in SecretStore().list())
    manager.disable(key, remove=True)
    assert not SecretStore().list()


@pytest.mark.parametrize("url", ["http://mcp.example.com", "https://127.0.0.1/mcp", "https://localhost/mcp", "https://a.internal/mcp", "https://user:pass@mcp.example.com", "https://mcp.example.com:8888/mcp"])
def test_public_catalog_rejects_local_and_embedded_credentials(url):
    with pytest.raises(ValueError): public_https(url)


def test_catalog_parse_is_pure_without_dns(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("Metadata must not resolve DNS"))
    raw = {"server":{"name":"io.example/mcp", "version":"1.2.3", "remotes":[{"type":"streamable-http","url":"https://mcp.example.com/mcp"}]}}
    result = from_registry(raw)
    assert result["recipe"]["url"] == "https://mcp.example.com/mcp"
    assert result["source"] == "community"


@pytest.mark.parametrize("package", [
    {"identifier":"--evil", "version":"1.0.0"},
    {"identifier":"example", "version":"latest"},
    {"identifier":"example", "version":"1.0.0", "runtimeArguments":[{"value":"--eval=code"}]},
    {"identifier":"example", "version":"1.0.0", "packageArguments":[{"type":"named","name":"--bad flag"}]},
])
def test_unsupported_package_recipe_is_visible_not_executable(package):
    result = from_registry({"server":{"name":"io.example/test", "version":"1", "packages":[{"registryType":"npm","transport":{"type":"stdio"},**package}]}})
    assert result["unsupported"] and result["recipe"] is None


def test_pinned_package_recipe_uses_array_not_shell(manager, monkeypatch):
    raw = {"server":{"name":"io.example/test", "version":"1", "packages":[{"registryType":"npm","identifier":"@example/mcp","version":"1.2.3","transport":{"type":"stdio"},
        "environmentVariables":[{"name":"API_KEY","isRequired":True,"isSecret":True}],
        "packageArguments":[{"type":"named","name":"--folder","isRequired":True}]}]}}
    entry = from_registry(raw)
    manager.cache["items"] = [entry]
    def prepare(entry,root,args,env):
        assert args==['--folder','path with spaces']
        return dict(transport='stdio',command='node',args=['owned/package/index.js',*args],env=env,sandbox=False)
    monkeypatch.setattr('core.plugins.local.prepare_registry_package',prepare)
    manager.registry.start = lambda _: {"alive":False}
    manager.connect(entry["id"], {"env_0":"fixture_only_key","arg_0":"path with spaces"}, True)
    cfg = manager.registry._find(manager.installed[entry["id"]]["server"])
    assert cfg.args == ['owned/package/index.js','--folder','path with spaces']
    assert cfg.env["API_KEY"].startswith("${wise-secret:")
    assert cfg.require_confirmation and not cfg.auto_approve


def test_invalid_import_is_all_or_nothing(manager):
    with pytest.raises(PluginError): manager.import_config('{"mcpServers":{"ok":{"command":"python"},"bad":{"args":[]}}}')
    assert not manager.cache["items"] and not manager.registry.list_servers()


@pytest.mark.parametrize("headers", [{"Host":"evil.example"},{"Authorization":"${wise-secret:other_secret}"},{"Authorization":"bad\r\nInjected:yes"}])
def test_import_cannot_reuse_other_credentials_or_inject_headers(manager, headers):
    with pytest.raises(PluginError): manager.import_config(json.dumps({"mcpServers":{"bad":{"url":"https://mcp.example.com/mcp","headers":headers}}}))


def test_installed_recipe_cannot_be_silently_replaced(manager):
    key = local(manager)
    manager.connect(key, trusted_local=True)
    with pytest.raises(PluginError, match="Uninstall"): local(manager)


def test_busy_connect_is_rejected(manager):
    key = local(manager)
    lock = manager._operation(key)
    lock.acquire()
    try:
        with pytest.raises(PluginError, match="busy"): manager.connect(key, trusted_local=True)
    finally: lock.release()


def test_offline_directory_retains_cache(manager):
    manager.cache = {"time":0, "items":[from_registry({"server":{"name":"io.example/keep","version":"1"}})]}
    manager.fetch = lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline"))
    manager._sync()
    assert len(manager.cache["items"]) == 1 and manager.warning and not manager.syncing


def test_directory_paging_filter_search_and_unknown_id(manager):
    def fetch(url, params):
        n = int(params.get("cursor", 0))
        return {"servers":[{"server":{"name":f"io.example/server-{n}","version":"1"}}],"metadata":{"nextCursor":str(n+1) if n<2 else None}}
    manager.fetch = fetch
    manager.search_directory = lambda *_a: None
    manager._sync()
    assert len(manager.cache["items"]) == 3
    assert manager.catalog(source="community",query="server-1")["total"] == 1
    assert manager.catalog(source="community",limit=1)["next_offset"] == 1
    with pytest.raises(PluginError): manager.detail("missing")


def test_corrupt_extension_state_fails_closed(manager):
    (manager.root/"plugins.json").write_text("{broken")
    with pytest.raises(PluginError, match="unreadable"): PluginManager(manager.root, manager.registry)
    assert (manager.root/"plugins.json").read_text() == "{broken"


@pytest.fixture
def routes(manager, monkeypatch):
    import api.plugin_routes as routes
    monkeypatch.setattr(routes, "get_plugin_manager", lambda: manager)
    from core.plugins.store import PackageStore
    store = PackageStore(manager.root,manager)
    monkeypatch.setattr(store,"refresh",lambda *args:None)
    monkeypatch.setattr(routes,"get_package_store",lambda:store)
    app = FastAPI(); app.include_router(routes.router)
    with TestClient(app) as client: yield client


@pytest.mark.parametrize("headers", [{},{"X-Wise-Action":"plugins","Origin":"https://evil.example"},{"X-Wise-Action":"plugins","Sec-Fetch-Site":"cross-site"}])
def test_mutations_require_same_origin_human_action(routes, headers):
    assert routes.post("/api/v2/plugins/import", headers=headers, json={"document":"{}"}).status_code == 403


def test_old_broker_setup_removed_and_import_api_is_real(routes):
    assert routes.get("/api/v2/plugins/setup").status_code == 404
    result = routes.post("/api/v2/plugins/import", headers={"X-Wise-Action":"plugins"},json={"document":json.dumps({"mcpServers":{"example":{"url":"https://mcp.example.com/mcp"}}})})
    assert result.status_code == 200 and not result.json()["items"][0]["installed"]
    # A raw MCP configuration remains manageable, but is not a plugin package
    # and must NOT appear in the shop.
    assert routes.get("/api/v2/plugins?source=imported").json()["total"] == 0
    assert routes.get("/api/v2/plugins/integrations/"+result.json()["items"][0]["id"]).status_code == 200


def test_http_sse_stops_on_matching_frame_and_redacts_errors(monkeypatch):
    from core.mcp.client import MCPClient, MCPError
    import core.mcp.client as module
    real_client = httpx.Client
    def handler(request):
        return httpx.Response(200,headers={"content-type":"text/event-stream"},content=b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n')
    monkeypatch.setattr(module._httpx,"Client",lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    cli = MCPClient(name="qa",transport="http",url="https://mcp.example.com/mcp",allowed_domains=["mcp.example.com"])
    assert cli._http_post({"id":1,"method":"ping"})["result"]["ok"]
    monkeypatch.setattr(module._httpx,"Client",lambda **kw: real_client(transport=httpx.MockTransport(lambda r:httpx.Response(401,text="private_token_echo"))))
    with pytest.raises(MCPError,match="http 401") as error: cli._http_post({"id":2,"method":"ping"})
    assert "private_token_echo" not in str(error.value)


def test_registry_header_template_becomes_required_secret_field(manager):
    row = from_registry({"server":{"name":"io.example/auth","version":"1","remotes":[{"type":"streamable-http","url":"https://mcp.example.com/mcp",
        "headers":[{"name":"Authorization","value":"Bearer {service_key}"}]}]}})
    assert row["fields"][0]["id"] == "header_0_service_key" and row["fields"][0]["secret"]
    manager.cache["items"] = [row]
    with pytest.raises(PluginError,match="Missing field"): manager.connect(row["id"])
    manager.registry.start = lambda _: {"alive":False}
    manager.connect(row["id"],{"header_0_service_key":"fixture_only_secret"})
    cfg = manager.registry._find(manager.installed[row["id"]]["server"])
    from core.mcp.config_import import resolve_values
    assert resolve_values(cfg.headers)["Authorization"] == "Bearer fixture_only_secret"
    assert "fixture_only_secret" not in (manager.root/"plugins.json").read_text()


def test_full_directory_search_fetches_uncached_names(manager):
    manager.fetch = lambda _url,params:{"servers":[{"server":{"name":"io.example/playwright", "version":"1"}}]}
    manager.search_directory("playwright")
    deadline = time.monotonic()+3
    while manager.searches and time.monotonic()<deadline: time.sleep(.01)
    assert any(r["publisher_id"]=="io.example/playwright" for r in manager.cache["items"])


def test_remove_uninstalled_import_removes_its_owned_secret_and_card(manager):
    from core.secrets_store import SecretStore
    key = manager.import_config(json.dumps({"mcpServers":{"owned":{"url":"https://mcp.example.com/mcp","headers":{"Authorization":"fixture_only_secret"}}}}))["items"][0]["id"]
    assert SecretStore().list()
    manager.disable(key,remove=True)
    assert not SecretStore().list()
    with pytest.raises(PluginError): manager.detail(key)


def test_connected_tools_enter_existing_capability_router(manager, monkeypatch):
    from core.capability_router import CapabilityRouter
    import core.mcp as mcp
    key = local(manager)
    detail = manager.connect(key,trusted_local=True)
    monkeypatch.setattr(mcp,"get_registry",lambda:manager.registry)
    router = CapabilityRouter()
    router.refresh_mcp_capabilities()
    cap = router.get_capability(f"mcp.{detail['server']}.echo")
    assert cap and cap.runtime_status == "CONNECTED" and callable(cap.execution_adapter)
    assert cap.execution_adapter({"text":"routed"})["content"][0]["text"] == "routed"
    manager.disable(key)
    router.refresh_mcp_capabilities()
    assert router.get_capability(cap.id) is None


@pytest.mark.parametrize("content", [b"",b"<svg><script>x</script></svg>",b"<svg onload='x'/>",b"<svg><foreignObject/></svg>",b"x"*256001],ids=["empty","script","event","foreign-object","oversized"])
def test_brand_artwork_is_bounded_and_active_content_rejected(manager, monkeypatch, content):
    import core.plugins.manager as module
    import core.plugins.logos as logos
    import core.paths as paths
    monkeypatch.setattr(paths,'MEMORY_DIR',manager.root)
    monkeypatch.setattr(module,"get_plugin_manager",lambda:manager)
    real = httpx.Client
    monkeypatch.setattr(logos.httpx,"Client",lambda **kw:real(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=content))))
    logos._cache.clear()
    with pytest.raises((ValueError,OSError)): logos.load_public_logo(manager._entry('figma')['logo'])
    safe,media=logos.load_logo('figma')
    assert media=='image/svg+xml' and b'<script' not in safe and b'onload' not in safe and b'foreignObject' not in safe


def test_brand_artwork_uses_catalog_identity_and_cache_not_url_proxy(manager, monkeypatch):
    import core.plugins.manager as module
    import core.plugins.logos as logos
    import core.paths as paths
    monkeypatch.setattr(paths,'MEMORY_DIR',manager.root)
    monkeypatch.setattr(module,"get_plugin_manager",lambda:manager)
    calls=[]
    def handler(r):
        calls.append(str(r.url))
        return httpx.Response(200,content=b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    real=httpx.Client
    monkeypatch.setattr(logos.httpx,"Client",lambda **kw:real(transport=httpx.MockTransport(handler)))
    logos._cache.clear()
    first=logos.load_logo("figma")
    assert first == logos.load_logo("figma") and len(calls)==1
    with pytest.raises(PluginError): logos.load_logo("../../secret.svg")


def test_only_reviewed_context7_lookup_methods_get_read_override(manager):
    cfg = manager._config(manager._entry("context7"),{},False)
    assert cfg["classifications"] == {"resolve-library-id":"read","query-docs":"read"}
    assert "classifications" not in manager._config(manager._entry("github"),{},False)


def test_dead_local_process_is_not_shown_as_connected(manager):
    key = local(manager)
    detail = manager.connect(key,trusted_local=True)
    cli = manager.registry.alive_clients()[detail["server"]]
    cli._proc.terminate();cli._proc.wait(timeout=5)
    result=manager.detail(key)
    assert result["status"] != "CONNECTED" and not result["tools"]


def test_invalid_config_error_cannot_echo_submitted_credentials(manager):
    with pytest.raises(PluginError) as error:
        manager.import_config(json.dumps({"mcpServers":{"":{"url":"https://mcp.example.com/mcp","headers":{"Authorization":"fixture_only_hidden_credential"}}}}))
    assert "fixture_only_hidden_credential" not in str(error.value)


def test_directory_read_retry_is_bounded_and_same_host(monkeypatch):
    import core.plugins.catalog as module
    calls=[]
    def download(url,params,*,address):
        calls.append((url,address))
        if address==0: raise httpx.ReadTimeout("fixture timeout")
        return {"servers":[]}
    monkeypatch.setattr(module,"_download_json",download)
    assert module.bounded_json(module.REGISTRY)=={"servers":[]}
    assert calls==[(module.REGISTRY,0),(module.REGISTRY,1)]


def test_existing_mcp_is_reusable_without_duplicate_connection_or_credential_changes(manager,monkeypatch):
    manager.registry.add({"name":"old_context7","transport":"http","url":"https://mcp.context7.com/mcp",
        "enabled":False,"headers":{"Authorization":"${wise-secret:owner_existing_key}"}})
    manager.registry.add({"name":"existing_local","command":"python","enabled":False})
    detail=manager.detail("context7")
    assert detail["managed_elsewhere"] and detail["status"]=="DISABLED" and detail["can_connect"]
    assert manager.catalog(source="installed")["total"] == 2
    started=[]
    monkeypatch.setattr(manager.registry,'start',lambda name:started.append(name) or {'alive':False})
    manager.connect("context7")
    assert started==['old_context7'] and not manager.installed
    with pytest.raises(PluginError,match='shared connection'): manager.connect('context7',{'header_0':'different'})
    with pytest.raises(PluginError,match='not installed'): manager.disable('context7',True)
    assert len(manager.registry.list_servers())==2
    assert manager.registry._find("old_context7").headers["Authorization"]=="${wise-secret:owner_existing_key}"
