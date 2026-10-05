"""Bounded real-UI observer; no application change, provider fallback or retry."""
from __future__ import annotations
import argparse
import json
import shutil
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

import httpx
from playwright.sync_api import expect
from owned_browser import owned_playwright, browser_environment

from user_journeys import ROOT, BRAVE, Runtime, Evidence
from source_stamp import source_stamp
from live_budget import LiveBudget
from run_acceptance import redact

sys.path.insert(0, str(ROOT))
from qa.acceptance.research_draft_evidence import PUBLIC_QUERY, PUBLIC_DOCS_QUERY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-additional-usd", type=float, required=True)
    parser.add_argument("--capture-public-draft", action="store_true")
    parser.add_argument("--public-docs-case", action="store_true")
    parser.add_argument("--docs-only", action="store_true", help="Run only the public selected MCP case; do not dispatch Gemini research")
    args = parser.parse_args()
    if args.docs_only:
        args.public_docs_case = True
    ledger = ROOT / "qa/results/additional-budget-20261002.json"
    # Stop before runtime creation, credentials, or any model dispatch when
    # another paid QA turn owns the shared ledger. Never reset its cap/state.
    preflight = json.loads(ledger.read_text(encoding="utf-8"))
    if preflight.get("active_turn") is not None:
        raise RuntimeError("Another paid QA turn is active; diagnostic was not dispatched")
    output = ROOT / "qa-results" / ("research-diagnostic-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir()
    private = ROOT / ".tooling" / ("research-runtime-" + uuid.uuid4().hex)
    runtime = Runtime(output, runtime_root=private, app_target="qa.acceptance.instrumented_app:app")
    evidence = Evidence(output, runtime)
    initial = source_stamp()
    budget = before = after = None
    turn = None
    responses = []
    record = {"schema": "wise.research-diagnostic.v1", "source_stamp": initial, "runtime": str(private),
              "release_verdict": "NOT_READY", "limitations": ["Diagnostic only; does not certify semantic truth or a release",
              "No private accounts, messages, microphone, scans or source edits"]}
    try:
        from core.llm.keystore import KeyStore
        entry = KeyStore(path=ROOT / "memory/keys.json", secret_path=ROOT / "memory/.keystore_secret").get_for_provider("openrouter", reveal=True)
        if not entry: raise RuntimeError("Configured paid QA key unavailable")
        model = next(row for row in httpx.get("https://openrouter.ai/api/v1/models", timeout=20).json()["data"]
                     if row["id"] == "qwen/qwen3-coder-flash")
        budget = LiveBudget(ledger, api_key=entry["api_key"])
        before = budget.initialize(additional_usd=args.max_additional_usd, model=model["id"], pricing=model["pricing"])
        runtime.env["WISE_QA_BUDGET_PATH"] = str(ledger)
        runtime.env["WISE_QA_TOOL_EVIDENCE_PATH"] = str(output / "actual-tool-observations.ndjson")
        diagnostics = output / "research-drafts.ndjson"
        runtime.env["WISE_QA_RESEARCH_DRAFT_PATH"] = str(diagnostics)
        if args.capture_public_draft: runtime.env["WISE_QA_PUBLIC_GEMINI_DRAFT"] = "1"
        if args.public_docs_case:
            runtime.env["WISE_QA_PUBLIC_DOCS_EVIDENCE_PATH"] = str(output / "public-docs-observations.ndjson")
        for name in ("keys.json", ".keystore_secret"):
            source = ROOT / "memory" / name
            if source.exists(): shutil.copy2(source, private / "memory" / name)
        runtime.start()
        configured = httpx.post(runtime.url + "/api/v2/models/external", json={"provider_id": "openrouter",
                       "model": model["id"], "set_as_active": True}, timeout=30)
        configured.raise_for_status()
        if not configured.json().get("ok"): raise RuntimeError("Stored QA credential unavailable")
        del entry
        with owned_playwright(output) as pw:
            browser = pw.chromium.launch(executable_path=str(BRAVE), headless=True, env=browser_environment(output))
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(runtime.url + "/app/", wait_until="networkidle")

            def send(prompt, max_requests):
                nonlocal turn
                turn = budget.begin_turn(max_requests=max_requests)
                evidence.step("Type exact synthetic public request", lambda: page.locator("#prompt").fill(prompt))
                with page.expect_response(lambda r: r.url.endswith("/api/v2/chat/events") and r.request.method == "POST", timeout=180000) as pending:
                    evidence.step("Submit through WISE Send button", lambda: page.locator("#sendButton").click())
                key = pending.value.request.headers["idempotency-key"]
                deadline, completed = time.monotonic() + 180, None
                while time.monotonic() < deadline:
                    status = httpx.get(runtime.url + "/api/v2/chat/requests/" + key, timeout=10).json()
                    if status.get("state") == "COMPLETED": completed = status["response"]; break
                    if status.get("state") == "UNKNOWN": raise AssertionError("Request did not complete")
                    page.wait_for_timeout(250)
                assert completed is not None, "No completed response"
                budget.end_turn(turn); turn = None
                responses.append(completed)
                (output / "responses.json").write_text(json.dumps(responses, ensure_ascii=False, indent=2), encoding="utf-8")
                expect(page.locator("#prompt")).to_be_enabled(timeout=10000)
                return completed

            def research_case():
                evidence.step("Choose real Research mode", lambda: page.locator("#prompt").fill("/research"))
                page.locator('[data-slash-id="research"]').click()
                result = send(PUBLIC_QUERY, 2)
                assert result["action_type"] == "RESEARCH"
                rows = [json.loads(line) for line in diagnostics.read_text(encoding="utf-8").splitlines()]
                record["draft_diagnostics"] = rows
                assert len(rows) == result["execution_metrics"]["model_requests"], "Missing actual draft observation"
                record["diagnostic_capture_complete"] = True
                assert not result.get("error"), result.get("error")
            if not args.docs_only:
                evidence.case("live-public-gemini-draft-diagnostic", context, page, research_case)

            if args.public_docs_case:
                # Isolate the optional case even when the previous UI case failed.
                page.reload(wait_until="networkidle")
                def docs_case():
                    page.locator("#settingsButton").click()
                    page.locator('[data-settings-tab="integrations"]').click()
                    expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false")
                    page.locator("#mcpConfig").fill(json.dumps({"mcpServers": {"qa-live-docs": {"url": "https://docs.mcp.cloudflare.com/mcp"}}}))
                    page.locator('#mcpImportForm [type="submit"]').click()
                    expect(page.locator('[data-mcp-name="qa-live-docs"][data-mcp-action="stop"]')).to_be_visible(timeout=60000)
                    page.locator("#closeSettings").click()
                    page.locator("#prompt").fill("/qa-live-docs")
                    page.locator('[data-slash-id="mcp:qa-live-docs"]').click()
                    result = send(PUBLIC_DOCS_QUERY, 12)
                    assert not result.get("error"), result.get("error")
                    captured = output / "public-docs-observations.ndjson"
                    assert captured.exists(), "No exact public MCP response captured"
                    record["public_docs_capture"] = str(captured)
                    # A successful call is evidence collection, not plan/context certification.
                    record["public_docs_semantic_verdict"] = "REQUIRES_INDEPENDENT_REVIEW"
                evidence.case("live-public-cloudflare-response-diagnostic", context, page, docs_case)
            context.close(); browser.close()
    except Exception:
        record["setup_or_harness_failure"] = redact(traceback.format_exc())
    finally:
        runtime.stop()
        if turn is not None and budget is not None: budget.end_turn(turn)
        if budget is not None: after = budget.summary()
        for name in ("keys.json", ".keystore_secret"):
            target = private / "memory" / name
            if target.exists() and target.resolve().parent == (private / "memory").resolve(): target.unlink()
        for handle in runtime.handles: handle.close()
        record.update(cases=evidence.cases, source_changed_during_run=initial != source_stamp(), budget_before=before,
                      budget_after=after, verdict="PASS" if evidence.cases and all(row["status"] == "PASS" for row in evidence.cases)
                      and not record.get("setup_or_harness_failure") and initial == source_stamp() else "FAIL")
        if before and after:
            record["attributed_generation_cost_delta"] = after["attributed_generation_cost_usd"] - before["attributed_generation_cost_usd"]
        (output / "report.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"verdict": record["verdict"], "report": str(output / "report.json")}), flush=True)
    return 0 if record["verdict"] == "PASS" else 1


if __name__ == "__main__": raise SystemExit(main())
