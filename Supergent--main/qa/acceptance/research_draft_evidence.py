"""Transparent diagnostics for one explicitly opted-in synthetic public query.

Observe the actual completion passed to the existing acceptance guard. Never
change that guard's result, completion requests, retrieval, or model selection.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from qa.acceptance.tool_evidence import ROOT, _lock, redact

PUBLIC_QUERY = "ابحث لي عن أحدث نموذج من عائلة جيمناي Gemini مع روابط المصادر، ولا تدّعِ أنه الأحدث إذا لم تؤكد المصادر ذلك."
PUBLIC_DOCS_TOOL = "mcp.qa-live-docs.search_cloudflare_documentation"
PUBLIC_DOCS_QUERY = "Use ONLY the selected Cloudflare documentation MCP to search official Workers documentation for CPU time limits. Report one documented limit with its plan/context and source URL. Do not use native web search, other MCPs, files, shell or messaging."
LOG = logging.getLogger("WISE.QA.ResearchObservation")


def owned_destination(destination):
    destination = Path(destination).resolve()
    runtime = Path(os.environ.get("WISE_RUNTIME_ROOT", "")).resolve()
    if (not os.environ.get("WISE_QA_BUDGET_PATH") or ROOT / "qa-results" not in destination.parents
            or destination.suffix != ".ndjson" or runtime.parent != (ROOT / ".tooling").resolve()
            or not runtime.name.startswith(("research-runtime-", "live-ui-runtime-"))):
        raise ValueError("Research diagnostics require an explicitly owned isolated QA runtime and NDJSON path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def write_record(destination, record):
    line = json.dumps(record, ensure_ascii=False, default=str)
    if len(line.encode("utf-8")) > 300000:
        raise ValueError("Public QA observation exceeds its bounded evidence budget")
    with _lock, destination.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def draft_record(response, incomplete, sequence, capture_public_draft=False):
    text = response.text
    reason = str(getattr(response, "finish_reason", "") or "").lower()
    quote_flags = {"odd_ascii_quote": text.count('"') % 2 != 0,
                   "unbalanced_curly_quotes": text.count("“") != text.count("”"),
                   "unbalanced_guillemets": text.count("«") != text.count("»"),
                   "odd_code_fence": text.count("```") % 2 != 0}
    record = {"kind": "research_draft_guard", "retrieved_at": datetime.now(timezone.utc).isoformat(),
              "draft_number": sequence, "finish_reason": getattr(response, "finish_reason", None),
              "provider_length_stop": reason in {"length", "max_tokens", "max_output_tokens"},
              "quote_flags": quote_flags, "guard_rejected": incomplete,
              "prompt_tokens": response.tokens_prompt, "completion_tokens": response.tokens_completion,
              "requested_max_tokens": 1400, "model": response.model_name,
              "chars": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest(),
              "scope": "Exact synthetic public Gemini query only; no prompts or arbitrary user data"}
    if capture_public_draft:
        record["redacted_public_draft"] = redact(text[:24000])
        record["draft_capture_truncated"] = len(text) > 24000
    return record


def install(destination, *, capture_public_draft=False):
    destination = owned_destination(destination)
    from core import web_research
    original_research, original_guard = web_research.research, web_research._draft_incomplete
    original_enrich = web_research.enrich_evidence
    state = threading.local()

    def research(provider, query, sources_limit=3, task_id=None):
        previous = getattr(state, "active", None)
        state.active = {"sequence": 0} if query == PUBLIC_QUERY else None
        try:
            return original_research(provider, query, sources_limit, task_id)
        finally:
            state.active = previous

    def guard(response):
        rejected = original_guard(response)
        active = getattr(state, "active", None)
        if active is not None:
            active["sequence"] += 1
            try:
                write_record(destination, draft_record(response, rejected, active["sequence"], capture_public_draft))
            except (OSError, ValueError):
                LOG.warning("Public QA draft observation unavailable; guard result preserved")
        return rejected

    def enrich(evidence, limit):
        observed = original_enrich(evidence, limit)
        if capture_public_draft and getattr(state, "active", None) is not None:
            try:
                rows = []
                from core.mcp_document_evidence import _document_url
                for item in observed[:10]:
                    if not isinstance(item, dict): continue
                    # Observe already fetched data; do not issue a second DNS
                    # lookup or alter the retrieval's network behavior.
                    if not _document_url(item.get("url", "")):
                        raise ValueError("Only actual public source URLs can enter this observer")
                    rows.append({key: item[key] for key in (
                        "url", "resolved_url", "title", "evidence_kind", "page_text", "page_headings",
                        "excerpt_truncated", "excerpt_scope", "published_at", "retrieved_at") if key in item})
                write_record(destination.with_name("public-research-observations.ndjson"), {
                    "kind": "public_gemini_source_observations", "sources": redact(rows),
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "scope": "Actually enriched public pages for exact synthetic Gemini query; no prompts or private task data"})
            except Exception:
                LOG.warning("Public source observation unavailable; actual enriched results preserved")
        return observed

    web_research.research, web_research._draft_incomplete = research, guard
    web_research.enrich_evidence = enrich


def install_public_docs(destination):
    """Save the full public docs response before the agent's range previews.

