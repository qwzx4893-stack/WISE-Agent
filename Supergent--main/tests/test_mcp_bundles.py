"""Real local protocol fixtures plus adversarial bundle data. No user accounts."""
import io
import json
from pathlib import Path
import stat
import zipfile
from unittest.mock import Mock
import pytest
from core.plugins.bundles import inspect,resolve,MAX_UPLOAD
from core.plugins.manager import PluginManager,PluginError
from core.plugins.connectors import catalog
from core.mcp.registry import MCPRegistry,build_mcp_tools


@pytest.fixture
def manager(tmp_path,monkeypatch):
    import core.secrets_store as secrets
    monkeypatch.setattr(secrets,'MEMORY_DIR',tmp_path)
    monkeypatch.setattr(secrets.SecretStore,'_instance',None)
    manager=PluginManager(tmp_path,MCPRegistry(tmp_path))
    monkeypatch.setattr(manager,'refresh',lambda *args:None)
    monkeypatch.setattr(manager,'search_directory',lambda *args:None)
    monkeypatch.setattr(manager,'_included',Mock(side_effect=AssertionError('Internal skills stay internal')))
    yield manager
    manager.registry.stop_all()


def make_bundle(update=None,extra=None):
    m=dict(manifest_version='0.3',name='fixture-bundle',version='1.0.0',description='QA protocol fixture, not a real service',author={'name':'WISE QA'},
        server=dict(type='python',entry_point='server.py',mcp_config=dict(command='python',args=['${__dirname}/server.py'],env={'QA_VALUE':'${user_config.key}'})),
        user_config={'key':dict(type='string',title='Fixture key',sensitive=True,required=True)})
    if update:update(m)
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as archive:
        archive.writestr('manifest.json',json.dumps(m))
        archive.writestr('server.py',(Path(__file__).parent/'_fake_mcp_server.py').read_bytes())
        for name,data in (extra or {}).items():archive.writestr(name,data)
    return buffer.getvalue()


def test_review_does_not_execute_and_catalog_retains_import_after_restart(manager,monkeypatch):
    monkeypatch.setattr(manager.registry,'start',Mock(side_effect=AssertionError('Inspection must not execute')))
    row=manager.import_bundle(make_bundle())
    assert row['kind']=='bundle' and row['local_execution'] and not row['installed']
    assert not manager.registry._configs
    other=PluginManager(manager.root,MCPRegistry(manager.root))
    monkeypatch.setattr(other,'refresh',lambda *args:None)
    monkeypatch.setattr(other,'search_directory',lambda *args:None)
    assert catalog(other,query='fixture-bundle')['items'][0]['id']==row['id']


def test_bundle_requires_trust_and_typed_required_setting(manager):
    row=manager.import_bundle(make_bundle())
    with pytest.raises(PluginError,match='trust'): manager.connect_service(row['id'],{'key':'fixture-value'})
    with pytest.raises(PluginError,match='Missing'): manager.connect_service(row['id'],{},True)
    assert not manager.installed


def test_actual_stdio_handshake_agent_tools_and_persisted_secret_reconnect(manager):
    row=manager.import_bundle(make_bundle())
    result=manager.connect_service(row['id'],{'key':'only-a-qa-fixture'},True)['connection']
    assert result['status']=='CONNECTED' and any(t['name']=='echo' for t in result['tools'])
    tools=build_mcp_tools(manager.registry)
    name=next(name for name in tools if name.endswith('_echo'))
    assert 'hello' in json.dumps(tools[name](text='hello'))
    assert 'only-a-qa-fixture' not in (manager.root/'plugins.json').read_text()
    assert 'only-a-qa-fixture' not in (manager.root/'mcp_servers.json').read_text()
    manager.registry.stop_all()
    reopened=PluginManager(manager.root,MCPRegistry(manager.root))
    try:
        assert reopened.connect_service(row['id'],{},True)['connection']['status']=='CONNECTED'
    finally:reopened.registry.stop_all()


@pytest.mark.parametrize('path',['../escape.py','/absolute.py','CON.txt','ads:name','folder\\bad.py','server.py/file','SERVER.py'])
def test_unsafe_bundle_paths_are_rejected(path):
    data=make_bundle(extra={path:'unsafe'})
    # On Windows ZipInfo normalizes backslashes on write. Restore a raw
    # malicious ZIP name in both headers to exercise the actual reader.
    if '\\' in path:data=data.replace(path.replace('\\','/').encode(),path.encode())
    with pytest.raises(ValueError):inspect(data)


