"""Small deterministic checks complement (not replace) the real UI journeys."""
from types import SimpleNamespace
import pytest

from core import web_research
from api.server import V2ChatRequest
from pydantic import ValidationError

REAL_ENRICH_EVIDENCE = web_research.enrich_evidence


@pytest.fixture(autouse=True)
def no_network_page_reads_in_contract_tests(monkeypatch):
    monkeypatch.setattr(web_research, "enrich_evidence", lambda evidence, limit: evidence)


class Provider:
    def __init__(self): self.requests = []
    def generate(self, request):
        self.requests.append(request)
        return SimpleNamespace(text="المصدر يذكر Gemini؛ لا أستطيع تأكيد أنه الأحدث.", model_name="qa-model",
                               tokens_prompt=100, tokens_completion=20, error=None)


def test_research_preserves_exact_arabic_topic_date_language_and_model(monkeypatch):
    queries = []
    def search(query):
        queries.append(query)
        return [{"title":"Gemini", "url":"https://ai.google.dev/gemini-api/docs/models", "snippet":"Gemini", "source":"ai.google.dev"}]
    monkeypatch.setattr(web_research, "search_web", search)
    provider = Provider()
    text = "ابحث لي عن أحدث نموذج من عائلة جيمناي"
    result = web_research.research(provider, text)
    assert queries[0] == text
    assert queries[1].startswith("site:ai.google.dev Gemini models ")
    assert len(provider.requests) == 1
    assert "in Arabic" in provider.requests[0].system_prompt
    assert "Current UTC date:" in provider.requests[0].messages[0]["content"]
    assert result["model_name"] == "qa-model"
    assert result["source_count"] == 1
    assert result["citations"][0]["url"].startswith("https://ai.google.dev")


@pytest.mark.parametrize("failure", [False, True])
def test_no_evidence_is_not_success_or_paid_synthesis(monkeypatch, failure):
    def search(query):
        if failure: raise RuntimeError("search unavailable")
        return []
    monkeypatch.setattr(web_research, "search_web", search)
    provider = Provider()
    result = web_research.research(provider, "أحدث نموذج")
    assert result["source_count"] == 0
    assert result["errors"] and not result["sources_succeeded"]
    assert not provider.requests
    assert "لم أستطع" in result["synthesis"]


def test_partial_search_failure_is_degraded_not_false_total_failure(monkeypatch):
    def search(query):
        if query.startswith("site:"):
            return [{"title":"Gemini", "url":"https://ai.google.dev/gemini-api/docs/models", "snippet":"Gemini", "source":"ai.google.dev"}]
        raise RuntimeError("One search source unavailable")
    monkeypatch.setattr(web_research, "search_web", search)
    provider = Provider()
    result = web_research.research(provider, "أحدث نموذج Gemini")
    assert result["degraded_sources"]
    assert not result["errors"]
    assert result["source_count"] == 1 and len(provider.requests) == 1


def test_research_fetch_priority_is_not_generic_homepage():
    items = [{"url":"https://gemini.google.com/"}, {"url":"https://ai.google.dev/gemini-api/docs/models"},
             {"url":"https://independent.example/article/new-gemini"}]
    ranked = web_research.prioritize_evidence(items, "Latest Gemini model")
    assert ranked[0]["url"].endswith("/models")
    assert len(ranked) == 3  # Independent sources are ranked, not removed.


def test_research_never_accepts_invented_citation(monkeypatch):
    monkeypatch.setattr(web_research, "search_web", lambda query:[{
        "title":"Observed", "url":"https://source.example/page", "snippet":"fact", "source":"source.example"}])
    provider = Provider()
    provider.generate = lambda request:SimpleNamespace(text="A fact [source](https://invented.example/page)",
        model_name="offline-contract", tokens_prompt=1, tokens_completion=1, error=None)
    result = web_research.research(provider, "Latest result")
    assert result["errors"] and "invented.example" not in result["synthesis"]


