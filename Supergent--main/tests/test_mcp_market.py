"""Registry discovery is metadata, never an execution or publisher trust grant."""
from pathlib import Path
from unittest.mock import Mock
import pytest
from core.plugins.market import registry_row,fetch_snapshot,seed_rows
from core.plugins.connectors import catalog
from core.plugins.manager import PluginManager,PluginError
from core.mcp.registry import MCPRegistry

def listing(name='io.github.example/search',**extra):
    return {'server':{'name':name,'version':'1.0.0','title':'Community search','description':'Search public documentation.',
        'remotes':[{'type':'streamable-http','url':'https://mcp.example.com/mcp'}],**extra}}

@pytest.fixture
def manager(tmp_path,monkeypatch):
    import core.secrets_store as secrets
    monkeypatch.setattr(secrets,'MEMORY_DIR',tmp_path);monkeypatch.setattr(secrets.SecretStore,'_instance',None)
    manager=PluginManager(tmp_path,MCPRegistry(tmp_path))
    monkeypatch.setattr(manager,'refresh',lambda *args:None);monkeypatch.setattr(manager,'search_directory',lambda *args:None)
    monkeypatch.setattr(manager,'_included',Mock(side_effect=AssertionError('Internal inventory stays internal')))
    return manager

def test_registry_and_reviewed_connections_share_market_without_execution(manager,monkeypatch):
    row=registry_row(listing());manager.cache['items']=[row]
    monkeypatch.setattr(manager.registry,'start',Mock(side_effect=AssertionError('Browsing cannot execute')))
    assert catalog(manager,category='All MCPs')['total']==49
    community=catalog(manager,query='io.github.example')['items'][0]
    assert community['source']=='community' and community['can_connect'] and community['registry_listing']
    assert community['category']=='Research' and not community['tools']
    assert manager.detail(row['id'])['publisher']=='io.github.example/search' and not manager.installed

def test_community_connection_requires_its_own_oauth(manager,monkeypatch):
    row=registry_row(listing());manager.cache['items']=[row]
    monkeypatch.setattr(manager.registry,'start',lambda *args:{'alive':False,'error':'HTTP 401'})
    auth=Mock(return_value={'status':'PENDING','browser_opened':False});monkeypatch.setattr(manager,'authorize',auth)
    result=manager.connect_service(row['id'])
    assert result['connection']['status']=='AUTH_REQUIRED' and not result['connection']['tools']
    auth.assert_called_once_with(row['id'])
    assert manager.registry._configs[0].allowed_domains==['mcp.example.com']

def test_unsupported_recipe_stays_visible_but_cannot_execute(manager):
    row=registry_row(listing(remotes=[{'type':'sse','url':'https://mcp.example.com/sse'}]));manager.cache['items']=[row]
    assert not catalog(manager,query='Community')['items'][0]['can_connect']
    with pytest.raises(PluginError):manager.connect_service(row['id'])
    assert not manager.registry._configs

@pytest.mark.parametrize('url',['javascript:alert(1)','https://127.0.0.1/logo','https://user:password@example.com/','http://example.com/'])
def test_source_links_cannot_inject_local_or_credential_urls(url):
    row=registry_row(listing(websiteUrl=url,repository={'url':url}))
    assert not row['website_url'] and not row['repository_url']
    assert row['docs_url'].startswith('https://registry.modelcontextprotocol.io/')

def test_snapshot_marks_partial_and_drops_deprecated_entries():
    active=listing();deleted=listing('io.github.example/deleted');deleted['_meta']={'io.modelcontextprotocol.registry/official':{'status':'deleted'}}
    partial=fetch_snapshot(lambda *a,**kw:{'servers':[active,deleted],'metadata':{'nextCursor':'next'}},maximum=1)
    assert not partial['complete'] and len(partial['servers'])==1 and registry_row(deleted) is None

def test_full_refresh_removes_stale_discovery_but_preserves_manual_import(manager):
    old=registry_row(listing('io.github.example/old'));manager.cache['items']=[old,{'id':'manual','source':'imported'}]
    manager.fetch=lambda *a,**kw:{'servers':[listing('io.github.example/current')],'metadata':{}}
    manager._sync()
    assert old['id'] not in {r['id'] for r in manager.cache['items']}
    assert 'manual' in {r['id'] for r in manager.cache['items']}

