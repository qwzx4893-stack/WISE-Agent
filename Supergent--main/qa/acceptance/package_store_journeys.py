"""Real package-store UI + WISE backend; no model keys or account auth.

Browser plugin not available: uses owned headless Brave/Playwright. Catalogs
and plugin archives come from their real public repositories, not mocked APIs.
Screenshots/logs/stack traces remain in the explicitly owned QA evidence tree.
"""
from datetime import datetime, timezone
import json
import re
import sys
import traceback
from pathlib import Path
import httpx
from playwright.sync_api import expect
from user_journeys import ROOT, BRAVE, Runtime, Evidence
from owned_browser import owned_playwright, browser_environment
from source_stamp import source_stamp


def main():
    output = ROOT/"qa-results"/("package-store-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir()
    runtime = Runtime(output)
    runtime.env.update(AGENT_OS_SKIP_FIRST_BOOT_CHECK="1")
    evidence = Evidence(output,runtime,trace_visuals=False)
    stamp = source_stamp(); identifier = None; catalog = {}
    try:
        runtime.start()
        with owned_playwright(output) as pw:
            browser = pw.chromium.launch(executable_path=str(BRAVE),headless=True,env=browser_environment(output))
            context = browser.new_context(viewport={"width":1440,"height":900},reduced_motion="reduce")
            page = context.new_page();step = evidence.step
            def discover():
                nonlocal catalog
                step("Open explicit WISE package store",lambda:page.goto(runtime.url+"/app/#plugins/packages"))
                expect(page.locator(".plugin-card").first).to_be_visible(timeout=120000)
                expect(page.locator("#pluginDirectoryState")).not_to_contain_text("Updating",timeout=120000)
                catalog = httpx.get(runtime.url+"/api/v2/plugins",timeout=30).json()
                assert catalog["total"]>100 and all(i["kind"]=="plugin-package" for i in catalog["items"])
                assert not page.locator('[data-plugin-source="native"], [data-plugin-source="skills"], [data-plugin-source="featured"]').count()
                assert not page.locator('[data-plugin-action="import"], [data-plugin-action="refresh"]').count()
                assert not page.locator('.plugin-source-tabs, [data-plugin-action="marketplace"], [data-plugin-action="more"]').count()
                expect(page.locator('.plugin-section').first).to_be_visible()
                assert not any(i.get("tools") or i.get("recipe") for i in catalog["items"])
                assert page.title().startswith("WISE")
            evidence.case("actual-package-catalog-no-internal-inventory",context,page,discover)
            def search():
                nonlocal identifier
                step("Find the real frontend-design plugin package",lambda:page.locator("#pluginSearch").fill("frontend-design"))
                expect(page.locator(".plugin-card").first).to_be_visible(timeout=15000)
                payload = httpx.get(runtime.url+"/api/v2/plugins?query=frontend-design",timeout=30).json()
                row = next(i for i in payload["items"] if i["name"]=="frontend-design" and i["curated"])
                identifier = row["id"]
                step("Open information without downloading an archive",lambda:page.locator(f'[data-plugin-id="{identifier}"]').first.click())
                expect(page.locator('.plugin-overview')).to_be_visible(timeout=15000)
                assert not (runtime.root/'memory/plugin-package-cache').exists()
                step("Prepare package only when installation is requested",lambda:page.locator('[data-plugin-action="prepare"]').click())
                expect(page.locator("#pluginInstallForm")).to_be_visible(timeout=120000)
                expect(page.locator(".plugin-capabilities li").first).to_be_visible()
                detail = httpx.get(runtime.url+"/api/v2/plugins/packages/"+identifier,timeout=120).json()
                assert detail["plan"]["can_install"] and not detail["installed"] and detail["plan"]["workflows"]
                assert not detail["plan"]["unsupported"]
                assert not (runtime.root/"memory/plugin_packages.json").exists()
            evidence.case("real-package-preview-without-activation",context,page,search)
            def install():
                step("Explicitly trust reviewed plugin publisher",lambda:page.locator('#pluginInstallForm [name="trusted_publisher"]').check())
                step("Install actual package workflows",lambda:page.locator('#pluginInstallForm [type="submit"]').click())
                expect(page.locator('[data-plugin-action="prepare"]')).to_contain_text(re.compile('Manage|إدارة'),timeout=30000)
                step("Manage actual installed package",lambda:page.locator('[data-plugin-action="prepare"]').click())
                expect(page.locator('[data-plugin-action="disable"]')).to_be_visible(timeout=30000)
                state = json.loads((runtime.root/"memory/plugin_packages.json").read_text(encoding="utf-8"))
                record = state["packages"][identifier]
                assert record["enabled"] and record["plan"]["workflows"]
                package = runtime.root/"memory/plugin-package-cache"/identifier/record["entry"]["origin"]["sha"]
                assert all((package/w["file"]).is_file() for w in record["plan"]["workflows"])
                assert not record["connections"]
            evidence.case("real-package-install-not-a-cosmetic-status",context,page,install)
            def restart():
                step("Restart only the owned backend",lambda:(runtime.stop(),runtime.start()))
                step("Reopen installed package",lambda:page.goto(runtime.url+"/app/#plugins/"+identifier))
                expect(page.locator('[data-plugin-action="prepare"]')).to_be_visible(timeout=90000)
                step("Inspect persisted package settings",lambda:page.locator('[data-plugin-action="prepare"]').click())
                expect(page.locator('[data-plugin-action="disable"]')).to_be_visible(timeout=90000)
                assert httpx.get(runtime.url+"/api/v2/plugins?source=installed",timeout=30).json()["total"]==1
            evidence.case("installed-package-survives-backend-restart",context,page,restart)
            def disable():
                step("Disable installed package",lambda:page.locator('[data-plugin-action="disable"]').click())
                expect(page.locator('[data-plugin-action="prepare"]')).to_be_visible(timeout=30000)
                step("Inspect disabled package",lambda:page.locator('[data-plugin-action="prepare"]').click())
                expect(page.locator(".plugin-detail-heading")).to_contain_text(re.compile("Disabled|معطّلة"),timeout=30000)
                state = json.loads((runtime.root/"memory/plugin_packages.json").read_text(encoding="utf-8"))
                assert not state["packages"][identifier]["enabled"]
            evidence.case("package-disable-persists-real-state",context,page,disable)
            def uninstall():
                page.once("dialog",lambda d:d.accept())
                step("Uninstall package without deleting external accounts",lambda:page.locator('[data-plugin-action="uninstall"]').click())
                expect(page.locator("#pluginResults")).to_be_visible(timeout=15000)
                assert httpx.get(runtime.url+"/api/v2/plugins?source=installed",timeout=30).json()["total"]==0
            evidence.case("package-uninstall-removes-active-registration",context,page,uninstall)
            def marketplace():
                step('Clear search to return to store sections',lambda:page.locator('#pluginSearch').fill(''))
                expect(page.locator('[data-plugin-category="Development"]')).to_be_visible(timeout=15000)
                step('Open a complete category',lambda:page.locator('[data-plugin-category="Development"]').click())
                expect(page.locator('.plugin-card').first).to_be_visible(timeout=15000)
                assert '/category/Development' in page.url
                assert not page.locator('[data-plugin-action="more"]').count()
                total=httpx.get(runtime.url+'/api/v2/plugins?category=Development',timeout=30).json()['total']
                for batch in range((total+47)//48):
                    loaded=page.locator('.plugin-card').count()
                    if loaded>=total: break
                    step('Scroll to automatically load the next real batch',lambda:page.locator('#pluginsView').evaluate('el=>el.scrollTop=el.scrollHeight'))
                    page.wait_for_function('target=>document.querySelectorAll(".plugin-card").length>=target',arg=min(total,loaded+48),timeout=15000)
                assert page.locator('.plugin-card').count()==total
                step("Back to package store",lambda:page.locator('[data-plugin-action="back"]').click())
            evidence.case("category-navigation-without-manual-pagination",context,page,marketplace)
            def widths():
                step("Clear package search",lambda:page.locator("#pluginSearch").fill(""))
                expect(page.locator(".plugin-card").first).to_be_visible()
                for width in (1440,1024,800):
                    step(f"Desktop width {width}",lambda w=width:page.set_viewport_size({"width":w,"height":900}))
                    page.wait_for_timeout(300)
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                page.set_viewport_size({"width":1440,"height":900})
            evidence.case("package-store-desktop-widths",context,page,widths)
            def offline():
                step('Search plugin before network fault',lambda:page.locator('#pluginSearch').fill('frontend-design'))
                expect(page.locator(f'[data-plugin-id="{identifier}"]').first).to_be_visible(timeout=15000)
                step('Open available plugin information',lambda:page.locator(f'[data-plugin-id="{identifier}"]').first.click())
                expect(page.locator('.plugin-overview')).to_be_visible(timeout=15000)
                context.set_offline(True)
                try:
                    step('Request installation with browser network offline',lambda:page.locator('[data-plugin-action="prepare"]').click())
                    expect(page.locator('#pluginAuthState')).not_to_be_empty(timeout=15000)
                    expect(page.locator('.plugin-detail-heading')).to_contain_text('frontend-design')
                    expect(page.locator('[data-plugin-action="prepare"]')).to_be_enabled()
                finally:context.set_offline(False)
            evidence.case('offline-install-keeps-plugin-information',context,page,offline)
            context.close();browser.close()
    except Exception: evidence.cases.append({"id":"harness","status":"FAIL","stack_trace":traceback.format_exc()})
    finally:
        runtime.stop()
        for handle in runtime.handles:handle.close()
        failed = [c for c in evidence.cases if c["status"]=="FAIL"]
        report = {"schema":"wise.package-store-acceptance.v1","verdict":"FAIL" if failed else "PASS","cases":evidence.cases,
            "catalog_total":catalog.get("total"),"marketplaces":catalog.get("marketplaces"),"source_stamp":stamp,
            "source_changed_during_run":stamp!=source_stamp(),"mocked_api_responses":False,"account_auth_completed":False,
            "browser":"Owned headless Brave/Playwright; Browser plugin not available","paid_model_calls":0}
        (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"verdict":report["verdict"],"report":str(output/"report.json"),"catalog_total":report["catalog_total"]}),flush=True)
    return bool(failed)


if __name__=="__main__":raise SystemExit(main())
