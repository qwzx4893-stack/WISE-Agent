"""One paid, isolated real-UI regression for requested source retrieval dates.

This is deliberately NOT the full live/project/semantic release matrix.
"""
from __future__ import annotations
import argparse
import hashlib
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-additional-usd", type=float, required=True)
    parser.add_argument("--capture-public-draft", action="store_true")
    args = parser.parse_args()
    output = ROOT / "qa-results" / ("research-receipts-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir()
    private = ROOT / ".tooling" / ("receipt-runtime-" + uuid.uuid4().hex)
    runtime = Runtime(output, runtime_root=private, app_target="qa.acceptance.instrumented_app:app")
    evidence = Evidence(output, runtime)
    initial = source_stamp()
    budget = before = after = None
    turn = None
    record = {"schema": "wise.narrow-research-regression.v1", "source_stamp": initial,
              "runtime": str(private), "release_verdict": "NOT_READY",
              "limitations": ["One targeted case, not repeated project or general semantic certification",
                              "No private accounts, outgoing messages, scans, microphone or GPU test"]}
    try:
        sys.path.insert(0, str(ROOT))
        from core.llm.keystore import KeyStore
        key = KeyStore(path=ROOT / "memory/keys.json", secret_path=ROOT / "memory/.keystore_secret").get_for_provider("openrouter", reveal=True)
        if not key: raise RuntimeError("Configured paid QA key is unavailable")
        model = next(row for row in httpx.get("https://openrouter.ai/api/v1/models", timeout=20).json()["data"]
                     if row["id"] == "qwen/qwen3-coder-flash")
        ledger = ROOT / "qa/results/additional-budget-20261002.json"
        budget = LiveBudget(ledger, api_key=key["api_key"])
        before = budget.initialize(additional_usd=args.max_additional_usd, model=model["id"], pricing=model["pricing"])
        runtime.env["WISE_QA_BUDGET_PATH"] = str(ledger)
        observations = output / "actual-tool-observations.ndjson"
        runtime.env["WISE_QA_TOOL_EVIDENCE_PATH"] = str(observations)
        if args.capture_public_draft:
            runtime.env["WISE_QA_RECEIPT_DRAFT_PATH"] = str(output / "public-receipt-drafts.ndjson")
        for name in ("keys.json", ".keystore_secret"):
            source = ROOT / "memory" / name
            if source.exists(): shutil.copy2(source, private / "memory" / name)
        runtime.start()
        httpx.post(runtime.url + "/api/v2/models/external", json={"provider_id": "openrouter", "model": model["id"],
                   "set_as_active": True}, timeout=30).raise_for_status()
        with owned_playwright(output) as pw:
            browser = pw.chromium.launch(executable_path=str(BRAVE), headless=True, env=browser_environment(output))
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(runtime.url + "/app/", wait_until="networkidle")
            def run_case():
                nonlocal turn
                prompt = ("Research CVE-2021-44228 using CISA Known Exploited Vulnerabilities and FIRST EPSS query adapters. "
                          "Create receipts.md with the observed CVE identity, known-exploitation status, exact unrounded EPSS score "
                          "and score date, exact source URLs and retrieval dates. Distinguish observed known exploitation from "
                          "predicted probability. Include only facts in those actual source results, no extra version recommendations. "
                          "Read the saved file to verify it. No shell, network scans or outgoing messages.")
                turn = budget.begin_turn(max_requests=12)
                evidence.step("Type ordinary research/file request", lambda: page.locator("#prompt").fill(prompt))
                with page.expect_response(lambda r: r.url.endswith("/api/v2/chat/events") and r.request.method == "POST", timeout=240000) as pending:
                    evidence.step("Submit through WISE Send button", lambda: page.locator("#sendButton").click())
                request_key = pending.value.request.headers["idempotency-key"]
                completed = None
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    status = httpx.get(runtime.url + "/api/v2/chat/requests/" + request_key, timeout=10).json()
                    if status.get("state") == "COMPLETED":
                        completed = status["response"]; break
                    if status.get("state") == "UNKNOWN": raise AssertionError("Request did not complete")
                    page.wait_for_timeout(250)
                assert completed is not None, "No completed response"
                budget.end_turn(turn); turn = None
                (output / "response.json").write_text(json.dumps(completed, ensure_ascii=False, indent=2), encoding="utf-8")
                assert not completed.get("error"), completed.get("error")
                expect(page.locator("#prompt")).to_be_enabled(timeout=10000)
                artifact = private / "workspace/receipts.md"
                text = artifact.read_text(encoding="utf-8")
                rows = [json.loads(line) for line in observations.read_text(encoding="utf-8").splitlines()]
                observed_sources = {}
                for row in rows:
                    result = row.get("result", {})
                    if row.get("tool", "").startswith("source.") and result.get("success"):
                        payload = result.get("output")
                        if isinstance(payload, dict) and isinstance(payload.get("provenance"), dict):
                            observed_sources[row["tool"]] = payload["provenance"]
                required = {"source.cisa-known-exploited-vulnerabilities", "source.first-epss"}
                assert required.issubset(observed_sources), "Both actual sources were not used"
                checks = []
                for name in sorted(required):
                    provenance = observed_sources[name]
                    assert provenance.get("retrieved_at") and provenance["retrieved_at"] in text, "Actual retrieval timestamp missing from file"
                    assert provenance.get("source_url") and provenance["source_url"] in text, "Actual source URL missing from file"
                    checks.append({"source": name, "provenance": provenance, "verdict": "SUPPORTED_ACTUAL_TOOL_OBSERVATION"})
                assert "CVE-2021-44228" in text
                assert "Observation timestamps, not source publication dates" in text
                assert completed["execution_metrics"]["outcome_obligations"]["artifact_obligations_met"]
                record["checked_receipts"] = checks
                record["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            evidence.case("live-research-actual-source-receipts", context, page, run_case)
            context.close(); browser.close()
    except Exception:
        record["setup_or_harness_failure"] = redact(traceback.format_exc())
    finally:
        runtime.stop()
        if turn is not None and budget is not None:
            budget.end_turn(turn)
        if budget is not None: after = budget.summary()
        for name in ("keys.json", ".keystore_secret"):
            target = private / "memory" / name
            if target.exists() and target.resolve().parent == (private / "memory").resolve(): target.unlink()
        for handle in runtime.handles: handle.close()
        record.update(cases=evidence.cases, source_changed_during_run=initial != source_stamp(), budget_before=before, budget_after=after,
                      verdict="PASS" if evidence.cases and all(row["status"] == "PASS" for row in evidence.cases)
                      and not record.get("setup_or_harness_failure") and initial == source_stamp() else "FAIL")
        if before and after:
            record["attributed_generation_cost_delta"] = after["attributed_generation_cost_usd"] - before["attributed_generation_cost_usd"]
        (output / "report.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"verdict": record["verdict"], "report": str(output / "report.json")}), flush=True)
    return 0 if record["verdict"] == "PASS" else 1


if __name__ == "__main__": raise SystemExit(main())
