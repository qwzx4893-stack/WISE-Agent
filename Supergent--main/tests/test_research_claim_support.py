"""Negative evidence contracts; passing these does not certify semantic truth."""
from types import SimpleNamespace

import pytest

from core import web_research as module


URL = "https://publisher.example/security"


def fetched(text):
    return [{"url": URL, "evidence_kind": "page_excerpt", "page_text": text}]


@pytest.mark.parametrize("version", ["2.17.1", "2.12.4", "v2.3.2"])
def test_prose_upgrade_targets_must_be_in_actual_cited_excerpt(version):
    answer = f"Upgrade to {version}. [Advisory]({URL})"
    assert module.validate_grounded_answer(answer, fetched("Current product advisories."))
    assert not module.validate_grounded_answer(answer, fetched(f"Affected releases; fixed in {version}."))


def test_version_substring_is_not_exact_version_support():
    assert module.validate_grounded_answer(f"Fixed in 2.17.1. [Source]({URL})", fetched("Fixed in 2.17.10."))


def test_another_bullets_citation_cannot_launder_version_claim():
    text = f"- Read [source]({URL}).\n- Upgrade to 2.17.1."
    assert module.validate_grounded_answer(text, fetched("Fixed in 2.17.1."))


def test_snippet_only_version_and_exact_date_are_unverified():
    evidence = [{"url": URL, "evidence_kind": "search_snippet", "snippet": "Released 2026-10-01, version 2.17.1."}]
    assert module.validate_grounded_answer(f"Released 2026-10-01, version 2.17.1 [source]({URL}).", evidence)


@pytest.mark.parametrize("answer", [
    f"Gemini 3 is the latest model. [Source]({URL})",
    f"أحدث نموذج هو Gemini 3. [المصدر]({URL})",
    f"I cannot guarantee completeness. Gemini 3 is the latest model. [Source]({URL})",
])
def test_definitive_latest_without_explicit_source_anchor_is_not_accepted(answer):
    assert module.validate_grounded_answer(answer, fetched("Gemini 4 Argon. Gemini 3 launched earlier."))


def test_latest_quote_must_cover_named_version_not_generic_navigation():
    evidence = fetched("Our latest news. Product 4 is our latest model.")
    assert module.validate_grounded_answer(f'Product 3 is the latest. "Our latest news." [Source]({URL})', evidence)
    assert not module.validate_grounded_answer(
        f'Product 4 is the latest. “Product 4 is our latest model.” [Source]({URL})', evidence)


@pytest.mark.parametrize("answer", [
    f"I cannot verify which model is latest. The page lists Product 4. [Source]({URL})",
    f"لا أستطيع تأكيد النموذج الأحدث. تعرض الصفحة Product 4. [المصدر]({URL})",
])
def test_scope_uncertainty_and_observed_catalog_are_allowed(answer):
    assert not module.validate_grounded_answer(answer, fetched("Product 4"))


def test_public_page_headings_preserve_current_catalog_names_without_launch_date():
    parser = module._DocumentText()
    parser.feed("<nav>Menu</nav><main><h1>Product <span>4</span> Argon</h1><h2>Overview</h2><script>ignore</script></main>")
    assert parser.headings == ["Product 4 Argon", "Overview"]


@pytest.mark.parametrize("corrected", [True, False])
def test_one_answer_correction_is_bounded_and_counted_without_extra_retrieval(monkeypatch, corrected):
    calls = []
    searches = []
    monkeypatch.setattr(module, "search_web", lambda query: searches.append(query) or fetched("Product 4"))
    monkeypatch.setattr(module, "enrich_evidence", lambda evidence, limit: evidence)
    unsafe = f"Product 3 is the latest model. [Source]({URL})"
    safe = f"The retrieved page lists Product 4; ordering remains unverified. [Source]({URL})"
    def generate(request):
        calls.append(request)
        return SimpleNamespace(text=safe if corrected and len(calls) == 2 else unsafe,
                               model_name="offline-fixture", tokens_prompt=10, tokens_completion=5, error=None)
    result = module.research(SimpleNamespace(generate=generate), "Newest Product model")
    assert len(calls) == 2 and len(searches) == 1
    assert result["execution_metrics"] == {"model_requests": 2, "prompt_tokens": 20, "completion_tokens": 10}
    assert bool(result["errors"]) is not corrected
    assert "Product 3" not in result["synthesis"]
    # Correct from observations, not a rejected assistant claim that can anchor
    # an economical model to its own unsupported answer.
    assert all(message["role"] != "assistant" for message in calls[1].messages)
    assert unsafe not in "\n".join(message["content"] for message in calls[1].messages)
    assert "Product 4" in calls[1].messages[0]["content"]


