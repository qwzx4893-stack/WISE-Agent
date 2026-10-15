"""Package marketplace contracts. Fixtures are not production catalog entries."""
import copy
import io
import json
import stat
import sys
import zipfile
from pathlib import Path
import pytest
from core.plugins.marketplace import parse_marketplace, repo_name, package_path
from core.plugins.packages import extract_package, inspect_package
from core.plugins.store import PackageStore, installed_workflows
from core.plugins.manager import PluginManager, PluginError
from core.mcp.registry import MCPRegistry

SHA = "a"*40
SOURCE = {"repo":"test-owner/marketplace"}


def package_zip(extra=None,manifest=None):
    files = {".claude-plugin/plugin.json":json.dumps(manifest or {"name":"example","version":"1.0.0"}),
        "skills/review/SKILL.md":"---\nname: review\ndescription: Review records and find mistakes\n---\nReview the user's records. See ./reference.md.",
        "skills/review/reference.md":"Keep supporting resources with the package."}
    files.update(extra or {})
    data = io.BytesIO()
    with zipfile.ZipFile(data,"w",zipfile.ZIP_DEFLATED) as z:
        for name,body in files.items(): z.writestr("repo-sha/plugins/example/"+name,body)
    return data.getvalue()


@pytest.fixture
def store(tmp_path,monkeypatch):
    import core.secrets_store as secrets
    monkeypatch.setattr(secrets,"MEMORY_DIR",tmp_path)
    monkeypatch.setattr(secrets.SecretStore,"_instance",None)
    registry = MCPRegistry(tmp_path)
    components = PluginManager(tmp_path,registry)
    monkeypatch.setattr(components,"_included",lambda:[])
    service = PackageStore(tmp_path,components,archive=lambda *args:package_zip())
    monkeypatch.setattr(service,"refresh",lambda *args:None)
    items,skipped = parse_marketplace({"name":"test-store","owner":{"name":"QA"},"plugins":[{"name":"example","source":"./plugins/example","description":"A real package","category":"testing"}]},SOURCE,SHA)
    service.cache = {"time":0,"snapshots":[{"repo":SOURCE["repo"],"name":"test-store","items":items,"skipped":skipped}]}
    yield service
    for r in registry.list_servers(): registry.stop(r["name"])


def key(store): return store.catalog()["items"][0]["id"]


def test_catalog_only_contains_packages_not_internal_tools_skills_or_mcp(store):
    data = store.catalog()
    assert data["total"]==1 and data["items"][0]["kind"]=="plugin-package"
    assert not any(k in data["items"][0] for k in ("recipe","fields","tools","skill_preview"))
    for source in ("skills","native","featured"): assert store.catalog(source=source)["total"]==0
    assert store.catalog(source="installed")["total"]==0
    assert store.catalog(query="package")["total"]==1
    assert store.catalog(query="not present")["total"]==0


def test_preview_fetches_data_but_does_not_install_or_execute(store):
    detail = store.detail(key(store))
    assert detail["plan"]["can_install"] and len(detail["plan"]["workflows"])==1
    assert not store.installed and not installed_workflows(store.root)
    assert not store.components.registry.list_servers()
    assert not detail["installed"] and detail["commit"]==SHA


def test_overview_never_fetches_or_extracts_package(store):
    def unavailable(*args): raise AssertionError('Browsing must not download archives')
    store.archive = unavailable
    item = store.overview(key(store))
    assert item['description']=='A real package' and not item['installed']
    assert item['logo']=='' and item['logo_kind']=='neutral'
    assert not (store.root/'plugin-package-cache').exists()
    assert not store.components.registry.list_servers()


def test_home_sections_are_real_catalog_packages_and_filter_reaches_all(store):
    home = store.catalog()
    assert home['sections'][0]['name']=='Development'
    assert home['sections'][0]['items'][0]['kind']=='plugin-package'
    category = store.catalog(category='Development')
    assert category['total']==1 and not category['sections']


