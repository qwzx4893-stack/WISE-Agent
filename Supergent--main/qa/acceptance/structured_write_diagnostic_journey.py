"""One real UI/CISA/FIRST write diagnostic, using the EXISTING shared $1 ledger.

Explicit command required; importing or --help never dispatches paid work.
Synthetic analysis fixture only. Not a release or semantic certification.
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

from user_journeys import ROOT, BRAVE, Runtime, Evidence
from source_stamp import source_stamp
from live_budget import LiveBudget
from owned_browser import owned_playwright, browser_environment

sys.path.insert(0, str(ROOT))
from qa.acceptance.structured_write_evidence import public_goal, CVE
from qa.acceptance.tool_evidence import redact


class NarrowEvidence(Evidence):
    """Final screenshot/log only: no continuous trace during API polling."""
    def case(self, name, context, page, action):
        self.steps = []
        logs, errors = [], []
        def on_error(error):
            if len(errors) < 50:
                errors.append(str(error)[:4000])
        def on_console(message):
            if len(logs) < 200:
                logs.append({"level": message.type, "text": message.text[:2000]})
        page.on("pageerror", on_error)
        page.on("console", on_console)
        record = {"id": name, "status": "PASS", "trace_profile": "DISABLED_CONTINUOUS_CAPTURE_FINAL_SCREENSHOT_AND_LOG_ONLY"}
        started = time.perf_counter()
        try:
            action()
            if errors:
                raise AssertionError(f"JavaScript errors: {errors}")
        except Exception:
            record.update(status="FAIL", stack_trace=redact(traceback.format_exc()))
        finally:
            screenshot = self.output / f"{name}.png"
            try:
                page.screenshot(path=str(screenshot), full_page=True)
                record["screenshot"] = str(screenshot)
            except Exception as exc:
                record["screenshot_error"] = str(exc)
            page.remove_listener("pageerror", on_error)
            page.remove_listener("console", on_console)
            log = self.output / f"{name}.log.json"
            log.write_text(json.dumps(redact({"console": logs, "page_errors": errors,
                           "stack_trace": record.get("stack_trace")}), ensure_ascii=False, indent=2), encoding="utf-8")
            record.update(steps=list(self.steps), log=str(log), duration_s=round(time.perf_counter() - started, 2),
                          backend_log=str(self.output / "backend.log"))
            self.cases.append(record)
            print(f"{record['status']} {name}", flush=True)


def inspect_artifact(text, tool_rows):
    observed = {}
    for row in tool_rows:
        tool = row.get("tool")
        result = row.get("result", {})
        payload = result.get("output")
        if (tool in {"source.cisa-known-exploited-vulnerabilities", "source.first-epss"}
                and result.get("success") and isinstance(payload, dict)):
            records = payload.get("records")
            if not isinstance(records, list):
                continue
            matching = [record for record in records if isinstance(record, dict)
                        and str(record.get("cveID") or record.get("cve", "")).upper() == CVE]
            if matching:
                observed[tool] = (matching[0], payload.get("provenance", {}))
    assert set(observed) == {"source.cisa-known-exploited-vulnerabilities", "source.first-epss"}, "Both actual public adapters were not used"
    assert CVE in text, "Requested CVE absent from actual artifact"
    for tool, (record, provenance) in observed.items():
        fields = ("dateAdded",) if tool.endswith("cisa-known-exploited-vulnerabilities") else ("epss", "date")
        for field in fields:
            assert isinstance(record.get(field), str) and record[field] in text, f"Actual unrounded {field} missing"
        assert provenance.get("source_url") and provenance["source_url"] in text, "Actual source URL missing"
        assert provenance.get("retrieved_at") and provenance["retrieved_at"] in text, "Actual retrieval receipt missing"
    return {"coverage": "EXACT_OBSERVED_PUBLIC_API_FIELD_AND_RECEIPT_PRESENCE_ONLY",
            "note": "Presence is not proof of complete semantic interpretation or general correctness",
            "artifact_sha256": hashlib.sha256(text.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-additional-usd", type=float, required=True)
    args = parser.parse_args()
    if args.max_additional_usd != 1:
        raise ValueError("This diagnostic must reuse the existing approved $1 cap, never replace or increase it")
    ledger = ROOT / "qa/results/additional-budget-20261002.json"
    preflight = json.loads(ledger.read_text(encoding="utf-8"))
    if preflight.get("active_turn") is not None:
        raise RuntimeError("Another paid QA turn is active; no diagnostic dispatched")
    if str(preflight.get("approved_additional_usd")) not in {"1", "1.0"}:
        raise RuntimeError("Expected existing approved $1 shared ledger; no diagnostic dispatched")
    output = ROOT / "qa-results" / ("structured-write-diagnostic-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir()
    private = ROOT / ".tooling" / ("project-ui-runtime-" + uuid.uuid4().hex)
    runtime = Runtime(output, runtime_root=private, app_target="qa.acceptance.instrumented_app:app")
    evidence = NarrowEvidence(output, runtime)
    initial = source_stamp()
    budget = before = after = None
    turn = None
    record = {"schema": "wise.structured-write-diagnostic.v1", "source_stamp": initial,
              "runtime": str(private), "release_verdict": "NOT_READY",
              "trace_profile": "DISABLED_CONTINUOUS_CAPTURE_FINAL_SCREENSHOT_AND_LOG_ONLY",
              "limitations": ["One narrow synthetic public project case, not a release or semantic certification",
                              "No private accounts, messages, scans, voice, user sessions or application edits"]}
    try:
        from core.llm.keystore import KeyStore
        entry = KeyStore(path=ROOT / "memory/keys.json", secret_path=ROOT / "memory/.keystore_secret").get_for_provider("openrouter", reveal=True)
        if not entry:
            raise RuntimeError("Configured paid QA key unavailable")
        model = next(row for row in httpx.get("https://openrouter.ai/api/v1/models", timeout=20).json()["data"]
                     if row["id"] == "qwen/qwen3-coder-flash")
        budget = LiveBudget(ledger, api_key=entry["api_key"])
        before = budget.initialize(additional_usd=1, model=model["id"], pricing=model["pricing"])
        observations = output / "actual-tool-observations.ndjson"
        drafts = output / "structured-write-observations.ndjson"
        runtime.env["WISE_QA_BUDGET_PATH"] = str(ledger)
        runtime.env["WISE_QA_TOOL_EVIDENCE_PATH"] = str(observations)
        runtime.env["WISE_QA_STRUCTURED_WRITE_EVIDENCE_PATH"] = str(drafts)
        # Explicit synthetic input, no imported user file or predecessor run.
        (private / "workspace/trial-1-analysis.json").write_text(json.dumps({
            "row_count": 3, "column_names": ["software", "cve"], "first_row_cve": CVE}), encoding="utf-8")
        for name in ("keys.json", ".keystore_secret"):
            source = ROOT / "memory" / name
            if source.exists():
                shutil.copy2(source, private / "memory" / name)
        runtime.start()
        configured = httpx.post(runtime.url + "/api/v2/models/external", json={"provider_id": "openrouter",
                    "model": model["id"], "set_as_active": True}, timeout=30)
        configured.raise_for_status()
        assert configured.json().get("ok"), "Stored QA credential unavailable"
        del entry
        with owned_playwright(output) as pw:
            browser = pw.chromium.launch(executable_path=str(BRAVE), headless=True, env=browser_environment(output))
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(runtime.url + "/app/", wait_until="networkidle")

            def run_case():
                nonlocal turn
                turn = budget.begin_turn(max_requests=12)
                evidence.step("Type exact public CISA/FIRST synthetic project request", lambda: page.locator("#prompt").fill(public_goal(1)))
                with page.expect_response(lambda r: r.url.endswith("/api/v2/chat/events") and r.request.method == "POST", timeout=240000) as pending:
                    evidence.step("Submit through WISE Send button", lambda: page.locator("#sendButton").click())
                request_key = pending.value.request.headers["idempotency-key"]
                deadline, completed = time.monotonic() + 240, None
                while time.monotonic() < deadline:
                    status = httpx.get(runtime.url + "/api/v2/chat/requests/" + request_key, timeout=10).json()
                    if status.get("state") == "COMPLETED":
                        completed = status["response"]
                        break
                    if status.get("state") == "UNKNOWN":
                        raise AssertionError("Request did not complete")
                    page.wait_for_timeout(250)
                assert completed is not None, "No completed response"
                budget.end_turn(turn)
                turn = None
                (output / "response.json").write_text(json.dumps(completed, ensure_ascii=False, indent=2), encoding="utf-8")
                expect(page.locator("#prompt")).to_be_enabled(timeout=10000)
                assert drafts.exists(), "No exact write proposal observed; diagnostic evidence incomplete"
                captured = [json.loads(line) for line in drafts.read_text(encoding="utf-8").splitlines()]
                record["captured_write_attempts"] = len(captured)
                record["observed_guard_errors"] = [row["guard_errors"] for row in captured]
                assert not completed.get("error"), completed.get("error")
                artifact = private / "workspace/trial-1-research.md"
                text = artifact.read_text(encoding="utf-8")
                rows = [json.loads(line) for line in observations.read_text(encoding="utf-8").splitlines()]
                record["field_presence_grader"] = inspect_artifact(text, rows)
                assert completed["execution_metrics"]["outcome_obligations"]["artifact_obligations_met"]

            evidence.case("live-structured-write-grounding-diagnostic", context, page, run_case)
            context.close()
            browser.close()
    except Exception:
        record["setup_or_harness_failure"] = redact(traceback.format_exc())
    finally:
        runtime.stop()
        if turn is not None and budget is not None:
            budget.end_turn(turn)
        if budget is not None:
            after = budget.summary()
        for name in ("keys.json", ".keystore_secret"):
            target = private / "memory" / name
            if target.exists() and target.resolve().parent == (private / "memory").resolve():
                target.unlink()
        for handle in runtime.handles:
            handle.close()
        record.update(cases=evidence.cases, source_changed_during_run=initial != source_stamp(),
                      budget_before=before, budget_after=after,
                      verdict="PASS" if evidence.cases and all(row["status"] == "PASS" for row in evidence.cases)
                      and not record.get("setup_or_harness_failure") and initial == source_stamp() else "FAIL")
        if before and after:
            record["attributed_generation_cost_delta"] = after["attributed_generation_cost_usd"] - before["attributed_generation_cost_usd"]
        (output / "report.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"verdict": record["verdict"], "report": str(output / "report.json")}), flush=True)
    return 0 if record["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
