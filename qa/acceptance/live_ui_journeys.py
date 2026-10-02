"""Opt-in real model journeys through WISE UI. No secrets in traces/reports."""
from __future__ import annotations
import json
import argparse
import shutil
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

import httpx
from playwright.sync_api import sync_playwright, expect
from user_journeys import ROOT, BRAVE, Runtime, Evidence


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--model",default="qwen/qwen3-coder-flash");args=parser.parse_args()
    output = ROOT / "qa-results" / ("live-ui-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(parents=True)
    private_root = ROOT / ".tooling" / ("live-ui-runtime-" + uuid.uuid4().hex)
    runtime = Runtime(output, runtime_root=private_root)
    evidence = Evidence(output, runtime)
    from source_stamp import source_stamp
    initial_stamp = source_stamp()
    cleanup = []
    try:
        # Only DPAPI-encrypted copies enter .tooling, never the evidence folder.
        for name in ("keys.json", ".keystore_secret"):
            source, target = ROOT / "memory" / name, private_root / "memory" / name
            if source.exists(): shutil.copy2(source, target); cleanup.append(target)
        runtime.start()
        models = httpx.get("https://openrouter.ai/api/v1/models", timeout=20).json()["data"]
        selected = next(item for item in models if item["id"] == args.model)
        prices = selected["pricing"]
        # Reserve each turn before submitting; team turns have a larger bound.
        unit_allowance = 16000 * float(prices["prompt"]) + 2048 * float(prices["completion"])
        worst_case = 10 * unit_allowance
        import sys
        sys.path.insert(0,str(ROOT))
        from core.llm.keystore import KeyStore
        entry = KeyStore(path=ROOT/"memory/keys.json",secret_path=ROOT/"memory/.keystore_secret").get_for_provider("openrouter",reveal=True)
        if not entry: raise RuntimeError("Live QA key unavailable")
        usage = httpx.get("https://openrouter.ai/api/v1/auth/key",headers={"Authorization":"Bearer "+entry["api_key"]},timeout=20)
        usage.raise_for_status(); spent = usage.json()["data"].get("usage")
        # Lifetime usage is deliberately conservative: a midnight reset must
        # never silently replenish this task's approved spending allowance.
        if not isinstance(spent,(float,int)) or spent < 0 or spent + worst_case > .50:
            raise RuntimeError("Live QA would exceed the approved $0.50 budget allowance")
        del entry
        configured = httpx.post(runtime.url + "/api/v2/models/external", json={"provider_id":"openrouter",
                                  "model":selected["id"], "set_as_active":True}, timeout=30)
        configured.raise_for_status()
        if not configured.json().get("ok"): raise RuntimeError("Stored OpenRouter credential is not available for QA")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(BRAVE), headless=True)
            context = browser.new_context(viewport={"width":1440,"height":900})
            page = context.new_page()
            page.goto(runtime.url + "/app/", wait_until="networkidle")
            expect(page.locator("#prompt")).to_be_visible()
            responses = []
            def send(text):
                entry = KeyStore(path=ROOT/"memory/keys.json",secret_path=ROOT/"memory/.keystore_secret").get_for_provider("openrouter",reveal=True)
                usage=httpx.get("https://openrouter.ai/api/v1/auth/key",headers={"Authorization":"Bearer "+entry["api_key"]},timeout=20)
                usage.raise_for_status(); spent=usage.json()["data"].get("usage")
                allowance=(24 if "Planner and researcher" in text else 10)*unit_allowance
                if not isinstance(spent,(float,int)) or spent<0 or spent+allowance>.50:
                    raise RuntimeError("Remaining approved allowance insufficient; no paid turn submitted")
                del entry
                evidence.step("Type current user request: " + text, lambda: page.locator("#prompt").fill(text))
                with page.expect_response(lambda r: r.url.endswith("/api/v2/chat/events") and r.request.method == "POST", timeout=180000) as pending:
                    evidence.step("Submit through WISE Send button", lambda: page.locator("#sendButton").click())
                response = pending.value
                # Chromium does not retain fetch-stream bodies after the UI
                # cancels its SSE reader. Inspect the actual completed request
                # by its key, read-only: no second paid model request/replay.
                key = response.request.headers["idempotency-key"]
                deadline, completed = time.monotonic() + 180, None
                while time.monotonic() < deadline:
                    status = httpx.get(runtime.url + "/api/v2/chat/requests/" + key, timeout=10).json()
                    if status.get("state") == "COMPLETED": completed = status["response"]; break
                    if status.get("state") == "UNKNOWN": raise AssertionError("Request failed or expired; inspect backend log")
                    page.wait_for_timeout(200)
                if completed is None: raise AssertionError("No completed response within bounded wait")
                responses.append(completed)
                # Wait for UI consumption even when the model reports failure.
                # The next scenario must not race the previous SSE reader.
                expect(page.locator("#prompt")).to_be_enabled(timeout=180000)
                assert not completed.get("error"), completed.get("error")
                assert completed.get("model_name"), "Missing actual model attribution"
                expect(page.locator("#sendButton")).to_be_disabled(timeout=180000)
                return completed
            def greeting():
                result = send("السلام عليكم")
                assert result["action_type"] == "DIRECT_ANSWER"
                assert result["reply_text"].strip()
                assert not any(item.get("stage") == "Tools" for item in result.get("milestones", []))
            evidence.case("live-ui-arabic-greeting", context, page, greeting)
            fixture = private_root / "workspace" / "qa-user-project.html"
            def build():
                send("Create qa-user-project.html in the WISE workspace, a minimal valid HTML page with one native button whose text is QA Original. Use native.write_file and read_file to verify the file. Do not access any other files or services.")
                assert fixture.exists() and "QA Original" in fixture.read_text(encoding="utf-8")
                assert any(call.get("tool") == "native.write_file" or call.get("id") == "native.write_file" for call in responses[-1].get("working_items", [])), "No observed file write call"
            evidence.case("live-ui-project-build-real-tools", context, page, build)
            # Keep the evidence page alive so failures always have a screenshot.
            def edit():
                send("Edit ONLY qa-user-project.html in WISE workspace: change QA Original to QA Saved. Read first, write the change, verify with read_file. Do not use shell or other services.")
                assert "QA Saved" in fixture.read_text(encoding="utf-8")
            evidence.case("live-ui-project-edit-real-tools", context, page, edit)
            def skill_review():
                result = send("Read the indexed skill fixing-accessibility with read_skill, then use native.read_file to inspect ONLY qa-user-project.html and report its accessible button text. Do not change anything or run shell, browser, messaging or network tools.")
                assert "fixing-accessibility" in result.get("execution_metrics",{}).get("skills_read",[]), "Skill was recommended but not actually read"
                assert any(item.get("tool") == "native.read_file" for item in result.get("working_items",[]))
                assert "QA Saved" in result["reply_text"]
            evidence.case("live-ui-actual-skill-read-and-file-inspection",context,page,skill_review)
            def team_review():
                evidence.step("Select bounded Agent team mode",lambda:page.locator("#prompt").fill("/team"))
                page.locator('[data-slash-id="team"]').click()
                result = send("Inspect ONLY qa-user-project.html. Create qa-team-review.txt containing the exact observed button text and a short verification note. Planner and researcher must not edit; executor alone creates the review file; reviewer must independently read both files. No shell, internet, browser or messages.")
                assert result["action_type"] == "MULTI_AGENT"
                metrics = result["execution_metrics"]
                assert metrics["architecture"] == "multi_agent"
                assert set(metrics["agents"]) == {"planner","researcher","executor","reviewer"}
                assert 1 <= metrics["model_requests"] <= 20
                review = private_root / "workspace/qa-team-review.txt"
                assert review.exists() and "QA Saved" in review.read_text(encoding="utf-8")
                assert any(item.get("tool") == "native.write_file" for item in result.get("working_items",[]))
                assert all(item.get("agent_role") == "executor" for item in result["working_items"] if item.get("tool") == "native.write_file")
                reviewer_paths = {(private_root / "workspace" / item["path"]).resolve() for item in result["working_items"]
                    if item.get("agent_role") == "reviewer" and item.get("tool") == "native.read_file" and item.get("success") and item.get("path")}
                assert {fixture.resolve(), review.resolve()} <= reviewer_paths, "Reviewer did not independently inspect both actual artifacts"
                page.locator("#composerSelection button").click()
            evidence.case("live-ui-real-multi-agent-one-writer-review",context,page,team_review)
            def current_research():
                evidence.step("Choose real Research mode through slash menu", lambda: page.locator("#prompt").fill("/research"))
                page.locator('[data-slash-id="research"]').click()
                result = send("ابحث لي عن أحدث نموذج من عائلة جيمناي Gemini مع روابط المصادر، ولا تدّعِ أنه الأحدث إذا لم تؤكد المصادر ذلك.")
                assert result["action_type"] == "RESEARCH"
                assert result["working_items"] and all(item.get("url") for item in result["working_items"])
                assert any(item.get("evidence_kind") == "page_excerpt" and item.get("retrieved_at") for item in result["working_items"]), "No actual fetched page evidence"
                assert result["reply_text"].startswith("حدود التحقق:"), "Missing bounded research coverage notice"
                assert "genshin" not in result["reply_text"].lower()
                assert any("\u0600" <= char <= "\u06ff" for char in result["reply_text"])
                page.locator("#composerSelection button").click()
            evidence.case("live-ui-arabic-current-gemini-research", context, page, current_research)
            def real_mcp():
                evidence.step("Open MCP settings", lambda:page.locator("#settingsButton").click())
                page.locator('[data-settings-tab="integrations"]').click()
                expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false")
                evidence.step("Import the official public Cloudflare docs server", lambda:page.locator("#mcpConfig").fill(json.dumps({
                    "mcpServers":{"qa-live-docs":{"url":"https://docs.mcp.cloudflare.com/mcp"}}})))
                page.locator('#mcpImportForm [type="submit"]').click()
                expect(page.locator('[data-mcp-name="qa-live-docs"][data-mcp-action="stop"]')).to_be_visible(timeout=60000)
                page.locator("#closeSettings").click()
                evidence.step("Select actual connected MCP in the composer", lambda:page.locator("#prompt").fill("/qa-live-docs"))
                page.locator('[data-slash-id="mcp:qa-live-docs"]').click()
                result = send("Use ONLY the selected Cloudflare documentation MCP to search official Workers documentation for CPU time limits. Report one documented limit with its plan/context and source URL. Do not use native web search, other MCPs, files, shell or messaging.")
                calls = result.get("working_items", [])
                assert any(item.get("tool", "").startswith("mcp.qa-live-docs.") and item.get("success") for item in calls), "No actual selected MCP invocation"
                assert not any(item.get("tool", "").startswith("mcp.") and not item["tool"].startswith("mcp.qa-live-docs.") for item in calls)
                assert not any(item.get("tool") in {"native.web_search","native.web_fetch","native.write_file"} for item in calls)
                assert "https://" in result["reply_text"]
                page.locator("#composerSelection button").click()
            evidence.case("live-ui-selected-public-mcp-actual-tool",context,page,real_mcp)
            def restart():
                before = fixture.read_bytes()
                evidence.step("Abruptly stop ONLY owned backend", lambda: runtime.stop(abrupt=True))
                evidence.step("Reopen same isolated application runtime", runtime.start)
                evidence.step("Reload application window", lambda: page.reload(wait_until="networkidle"))
                assert fixture.read_bytes() == before
                expect(page.locator("#sessionList")).not_to_be_empty()
            evidence.case("live-ui-project-save-restart", context, page, restart)
            context.close(); browser.close()
        (output / "responses.json").write_text(json.dumps(responses, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {"cases":evidence.cases, "verdict":"FAIL" if any(case["status"] == "FAIL" for case in evidence.cases) else "PASS",
                  "provider":"openrouter", "model":selected["id"], "worst_case_budget_usd":round(worst_case,4),
                  "billing_verified":False, "runtime":str(private_root)}
        report.update(source_stamp=initial_stamp,source_changed_during_run=initial_stamp != source_stamp())
        (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"verdict":report["verdict"], "report":str(output / "report.json")}), flush=True)
        return 0 if report["verdict"] == "PASS" else 1
    finally:
        runtime.stop()
        # Remove only this run's credential copies, never user credentials.
        for name in ("keys.json", ".keystore_secret"):
            target = private_root / "memory" / name
            if target.exists() and target.resolve().parent == (private_root / "memory").resolve(): target.unlink()
        for handle in runtime.handles: handle.close()


if __name__ == "__main__": raise SystemExit(main())