def test_symlink_and_reserved_marker_are_rejected(manager):
    data=io.BytesIO(make_bundle())
    with zipfile.ZipFile(data,'a') as archive:
        entry=zipfile.ZipInfo('link.py');entry.external_attr=(stat.S_IFLNK|0o777)<<16
        archive.writestr(entry,'server.py')
    with pytest.raises(ValueError,match='links'):inspect(data.getvalue())
    with pytest.raises(PluginError,match='Reserved'):manager.import_bundle(make_bundle(extra={'.WISE-bundle.json':'{}'}))


@pytest.mark.parametrize('tamper',['modify','extra'])
def test_every_registry_start_rechecks_integrity_even_after_restart(manager,tamper):
    row=manager.import_bundle(make_bundle());manager.connect_service(row['id'],{'key':'qa'},True)
    manager.registry.stop_all()
    folder=manager.root/'mcp-bundles'/row['bundle_sha256']
    (folder/('server.py' if tamper=='modify' else 'injected.py')).write_text('changed',encoding='utf-8')
    registry=MCPRegistry(manager.root)
    result=registry.start(manager.installed[row['id']]['server'])
    assert not result['alive'] and 'changed' in result['error']


def test_typed_values_arrays_platform_overrides_and_runtime_constraints(manager,tmp_path):
    def update(m):
        m['user_config'].update(count=dict(type='number',title='Count',min=1,max=3,default=2),enabled=dict(type='boolean',title='Enabled'),folders=dict(type='directory',title='Folders',multiple=True,required=True))
        m['server']['mcp_config']['args']+=['${user_config.folders}']
        m['server']['mcp_config']['env']['COUNT']='${user_config.count}'
        m['compatibility']={'runtimes':{'python':'>=3.10 <4'}}
    row=manager.import_bundle(make_bundle(update))
    entry=manager._entry(row['id'])
    cfg=resolve(entry,manager.root,{'key':'fixture','folders':[str(tmp_path)]})
    assert cfg['args'][-1]==str(tmp_path.resolve()) and cfg['env']['COUNT']=='2'
    with pytest.raises(ValueError,match='allowed range'):resolve(entry,manager.root,{'key':'fixture','folders':[str(tmp_path)],'count':4})


def test_no_secret_process_args_unknown_variables_or_shell_runtime(manager):
    def secret_args(m):m['server']['mcp_config']['args'].append('${user_config.key}')
    with pytest.raises(ValueError,match='Secrets'):inspect(make_bundle(secret_args))
    def unknown(m):m['server']['mcp_config']['env']['PASSWORD']='${OTHER_PROVIDER_SECRET}'
    with pytest.raises(ValueError,match='Unknown'):inspect(make_bundle(unknown))
    def shell(m):m['server']['mcp_config']['command']='powershell'
    row=manager.import_bundle(make_bundle(shell))
    with pytest.raises(ValueError,match='runtime'):resolve(manager._entry(row['id']),manager.root,{'key':'fixture'})


