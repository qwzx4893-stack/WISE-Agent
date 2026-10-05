"""Keyless owned Brave settings stress, not a day-long reliability certificate."""
from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from actionability_probe import install_probe, observe_actionability
from source_stamp import ROOT, source_stamp
from user_journeys import BRAVE, Runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=60)
    parser.add_argument("--interval-ms", type=int, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 150 or not 0 <= args.interval_ms <= 20000:
        parser.error("Bounded cycles/interval required")
    output = args.output or ROOT / "qa-results" / ("settings-actionability-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(parents=True, exist_ok=False)
    runtime = Runtime(output)
    # Keep Playwright/Node artifacts inside the owned test tree. This does not
    # relax filesystem permissions or attach to any existing browser profile.
    browser_temp = (output / "browser-temp").resolve()
    browser_temp.mkdir()
    previous_temp = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}
    initial, started = source_stamp(), time.monotonic()
    report = {"schema": "wise.settings-actionability.v1", "source_stamp": initial,
        "scope": "KEYLESS_OWNED_BRAVE_SETTINGS_BURST_NOT_LONG_SOAK", "requested_cycles": args.cycles,
        "mocked_api_responses": False, "forced_clicks": False, "cycles": [], "page_errors": []}
    try:
        runtime.start()
        for name in previous_temp:
            os.environ[name] = str(browser_temp)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(BRAVE), headless=True,
                env={**runtime.env, **{name: str(browser_temp) for name in previous_temp}})
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            install_probe(page)
            page.goto(runtime.url + "/app/", wait_until="networkidle")
            expect(page.locator("#prompt")).to_be_visible()
            for index in range(args.cycles):
                cycle = {"index": index, "status": "PASS", "before": observe_actionability(page)}
                tick = time.monotonic()
                try:
                    page.locator("#settingsButton").click(timeout=30000)
                    expect(page.locator("#settingsModal")).to_be_visible()
                    expect(page.locator("#settingsContent")).to_have_attribute("aria-busy", "false", timeout=15000)
                    page.locator("#closeSettings").click(timeout=30000)
                    expect(page.locator("#settingsModal")).to_be_hidden()
                    if report["page_errors"]:
                        raise AssertionError("Actual JavaScript page error")
                    if index and index % 20 == 0:
                        page.reload(wait_until="networkidle")
                        expect(page.locator("#prompt")).to_be_visible()
                except Exception:
                    cycle.update(status="FAIL", stack_trace=traceback.format_exc(),
                        failure_observation=observe_actionability(page))
                    page.screenshot(path=str(output / "failure.png"), full_page=True)
                cycle.update(duration_s=round(time.monotonic() - tick, 4), after=observe_actionability(page))
                report["cycles"].append(cycle)
                if cycle["status"] == "FAIL":
                    break
                if args.interval_ms:
                    page.wait_for_timeout(args.interval_ms)
            page.screenshot(path=str(output / "final.png"), full_page=True)
            context.tracing.stop(path=str(output / "settings.trace.zip"))
            context.close()
            browser.close()
    except Exception:
        report["runner_error"] = traceback.format_exc()
    finally:
        for name, value in previous_temp.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        runtime.stop()
        for handle in runtime.handles:
            handle.close()
        report.update(elapsed_s=round(time.monotonic() - started, 3),
            source_changed_during_run=initial != source_stamp(), runtime=str(runtime.root),
            runtime_url=runtime.url, owned_backend_pid=runtime.process.pid if runtime.process else None,
            backend_log=str(output / "backend.log"))
        report["verdict"] = "PASS" if len(report["cycles"]) == args.cycles and all(c["status"] == "PASS" for c in report["cycles"]) and not report.get("runner_error") and not report["source_changed_during_run"] else "FAIL"
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output / "report.json"), "verdict": report["verdict"], "cycles": len(report["cycles"])}), flush=True)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
