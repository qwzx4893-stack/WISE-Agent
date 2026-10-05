"""Nango credential-dependent HTTP boundary only is mocked; WISE lifecycle is real."""
from datetime import datetime, timezone
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from core.integrations.provider import IntegrationError, NangoProvider
from core.integrations.service import IntegrationService


@pytest.fixture
def integration(tmp_path):
    state = {"calls":[], "remote":[], "browser":[], "fail_delete":False, "now":1_790_000_000.0, "bad_link":False}
    def handler(request):
        state["calls"].append((request.method, request.url.path, dict(request.url.params), json.loads(request.content or b"{}")))
        assert request.headers["authorization"] == "Bearer boundary-fixture-only"
        path = request.url.path
        if path == "/integrations": return httpx.Response(200,json={"data":[{"unique_key":"github-wise","provider":"github","display_name":"GitHub","client_secret":"must-not-leak"},{"unique_key":"slack-wise","provider":"slack","display_name":"Slack"}]})
        if path == "/providers": return httpx.Response(200,json={"data":[{"name":"github","display_name":"GitHub","logo_url":"https://app.nango.dev/images/template-logos/github.svg","categories":["dev-tools"],"auth_mode":"OAUTH2"},{"name":"slack","display_name":"Slack","categories":["communication"],"auth_mode":"OAUTH2"}]})
        if path.startswith("/connect/sessions"):
            state["tags"] = json.loads(request.content)["tags"]
            return httpx.Response(201,json={"data":{"token":"never-to-renderer","connect_link":"https://evil.example/?token=private" if state["bad_link"] else "https://connect.nango.dev/?session_token=never-to-renderer",
                "expires_at":datetime.fromtimestamp(state["now"]+1800, timezone.utc).isoformat()}})
        if path == "/connections": return httpx.Response(200,json={"connections":state["remote"]})
        if request.method == "DELETE":
            if state["fail_delete"]: return httpx.Response(500,json={"error":"never-to-renderer"})
            state["remote"]=[]; return httpx.Response(200,json={"success":True})
        if path == "/proxy/search/repositories": return httpx.Response(200,json={"items":[{"full_name":"qa/repo","html_url":"https://github.com/qa/repo","token":"must-not-leak"}]})
        if path == "/proxy/user/repos": return httpx.Response(200,json=[])
        if path.startswith("/proxy/repos/"): return httpx.Response(200,json={"type":"file","size":3,"name":"a.txt","encoding":"base64","content":"YWJj"})
        return httpx.Response(404,json={})
    provider=NangoProvider("boundary-fixture-only",transport=httpx.MockTransport(handler))
    service=IntegrationService(provider,tmp_path/"integrations.db",opener=lambda url,**kwargs: state["browser"].append(url) or True,clock=lambda:state["now"])
    state["service"]=service
    def authorize(attempt, errors=None, owner=None, remote="remote-1"):
        state["remote"]=[{"connection_id":remote,"provider":"github","provider_config_key":"github-wise",
            "tags":{"end_user_id":owner or service.owner,"wise_attempt_id":attempt},"metadata":{"username":"qa-account","access_token":"must-not-leak"},"errors":errors or []}]
    state["authorize"]=authorize
    return state


def test_unconfigured_is_honest_and_offline(tmp_path):
    service=IntegrationService(NangoProvider(""),tmp_path/"db")
    result=service.catalog()
    assert result["configured"] is False and result["total"] >= 1000
    assert result["items"] and all(item["catalog_only"] and not item["can_connect"] for item in result["items"])
    assert all(not item["accounts"] and not item["tools"] for item in result["items"])
    with pytest.raises(IntegrationError,match="Configure"):
        service.connect(service.catalog(query="catalog_github")["items"][0]["id"])


def test_catalog_metadata_filters_pagination_and_cache(integration):
    service=integration["service"]
    result=service.catalog(query="-wise",limit=1)
    assert result["total"]==2 and result["next_offset"]==1
    assert result["items"][0]["logo"].endswith("github.svg")
    assert "client_secret" not in json.dumps(result)
    assert service.catalog(query="Slack")["items"][0]["tools"]==[]
    assert service.catalog(query="-wise",category="dev-tools")["total"]==1
    assert len(integration["calls"])==2


