"""Offline contract checks; do not substitute for live-model acceptance."""
import importlib
import json
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def messaging(monkeypatch):
    from core.channels import service_catalog as catalog
    # Earlier legacy tests deliberately replace Apprise in sys.modules.
    if not getattr(sys.modules.get("apprise"), "__file__", None):
        monkeypatch.delitem(sys.modules, "apprise", raising=False)
    actual = importlib.import_module("apprise")
    monkeypatch.setattr(catalog, "_apprise_module", lambda:actual)
    saved = []
    def register(name, url):
        assert actual.Apprise().add(url), "Installed Apprise must validate the URL"
        saved.append((name,url))
    monkeypatch.setattr(catalog, "register_channel", register)
    return catalog,saved


def test_real_messaging_schema_serializes_and_has_explicit_destinations(messaging):
    catalog,_ = messaging
    rows = catalog.services()
    assert len(rows) > 100
    json.dumps(rows)
    telegram = next(row for row in rows if row["id"] == "tgram")
    assert {field["id"] for field in telegram["fields"]} == {"bot_token","targets"}
    assert all(field["required"] for field in telegram["fields"])
    assert all(row["supports_incoming"] is False for row in rows)


@pytest.mark.parametrize("service,fields", [
    ("tgram",{"bot_token":"123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", "targets":"123456789"}),
    ("discord",{"webhook_id":"123456789012345678", "webhook_token":"A"*68}),
    ("slack",{"token_a":"T12345678", "token_b":"B12345678", "token_c":"a"*24}),
    ("whatsapp",{"token":"A"*64,"from_phone_id":"123456789", "targets":"+15551234567"}),
])
def test_connection_configuration_uses_actual_url_parser_without_sending(messaging,service,fields):
    catalog,saved = messaging
    result = catalog.configure(service,"qa_connection",fields)
    assert len(saved) == 1
    assert result["authentication_status"] == "configured_unverified"
    assert result["message_sent"] is False


@pytest.mark.parametrize("fields", [{"bot_token":"abc"}, {"bot_token":"abc","targets":"123","unknown":"1"}, {"bot_token":"abc\nInjected","targets":"123"}])
def test_connection_bad_fields_are_not_persisted(messaging,fields):
    catalog,saved = messaging
    with pytest.raises(ValueError): catalog.configure("tgram","qa",fields)
    assert not saved


def test_grouped_resource_pages_do_not_claim_execution():
    from core.capability_catalog import browse_resources
    result = browse_resources(limit=500)
    assert result["total"] >= 170 and len(result["items"]) == 20
    assert len(result["groups"]) >= 15 and result["next_offset"] == 20
    # A deployed platform must not inherit public-page availability merely
    # because a query/read adapter was added for a different resource.
    assert all(not item["available"] for item in result["items"] if item["kind"] in {"platform","model"})
    assert all(item.get("actions") for item in result["items"] if item["available"])
    website = browse_resources(query="drivenlisten.com")["items"][0]
    assert website["url"] == "https://drivenlisten.com"
    assert website["review_status"] == "AVAILABILITY_UNVERIFIED"


def test_browse_pages_have_group_filter_and_stable_continuation():
    from core.capability_catalog import _page
    rows = [{"name":str(i).zfill(2),"id":str(i),"category":"security"} for i in range(25)]
    first = _page(rows,category="security",limit=10)
    second = _page(rows,offset=first["next_offset"],limit=10)
    assert {item["id"] for item in first["items"]}.isdisjoint(item["id"] for item in second["items"])
    assert _page(rows,category="other")["items"] == []


@pytest.mark.parametrize("content", ["{broken", "[]", '{"retired":"not-an-object"}'])
def test_invalid_skill_eligibility_fails_closed(monkeypatch,tmp_path,content):
    from core.capability_catalog import eligible_skills
    import core.skills.lifecycle as lifecycle
    source = tmp_path / "lifecycle.json"; source.write_text(content)
    monkeypatch.setattr(lifecycle,"LIFECYCLE_FILE",source)
    assert eligible_skills({"retired":{}}) == {}


def test_research_parser_excludes_scripts_and_preserves_publication_date():
    from core.web_research import _DocumentText
    parser = _DocumentText(); parser.feed('<meta property="article:published_time" content="2026-10-01"><main>Observed fact</main><script>NOT EVIDENCE</script>')
    assert parser.published_at == "2026-10-01" and "NOT EVIDENCE" not in " ".join(parser.text)


def test_research_rejects_private_network(monkeypatch):
    import socket
    from core.web_research import _public_url
    monkeypatch.setattr(socket,"getaddrinfo",lambda *a,**k:[(0,0,0,"",("192.168.1.1",443))])
    with pytest.raises(ValueError,match="blocked"): _public_url("https://example.test/")


