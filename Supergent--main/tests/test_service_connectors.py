"""Service connection UX contracts; account consent is a protocol fixture."""
import json
from unittest.mock import Mock
from pathlib import Path
import pytest
from core.plugins.connectors import catalog, service_rows
from core.plugins.manager import PluginManager, PluginError
from core.mcp.registry import MCPRegistry


@pytest.fixture
def manager(tmp_path,monkeypatch):
    import core.secrets_store as secrets
    monkeypatch.setattr(secrets,'MEMORY_DIR',tmp_path)
    monkeypatch.setattr(secrets.SecretStore,'_instance',None)
    manager=PluginManager(tmp_path,MCPRegistry(tmp_path))
    monkeypatch.setattr(manager,'refresh',lambda *args:None)
    monkeypatch.setattr(manager,'search_directory',lambda *args:None)
    monkeypatch.setattr(manager,'_included',Mock(side_effect=AssertionError('Internal inventory must stay internal')))
    return manager


def test_service_catalog_is_large_reviewed_and_download_free(manager,monkeypatch):
    monkeypatch.setattr(manager,'fetch',Mock(side_effect=AssertionError('No synchronous download while browsing')))
    result=catalog(manager)
    assert result['total']==48 and len({row['id'] for row in result['items']})==48
    assert all(not row['tools'] for row in result['items'])
    assert all(not row['download_required'] for row in result['items'] if row['connection_type']=='cloud')
    assert all(row['docs_url'].startswith('https://') for row in result['items'])
    assert all(row['kind'] in {'remote','local-app','local-package'} for row in result['items'])
    assert not any(row['id'].startswith(('pkg_','native_','skill_','mcp_')) for row in result['items'])
    assert {'canva','dropbox','miro','netlify','airtable','figma-desktop','github'} <= {row['id'] for row in result['items']}
    assert not (manager.root/'plugin-package-cache').exists()


def test_service_search_categories_and_paging(manager):
    assert catalog(manager,query='Canva')['items'][0]['id']=='canva'
    assert all(row['category']=='Design' for row in catalog(manager,category='Design')['items'])
    first=catalog(manager,limit=2);second=catalog(manager,offset=first['next_offset'],limit=2)
    assert len(first['items'])==2 and first['items'][0]['id']!=second['items'][0]['id']


def test_no_connection_is_started_by_browsing(manager,monkeypatch):
    monkeypatch.setattr(manager.registry,'start',Mock(side_effect=AssertionError('Only user Connect can start')))
    catalog(manager);manager.detail('figma-desktop');manager.detail('notion')
    assert not manager.installed and not manager.registry._configs


def test_one_connect_action_advances_to_oauth_without_download(manager,monkeypatch):
    monkeypatch.setattr(manager.registry,'start',lambda name:{'alive':False,'error':'HTTP 401'})
    authorize=Mock(return_value={'id':'fixture_oauth','integration':'notion','status':'PENDING','browser_opened':True})
    monkeypatch.setattr(manager,'authorize',authorize)
    result=manager.connect_service('notion')
    assert result['connection']['status']=='AUTH_REQUIRED'
    assert result['authorization']['status']=='PENDING'
    authorize.assert_called_once_with('notion')
    assert len(manager.registry._configs)==1 and not (manager.root/'plugin-package-cache').exists()


def test_oauth_rejection_never_claims_connected_or_loses_retry(manager,monkeypatch):
    monkeypatch.setattr(manager.registry,'start',lambda name:{'alive':False,'error':'HTTP 401'})
    monkeypatch.setattr(manager,'authorize',Mock(side_effect=PluginError('Registered client required',409)))
    result=manager.connect_service('canva')
    assert result['connection']['status']=='AUTH_REQUIRED' and not result['connection']['tools']
    assert result['authorization_error']=='Registered client required'
    assert manager.detail('canva')['installed']


