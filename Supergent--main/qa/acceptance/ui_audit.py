"""Playwright + Axe audit of the WISE WebView surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from owned_browser import owned_playwright, browser_environment


ROOT = Path(__file__).resolve().parents[2]
BRAVE = Path.home() / "AppData/Local/BraveSoftware/Brave-Browser/Application/brave.exe"
AXE = ROOT / "qa/tooling/node_modules/axe-core/axe.min.js"


def _box(page, selector: str):
    return page.locator(selector).bounding_box()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    results: dict[str, object] = {"viewports": [], "failures": []}
    # WISE's native launcher enforces a 1024px minimum width.  Test the real
    # supported desktop range rather than awarding/denying a release based on
    # an unsupported phone layout.
    viewports = ((1440, 900), (1180, 760), (1024, 640))

    with owned_playwright(args.output) as playwright:
        executable = str(BRAVE) if BRAVE.exists() else None
        browser = playwright.chromium.launch(executable_path=executable, headless=True, env=browser_environment(args.output))
        for width, height in viewports:
            console_errors: list[str] = []
            network_errors: list[dict[str, object]] = []
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.on("pageerror", lambda error, bucket=console_errors: bucket.append(str(error)))
            page.on(
                "response",
                lambda response, bucket=network_errors: bucket.append({"url": response.url, "status": response.status})
                if response.status >= 400 else None,
            )
            page.goto(f"{args.base_url}/app/", wait_until="networkidle", timeout=90_000)
            page.locator("#prompt").wait_for(state="visible", timeout=20_000)

            composer = _box(page, "#composerForm")
            workspace = _box(page, "#chatView")
            logo = _box(page, ".wise-wordmark")
            centered = bool(
                composer and workspace
                and abs((composer["x"] + composer["width"] / 2) - (workspace["x"] + workspace["width"] / 2)) <= 24
            )
            composer_in_viewport = bool(
                composer
                and composer["x"] >= 0
                and composer["y"] >= 0
                and composer["x"] + composer["width"] <= width + 1
                and composer["y"] + composer["height"] <= height + 1
            )
            logo_in_viewport = bool(
                logo
                and logo["x"] >= 0
                and logo["y"] >= 0
                and logo["x"] + logo["width"] <= width + 1
                and logo["y"] + logo["height"] <= height + 1
            )
            no_overflow = page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
            send_disabled = page.locator("#sendButton").is_disabled()
            page.locator("#prompt").fill("اختبار واجهة آمن")
            send_enabled_after_input = page.locator("#sendButton").is_enabled()
            page.locator("#prompt").fill("")

            page.locator("#modelButton").click()
            page.locator("#modelMenu").wait_for(state="visible")
            model_button = _box(page, "#modelButton")
            model_menu = _box(page, "#modelMenu")
            model_rows = page.locator(".model-provider-row").count()
            menu_near_button = bool(
                model_button and model_menu
                and abs(model_menu["y"] + model_menu["height"] - model_button["y"]) <= 16
            )
            page.keyboard.press("Escape")

            page.locator("#settingsButton").click()
            page.locator("#settingsModal").wait_for(state="visible")
            page.locator("#settingsNav [data-settings-tab]").first.wait_for(state="visible", timeout=30_000)
            settings_tabs = page.locator("#settingsNav [data-settings-tab]").count()
            settings_scroll = page.locator("#settingsContent").evaluate(
                "el => ({clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, overflowY: getComputedStyle(el).overflowY})"
            )
            page.locator('[data-settings-tab="models"]').click()
            page.locator("#settingsContent .provider-directory-row").first.wait_for(state="visible", timeout=30_000)
            provider_rows = page.locator(".provider-directory-row").count()
            settings_models_real = provider_rows > 0 and page.locator(".provider-directory-row button, .provider-directory-row").count() > 0
            settings_shot = args.output / f"wise-settings-{width}x{height}.png"
            page.screenshot(path=str(settings_shot), full_page=True)
            page.keyboard.press("Escape")

            page.locator("#collapseSidebar").click()
            sidebar_collapsed = page.locator("#appShell").evaluate("el => el.classList.contains('sidebar-collapsed')")
            open_visible = page.locator("#openSidebar").is_visible()
            page.locator("#openSidebar").click()

            page.locator("#commandButton").click()
            command_visible = page.locator("#commandPalette").is_visible()
            page.keyboard.press("Escape")

            page.add_script_tag(path=str(AXE))
            axe = page.evaluate(
                "async () => await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})"
            )
            serious = [
                {"id": item["id"], "impact": item.get("impact"), "nodes": len(item.get("nodes", [])), "help": item.get("help"),
                 "targets": [node.get("target") for node in item.get("nodes", [])],
                 "details": [node.get("failureSummary") for node in item.get("nodes", [])]}
                for item in axe.get("violations", [])
                if item.get("impact") in {"serious", "critical"}
            ]

            shot = args.output / f"wise-{width}x{height}.png"
            page.screenshot(path=str(shot), full_page=True)
            record = {
                "viewport": [width, height],
                "centered_in_workspace": centered,
                "composer_in_viewport": composer_in_viewport,
                "logo_in_viewport": logo_in_viewport,
                "no_horizontal_overflow": no_overflow,
                "send_disabled_when_empty": send_disabled,
                "send_enabled_after_input": send_enabled_after_input,
                "model_provider_rows": model_rows,
                "model_menu_near_button": menu_near_button,
                "settings_tabs": settings_tabs,
                "settings_scroll": settings_scroll,
                "settings_models_real": settings_models_real,
                "settings_screenshot": str(settings_shot),
                "sidebar_collapsed": sidebar_collapsed,
                "sidebar_reopen_visible": open_visible,
                "command_palette_visible": command_visible,
                "console_errors": console_errors,
                "network_errors": network_errors,
                "axe_serious_or_critical": serious,
                "screenshot": str(shot),
            }
            results["viewports"].append(record)
            required = (
                centered, composer_in_viewport, logo_in_viewport, no_overflow,
                send_disabled, send_enabled_after_input,
                model_rows > 0, menu_near_button, settings_tabs >= 5,
                settings_models_real, sidebar_collapsed, open_visible, command_visible,
                not console_errors, not serious,
            )
            if not all(required):
                results["failures"].append({"viewport": [width, height], "record": record})
            page.close()
        browser.close()

    results["passed"] = not results["failures"]
    (args.output / "ui-audit.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": results["passed"], "failed_viewports": len(results["failures"])}, ensure_ascii=False))
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