def test_provider_failure_is_not_automatically_retried(monkeypatch):
    monkeypatch.setattr(module, "search_web", lambda query: fetched("Product 4"))
    monkeypatch.setattr(module, "enrich_evidence", lambda evidence, limit: evidence)
    calls = []
    def generate(request):
        calls.append(request)
        return SimpleNamespace(text="", model_name="offline", tokens_prompt=0, tokens_completion=0, error="unavailable")
    result = module.research(SimpleNamespace(generate=generate), "Research Product")
    assert len(calls) == 1 and result["errors"]


def test_current_repair_is_observation_note_not_repeated_ranking_request(monkeypatch):
    fetched_page = fetched("Product 4 and Product 3 catalog entries.")
    snippets = [{"url": "https://snippet.example/", "evidence_kind": "search_snippet",
                 "snippet": "Product 99 is newest"}]
    monkeypatch.setattr(module, "search_web", lambda query: fetched_page + snippets)
    monkeypatch.setattr(module, "enrich_evidence", lambda evidence, limit: evidence)
    calls = []
    def generate(request):
        calls.append(request)
        text = (f"Product 3 is the latest. [Source]({URL})" if len(calls) == 1 else
                f"I cannot verify which model is newest. The fetched catalog lists Product 4. [Source]({URL})")
        return SimpleNamespace(text=text, model_name="offline", tokens_prompt=1, tokens_completion=1, error=None)
    result = module.research(SimpleNamespace(generate=generate), "Newest Product model")
    assert not result["errors"] and len(calls) == 2
    assert calls[1].system_prompt != calls[0].system_prompt
    assert "source-observation note" in calls[1].system_prompt
    assert "Do NOT label" in calls[1].system_prompt
    assert "Product 4" in calls[1].messages[0]["content"]
    assert "Product 99" not in calls[1].messages[0]["content"]
    assert "search_snippet" not in calls[1].messages[0]["content"]
    assert len(result["sources"]) == 2  # Do not erase the actual retrieval inventory.


def test_current_repair_cannot_promote_snippet_only_evidence(monkeypatch):
    monkeypatch.setattr(module, "search_web", lambda query: [{"url": URL,
        "evidence_kind": "search_snippet", "snippet": "Product 4 newest"}])
    monkeypatch.setattr(module, "enrich_evidence", lambda evidence, limit: evidence)
    calls = []
    def generate(request):
        calls.append(request)
        return SimpleNamespace(text=f"Product 4 is the latest. [Source]({URL})",
                               model_name="offline", tokens_prompt=1, tokens_completion=1, error=None)
    result = module.research(SimpleNamespace(generate=generate), "Newest Product model")
    assert result["errors"] and len(calls) == 2
    assert "Product 4" not in calls[1].messages[0]["content"]
    assert "Product 4" not in result["synthesis"]


@pytest.mark.parametrize("text,reason", [
    ('An answer ending in "Product 3', None),
    ("An answer ending in “Product 3", None),
    ("An otherwise complete sentence.", "length"),
    ("An otherwise complete sentence.", "MAX_TOKENS"),
])
def test_truncated_draft_never_becomes_success_even_with_a_valid_url(monkeypatch, text, reason):
    monkeypatch.setattr(module, "search_web", lambda query: fetched("Product 4"))
    monkeypatch.setattr(module, "enrich_evidence", lambda evidence, limit: evidence)
    calls = []
    def generate(request):
        calls.append(request)
        return SimpleNamespace(text=text, finish_reason=reason, model_name="offline",
                               tokens_prompt=1, tokens_completion=1, error=None)
    result = module.research(SimpleNamespace(generate=generate), "Newest Product model")
    assert len(calls) == 2 and result["errors"]
    assert text not in result["synthesis"]
    assert all(request.max_tokens == 1400 for request in calls)


def test_a_small_complete_quotation_is_not_truncation():
    assert not module._draft_incomplete(SimpleNamespace(text='The page says “Product 4”.', finish_reason="stop"))
