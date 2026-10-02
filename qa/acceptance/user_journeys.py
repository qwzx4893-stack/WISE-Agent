"""Real UI journeys against an owned, isolated WISE runtime; no mocked APIs.

Every case records steps, a screenshot, Playwright trace and structured logs.
Native FlaUI runs only against the PID this runner starts, never a title match.
"""
from __future__ import annotations
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BRAVE = Path.home() / "AppData/Local/BraveSoftware/Brave-Browser/Application/brave.exe"


class Runtime:
    def __init__(self, output, runtime_root=None):
        self.output = output
        self.root = runtime_root or output / "runtime"
        self.root.mkdir()
        for directory in ("config", "memory", "sessions", "workspace", "logs"):
            (self.root / directory).mkdir()
        # Test fixtures, not fabricated model output or copies of user sessions.
        fixture = {"session_id": "qa-persist", "history": [{"role": "user", "content": "QA fixture: مرحباً 🙂 <script>window.bad=1</script>"}],
                   "metadata": {"title": "QA persistence", "archived": False}}
        (self.root / "sessions/qa-persist.json").write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        (self.root / "sessions/corrupt.json").write_text("{broken", encoding="utf-8")
        (self.root / "memory/mcp_servers.json").write_text("[]", encoding="utf-8")
        # Force keyless operation, regardless of inherited provider env vars.
        self.env = {key: value for key, value in os.environ.items()
                    if not any(word in key.upper() for word in ("API_KEY", "AGENT_TOKEN", "OPENROUTER", "ANTHROPIC"))}
        self.env.update(WISE_RUNTIME_ROOT=str(self.root), WISE_CONFIG_DIR=str(self.root / "config"),
                        PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None
        self.handles = []

    def start(self):
        # Never silently attach to a leftover server and call it a restart.
        try:
            httpx.get(self.url + "/health", timeout=.5).raise_for_status()
        except httpx.HTTPError:
            pass
        else:
            raise RuntimeError("Owned test port is still serving before startup; restart evidence would be invalid")
        handle = (self.output / "backend.log").open("a", encoding="utf-8")
        self.handles.append(handle)
        self.process = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.server:app", "--host", "127.0.0.1",
                                         "--port", str(self.port), "--log-level", "info"],
                                        cwd=ROOT, env=self.env, stdout=handle, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Owned backend exited: {self.process.returncode}")
            try:
                if httpx.get(self.url + "/health", timeout=2).status_code == 200: return
            except httpx.HTTPError: pass
            time.sleep(.3)
        raise TimeoutError("Owned backend failed to become healthy")

    def stop(self, abrupt=False):
        if self.process and self.process.poll() is None:
            import psutil
            children = psutil.Process(self.process.pid).children(recursive=True)
            for child in reversed(children):
                try: (child.kill if abrupt else child.terminate)()
                except psutil.NoSuchProcess: pass
            (self.process.kill if abrupt else self.process.terminate)()
            self.process.wait(timeout=20)
            _, remaining = psutil.wait_procs(children, timeout=10)
            for child in remaining:
                child.kill(); child.wait(timeout=5)
        for handle in self.handles: handle.flush()


