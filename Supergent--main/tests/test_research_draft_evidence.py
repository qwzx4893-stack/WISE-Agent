"""Offline QA observer scope and noninterference contracts; no paid calls."""
import json
from types import SimpleNamespace

import pytest

from qa.acceptance import research_draft_evidence as observer
from core import web_research


@pytest.fixture
def owned(tmp_path, monkeypatch):
    from core.brain import capability_agent
    monkeypatch.setattr(capability_agent.CapabilityAgent, "_run", capability_agent.CapabilityAgent._run)
    monkeypatch.setattr(capability_agent, "validate_scoped_limit_answer", capability_agent.validate_scoped_limit_answer)
    monkeypatch.setattr(web_research, "enrich_evidence", web_research.enrich_evidence)
    monkeypatch.setattr(observer, "ROOT", tmp_path)
    runtime = tmp_path / ".tooling/research-runtime-fixture"
    (runtime / "memory").mkdir(parents=True)
    destination = tmp_path / "qa-results/diagnostic/drafts.ndjson"
    monkeypatch.setenv("WISE_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("WISE_QA_BUDGET_PATH", str(tmp_path / "qa/results/shared-ledger.json"))
    return runtime, destination


def response(text, reason=None):
    return SimpleNamespace(text=text, finish_reason=reason, tokens_prompt=17,
                           tokens_completion=9, model_name="offline")


@pytest.mark.parametrize("text,reason,length,quote", [
    ("A complete answer.", "length", True, False),
    ('A dangling "quotation', "stop", False, True),
    ('A balanced "quotation".', "stop", False, False),
])
def test_diagnostics_distinguish_true_length_stop_from_quote_rejection(text, reason, length, quote):
    actual = response(text, reason)
    row = observer.draft_record(actual, web_research._draft_incomplete(actual), 2)
    assert row["provider_length_stop"] is length
    assert row["quote_flags"]["odd_ascii_quote"] is quote
    assert row["draft_number"] == 2 and row["completion_tokens"] == 9
    assert "redacted_public_draft" not in row
    assert len(row["sha256"]) == 64


def test_observer_keeps_guard_return_and_only_observes_exact_public_query(owned, monkeypatch):
    _, destination = owned
    guard_returns = []
    def original_research(provider, query, sources_limit=3, task_id=None):
        for actual in [response("Complete.", "length"), response('A "quote', "stop")]:
            guard_returns.append(web_research._draft_incomplete(actual))
        return {"original": True, "query": query}
    monkeypatch.setattr(web_research, "research", original_research)
    monkeypatch.setattr(web_research, "_draft_incomplete", web_research._draft_incomplete)
    observer.install(destination, capture_public_draft=True)
    assert web_research.research(None, "Private unrelated query")["original"]
    assert not destination.exists()
    outcome = web_research.research(None, observer.PUBLIC_QUERY)
    rows = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert outcome == {"original": True, "query": observer.PUBLIC_QUERY}
    assert guard_returns == [True, True, True, True]
    assert [row["draft_number"] for row in rows] == [1, 2]
    assert rows[1]["redacted_public_draft"] == 'A "quote'
    assert all("query" not in row and "prompt" not in row for row in rows)


def test_observation_write_failure_never_changes_application_guard(owned, monkeypatch):
    _, destination = owned
    monkeypatch.setattr(web_research, "research", lambda provider, query, sources_limit=3, task_id=None:
                        web_research._draft_incomplete(response("Complete.", "stop")))
    monkeypatch.setattr(web_research, "_draft_incomplete", web_research._draft_incomplete)
    observer.install(destination)
    monkeypatch.setattr(observer, "write_record", lambda *args: (_ for _ in ()).throw(OSError("offline")))
    assert web_research.research(None, observer.PUBLIC_QUERY) is False


def test_public_source_capture_is_exact_scope_and_noninterfering(owned, monkeypatch):
    _, destination = owned
    actual = [{"url": "https://publisher.example/models", "evidence_kind": "page_excerpt",
               "page_text": "Product 4 listed", "page_headings": ["Product 4"]}]
    monkeypatch.setattr(web_research, "enrich_evidence", lambda evidence, limit: actual)
    monkeypatch.setattr(web_research, "research", lambda provider, query, sources_limit=3, task_id=None:
                        web_research.enrich_evidence([], sources_limit))
    observer.install(destination, capture_public_draft=True)
    observed_path = destination.with_name("public-research-observations.ndjson")
    assert web_research.research(None, "Private query") is actual
    assert not observed_path.exists()
    assert web_research.research(None, observer.PUBLIC_QUERY) is actual
    row = json.loads(observed_path.read_text(encoding="utf-8"))
    assert row["sources"] == actual and row["kind"] == "public_gemini_source_observations"
    monkeypatch.setattr(observer, "write_record", lambda *args: (_ for _ in ()).throw(RuntimeError("observer failed")))
    assert web_research.research(None, observer.PUBLIC_QUERY) is actual


@pytest.mark.parametrize("runtime_name,path_name,budget", [
    ("receipt-runtime-other", "qa-results/drafts.ndjson", True),
    ("research-runtime-owned", "other/drafts.ndjson", True),
    ("research-runtime-owned", "qa-results/drafts.json", True),
    ("research-runtime-owned", "qa-results/drafts.ndjson", False),
])
def test_scope_rejects_unowned_paths_and_missing_explicit_budget(tmp_path, monkeypatch, runtime_name, path_name, budget):
    monkeypatch.setattr(observer, "ROOT", tmp_path)
    monkeypatch.setenv("WISE_RUNTIME_ROOT", str(tmp_path / ".tooling" / runtime_name))
    if budget: monkeypatch.setenv("WISE_QA_BUDGET_PATH", "opted-in")
    else: monkeypatch.delenv("WISE_QA_BUDGET_PATH", raising=False)
    with pytest.raises(ValueError): observer.owned_destination(tmp_path / path_name)


@pytest.mark.parametrize("tool,url,headers,query,captured", [
    (observer.PUBLIC_DOCS_TOOL, "https://docs.mcp.cloudflare.com/mcp", {}, "Workers CPU time", True),
    ("mcp.private.read", "https://docs.mcp.cloudflare.com/mcp", {}, "CPU", False),
    (observer.PUBLIC_DOCS_TOOL, "https://private.example/mcp", {}, "CPU", False),
    (observer.PUBLIC_DOCS_TOOL, "https://docs.mcp.cloudflare.com/mcp", {"Authorization": "private"}, "CPU", False),
    (observer.PUBLIC_DOCS_TOOL, "https://docs.mcp.cloudflare.com/mcp", {}, "unrelated", False),
])
def test_full_public_mcp_response_capture_excludes_generic_private_or_authenticated_payloads(
        owned, monkeypatch, tool, url, headers, query, captured):
    runtime, destination = owned
    (runtime / "memory/mcp_servers.json").write_text(json.dumps([{"name": "qa-live-docs", "url": url, "headers": headers}]))
    from core.capability_router import CapabilityRouter
    actual = SimpleNamespace(to_dict=lambda: {"success": True, "output": "Public CPU documentation " * 1000})
    monkeypatch.setattr(CapabilityRouter, "execute", lambda *args, **kwargs: actual)
    observer.install_public_docs(destination)
    assert CapabilityRouter.execute(None, tool, {"query": query}) is actual
    assert destination.exists() is captured
    if captured:
        row = json.loads(destination.read_text(encoding="utf-8"))
        assert row["result"]["output"] == actual.to_dict()["output"]
        assert len(row["result"]["output"]) > 12000  # includes retained range-read content


@pytest.mark.parametrize("goal,selected,url,headers,evidence_tool,captured", [
    (observer.PUBLIC_DOCS_QUERY, "qa-live-docs", "https://docs.mcp.cloudflare.com/mcp", {}, observer.PUBLIC_DOCS_TOOL, True),
    ("Private task", "qa-live-docs", "https://docs.mcp.cloudflare.com/mcp", {}, observer.PUBLIC_DOCS_TOOL, False),
    (observer.PUBLIC_DOCS_QUERY, "other", "https://docs.mcp.cloudflare.com/mcp", {}, observer.PUBLIC_DOCS_TOOL, False),
    (observer.PUBLIC_DOCS_QUERY, "qa-live-docs", "https://private.example/mcp", {}, observer.PUBLIC_DOCS_TOOL, False),
    (observer.PUBLIC_DOCS_QUERY, "qa-live-docs", "https://docs.mcp.cloudflare.com/mcp", {"Authorization": "private"}, observer.PUBLIC_DOCS_TOOL, False),
    (observer.PUBLIC_DOCS_QUERY, "qa-live-docs", "https://docs.mcp.cloudflare.com/mcp", {}, "mcp.private.read", False),
])
def test_public_docs_answer_capture_exact_task_tool_and_public_endpoint_only(
        owned, monkeypatch, goal, selected, url, headers, evidence_tool, captured):
    runtime, destination = owned
    (runtime / "memory/mcp_servers.json").write_text(json.dumps([{"name": "qa-live-docs", "url": url, "headers": headers}]))
    from core.brain import capability_agent
    from core.capability_router import CapabilityRouter
    errors = ["Original rejected scoped claim"]
    monkeypatch.setattr(CapabilityRouter, "execute", lambda *args, **kwargs: None)
    monkeypatch.setattr(capability_agent, "validate_scoped_limit_answer", lambda *args: errors)
    evidence = [{"mcp_tool": evidence_tool, "evidence_kind": "mcp_document_excerpt"}]
    def original_run(self, task, **kwargs):
        return capability_agent.validate_scoped_limit_answer("Public answer.", evidence, task)
    monkeypatch.setattr(capability_agent.CapabilityAgent, "_run", original_run)
    observer.install_public_docs(destination)
    agent = SimpleNamespace(selected_mcp=selected)
    assert capability_agent.CapabilityAgent._run(agent, goal, session_id="fixture") is errors
    assert destination.exists() is captured
    if captured:
        row = json.loads(destination.read_text(encoding="utf-8"))
        assert row["guard_errors"] == errors
        assert row["redacted_public_draft"] == "Public answer."
        assert row["kind"] == "public_docs_proposed_answer"
        assert "prompt" not in row and "query" not in row
    # Outside the exact active task, no additional draft is recorded.
    before = destination.read_bytes() if destination.exists() else None
    assert capability_agent.validate_scoped_limit_answer("Not in task", evidence, observer.PUBLIC_DOCS_QUERY) is errors
    assert (destination.read_bytes() if destination.exists() else None) == before


def test_public_docs_draft_failure_preserves_guard_and_bounds_capture(owned, monkeypatch):
    runtime, destination = owned
    (runtime / "memory/mcp_servers.json").write_text(json.dumps([{"name": "qa-live-docs", "url": "https://docs.mcp.cloudflare.com/mcp"}]))
    from core.brain import capability_agent
    from core.capability_router import CapabilityRouter
    errors = []
    monkeypatch.setattr(CapabilityRouter, "execute", lambda *args, **kwargs: None)
    monkeypatch.setattr(capability_agent, "validate_scoped_limit_answer", lambda *args: errors)
    evidence = [{"mcp_tool": observer.PUBLIC_DOCS_TOOL, "evidence_kind": "mcp_document_excerpt"}]
    monkeypatch.setattr(capability_agent.CapabilityAgent, "_run", lambda self, goal, **kwargs:
                        capability_agent.validate_scoped_limit_answer("a" * 25000, evidence, goal))
    observer.install_public_docs(destination)
    agent = SimpleNamespace(selected_mcp="qa-live-docs")
    assert capability_agent.CapabilityAgent._run(agent, observer.PUBLIC_DOCS_QUERY) is errors
    row = json.loads(destination.read_text(encoding="utf-8"))
    assert len(row["redacted_public_draft"]) == 24000 and row["draft_capture_truncated"]
    monkeypatch.setattr(observer, "write_record", lambda *args: (_ for _ in ()).throw(RuntimeError("observer failure")))
    assert capability_agent.CapabilityAgent._run(agent, observer.PUBLIC_DOCS_QUERY) is errors