def test_extension_requires_trust_and_cannot_overwrite():
    from core.capability_router import CapabilityRouter,CapabilityDescriptor,CapabilitySource
    router = object.__new__(CapabilityRouter); router._capabilities = {}
    desc = CapabilityDescriptor("extension.qa.inspect","inspect",CapabilitySource.NATIVE,"Read-only inspection",category="security",execution_adapter=lambda args: {})
    with pytest.raises(ValueError,match="trust"): router.register_extension(desc)
    router.register_extension(desc,trusted=True)
    with pytest.raises(ValueError,match="overwrite"): router.register_extension(desc,trusted=True)


def test_readonly_team_worker_cannot_execute_writer():
    from core.brain.capability_team import ReadOnlyRouter
    cap = SimpleNamespace(id="native.write_file")
    router = ReadOnlyRouter(SimpleNamespace(get_capability=lambda name:cap,list_capabilities=lambda:[cap]))
    assert router.list_capabilities() == []
    with pytest.raises(PermissionError): router.execute("native.write_file",{})


def test_messaging_major_brand_icons_are_local_and_served(messaging):
    catalog_module, _ = messaging
    from fastapi.testclient import TestClient
    from api.server import app
    catalog = {item["id"]:item for item in catalog_module.services()}
    with TestClient(app) as client:
        for service in ("tgram", "whatsapp", "discord"):
            url = catalog[service]["icon_url"]
            assert url.startswith("/app/assets/messaging/")
            response = client.get(url)
            assert response.status_code == 200 and "<svg" in response.text
            assert "<script" not in response.text


def test_team_budget_bounds_calls_and_rejects_simulation():
    from core.brain.capability_team import SharedBudget
    from core.models.provider_interface import ModelCompletionResponse
    provider = SimpleNamespace(generate=lambda request:ModelCompletionResponse(text="Observed",model_name="offline-contract",tokens_prompt=3,tokens_completion=2))
    budget = SharedBudget(provider,1); budget.generate(None)
    assert (budget.requests,budget.prompt_tokens,budget.completion_tokens) == (1,3,2)
    with pytest.raises(RuntimeError,match="budget"): budget.generate(None)
    provider.generate = lambda request:ModelCompletionResponse(text="fake",is_simulated=True)
    with pytest.raises(RuntimeError,match="simulated"): SharedBudget(provider,1).generate(None)


@pytest.mark.parametrize("scanner",["detect-secrets","bandit"])
def test_real_offline_local_scanner_redacts_source(monkeypatch,tmp_path,scanner):
    import core.tools_bridge as bridge
    from core.security.local_scanners import scan
    monkeypatch.setattr(bridge,"WORKSPACE_DIR",tmp_path)
    source = tmp_path / "fixture.py"
    # Synthetic secret only. Both tools execute their installed implementations.
    synthetic = "J7vQ2mN9zR5tL8sC4dF6pW1b"
    source.write_text("password = '" + synthetic + "'\nimport subprocess\nsubprocess.run('hello',shell=True)\n")
    result = scan(scanner,"fixture.py")
    assert result["offline"] and result["files_submitted"] == 1
    assert result["finding_count"] > 0
    assert synthetic not in json.dumps(result) and "hashed_secret" not in json.dumps(result)


def test_local_scanner_rejects_escape_and_arbitrary_scanner(tmp_path,monkeypatch):
    import core.tools_bridge as bridge
    from core.security.local_scanners import scan
    monkeypatch.setattr(bridge,"WORKSPACE_DIR",tmp_path)
    with pytest.raises(ValueError,match="OUTSIDE"): scan("bandit","../outside.py")
    with pytest.raises(ValueError,match="Unsupported"): scan("shell",".")


def test_passive_knowledge_status_does_not_probe_network():
    from core.rag.router import KnowledgeRouter
    router = object.__new__(KnowledgeRouter)
    router._sources = {"qa":SimpleNamespace(category="research",description="fixture",requires_key=False,
        probe=lambda:pytest.fail("Opening settings must not run a network probe"))}
    router._status_cache = {}
    assert router.cached_status()[0].last_checked == 0


def test_public_transport_pins_checked_ip_and_original_tls_host(monkeypatch):
    import httpx
    import core.public_http as public
    monkeypatch.setattr(public.socket,"getaddrinfo",lambda *a,**k:[(0,0,0,"",("8.8.8.8",443))])
    seen = []
    monkeypatch.setattr(public.httpx,"HTTPTransport",lambda **kwargs:SimpleNamespace(handle_request=lambda request:seen.append(request) or httpx.Response(200),close=lambda:None))
    transport = public.PublicTransport()
    transport.handle_request(httpx.Request("GET","https://public.example/doc",headers={"Host":"wrong.example"}))
    assert seen[0].url.host == "8.8.8.8"
    assert seen[0].headers["Host"] == "public.example"
    assert seen[0].extensions["sni_hostname"] == "public.example"
    monkeypatch.setattr(public.socket,"getaddrinfo",lambda *a,**k:[(0,0,0,"",("127.0.0.1",443))])
    with pytest.raises(ValueError,match="blocked"):
        transport.handle_request(httpx.Request("GET","https://public.example/doc"))
    assert len(seen) == 1