def test_execution_agent_reads_active_installed_package_and_revokes_stale_index(store,monkeypatch,tmp_path):
    import core.paths as paths
    from core.brain.capability_agent import _read_indexed_skill
    monkeypatch.setattr(paths,'MEMORY_DIR',store.root)
    monkeypatch.setattr(paths,'SKILLS_DIR',tmp_path/'base-skills')
    store.install(key(store),SHA,True)
    name,info = next(iter(installed_workflows(store.root).items()))
    instructions = _read_indexed_skill(name,info)
    assert "Review the user's records" in instructions
    assert 'Resource directory:' in instructions
    store.disable(key(store))
    with pytest.raises(ValueError,match='not active'): _read_indexed_skill(name,info)


def test_plugin_metadata_cannot_authorize_forged_instruction_file(store,monkeypatch):
    import core.paths as paths
    from core.brain.capability_agent import _read_indexed_skill
    monkeypatch.setattr(paths,'MEMORY_DIR',store.root)
    store.install(key(store),SHA,True)
    name,info = next(iter(installed_workflows(store.root).items()))
    with pytest.raises(ValueError,match='identity changed'):
        _read_indexed_skill(name,{**info,'file':str(store.root/'not-authorized.txt')})


def test_agent_loop_actually_delivers_plugin_instructions_to_provider(store,monkeypatch):
    from types import SimpleNamespace
    import core.paths as paths
    import core.skills.indexer as indexer
    import core.skills.lifecycle as lifecycle
    from core.brain.capability_agent import CapabilityAgent
    from core.models.provider_interface import ModelCompletionResponse
    monkeypatch.setattr(paths,'MEMORY_DIR',store.root)
    monkeypatch.setattr(lifecycle,'LIFECYCLE_FILE',store.root/'unused-lifecycle.json')
    store.install(key(store),SHA,True)
    name = next(iter(installed_workflows(store.root)))
    monkeypatch.setattr(indexer.SkillIndexer,'get_index',lambda self:installed_workflows(store.root))
    class Provider:
        def __init__(self):
            self.requests=[]
            self.actions=iter([{'tool':'read_skill','arguments':{'name':name}},{'answer':'Procedure read.'}])
        def generate(self,request):
            self.requests.append(request)
            action=next(self.actions)
            return ModelCompletionResponse(text=json.dumps(action),parsed_json=action,model_name='offline-contract')
    provider=Provider()
    result=CapabilityAgent(provider,router=SimpleNamespace(list_capabilities=lambda:[]),max_steps=2).run('Read installed procedure '+name,session_id='package-contract')
    assert result.metrics['skills_read']==[name]
    assert result.calls[0]['success']
    assert any("Review the user's records" in m.get('content','') for r in provider.requests for m in r.messages)


def test_plugin_resources_are_scoped_paged_and_revoked_on_disable(store,monkeypatch):
    import core.paths as paths
    from core.brain.capability_agent import _read_plugin_resource
    monkeypatch.setattr(paths,'MEMORY_DIR',store.root)
    store.install(key(store),SHA,True)
    name,info = next(iter(installed_workflows(store.root).items()))
    result = _read_plugin_resource(name,info,'skills/review/reference.md',0,4)
    assert result['text']=='Keep' and result['next_offset']==4
    for path in ('../plugin_packages.json','C:/private.txt','skills/review/../../../outside.txt'):
        with pytest.raises(ValueError): _read_plugin_resource(name,info,path)
    store.disable(key(store))
    with pytest.raises(ValueError,match='not active'):
        _read_plugin_resource(name,info,'skills/review/reference.md')


def test_tampered_completion_marker_revokes_agent_instruction_access(store,monkeypatch):
    import core.paths as paths
    from core.brain.capability_agent import _read_indexed_skill
    monkeypatch.setattr(paths,'MEMORY_DIR',store.root)
    store.install(key(store),SHA,True)
    name,info = next(iter(installed_workflows(store.root).items()))
    (Path(info['plugin_root'])/'.wise-package.json').write_text('{}',encoding='utf-8')
    assert not installed_workflows(store.root)
    with pytest.raises(ValueError,match='not active'): _read_indexed_skill(name,info)