def test_upload_api_requires_same_origin_and_real_binary_data(manager,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.plugin_routes as routes
    monkeypatch.setattr(routes,'get_plugin_manager',lambda:manager)
    app=FastAPI();app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.post('/api/v2/plugins/bundles',content=make_bundle()).status_code==403
        headers={'X-Wise-Action':'plugins','Content-Type':'application/octet-stream'}
        assert client.post('/api/v2/plugins/bundles',headers={**headers,'Origin':'https://evil.example'},content=make_bundle()).status_code==403
        assert client.post('/api/v2/plugins/bundles',headers=headers,content=b'broken').status_code==422
        assert client.post('/api/v2/plugins/bundles',headers=headers,content=make_bundle()).json()['kind']=='bundle'


def test_local_runtime_recipe_is_pinned_and_cannot_be_replaced(manager,monkeypatch):
    from core.plugins.local import local_rows,prepare_package
    entry=local_rows()[0];entry['recipe']['package']='attacker-package'
    with pytest.raises(ValueError,match='Unreviewed'):prepare_package(entry,manager.root,{'directory':str(manager.root)})
    with pytest.raises(PluginError,match='trust'):manager.connect_service('local-filesystem',{'directory':str(manager.root)})
    assert not manager.registry._configs


def test_dependency_install_does_not_lock_the_catalog(manager,monkeypatch):
    import threading
    started=threading.Event();release=threading.Event();errors=[]
    def prepare(*args):
        started.set();release.wait(5);raise PluginError('Fixture install declined')
    monkeypatch.setattr(manager,'_config',prepare)
    def connect():
        try:manager.connect_service('local-filesystem',{'directory':str(manager.root)},True)
        except PluginError as error:errors.append(str(error))
    thread=threading.Thread(target=connect);thread.start()
    assert started.wait(2)
    result=[]
    browse=threading.Thread(target=lambda:result.append(catalog(manager)))
    browse.start();browse.join(2)
    try:assert result and result[0]['total']==48
    finally:release.set();thread.join(5);browse.join(5)
    assert errors==['Fixture install declined']


def test_optional_numeric_and_secret_values_can_be_left_unset(manager):
    def update(m):
        m['user_config']['key']['required']=False
        m['user_config']['count']={'type':'number','title':'Count'}
        m['server']['mcp_config']['env']['COUNT']='${user_config.count}'
    row=manager.import_bundle(make_bundle(update))
    cfg=resolve(manager._entry(row['id']),manager.root,{})
    assert 'QA_VALUE' not in cfg['env'] and 'COUNT' not in cfg['env']


def test_registry_preflight_preserves_executable_spaces(manager,monkeypatch):
    from types import SimpleNamespace
    import core.mcp.registry as registry_module
    import shutil
    executable='C:/Program Files/Node/node.exe'
    lookups=[]
    monkeypatch.setattr(shutil,'which',lambda name:lookups.append(name) or executable)
    class Client:
        tools=[]
        def __init__(self,**kwargs):assert kwargs['command']==executable
        def open(self):pass
        def close(self):pass
    monkeypatch.setattr(registry_module,'MCPClient',Client)
    manager.registry.add({'name':'spaced','command':executable,'sandbox':False,'enabled':False})
    assert manager.registry.start('spaced')['alive']
    assert lookups==[executable]


def test_real_app_upload_middleware_accepts_bundles_but_not_oversized_json(monkeypatch):
    import asyncio
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    import api.server as server
    monkeypatch.setattr(server,'load_server_config',lambda:{'max_request_bytes':1024})
    async def call_next(request):return JSONResponse({'accepted':True})
    async def check(path,size,content_type):
        headers=[(b'content-length',str(size).encode()),(b'content-type',content_type.encode())]
        req=Request({'type':'http','method':'POST','path':path,'headers':headers,'scheme':'http','server':('127.0.0.1',8765),'query_string':b''})
        return (await server._request_size_limit(req,call_next)).status_code
    assert asyncio.run(check('/api/v2/plugins/bundles',2_000_000,'application/octet-stream'))==200
    assert asyncio.run(check('/api/v2/plugins/bundles',64_000_001,'application/octet-stream'))==413
    assert asyncio.run(check('/api/v2/plugins/bundles',2_000_000,'application/json'))==413
    assert asyncio.run(check('/api/v2/chat',2_000_000,'application/json'))==413


def test_windows_bundle_publish_retries_sharing_lock_without_relaxing_permissions(manager,monkeypatch):
    import core.plugins.bundles as bundles
    if bundles.os.name!='nt':pytest.skip('Windows sharing-lock retry')
    rename=Path.rename;calls=[]
    def busy_once(source,target):
        calls.append(str(source))
        if len(calls)==1:raise PermissionError('QA transient sharing lock')
        return rename(source,target)
    monkeypatch.setattr(Path,'rename',busy_once)
    monkeypatch.setattr(bundles.time,'sleep',lambda _:None)
    row=manager.import_bundle(make_bundle())
    assert row['kind']=='bundle' and len(calls)==2


def test_registry_recreates_its_owned_state_directory_after_recovery(tmp_path):
    import shutil
    folder=tmp_path/'memory';registry=MCPRegistry(folder)
    shutil.rmtree(folder)
    assert registry.add({'name':'restored','command':'python','enabled':False})['added']
    assert registry.path.is_file()
