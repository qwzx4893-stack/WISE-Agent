"""Bounded, keyless desktop-webview soak against an owned temporary runtime.

Not an 8-hour reliability certificate, microphone/voice test or cloud-agent
quality evaluation. Exercises real UI navigation and durable state repeatedly.
"""
from __future__ import annotations
import argparse
import json
import time
import traceback
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect
from source_stamp import ROOT, source_stamp
from user_journeys import Runtime, BRAVE
from process_resources import OwnedProcessSampler
from owned_browser import owned_playwright, browser_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 28_800 or not 5 <= args.interval <= 60:
        parser.error("Soak duration/interval outside explicit bounded limits")
    args.output.mkdir(parents=True, exist_ok=False)
    runtime = Runtime(args.output)
    initial = source_stamp()
    samples, cycles, failures, errors = [], [], [], []
    started = time.monotonic()
    report = {"schema": "wise.desktop-soak.v1", "scope": "KEYLESS_UI_AND_DURABLE_STATE_SOAK_NOT_AGENT_QUALITY",
              "source_stamp": initial, "requested_seconds": args.seconds, "mocked_api_responses": False}
    try:
        runtime.start()
        sampler = OwnedProcessSampler(runtime.process.pid)
        with owned_playwright(args.output) as pw:
            browser = pw.chromium.launch(executable_path=str(BRAVE), headless=True, env=browser_environment(args.output))
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(runtime.url + "/app/index.html", wait_until="networkidle")
            expect(page.locator("#prompt")).to_be_visible(timeout=15_000)
            with httpx.Client(base_url=runtime.url, timeout=20) as client:
                deadline = time.monotonic() + args.seconds
                index = 0
                while time.monotonic() < deadline:
                    cycle = {"index": index, "steps": [], "status": "PASS"}
                    tick = time.monotonic()
                    try:
                        cycle["steps"].append("Read actual health and assert ready owned service")
                        health = client.get("/health"); health.raise_for_status()
                        assert health.json()["service"] == "wise" and health.json()["status"] == "ok"
                        cycle["steps"].append("Open and close Settings through actual controls")
                        page.locator("#settingsButton").click()
                        expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false")
                        page.locator("#closeSettings").click()
                        cycle["steps"].append("Edit Unicode draft without creating phantom sessions")
                        page.locator("#prompt").fill(f"Soak draft {index}: العربية English 🙂")
                        page.locator("#prompt").fill("")
                        cycle["steps"].append("Save/reload synthetic preferences using actual revision-guarded API")
                        before = client.get("/api/v2/settings/instructions"); before.raise_for_status()
                        text = f"Soak preferences {index}: استخدم مصادر موثقة."
                        saved = client.put("/api/v2/settings/instructions", headers={"X-Wise-Action": "settings"},
                            json={"instructions": text, "expected_revision": before.json()["revision"]})
                        saved.raise_for_status()
                        assert client.get("/api/v2/settings/instructions").json()["instructions"] == text
                        assert not errors, "JavaScript page error: " + errors[-1]
                        if index and index % 10 == 0:
                            cycle["steps"].append("Reload actual view and confirm composer remains accessible")
                            page.reload(wait_until="networkidle")
                            expect(page.locator("#prompt")).to_be_visible()
                    except Exception:
                        cycle.update(status="FAIL", stack_trace=traceback.format_exc())
                        shot = args.output / f"failure-{index}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        cycle["screenshot"] = str(shot.resolve())
                        failures.append(cycle)
                    cycle["duration_s"] = round(time.monotonic() - tick, 3)
                    cycles.append(cycle)
                    samples.append({"elapsed_s": round(time.monotonic() - started, 2), **sampler.sample()})
                    (args.output / "progress.json").write_text(json.dumps({"cycles": len(cycles), "failures": len(failures),
                        "elapsed_s": round(time.monotonic()-started, 2)}, indent=2), encoding="utf-8")
                    if failures:
                        break
                    index += 1
                    time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
            screenshot = args.output / "final.png"
            page.screenshot(path=str(screenshot), full_page=True)
            report["screenshot"] = str(screenshot.resolve())
            context.tracing.stop(path=str(args.output / "soak.trace.zip"))
            context.close(); browser.close()
    except Exception:
        failures.append({"status": "FAIL", "steps": ["Start isolated backend/browser"], "stack_trace": traceback.format_exc()})
    finally:
        runtime.stop()
        for handle in runtime.handles: handle.close()
        report.update(cycles=cycles, failures=failures, page_errors=errors, samples=samples,
            elapsed_seconds=round(time.monotonic()-started, 2), source_changed_during_run=initial != source_stamp(),
            backend_log=str((args.output / "backend.log").resolve()))
        report["verdict"] = "PASS" if not failures and cycles and not report["source_changed_during_run"] else "FAIL"
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"verdict": report["verdict"], "cycles": len(cycles), "failures": len(failures),
                          "report": str((args.output / "report.json").resolve())}))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