def test_local_app_uses_existing_process_without_install_or_launch(manager,monkeypatch):
    with pytest.raises(PluginError,match='local application'): manager.connect_service('figma-desktop')
    assert not manager.registry._configs
    monkeypatch.setattr(manager.registry,'start',lambda name:{'alive':False,'error':'connection refused'})
    result=manager.connect_service('figma-desktop',trusted_local=True)
    cfg=manager.registry._configs[0]
    assert cfg.transport=='http' and cfg.url=='http://127.0.0.1:3845/mcp'
    assert cfg.allowed_domains==['127.0.0.1'] and not cfg.command
    assert not cfg.auto_approve and cfg.require_confirmation
    assert result['connection']['status']=='FAILED' and result['authorization'] is None
    malicious=next(r for r in service_rows() if r['id']=='figma-desktop')
    malicious['recipe']['url']='http://127.0.0.1:8080/admin'
    with pytest.raises(PluginError,match='Unreviewed'):manager._config(malicious,{},True)


def test_service_connect_rejects_internal_and_package_ids(manager):
    for key in ('pkg_abcd','skill_internal','existing_other','mcp_any'):
        with pytest.raises(PluginError,match='Service not found'):manager.connect_service(key)


def test_token_service_requires_real_credentials_and_hides_them(manager,monkeypatch):
    with pytest.raises(PluginError,match='Missing field'):manager.connect_service('postman')
    monkeypatch.setattr(manager.registry,'start',lambda name:{'alive':False,'error':'HTTP 401'})
    monkeypatch.setattr(manager,'authorize',Mock(side_effect=AssertionError('Token service does not launch OAuth')))
    result=manager.connect_service('postman',{'header_0':'fixture_only_not_a_real_key'})
    assert result['connection']['status']=='AUTH_REQUIRED' and not result['authorization']
    assert 'fixture_only_not_a_real_key' not in json.dumps(result)
    assert 'fixture_only_not_a_real_key' not in (manager.root/'mcp_servers.json').read_text()


def test_primary_store_uses_services_and_retains_explicit_packages_route():
    source=(Path(__file__).resolve().parents[1]/'ui/wise_web/plugins.js').read_text(encoding='utf-8')
    assert 'mode:"services"' in source and "'/connections'" in source
    assert 'submitServiceConnection' in source and 'service-disconnect' in source
    assert '#plugins/packages' in source and 'publisher avatar' not in source.lower()


def test_existing_service_can_authorize_without_owning_or_replacing_it(manager,monkeypatch):
    from core.mcp import oauth
    manager.registry.add({'name':'owner_notion','transport':'http','url':'https://mcp.notion.com/mcp',
        'headers':{'Authorization':'${wise-secret:owner_key}'},'enabled':False})
    start=Mock(return_value={'requires_auth':False,'message':'Public endpoint'})
    monkeypatch.setattr(oauth,'start',start)
    assert manager.authorize('notion')['status']=='PUBLIC'
    start.assert_called_once_with('owner_notion',manager.registry,client_id='')
    assert not manager.installed and manager.registry._find('owner_notion').headers['Authorization']=='${wise-secret:owner_key}'


def test_service_api_preserves_same_origin_and_bounded_request_contract(manager,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.plugin_routes as routes
    monkeypatch.setattr(routes,'get_plugin_manager',lambda:manager)
    app=FastAPI();app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get('/api/v2/plugins/connections').json()['total']==48
        assert client.post('/api/v2/plugins/connections/notion/connect',json={}).status_code==403
        headers={'X-Wise-Action':'plugins'}
        assert client.post('/api/v2/plugins/connections/notion/connect',headers={**headers,'Origin':'https://evil.example'},json={}).status_code==403
        assert client.post('/api/v2/plugins/connections/notion/connect',headers=headers,json={'command':'arbitrary'}).status_code==422
        assert client.post('/api/v2/plugins/connections/pkg_arbitrary/connect',headers=headers,json={}).status_code==404
        assert not manager.installed