def test_research_main_content_and_evidence_metadata_are_preserved():
    from core.contracts import SourceCitation, ResearchResult
    parser = web_research._DocumentText()
    parser.feed("<nav>Site navigation</nav><main>Observed release<script>ignore</script></main><footer>Footer</footer>")
    assert parser.main_text == ["Observed release"]
    citation = SourceCitation("official", "Release", "https://source.example", "Observed", evidence_kind="page_excerpt", retrieved_at="2026-10-01")
    result = ResearchResult("query", "answer", citations=[citation]).to_dict()["citations"][0]
    assert result["evidence_kind"] == "page_excerpt" and result["retrieved_at"] == "2026-10-01"


def test_page_fetch_budget_does_not_repeat_language_variants(monkeypatch):
    fetched = []
    def read(url):
        fetched.append(url)
        return {"evidence_kind":"page_excerpt", "page_text":"Observed"}
    monkeypatch.setattr(web_research, "read_web_evidence", read)
    items = [{"url":"https://source.example/models?hl=ar"},
        {"url":"https://source.example/models?hl=en"},
        {"url":"https://source.example/changelog"}, {"url":"https://independent.example/article"}]
    result = REAL_ENRICH_EVIDENCE(items, 3)
    assert len(fetched) == 3 and items[1]["url"] not in fetched
    assert result[3]["evidence_kind"] == "page_excerpt"


@pytest.mark.parametrize("mode", ["normal", "research", "agents_team"])
def test_real_modes(mode):
    assert V2ChatRequest(message="hi", mode=mode).mode == mode


@pytest.mark.parametrize("mode", ["fake", "workflow", None])
def test_unimplemented_v2_modes_rejected(mode):
    with pytest.raises(ValidationError): V2ChatRequest(message="hi", mode=mode)


@pytest.mark.parametrize("name", ["../escape", "x.y", "", "a"*65])
def test_mcp_selection_names_validated(name):
    with pytest.raises(ValidationError): V2ChatRequest(message="hi", selected_mcp=name)


def test_readonly_request_inspection_never_executes_or_replays():
    from core.idempotency import IdempotencyStore
    store = IdempotencyStore()
    assert store.inspect("v2.chat", "unknown")["state"] == "UNKNOWN"
    assert store.stats()["entries"] == 0
    store.begin("v2.chat", "key", {"message":"hi"})
    assert store.inspect("v2.chat", "key")["state"] == "IN_PROGRESS"
    store.complete("v2.chat", "key", {"reply":"observed"})
    result = store.inspect("v2.chat", "key")
    result["response"]["reply"] = "tampered"
    assert store.inspect("v2.chat", "key")["response"]["reply"] == "observed"


@pytest.mark.parametrize("text", ["Edit qa.html: change Original to Saved", "Compare the first and third files", "Remove the second file", "اكتب كلمة احفظ داخل ملف qa.txt"])
def test_context_shortcuts_do_not_hijack_artifact_work(text):
    from core.brain.conversational_core import ConversationalCore
    session = SimpleNamespace(working_items=[{"name":"old recommendation"}], history=[{"role":"user","content":"hi"}])
    result = ConversationalCore._handle_contextual_followups(SimpleNamespace(), text, session, lambda *a: None)
    assert result is None
    assert len(session.working_items) == 1


def test_settings_save_failure_is_not_false_success(tmp_path, monkeypatch):
    from core import server_config
    target = tmp_path / "server.json"
    target.write_text('{"ui_language":"ar"}', encoding="utf-8")
    monkeypatch.setattr(server_config, "SERVER_CONFIG_PATH", target)
    def fail_replace(*args): raise OSError("Injected disk failure")
    monkeypatch.setattr(server_config.os, "replace", fail_replace)
    with pytest.raises(OSError): server_config.save_server_config({"ui_language":"en"})
    assert target.read_text(encoding="utf-8") == '{"ui_language":"ar"}'
    assert not list(tmp_path.glob("*.tmp"))