def test_full_library_can_be_paged_searched_and_configured_variants_are_not_lost(integration):
    service=integration["service"]
    ids=set(); offset=0
    while True:
        page=service.catalog(offset=offset,limit=48)
        current={item["id"] for item in page["items"]}
        assert ids.isdisjoint(current)
        ids.update(current)
        if page["next_offset"] is None: break
        offset=page["next_offset"]
    assert len(ids)==page["total"] >= 1000
    assert {"github-wise","slack-wise"} <= ids
    assert "catalog_github" not in ids and "catalog_slack" not in ids
    assert service.catalog(query="notion")["total"] > 0
    detail=service.detail("catalog_notion")
    assert detail["catalog_only"] and not detail["can_connect"] and not detail["tools"]
    assert detail["logo"].endswith("notion.svg")
    assert detail["docs_url"].startswith("https://nango.dev/")


def test_remote_outage_retains_public_catalog_without_fake_connections(tmp_path):
    provider=NangoProvider("boundary-fixture-only",transport=httpx.MockTransport(lambda request:httpx.Response(503)))
    service=IntegrationService(provider,tmp_path/"db")
    result=service.catalog()
    assert result["configured"] is True and result["total"]>=1000 and result["warning"]
    assert all(not item["can_connect"] and not item["accounts"] for item in result["items"])


def test_catalog_only_connect_never_creates_authorization_attempt(integration):
    service=integration["service"]
    with pytest.raises(IntegrationError,match="Configure"):
        service.connect("catalog_notion")
    assert not service.pending()
    assert not any(path.startswith("/connect/") for _,path,_,_ in integration["calls"])


def test_environment_key_cannot_collide_with_catalog_only_identifier(integration):
    service=integration["service"]
    service.provider.catalog=lambda: ([{"unique_key":"catalog_notion","provider":"github"}], [{"name":"github"}])
    catalog=service._catalog(refresh=True)
    assert len({item["id"] for item in catalog})==len(catalog)
    actual=service.detail("catalog_notion")
    assert actual["provider"]=="github" and actual["can_connect"]
    notion=next(item for item in catalog if item["provider"]=="notion")
    assert notion["catalog_only"] and not notion["can_connect"] and notion["id"]!="catalog_notion"


def test_verified_connect_persistence_no_tokens_and_real_proxy(integration):
    service=integration["service"]
    result=service.connect("github-wise")
    assert result["browser_opened"] and "token" not in json.dumps(result)
    assert integration["calls"][-1][3]["allowed_integrations"]==["github-wise"]
    integration["authorize"](result["id"])
    connected=service.poll(result["id"])
    assert connected["status"]=="CONNECTED"
    account=connected["account"]
    restored=IntegrationService(service.provider,service.database,opener=service.opener,clock=service.clock)
    assert restored.owner==service.owner and restored.accounts()[0]["id"]==account
    assert b"never-to-renderer" not in service.database.read_bytes()
    assert b"must-not-leak" not in service.database.read_bytes()
    output=service.execute_read("search_repositories",{"account":account,"query":"test"})
    assert output["repositories"][0]["full_name"]=="qa/repo" and "token" not in json.dumps(output)
    assert service.catalog(connected=True)["total"]==1


def test_forged_completion_ownership_is_rejected(integration):
    service=integration["service"]; attempt=service.connect("github-wise")["id"]
    integration["authorize"](attempt,owner="another-owner")
    assert service.poll(attempt)["status"]=="PENDING" and service.accounts()==[]
    integration["authorize"]("different-attempt")
    assert service.poll(attempt)["status"]=="PENDING"


def test_pending_survives_restart_without_persisting_launch_token(integration):
    service=integration["service"]
    service.opener=lambda *args,**kwargs: False
    result=service.connect("github-wise")
    assert result["browser_opened"] is False
    restored=IntegrationService(service.provider,service.database,opener=service.opener,clock=service.clock)
    assert restored.pending()[0]["id"]==result["id"]
    with pytest.raises(IntegrationError,match="expired"):
        restored.launch(result["id"])
    integration["authorize"](result["id"])
    assert restored.poll(result["id"])["status"]=="CONNECTED"
    assert restored.pending()==[]


def test_file_adapter_rejects_traversal_before_proxy_request(integration):
    service=integration["service"]; attempt=service.connect("github-wise")["id"]
    integration["authorize"](attempt); account=service.poll(attempt)["account"]
    for path in ("../secret", "/etc/passwd", "a/../../secret", "a%2f..%2fsecret", "a\\secret"):
        with pytest.raises(IntegrationError,match="path"):
            service.execute_read("read_file",{"account":account,"owner":"qa","repo":"repo","path":path})
    assert not any(call[1].startswith("/proxy/") for call in integration["calls"])