class Evidence:
    def __init__(self, output, runtime):
        self.output, self.runtime = output, runtime
        self.cases, self.steps = [], []

    def step(self, description, action):
        self.steps.append(description)
        return action()

    def case(self, name, context, page, action):
        self.steps = []
        logs, errors = [], []
        on_error = lambda error: errors.append(str(error))
        on_console = lambda msg: logs.append({"level": msg.type, "text": msg.text})
        page.on("pageerror", on_error); page.on("console", on_console)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        record = {"id": name, "status": "PASS"}
        started = time.perf_counter()
        try:
            action()
            if errors: raise AssertionError(f"JavaScript errors: {errors}")
        except Exception:
            record.update(status="FAIL", stack_trace=traceback.format_exc())
        finally:
            screenshot = self.output / f"{name}.png"
            try: page.screenshot(path=str(screenshot), full_page=True)
            except Exception as exc: record["screenshot_error"] = str(exc)
            trace = self.output / f"{name}.trace.zip"
            context.tracing.stop(path=str(trace))
            page.remove_listener("pageerror", on_error); page.remove_listener("console", on_console)
            log = self.output / f"{name}.log.json"
            log.write_text(json.dumps({"console": logs, "page_errors": errors, "stack_trace": record.get("stack_trace")},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
            record.update(steps=list(self.steps), screenshot=str(screenshot), trace=str(trace), log=str(log),
                          duration_s=round(time.perf_counter() - started, 2), backend_log=str(self.output / "backend.log"))
            self.cases.append(record)
            print(f"{record['status']} {name}", flush=True)
            if record["status"] == "FAIL":
                # Capture first, then isolate later cases from an open modal
                # or broken page. Recovery does not turn this case into PASS.
                try:
                    page.reload(wait_until="domcontentloaded",timeout=10000)
                    record["harness_recovery"] = "Reloaded the isolated page after preserving failure evidence"
                except Exception: pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--network-mcp", action="store_true", help="Connect to public Cloudflare documentation MCP; no account login")
    args = parser.parse_args()
    output = (args.output or ROOT / "qa-results" / ("user-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))).resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = Runtime(output); evidence = Evidence(output, runtime)
    from source_stamp import source_stamp
    initial_stamp = source_stamp()
    blockers = ["Live model/tool/skill execution is a separate opt-in gate; these UI cases do not certify model answers.",
                "Real-account OAuth completion, voice/GPU and long-running reliability require additional acceptance evidence."]
    try:
        runtime.start()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(BRAVE), headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            step = evidence.step
            def open_app():
                step("Open WISE against isolated loopback backend", lambda: page.goto(runtime.url + "/app/", wait_until="networkidle"))
                expect(page.locator("#prompt")).to_be_visible()
                page.wait_for_timeout(500)
            def smoke():
                open_app()
                expect(page.locator(".wise-wordmark")).to_be_visible()
                expect(page.locator('[data-view="tasks"]')).to_have_count(0)
                expect(page.locator("#sendButton")).to_be_disabled()
                step("Open settings", lambda: page.locator("#settingsButton").click())
                expect(page.locator("#settingsModal")).to_be_visible()
                for button in page.locator("#settingsNav button").all():
                    step("Open settings section: " + button.inner_text(), button.click)
                    expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false")
                step("Close settings", lambda: page.locator("#closeSettings").click())
            evidence.case("smoke-settings-navigation", context, page, smoke)
            def layout():
                for width, height in ((1440,900), (1180,760), (1024,640)):
                    step(f"Resize to {width}x{height}", lambda w=width,h=height: page.set_viewport_size({"width":w,"height":h}))
                    for collapse in (True, False):
                        step("Toggle sidebar", lambda c=collapse: page.locator("#collapseSidebar" if c else "#openSidebar").click())
                        page.wait_for_timeout(300)
                        workspace = page.locator("#chatView").bounding_box(); composer = page.locator("#composerForm").bounding_box()
                        assert abs(composer["x"] + composer["width"]/2 - workspace["x"] - workspace["width"]/2) < 3
                        assert 0 <= composer["y"] and composer["y"] + composer["height"] <= height
                page.set_viewport_size({"width":1440,"height":900})
            evidence.case("regression-composer-centering", context, page, layout)
            def settings_persistence():
                step("Open General settings", lambda: page.locator("#settingsButton").click())
                expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false")
                step("Choose English", lambda: page.locator("#language").select_option("en"))
                step("Save through actual settings API", lambda: page.locator('[data-save-settings="general"]').click())
                expect(page.locator("html")).to_have_attribute("lang", "en")
                close = page.locator("#closeSettings").bounding_box(); dialog = page.locator(".settings-dialog").bounding_box()
                assert dialog["x"] + dialog["width"] - close["x"] - close["width"] <= 25
                for button in page.locator(".settings-dialog .primary-button").all(): assert button.bounding_box()["height"] <= 38
                toast = page.locator(".toast").last
                if toast.is_visible(): assert toast.bounding_box()["width"] <= 311
                step("Close settings before closing application", lambda: page.locator("#closeSettings").click())
            evidence.case("journey-save-settings-close", context, page, settings_persistence)
            context.close()
            # A closed page cannot supply a screenshot; reopen for restart case.
            context = browser.new_context(viewport={"width":1440,"height":900}); page = context.new_page()
            def crash_restart():
                step("Kill ONLY owned backend PID", lambda: runtime.stop(abrupt=True))
                step("Restart same isolated runtime", runtime.start)
                open_app()
                expect(page.locator("html")).to_have_attribute("lang", "en")
                assert (runtime.root / "sessions/corrupt.json").read_text() == "{broken"
                expect(page.locator("#sessionList")).to_contain_text("QA persistence")
                step("Open persisted conversation", lambda: page.get_by_text("QA persistence", exact=True).click())
                expect(page.locator("#messageList")).to_contain_text("<script>")
                assert page.evaluate("window.bad === undefined")
            evidence.case("fault-abrupt-restart-corrupt-file-xss", context, page, crash_restart)
            def rename():
                step("Open conversation menu", lambda: page.locator(".session-more").first.click())
                page.once("dialog", lambda dialog: dialog.accept("QA renamed العربية 🙂"))
                step("Rename conversation", lambda: page.locator('[data-session-action="rename"]').click())
                expect(page.locator("#sessionList")).to_contain_text("QA renamed العربية 🙂")
                step("Reload and verify durable rename", lambda: page.reload(wait_until="networkidle"))
                expect(page.locator("#sessionList")).to_contain_text("QA renamed العربية 🙂")
            evidence.case("journey-session-rename-persistence", context, page, rename)
            def slash():
                step("Reload then immediately type before async catalog settles", lambda: page.reload(wait_until="domcontentloaded"))
                step("Type slash", lambda: page.locator("#prompt").fill("/"))
                expect(page.locator("#slashMenu")).to_be_visible()
                expect(page.locator('[data-slash-id="research"]')).to_be_visible()
                step("Choose Research with keyboard", lambda: page.locator("#prompt").press("ArrowDown"))
                page.locator("#prompt").press("Enter")
                expect(page.locator("#composerSelection")).to_contain_text("Research")
                assert httpx.get(runtime.url + "/api/v2/chat/commands").json()["commands"][1]["mode"] == "research"
                step("Clear mode", lambda: page.locator("#composerSelection button").click())
                expect(page.locator("#composerSelection")).to_be_hidden()
            evidence.case("regression-slash-real-commands", context, page, slash)
            def team_selection():
                step("Type team slash command", lambda: page.locator("#prompt").fill("/team"))
                expect(page.locator('[data-slash-id="team"]')).to_be_enabled()
                page.locator('[data-slash-id="team"]').click()
                expect(page.locator("#composerSelection")).to_contain_text("Agent team")
                page.locator("#composerSelection button").click()
            evidence.case("regression-team-mode-real-selection", context, page, team_selection)
            def capability_browser():
                step("Open Knowledge & skills",lambda:page.locator("#settingsButton").click())
                page.locator('[data-settings-tab="knowledge"]').click()
                expect(page.locator("#capabilityKind")).to_be_visible()
                browser_bounds = page.locator("#capabilityKind").bounding_box()
                settings_bounds = page.locator("#settingsContent").bounding_box()
                assert settings_bounds["y"] <= browser_bounds["y"] <= settings_bounds["y"] + settings_bounds["height"]
                expect(page.locator(".knowledge-source-list")).not_to_have_attribute("open", "")
                step("Browse imported reference resources",lambda:page.locator("#capabilityKind").select_option("resources"))
                expect(page.locator("#capabilityResults article")).to_have_count(10)
                expect(page.locator("#capabilityResults")).to_contain_text("required")
                first = page.locator("#capabilityResults").inner_text()
                step("Load next bounded page",lambda:page.locator("#capabilityNext").click())
                expect(page.locator("#capabilityCount")).to_contain_text("11–20")
                assert first != page.locator("#capabilityResults").inner_text()
                step("Search exact user-requested website",lambda:page.locator("#capabilityQuery").fill("drivenlisten.com"))
                expect(page.locator("#capabilityResults")).to_contain_text("drivenlisten.com")
                page.locator("#capabilityQuery").fill("")
                step("Browse actual grouped tools",lambda:page.locator("#capabilityKind").select_option("tools"))
                page.locator("#capabilityQuery").fill("security_scan")
                expect(page.locator("#capabilityResults")).to_contain_text("security_scan")
                page.locator("#closeSettings").click()
            evidence.case("journey-grouped-paginated-capability-browser",context,page,capability_browser)
            def messaging_form():
                step("Open dedicated Messaging section",lambda:page.locator('[data-view="messaging"]').click())
                expect(page.locator("#messagingSearch")).to_be_visible()
                page.locator("#messagingSearch").fill("Telegram")
                step("Open actual Telegram connection schema",lambda:page.locator('[data-messaging-service="tgram"]').click())
                expect(page.locator('[data-messaging-service="tgram"] img')).to_have_attribute("src", "/app/assets/messaging/telegram.svg")
                page.wait_for_function("()=>document.querySelector('[data-messaging-service=\"tgram\"] img').naturalWidth > 0")
                expect(page.locator('[name="bot_token"]')).to_be_visible()
                expect(page.locator('[name="targets"]')).to_be_visible()
                expect(page.locator("#messagingEditor")).to_contain_text("does not receive conversations")
                assert page.locator('#messagingConnectionForm [type="submit"]').bounding_box()["width"] < 260
                page.locator("#messagingConnectionName").fill("qa_telegram")
                page.locator('[name="bot_token"]').fill("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
                page.locator('[name="targets"]').fill("123456789")
                step("Persist synthetic credentials only; do NOT send a message",lambda:page.locator('#messagingConnectionForm [type="submit"]').click())
                expect(page.locator(".messaging-connections")).to_contain_text("qa_telegram")
                configured = httpx.get(runtime.url+"/admin/channels").json()
                assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in json.dumps(configured)
                step("Reload and verify durable connection",lambda:page.reload(wait_until="networkidle"))
                page.locator('[data-view="messaging"]').click()
                expect(page.locator(".messaging-connections")).to_contain_text("qa_telegram")
                page.once("dialog",lambda dialog:dialog.accept())
                step("Delete only synthetic connection",lambda:page.locator('[data-messaging-remove="qa_telegram"]').click())
                expect(page.locator(".messaging-connections")).not_to_contain_text("qa_telegram")
                for name,scheme,field in (("Discord","discord","webhook_token"),("WhatsApp","whatsapp","from_phone_id")):
                    page.locator("#messagingSearch").fill(name)
                    page.locator(f'[data-messaging-service="{scheme}"]').click()
                    expect(page.locator(f'[name="{field}"]')).to_be_visible()
                page.locator('[data-view="chat"]').click()
            evidence.case("journey-messaging-real-schema-save-reopen",context,page,messaging_form)
            def mcp_validation():
                step("Open Integrations", lambda: page.locator("#settingsButton").click())
                step("Open MCP settings", lambda: page.locator('[data-settings-tab="integrations"]').click())
                expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false")
                page.locator("#mcpConfig").fill("{broken JSON")
                step("Submit malformed MCP configuration", lambda: page.locator('#mcpImportForm [type="submit"]').click())
                expect(page.locator("#mcpImportResult")).not_to_be_empty()
                expect(page.locator("#mcpConfig")).to_have_value("{broken JSON")
                step("Close settings", lambda: page.locator("#closeSettings").click())
            evidence.case("fault-malformed-mcp-input", context, page, mcp_validation)
            if args.network_mcp:
                def public_mcp():
                    step("Open integrations", lambda: page.locator("#settingsButton").click())
                    page.locator('[data-settings-tab="integrations"]').click()
                    expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false")
                    step("Paste official public documentation MCP", lambda: page.locator("#mcpConfig").fill(json.dumps({"mcpServers":{"qa-docs":{"url":"https://docs.mcp.cloudflare.com/mcp"}}})))
                    step("Save and connect real HTTP MCP", lambda: page.locator('#mcpImportForm [type="submit"]').click())
                    expect(page.locator("#settingsContent")).to_contain_text("qa-docs", timeout=60000)
                    expect(page.locator('[data-mcp-name="qa-docs"][data-mcp-action="stop"]')).to_be_visible(timeout=60000)
                    step("Verify connected public server does not advertise needless login", lambda: expect(page.locator('[data-mcp-name="qa-docs"][data-mcp-action="authorize"]')).to_have_count(0))
                    page.locator("#closeSettings").click()
                    step("Choose actual connected MCP from slash menu", lambda: page.locator("#prompt").fill("/qa-docs"))
                    expect(page.locator('[data-slash-id="mcp:qa-docs"]')).to_be_enabled()
                    page.locator('[data-slash-id="mcp:qa-docs"]').click()
                    expect(page.locator("#composerSelection")).to_contain_text("qa-docs")
                    page.locator("#composerSelection button").click()
                evidence.case("smoke-real-mcp-import-auth-feedback-slash", context, page, public_mcp)
            def attachment():
                fixture = runtime.root / "workspace" / "مرحبا-qa.txt"
                fixture.write_text("Synthetic QA attachment; no secrets", encoding="utf-8")
                step("Attach a real local file", lambda: page.locator("#fileInput").set_input_files(str(fixture)))
                expect(page.locator("#attachmentList")).to_contain_text("مرحبا-qa.txt")
                response = httpx.post(runtime.url + "/api/v2/attachments", json={"name":"../bad.txt","data_base64":"!not-base64"})
                assert response.status_code == 422
                step("Clear attachment", lambda: page.locator("#attachmentList button").first.click())
            evidence.case("smoke-attachment-invalid-encoding", context, page, attachment)
            def rapid():
                before = httpx.get(runtime.url + "/api/v2/sessions").json()
                for i in range(12): step(f"New chat click {i+1}", lambda: page.locator('[data-view="chat"]').click())
                assert httpx.get(runtime.url + "/api/v2/sessions").json() == before
                step("Type unusual text safely", lambda: page.locator("#prompt").fill("🙂 العربية English <img src=x onerror=alert(1)>\n" + "x"*2000))
                expect(page.locator("#sendButton")).to_be_enabled()
                page.locator("#prompt").fill("")
            evidence.case("fault-rapid-newchat-unicode-no-phantom-sessions", context, page, rapid)
            def multiwindow():
                second = context.new_page()
                step("Open second WISE window", lambda: second.goto(runtime.url + "/app/", wait_until="networkidle"))
                expect(second.locator("#prompt")).to_be_visible()
                step("Persist rename while two windows exist", lambda: httpx.patch(runtime.url + "/api/v2/sessions/qa-persist", json={"title":"QA two windows"}).raise_for_status())
                for target in (page, second):
                    target.reload(wait_until="networkidle")
                    expect(target.locator("#sessionList")).to_contain_text("QA two windows")
                second.close()
            evidence.case("regression-two-window-durable-state", context, page, multiwindow)
            def invalid_paths():
                response = step("Try nonexistent local model path through API", lambda: httpx.post(runtime.url + "/api/v2/models/local/import", json={"file_path": str(runtime.root / "missing.gguf")}))
                assert response.status_code < 500 and not response.json().get("ok")
                response = httpx.post(runtime.url + "/api/v2/chat", json={"message":"hi", "mode":"invented"})
                assert response.status_code == 422
                response = httpx.post(runtime.url + "/api/v2/chat", json={"message":"hi", "selected_mcp":"nonexistent"})
                assert response.status_code == 409
            evidence.case("fault-bad-path-unavailable-mode-mcp", context, page, invalid_paths)
            def scheduled_crud():
                step("Open Scheduled section", lambda: page.locator('[data-view="schedules"]').click())
                answers = iter(("QA future schedule", "Synthetic QA task; do not send messages", "0 0 1 1 *"))
                handler = lambda dialog: dialog.accept(next(answers))
                page.on("dialog", handler)
                try:
                    step("Create schedule through actual UI prompts; next January only", lambda: page.locator("#newSchedule").click())
                    expect(page.locator("#schedulesGrid")).to_contain_text("QA future schedule", timeout=10000)
                finally: page.remove_listener("dialog", handler)
                page.once("dialog", lambda dialog: dialog.accept())
                step("Delete only synthetic schedule", lambda: page.locator("[data-delete-schedule]").first.click())
                expect(page.locator("#schedulesGrid")).not_to_contain_text("QA future schedule")
                page.locator('[data-view="chat"]').click()
            evidence.case("smoke-scheduled-create-delete", context, page, scheduled_crud)
            def provider_validation():
                step("Open Models and providers", lambda: page.locator("#settingsButton").click())
                page.locator('[data-settings-tab="models"]').click()
                expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false")
                step("Open real OpenRouter configuration form", lambda: page.locator('[data-open-provider-settings="openrouter"]').click())
                expect(page.locator("#settingsProviderApiKey")).to_be_visible()
                expect(page.locator("#settingsProviderBaseUrl")).to_have_value("https://openrouter.ai/api/v1")
                step("Attempt save without required credential", lambda: page.locator('#settingsProviderForm [type="submit"]').click())
                expect(page.locator(".toast.error")).to_be_visible(timeout=10000)
                step("Close provider settings", lambda: page.locator("#closeSettings").click())
            evidence.case("smoke-provider-real-form-validation", context, page, provider_validation)
            def conversation_delete():
                step("Open synthetic conversation menu", lambda: page.locator(".session-more").first.click())
                page.once("dialog", lambda dialog: dialog.accept())
                step("Delete synthetic conversation through actual UI", lambda: page.locator('[data-session-action="delete"]').click())
                expect(page.locator(".session-row")).to_have_count(0)
                expect(page.locator("#prompt")).to_be_visible()
                expect(page.locator(".wise-wordmark")).to_be_visible()
            evidence.case("regression-delete-last-chat-stays-composer", context, page, conversation_delete)
            context.close(); browser.close()
        if args.native:
            from native_smoke import run_native
            native = run_native(runtime.port, output / "native", env=runtime.env)
            evidence.cases.append(native)
        else:
            blockers.append("Native FlaUI window checks were not requested (--native).")
    except Exception:
        evidence.cases.append({"id":"harness", "status":"FAIL", "stack_trace":traceback.format_exc(), "steps":["Start isolated runtime and browser"]})
    finally:
        runtime.stop()
        for handle in runtime.handles: handle.close()
        failed = [case for case in evidence.cases if case["status"] == "FAIL"]
        report = {"schema":"wise.user-acceptance.v1", "ui_verdict":"FAIL" if failed else "PASS",
                  "release_verdict":"NOT_READY" if failed or blockers else "READY", "cases":evidence.cases,
                  "unverified_gates":blockers, "isolated_runtime":str(runtime.root), "mocked_api_responses":False}
        report.update(source_stamp=initial_stamp,source_changed_during_run=initial_stamp != source_stamp())
        (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        lines = ["# WISE user acceptance", "", f"UI verdict: {report['ui_verdict']}", f"Release verdict: {report['release_verdict']}",
                 "", "Real UI + real backend, synthetic fixtures, no cloud spend, no user accounts modified.", ""]
        for case in evidence.cases:
            lines += [f"## {case['status']} — {case['id']}", "", "Reproduction:"]
            lines += [f"{i+1}. {description}" for i,description in enumerate(case.get("steps", []))]
            for key in ("screenshot", "log", "trace", "backend_log"):
                if key in case: lines.append(f"[{key}]({Path(case[key]).relative_to(output).as_posix()})")
            if case.get("stack_trace"): lines += ["```", case["stack_trace"], "```"]
            lines += [""]
        lines += ["## Unverified release gates", ""] + [f"- {item}" for item in blockers]
        (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        print(json.dumps({"ui_verdict":report["ui_verdict"], "report":str(output / "REPORT.md")}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__": raise SystemExit(main())