This contains everything later read through read_tool_result without observing
generic MCP payloads or arbitrary range reads from unrelated tools.
"""
    destination = owned_destination(destination)
    runtime = Path(os.environ["WISE_RUNTIME_ROOT"]).resolve()
    from core.capability_router import CapabilityRouter
    original = CapabilityRouter.execute

    def public_server():
        try:
            servers = json.loads((runtime / "memory/mcp_servers.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        rows = servers.values() if isinstance(servers, dict) else servers if isinstance(servers, list) else []
        return any(isinstance(row, dict) and row.get("name") == "qa-live-docs"
                   and row.get("url") == "https://docs.mcp.cloudflare.com/mcp"
                   and not row.get("headers") and not row.get("env") for row in rows)

    def execute(self, id_or_name, *args, **kwargs):
        result = original(self, id_or_name, *args, **kwargs)
        if id_or_name != PUBLIC_DOCS_TOOL:
            return result
        arguments = args[0] if args else kwargs.get("arguments", {})
        if not isinstance(arguments, dict) or not any("cpu" in str(value).lower() for value in arguments.values()):
            return result
        # Only this exact, public server is eligible. A same-name private MCP
        # must never enter the evidence folder.
        if not public_server():
            return result
        try:
            write_record(destination, {"kind": "public_docs_full_tool_result", "tool": PUBLIC_DOCS_TOOL,
                     "retrieved_at": datetime.now(timezone.utc).isoformat(), "result": redact(result.to_dict()),
                     "scope": "Exact public Cloudflare CPU docs MCP response, including retained range-read content"})
        except (OSError, ValueError):
            LOG.warning("Public QA docs observation unavailable; actual tool result preserved")
        return result
    CapabilityRouter.execute = execute

    # Observe the final proposed public answer at the actual acceptance gate.
    # Thread-local exact-goal scope excludes unrelated tasks or private drafts;
    # only associated records from this selected public tool are eligible.
    from core.brain import capability_agent
    original_run = capability_agent.CapabilityAgent._run
    original_limit_guard = capability_agent.validate_scoped_limit_answer
    state = threading.local()

    def run(self, goal, *args, **kwargs):
        previous = getattr(state, "active", None)
        state.active = {"sequence": 0} if (
            goal == PUBLIC_DOCS_QUERY and getattr(self, "selected_mcp", None) == "qa-live-docs"
            and public_server()) else None
        try:
            return original_run(self, goal, *args, **kwargs)
        finally:
            state.active = previous

    def limit_guard(answer, evidence, goal):
        errors = original_limit_guard(answer, evidence, goal)
        active = getattr(state, "active", None)
        if (active is not None and goal == PUBLIC_DOCS_QUERY and isinstance(answer, str)
                and public_server() and any(isinstance(item, dict)
                    and item.get("mcp_tool") == PUBLIC_DOCS_TOOL
                    and item.get("evidence_kind") == "mcp_document_excerpt" for item in evidence)):
            active["sequence"] += 1
            try:
                write_record(destination, {
                    "kind": "public_docs_proposed_answer", "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "draft_number": active["sequence"], "sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    "chars": len(answer), "redacted_public_draft": redact(answer[:24000]),
                    "draft_capture_truncated": len(answer) > 24000, "guard_errors": list(errors),
                    "scope": "Exact synthetic public Cloudflare query; no prompts, system messages or arbitrary user data"})
            except Exception:
                LOG.warning("Public QA docs draft unavailable; guard verdict preserved")
        return errors

    capability_agent.CapabilityAgent._run = run
    capability_agent.validate_scoped_limit_answer = limit_guard