def test_new_default_marketplace_refreshes_without_waiting_a_day(store,monkeypatch):
    import time
    import core.plugins.store as module
    from types import SimpleNamespace
    store.cache['time']=time.time()
    store.sources=[SOURCE,{'repo':'new-owner/new-marketplace'}]
    started=[]
    monkeypatch.setattr(module.threading,'Thread',lambda **kw:SimpleNamespace(start=lambda:started.append(kw['target'])))
    PackageStore.refresh(store)
    assert store.syncing and started


def test_trust_and_reviewed_commit_required(store):
    identifier = key(store)
    with pytest.raises(PluginError,match="trust"): store.install(identifier,SHA)
    with pytest.raises(PluginError,match="changed"): store.install(identifier,"b"*40,True)
    assert not store.installed


def test_install_preserves_resources_namespaces_and_restart_state(store):
    identifier = key(store);store.install(identifier,SHA,True)
    workflows = installed_workflows(store.root)
    assert len(workflows)==1
    info = next(iter(workflows.values()))
    assert "example@" in info["name"] and info["metadata"]["plugin_id"]==identifier
    assert (Path(info["path"])/"reference.md").is_file()
    reopened = PackageStore(store.root,store.components)
    assert reopened.installed[identifier]["enabled"]
    assert reopened._present(reopened._entry(identifier))["status"]=="ENABLED"
    assert not store.components.registry.list_servers()
    store.disable(identifier)
    assert not installed_workflows(store.root)
    reopened = PackageStore(store.root,store.components)
    assert not reopened.installed[identifier]["enabled"]
    reopened.install(identifier,SHA,True)
    assert len(installed_workflows(store.root))==1
    reopened.disable(identifier,True)
    assert not installed_workflows(store.root) and not reopened.installed