def test_local_registry_package_uses_isolated_install_with_consent(manager,monkeypatch):
    row=registry_row(listing(remotes=[],packages=[{'registryType':'npm','identifier':'example-mcp','version':'1.0.0','transport':{'type':'stdio'}}]));manager.cache['items']=[row]
    with pytest.raises(PluginError,match='trust'):manager.connect_service(row['id'])
    install=Mock(return_value={'transport':'stdio','command':'node','args':['owned/index.js'],'env':{},'sandbox':False})
    monkeypatch.setattr('core.plugins.local.prepare_registry_package',install)
    monkeypatch.setattr(manager.registry,'start',lambda *a:{'alive':False,'error':'fixture'})
    manager.connect_service(row['id'],trusted_local=True)
    install.assert_called_once();assert manager.registry._configs[0].command=='node'

def test_logo_missing_is_neutral_not_fake_brand(manager,monkeypatch):
    import core.plugins.manager as managers
    from core.plugins.logos import load_logo
    row=registry_row(listing(title='Example project'));manager.cache['items']=[row]
    monkeypatch.setattr(managers,'get_plugin_manager',lambda:manager)
    content,media=load_logo(row['id'])
    assert media=='image/svg+xml' and b'EP' in content and b'github' not in content.lower()

def test_disk_artwork_survives_memory_cache_reset_without_network(tmp_path,monkeypatch):
    import core.paths as paths
    import core.plugins.logos as logos
    monkeypatch.setattr(paths,'MEMORY_DIR',tmp_path);logos._cache.clear()
    url='https://example.com/logo.svg';path=logos.disk_path(url)
    content=b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20"/></svg>';path.write_bytes(content)
    monkeypatch.setattr(logos.httpx,'Client',Mock(side_effect=AssertionError('Disk cache cannot require network')))
    assert logos.load_public_logo(url)==(content,'image/svg+xml')

def test_market_ui_named_without_local_bundle_button():
    root=Path(__file__).resolve().parents[1]/'ui/wise_web'
    html=(root/'index.html').read_text(encoding='utf-8');js=(root/'plugins.js').read_text(encoding='utf-8')
    assert '<span>MCP Market</span>' in html
    assert 'data-plugin-action="bundle-import"' not in js and 'id="pluginBundleFile"' not in js
    assert 'refineMarketDetail' in js and 'market-facts' in js and 'Browse all' in js

def test_seed_large_and_from_registry_metadata_not_synthetic_tools():
    rows=seed_rows();assert len(rows)>=1000
    assert all(r['registry_listing'] and r['source']=='community' for r in rows)
    assert any(r['publisher_id'].startswith('io.github.') for r in rows)
    assert all(not r.get('tools') for r in rows)

def test_legacy_cached_packages_keep_their_local_connection_type(manager):
    from core.plugins.catalog import from_registry
    row=from_registry(listing(remotes=[],packages=[{'registryType':'npm','identifier':'example-mcp','version':'1.0.0','transport':{'type':'stdio'}}]))
    manager.cache['items']=[row]
    item=catalog(manager,query='Community')['items'][0]
    assert item['connection_type']=='local-package' and item['local_execution']

def test_atomic_state_publish_recovers_windows_lock_without_partial_write(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import core.atomic_io as atomic
    source=tmp_path/'new.json';target=tmp_path/'state.json'
    source.write_text('new');target.write_text('old')
    real=atomic.os.replace;calls=[];delays=[]
    def replace(src,dst):
        calls.append(True)
        if len(calls)==1:
            assert target.read_text()=='old'
            error=PermissionError('sharing lock');error.winerror=5;raise error
        return real(src,dst)
    monkeypatch.setattr(atomic,'os',SimpleNamespace(name='nt',replace=replace))
    monkeypatch.setattr(atomic.time,'sleep',delays.append)
    atomic.replace_file(source,target)
    assert target.read_text()=='new' and len(calls)==2 and delays==[.05]