def test_cancel_and_expiry_never_import_late_connections(integration):
    service=integration["service"]; attempt=service.connect("github-wise")["id"]
    assert service.cancel(attempt)["status"]=="CANCELLED"
    integration["authorize"](attempt)
    assert service.poll(attempt)["status"]=="CANCELLED" and service.accounts()==[]
    attempt=service.connect("github-wise")["id"]; integration["now"]+=1801
    integration["authorize"](attempt)
    assert service.poll(attempt)["status"]=="EXPIRED" and service.accounts()==[]


def test_authentication_failure_is_not_connected(integration):
    service=integration["service"]; attempt=service.connect("github-wise")["id"]
    integration["authorize"](attempt,errors=[{"type":"auth"}])
    assert service.poll(attempt)["status"]=="FAILED" and service.accounts()==[]


def test_remote_disconnect_failure_does_not_fake_success(integration):
    service=integration["service"]; attempt=service.connect("github-wise")["id"]
    integration["authorize"](attempt); account=service.poll(attempt)["account"]
    integration["fail_delete"]=True
    with pytest.raises(IntegrationError): service.disconnect(account)
    assert service.accounts()[0]["status"]=="CONNECTED"
    integration["fail_delete"]=False
    assert service.disconnect(account)["disconnected"] and service.accounts()==[]
    assert integration["calls"][-1][0]=="DELETE"


def test_reconnect_uses_real_endpoint_and_account(integration):
    service=integration["service"]; first=service.connect("github-wise")["id"]
    integration["authorize"](first); account=service.poll(first)["account"]
    result=service.connect("github-wise",account)
    assert integration["calls"][-1][1]=="/connect/sessions/reconnect"
    assert integration["calls"][-1][3]["connection_id"]=="remote-1"
    integration["authorize"](result["id"])
    assert service.poll(result["id"])["account"]==account


def test_expired_remote_blocks_tools_and_marks_reconnect(integration):
    service=integration["service"]; attempt=service.connect("github-wise")["id"]
    integration["authorize"](attempt); account=service.poll(attempt)["account"]
    integration["remote"][0]["errors"]=[{"type":"auth"}]
    with pytest.raises(IntegrationError): service.execute_read("list_repositories",{"account":account})
    assert service.accounts()[0]["status"]=="REAUTH_REQUIRED"


def test_safe_redirect_and_unknown_operation(integration):
    service=integration["service"]; integration["bad_link"]=True
    with pytest.raises(IntegrationError,match="allowed"): service.connect("github-wise")
    assert integration["browser"]==[]
    with pytest.raises(IntegrationError,match="reviewed"): service.execute_read("delete_repository",{})


def test_api_real_service_with_csrf_boundary(integration,monkeypatch):
    import api.plugin_routes as routes
    monkeypatch.setattr(routes,"get_integration_service",lambda:integration["service"])
    app=FastAPI(); app.include_router(routes.router); client=TestClient(app)
    assert client.get("/api/v2/plugins").status_code==200
    assert client.post("/api/v2/plugins/integrations/github-wise/connect",json={}).status_code==403
    headers={"X-Wise-Action":"plugins","Origin":"http://testserver"}
    assert client.post("/api/v2/plugins/integrations/github-wise/connect",headers={**headers,"Origin":"https://evil.example"},json={}).status_code==403
    assert client.get("/api/v2/plugins",headers={"Host":"rebind.example"}).status_code==403
    response=client.post("/api/v2/plugins/integrations/github-wise/connect",headers=headers,json={})
    assert response.status_code==200 and "token" not in response.text
    assert client.get("/api/v2/plugins/attempts/"+response.json()["id"]).json()["status"]=="PENDING"


def test_registry_uses_existing_permissions_and_only_connected_accounts(integration,monkeypatch):
    import core.integrations.service as module
    from core.capability_router import CapabilityRouter
    monkeypatch.setenv("NANGO_SECRET_KEY","boundary-fixture-only")
    monkeypatch.setattr(module,"get_integration_service",lambda:integration["service"])
    router=CapabilityRouter()
    assert not any(row.id.startswith("extension.plugins.") for row in router.list_capabilities())
    attempt=integration["service"].connect("github-wise")["id"]; integration["authorize"](attempt)
    account=integration["service"].poll(attempt)["account"]
    cap=router.get_capability("extension.plugins.search_repositories")
    assert cap.risk_level=="LOW" and cap.category=="integrations"
    result=router.execute(cap.id,{"account":account,"query":"qa"})
    assert result.success
    integration["service"].disconnect(account)
    assert router.get_capability(cap.id) is None