def test_host_specific_hooks_not_executed_and_partial_requires_consent(store):
    store.archive = lambda *args:package_zip({"hooks/hooks.json":json.dumps({"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"never-run-this"}]}]}})})
    identifier = key(store);detail = store.detail(identifier)
    assert detail["plan"]["compatibility"]=="partial" and "Host-specific component: hooks" in detail["plan"]["unsupported"]
    with pytest.raises(PluginError,match="partial"): store.install(identifier,SHA,True)
    store.install(identifier,SHA,True,True)
    assert len(installed_workflows(store.root))==1 and not store.components.registry.list_servers()


def test_real_bundled_stdio_tool_lifecycle_is_owned_by_package(store):
    fixture = Path(__file__).with_name("_fake_mcp_server.py").read_text(encoding="utf-8")
    doc = {"mcpServers":{"sample":{"command":sys.executable,"args":["${CLAUDE_PLUGIN_ROOT}/server.py"]}}}
    store.archive = lambda *args:package_zip({"server.py":fixture,".mcp.json":json.dumps(doc)})
    identifier = key(store);detail = store.detail(identifier)
    assert len(detail["plan"]["servers"])==1 and "config" not in detail["plan"]["servers"][0]
    store.install(identifier,SHA,True)
    assert not store.components.registry.list_servers()
    with pytest.raises(PluginError,match="local code"): store.connect(identifier,"sample")
    assert not store.components.registry.list_servers()
    result = store.connect(identifier,"sample",trusted_local=True)
    assert result["status"]=="CONNECTED" and len(result["tools"])==4
    from core.mcp.registry import build_mcp_tools
    out = build_mcp_tools(store.components.registry,auto_start=False)[f"mcp_{result['server']}_echo"](text="العربية English")
    assert out["content"][0]["text"]=="العربية English"
    assert len(store.detail(identifier)["connections"][0]["tools"])==4
    store.disable(identifier)
    assert not store.components.registry._clients[result["server"]].alive if result["server"] in store.components.registry._clients else True
    assert not installed_workflows(store.root)
    store.disable(identifier,True)
    assert not store.components.registry.list_servers()


def test_remote_credentials_protected_and_base_config_not_touched(store):
    doc = {"service":{"type":"http","url":"https://mcp.example.com/mcp","headers":{"Authorization":"Bearer ${SERVICE_TOKEN}"}}}
    store.archive = lambda *args:package_zip({".mcp.json":json.dumps(doc)})
    identifier = key(store);detail = store.detail(identifier)
    assert detail["plan"]["servers"][0]["fields"][0]["id"]=="SERVICE_TOKEN"
    store.install(identifier,SHA,True)
    store.components.registry.add({"name":"base_config","transport":"http","url":"https://base.example.com/mcp","allowed_domains":["base.example.com"],"enabled":False})
    store.components.registry.start = lambda name:{"alive":False,"error":"http 401 fixture_SECRET"}
    result = store.connect(identifier,"service",{"SERVICE_TOKEN":"fixture_SECRET"})
    assert result["status"]=="AUTH_REQUIRED"
    assert "fixture_SECRET" not in json.dumps(store.detail(identifier))
    assert "fixture_SECRET" not in (store.root/"plugin_packages.json").read_text()
    assert "fixture_SECRET" not in (store.root/"plugins.json").read_text()
    store.disable(identifier,True)
    assert [r["name"] for r in store.components.registry.list_servers()]==["base_config"]


@pytest.mark.parametrize("value",["../escape","/absolute","a/../b","C:/escape","a\\b","CON","a/nul.txt","a/file. ","a//b"])
def test_package_paths_reject_escape_and_windows_aliases(value):
    with pytest.raises(ValueError): package_path(value)


@pytest.mark.parametrize("value",["https://evil.example/repo","owner/repo/extra","owner/..","ssh://git@github.com/owner/repo","https://github.com/owner/repo?token=secret"])
def test_marketplace_repo_names_are_not_arbitrary_network_locations(value):
    with pytest.raises(ValueError): repo_name(value)


def test_archive_symlink_duplicate_and_traversal_rejected(tmp_path):
    for name,symlink in [("../escape",False),("file.md",True)]:
        data = io.BytesIO()
        with zipfile.ZipFile(data,"w") as z:
            info = zipfile.ZipInfo("repo/plugins/example/"+name)
            if symlink: info.external_attr=(stat.S_IFLNK|0o777)<<16
            z.writestr(info,"outside")
        with pytest.raises(ValueError): extract_package(data.getvalue(),"plugins/example",tmp_path/name.replace("/","_"))
    data = io.BytesIO()
    with zipfile.ZipFile(data,"w") as z:
        z.writestr("repo/plugins/example/A.md","a");z.writestr("repo/plugins/example/a.md","b")
    with pytest.raises(ValueError,match="Duplicate"): extract_package(data.getvalue(),"plugins/example",tmp_path/"duplicate")


def test_marketplace_external_package_sources_and_skipped_unsupported():
    entries = [{"name":"nested","source":{"source":"git-subdir","url":"https://github.com/owner/package.git","path":"./plugins/nested","sha":"b"*40}},
        {"name":"invalid","source":{"source":"url","url":"https://private.local/repo"}},
        {"name":"bad-path","source":"./../escape"}]
    items,skipped = parse_marketplace({"plugins":entries},SOURCE,SHA)
    assert len(items)==1 and skipped==2
    assert items[0]["origin"]["sha"]=="b"*40 and items[0]["origin"]["path"]=="plugins/nested"


def test_commands_and_portable_root_manifest_are_real_workflows(tmp_path):
    files = {"plugin.json":json.dumps({"name":"example","version":"2.0.0","$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"}),
        "commands/explain.md":"---\ndescription: Explain records\n---\nExplain the records."}
    root = tmp_path/"portable";extract_package(package_zip(files),"plugins/example",root)
    plan = inspect_package(root,{"name":"example","display_name":"Example","version":"","strict":True})
    assert plan["version"]=="2.0.0" and {s["kind"] for s in plan["workflows"]}=={"commands","skills"}


def test_invalid_plugin_manifest_or_missing_component_not_faked(store):
    store.archive = lambda *args:package_zip(manifest={"name":"wrong","version":"1.0.0"})
    with pytest.raises(ValueError,match="identity"): store.detail(key(store))
    assert not store.installed


def test_api_exposes_package_catalog_and_rejects_cross_origin(store,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.plugin_routes as routes
    monkeypatch.setattr(routes,"get_package_store",lambda:store)
    app = FastAPI();app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get("/api/v2/plugins").json()["items"][0]["kind"]=="plugin-package"
        identifier = key(store)
        assert client.post(f"/api/v2/plugins/packages/{identifier}/install",json={"commit":SHA,"trusted_publisher":True}).status_code==403
        assert client.post(f"/api/v2/plugins/packages/{identifier}/install",headers={"X-Wise-Action":"plugins","Origin":"https://evil.example"},json={"commit":SHA}).status_code==403
        assert client.post(f"/api/v2/plugins/packages/{identifier}/install",headers={"X-Wise-Action":"plugins"},json={"commit":SHA,"trusted_publisher":True}).status_code==200


def test_skill_browser_overlay_and_loader_use_installed_package(store,monkeypatch):
    store.install(key(store),SHA,True)
    import core.paths as paths
    monkeypatch.setattr(paths,"MEMORY_DIR",store.root)
    from core.skills.indexer import SkillIndexer
    from core.skills.loader import SkillLoader
    indexer = SkillIndexer()
    found = next(n for n in indexer.get_index() if "example@" in n)
    content,truncated = SkillLoader().load(found)
    assert "Plugin workflow:" in content and "reference.md" in content
    store.disable(key(store))
    assert found not in indexer.get_index() and SkillLoader().load(found)==(None,False)


def test_bad_completion_marker_is_never_activated(store):
    identifier = key(store);store.detail(identifier)
    marker = store._package_dir(identifier,SHA)/".wise-package.json"
    marker.write_text("{broken",encoding="utf-8")
    with pytest.raises(PluginError,match="marker"):store.install(identifier,SHA,True)
    assert not installed_workflows(store.root)


def test_busy_package_mutations_fail_instead_of_racing(store):
    identifier = key(store);lock=store._operation(identifier);lock.acquire()
    try:
        with pytest.raises(PluginError,match="in progress"):store.install(identifier,SHA,True)
        with pytest.raises(PluginError,match="in progress"):store.detail(identifier)
    finally:lock.release()


def test_bundled_connection_can_be_reset_without_uninstalling_workflows(store):
    store.archive=lambda *args:package_zip({".mcp.json":json.dumps({"docs":{"type":"http","url":"https://mcp.example.com/mcp"}})})
    identifier=key(store);store.install(identifier,SHA,True)
    store.components.registry.start=lambda name:{"alive":False,"error":"http 401"}
    store.connect(identifier,"docs")
    assert store.installed[identifier]["connections"]
    store.reset_connection(identifier,"docs")
    assert not store.installed[identifier]["connections"] and len(installed_workflows(store.root))==1
    assert not store.components.registry.list_servers()


def test_catalog_refresh_outage_keeps_true_package_cache(store):
    store.fetch=lambda *args,**kwargs:(_ for _ in ()).throw(OSError("offline"))
    store._sync()
    assert store.catalog()["total"]==1 and store.warning and not store.syncing


def test_corrupt_package_state_fails_closed(store):
    (store.root/"plugin_packages.json").write_text("{broken",encoding="utf-8")
    with pytest.raises(PluginError,match="unreadable"):PackageStore(store.root,store.components)
    assert (store.root/"plugin_packages.json").read_text()=="{broken"


def test_failed_state_save_does_not_activate_a_package(store,monkeypatch):
    identifier=key(store)
    monkeypatch.setattr(store,"_save",lambda:(_ for _ in ()).throw(OSError("disk write failed")))
    with pytest.raises(OSError):store.install(identifier,SHA,True)
    assert not store.installed and not installed_workflows(store.root)


def test_installed_workflows_remain_available_when_new_version_cannot_download(store):
    identifier=key(store);store.install(identifier,SHA,True)
    store.cache["snapshots"][0]["items"][0]["origin"]["sha"]="b"*40
    store.archive=lambda *args:(_ for _ in ()).throw(OSError("offline"))
    detail=store.detail(identifier)
    assert detail["installed"] and detail["commit"]==SHA and detail["warning"]
    assert len(installed_workflows(store.root))==1
