"""Actual UI/backend flows; only external credentials/browser launch are mocked.

Also tests the real unconfigured app with zero HTTP mocks. Does not touch user's
window, profiles, providers or accounts. Evidence explicitly labels the boundary.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback

from playwright.sync_api import expect
from owned_browser import owned_playwright, browser_environment

from user_journeys import ROOT, BRAVE, Runtime, Evidence
from source_stamp import source_stamp


def main():
    output=ROOT/"qa-results"/("plugins-ui-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(); stamp=source_stamp()
    cases=[]
    for boundary in (False,True):
        folder=output/("external-boundary" if boundary else "real-unconfigured"); folder.mkdir()
        runtime=Runtime(folder,app_target="qa.acceptance.plugin_boundary_app:app" if boundary else "api.server:app")
        if boundary: runtime.env.update(WISE_PLUGIN_BOUNDARY_TEST="1",NANGO_SECRET_KEY="external-qa-only")
        evidence=Evidence(folder,runtime)
        try:
            runtime.start()
            with owned_playwright(folder) as pw:
                browser=pw.chromium.launch(executable_path=str(BRAVE),headless=True,env=browser_environment(folder))
                context=browser.new_context(viewport={"width":1440,"height":900}); page=context.new_page()
                step=evidence.step
                def regular_type(root):
                    pixel=page.locator(root).evaluate("""root => [...root.querySelectorAll('button,input,select,textarea,label,h1,h2,h3,p,span,b,strong,small,kbd')].filter(el => el.getClientRects().length && getComputedStyle(el).fontFamily.includes('Pixel')).map(el => ({tag:el.tagName,text:el.textContent.slice(0,70),font:getComputedStyle(el).fontFamily}))""")
                    assert not pixel, pixel
                def open_plugins():
                    step("Open real WISE UI",lambda:page.goto(runtime.url+"/app/",wait_until="domcontentloaded"))
                    expect(page.locator("#prompt")).to_be_visible()
                    step("Click existing Sidebar Plugins item",lambda:page.locator('[data-view="plugins"]').click())
                    expect(page.locator('[data-view="plugins"]')).to_have_class("nav-item active")
                    expect(page.locator("#pluginsView")).to_be_visible()
                    expect(page.locator("#pluginResults")).to_have_attribute("aria-busy","false")
                def unconfigured():
                    open_plugins()
                    expect(page.locator(".plugins-service-note")).to_contain_text("ربط الحسابات يحتاج إعداد Nango")
                    expect(page.locator(".plugin-card")).to_have_count(24)
                    step("Filter offline library by Notion",lambda:page.locator('#pluginSearch').fill('catalog_notion'))
                    expect(page.locator('.plugin-card-open[data-plugin-id="catalog_notion"]')).to_be_visible()
                    step("Open genuine provider catalog details",lambda:page.locator('.plugin-card-open[data-plugin-id="catalog_notion"]').click())
                    expect(page.locator('[data-plugin-action="connect"]')).to_be_disabled()
                    expect(page.locator('.plugin-docs')).to_be_visible()
                    regular_type("#pluginsView")
                    page.screenshot(path=str(folder/"catalog-only-detail.png"),full_page=True)
                    step("Return to catalog",lambda:page.locator('[data-plugin-action="back"]').click())
                    step("Retry configuration check",lambda:page.locator('[data-plugin-action="refresh"]').click())
                    expect(page.locator("#pluginResults")).to_have_attribute("aria-busy","false")
                    step("Return to new chat",lambda:page.locator('[data-view="chat"]').click())
                    expect(page.locator("#prompt")).to_be_visible()
                    assert page.evaluate("location.hash")==""
                    step("Reopen Plugins",lambda:page.locator('[data-view="plugins"]').click())
                    expect(page.locator(".plugins-service-note")).to_be_visible()
                def catalog():
                    open_plugins(); expect(page.locator(".plugin-card")).to_have_count(24)
                    step("Search configured integration through real API",lambda:page.locator("#pluginSearch").fill("github-qa"))
                    expect(page.locator(".plugin-card")).to_have_count(1)
                    step("Find all configured QA integrations",lambda:page.locator("#pluginSearch").fill("-qa"))
                    expect(page.locator(".plugin-card")).to_have_count(2)
                    step("Filter category",lambda:page.locator("#pluginCategory").select_option("communication"))
                    expect(page.locator(".plugin-card")).to_have_count(1)
                    step("Restore all categories",lambda:page.locator("#pluginCategory").select_option(""))
                    expect(page.locator(".plugin-card")).to_have_count(2)
                def progressive_catalog_and_missing_logo():
                    open_plugins()
                    step("Clear previous filters",lambda:page.locator("#pluginSearch").fill(""))
                    expect(page.locator(".plugin-card")).to_have_count(24)
                    step("Scroll the actual library",lambda:page.locator("#pluginsView").hover())
                    page.mouse.wheel(0,2500)
                    expect(page.locator(".plugin-card")).to_have_count(48,timeout=15000)
                    step("Search local-logo app",lambda:page.locator("#pluginSearch").fill("catalog_notion"))
                    expect(page.locator('.plugin-card-open[data-plugin-id="catalog_notion"]')).to_be_visible()
                    image=page.locator('.plugin-card-open[data-plugin-id="catalog_notion"] .brand-logo img')
                    expect(image).to_be_visible()
                    assert image.evaluate("img => img.complete && img.naturalWidth>0")
                    # Deliberate missing-image fault, not simulated app behavior.
                    step("Inject missing logo asset to exercise real image error recovery",lambda:image.evaluate("img => img.src='/app/assets/integrations/qa-missing.svg'"))
                    expect(page.locator('.plugin-card-open[data-plugin-id="catalog_notion"] .brand-logo')).not_to_have_class("brand-logo plugin-logo is-loaded")
                    expect(page.locator('.plugin-card-open[data-plugin-id="catalog_notion"] .brand-logo-fallback')).to_be_visible()
                    step("No-result recovery",lambda:page.locator("#pluginSearch").fill("qa-no-such-app-987654321"))
                    expect(page.locator(".plugins-empty h3")).to_have_text("لا توجد تطبيقات مطابقة")
                    step("Reset filters",lambda:page.locator('[data-plugin-action="reset"]').click())
                    expect(page.locator(".plugin-card")).to_have_count(24)
                def instructions_persistence_and_conflict():
                    step("Open Instructions settings",lambda:page.locator("#settingsButton").click())
                    step("Select Instructions",lambda:page.locator('[data-settings-tab="instructions"]').click())
                    editor=page.locator("#userInstructions")
                    expect(editor).to_be_visible()
                    draft="QA preference: concise Arabic answers; include checked source links."
                    step("Enter real user preferences",lambda:editor.fill(draft))
                    step("Save through real Instructions API",lambda:page.locator('[data-instructions-action="save"]').click())
                    expect(page.locator('#instructionsDraftState')).to_have_text("محفوظ")
                    step("Reload renderer to verify durable preferences",lambda:page.reload(wait_until="domcontentloaded"))
                    step("Reopen settings",lambda:page.locator("#settingsButton").click())
                    step("Reopen Instructions",lambda:page.locator('[data-settings-tab="instructions"]').click())
                    expect(page.locator("#userInstructions")).to_have_value(draft)
                    current=page.request.get(runtime.url+"/api/v2/settings/instructions").json()
                    response=page.request.put(runtime.url+"/api/v2/settings/instructions",headers={"X-Wise-Action":"settings"},data={"instructions":"QA second-window preference", "expected_revision":current["revision"]})
                    assert response.status==200
                    step("Edit stale first-window draft",lambda:page.locator("#userInstructions").fill(draft+" Keep this draft."))
                    step("Save against changed revision",lambda:page.locator('[data-instructions-action="save"]').click())
                    expect(page.locator("#instructionsFeedback")).to_contain_text("تغيرت التعليمات في نافذة أخرى")
                    expect(page.locator("#userInstructions")).to_have_value(draft+" Keep this draft.")
                    page.once("dialog",lambda dialog:dialog.dismiss())
                    step("Cancel draft replacement",lambda:page.locator('[data-instructions-action="reload"]').click())
                    expect(page.locator("#userInstructions")).to_have_value(draft+" Keep this draft.")
                    page.once("dialog",lambda dialog:dialog.accept())
                    step("Explicitly reload newest saved instructions",lambda:page.locator('[data-instructions-action="reload"]').click())
                    expect(page.locator("#userInstructions")).to_have_value("QA second-window preference")
                    # A numeric used/max counter stays ordered even in RTL.
                    assert page.locator("#instructionsCount").evaluate("el => getComputedStyle(el).direction")=="ltr"
                    page.screenshot(path=str(folder/"instructions-settings.png"),full_page=True)
                    step("Render English Instructions without losing saved content",lambda:page.evaluate("state.settings.ui_language='en'; applyLanguage('en'); renderSettings()"))
                    expect(page.locator('[data-instructions-action="save"]')).to_have_text("Save instructions")
                    expect(page.locator("#userInstructions")).to_have_value("QA second-window preference")
                    assert page.locator("#instructionsCount").evaluate("el => getComputedStyle(el).direction")=="ltr"
                    for selector in ('[data-settings-tab="instructions"]','[data-view="chat"]'):
                        assert "Pixel" not in page.locator(selector).evaluate("el => getComputedStyle(el).fontFamily")
                    page.screenshot(path=str(folder/"instructions-settings-en.png"),full_page=True)
                    regular_type("#settingsModal")
                    step("Inspect English General settings typography",lambda:page.locator('[data-settings-tab="general"]').click())
                    expect(page.locator('[data-save-settings="general"]')).to_be_visible()
                    regular_type("#settingsModal")
                    page.screenshot(path=str(folder/"settings-general-en.png"),full_page=True)
                    step("Inspect English provider settings typography",lambda:page.locator('[data-settings-tab="models"]').click())
                    expect(page.locator(".provider-directory-row").first).to_be_visible()
                    regular_type("#settingsModal")
                    page.screenshot(path=str(folder/"settings-providers-en.png"),full_page=True)
                    step("Return to saved Instructions",lambda:page.locator('[data-settings-tab="instructions"]').click())
                    expect(page.locator("#userInstructions")).to_have_value("QA second-window preference")
                    step("Restore Arabic Instructions",lambda:page.evaluate("state.settings.ui_language='ar'; applyLanguage('ar'); renderSettings()"))
                    expect(page.locator('[data-instructions-action="save"]')).to_have_text("حفظ التعليمات")
                    regular_type("#settingsModal")
                    step("Close settings",lambda:page.locator("#closeSettings").click())
                    open_plugins()
                def messaging_brand_schema_and_filter():
                    step("Open real messaging service browser",lambda:page.locator('[data-view="messaging"]').click())
                    expect(page.locator("#messagingSearch")).to_be_visible()
                    assert page.locator(".messaging-app").count()>100
                    step("Find Slack using the actual service catalog",lambda:page.locator("#messagingSearch").fill("Slack"))
                    expect(page.locator('.messaging-app[data-messaging-service="slack"]')).to_be_visible()
                    expect(page.locator('.messaging-app[data-messaging-service="slack"] .brand-logo')).to_have_class("brand-logo messaging-logo is-loaded")
                    assert page.locator('.messaging-app[data-messaging-service="slack"] img').get_attribute("src")=="/app/assets/integrations/slack.svg"
                    step("Open Telegram authentication fields without connecting or sending",lambda:page.locator("#messagingSearch").fill("Telegram"))
                    step("Select installed Telegram connector",lambda:page.locator('[data-messaging-service="tgram"]').click())
                    expect(page.locator('input[name="bot_token"]')).to_have_attribute("type","password")
                    expect(page.locator('input[name="targets"]')).to_have_attribute("required","")
                    expect(page.locator("#messagingEditor")).to_contain_text("استقبال المحادثات غير مدعوم")
                    regular_type("#messagingView")
                    page.screenshot(path=str(folder/"messaging-service-fields.png"),full_page=True)
                    step("Exercise messaging no-result recovery",lambda:page.locator("#messagingSearch").fill("qa-no-such-messaging-app"))
                    expect(page.locator("#messagingApps p")).to_contain_text("لا توجد تطبيقات مطابقة")
                    open_plugins()
                def oauth_boundary():
                    step("Open GitHub details",lambda:page.locator('.plugin-card-open[data-plugin-id="github-qa"]').click())
                    expect(page.locator(".plugin-detail h2")).to_have_text("GitHub")
                    expect(page.locator(".plugin-capabilities li")).to_have_count(3)
                    step("Connect (external browser/Nango only simulated)",lambda:page.locator('[data-plugin-action="connect"]').click())
                    expect(page.locator(".plugin-account strong")).to_have_text("QA boundary account",timeout=20000)
                    # Renderer cannot see connect session tokens, even in logs/local storage.
                    assert "external-qa-only" not in page.content()
                    assert "session_token" not in page.evaluate("JSON.stringify(localStorage)")
                    step("Reload real UI and recover persisted account",lambda:page.reload(wait_until="domcontentloaded"))
                    expect(page.locator(".plugin-account strong")).to_have_text("QA boundary account",timeout=20000)
                    page.screenshot(path=str(folder/"connected-account-detail.png"),full_page=True)
                    step("Back to library",lambda:page.locator('[data-plugin-action="back"]').click())
                    expect(page.locator("#pluginConnected")).to_be_visible()
                    step("Connected filter",lambda:page.locator("#pluginConnected").check())
                    expect(page.locator(".plugin-card")).to_have_count(1)
                def disconnect():
                    step("Open connected account",lambda:page.locator('.plugin-card-open[data-plugin-id="github-qa"]').click())
                    expect(page.locator(".plugin-account")).to_have_count(1)
                    page.once("dialog",lambda dialog:dialog.accept())
                    step("Disconnect through real WISE API",lambda:page.locator('[data-plugin-action="disconnect"]').click())
                    expect(page.locator(".plugin-account")).to_have_count(0)
                    assert not page.request.get(runtime.url+"/api/v2/plugins/accounts").json()["accounts"]
                def layout():
                    for width,height in ((1440,900),(1024,640),(375,812),(812,375)):
                        step(f"Resize to {width}x{height}",lambda w=width,h=height:page.set_viewport_size({"width":w,"height":h}))
                        expect(page.locator("#pluginsView")).to_be_visible()
                        if width < 900:
                            expect(page.locator(".conversation-sidebar")).not_to_be_visible()
                        else:
                            expect(page.locator(".conversation-sidebar")).to_be_visible()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        # Check hit-testing, not just scroll width: drawers can
                        # occlude controls without creating horizontal overflow.
                        target=page.locator("#pluginSearch") if page.locator("#pluginSearch").count() else page.locator('[data-plugin-action="back"]')
                        assert target.evaluate("el => { const r=el.getBoundingClientRect(); const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2); return hit===el || el.contains(hit); }")
                        page.screenshot(path=str(folder/f"layout-{width}.png"))
                    step("Open compact Sidebar",lambda:page.locator("#openSidebar").click())
                    expect(page.locator(".conversation-sidebar")).to_be_visible()
                    step("Close compact Sidebar",lambda:page.locator("#collapseSidebar").click())
                    expect(page.locator(".conversation-sidebar")).not_to_be_visible()
                    page.emulate_media(reduced_motion="reduce")
                    assert page.locator(".plugins-skeleton").count()==0
                    page.set_viewport_size({"width":1440,"height":900})
                if boundary:
                    evidence.case("catalog-search-filter",context,page,catalog)
                    evidence.case("oauth-external-boundary-and-persistence",context,page,oauth_boundary)
                    evidence.case("disconnect",context,page,disconnect)
                else: evidence.case("real-unconfigured-navigation",context,page,unconfigured)
                evidence.case("catalog-scroll-search-and-logo-failure",context,page,progressive_catalog_and_missing_logo)
                evidence.case("instructions-real-save-reload-conflict",context,page,instructions_persistence_and_conflict)
                evidence.case("messaging-logo-schema-and-search",context,page,messaging_brand_schema_and_filter)
                evidence.case("responsive-and-reduced-motion",context,page,layout)
                context.close(); browser.close()
        except Exception:
            evidence.cases.append({"id":"runtime-or-browser-startup", "status":"FAIL",
                "steps":["Start owned isolated backend and browser"],
                "stack_trace":traceback.format_exc(), "backend_log":str(folder/"backend.log"),
                "screenshot_unavailable":"No page was available during setup failure"})
        finally:
            runtime.stop(); cases.extend([{**row,"external_boundary_mocked":boundary} for row in evidence.cases])
    changed=stamp != source_stamp()
    result={"status":"PASS" if not changed and all(row["status"]=="PASS" for row in cases) else "FAIL", "source_stamp":stamp,"source_changed_during_run":changed,"cases":cases,
        "limitations":["Real-account OAuth not verified: Nango environment is not configured.","Headless Brave tests renderer; not native Windows backdrop/UAC.","No live model or paid provider requests in this run."]}
    (output/"report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"status":result["status"],"cases":len(cases),"report":str(output/"report.json")}),flush=True)
    return 0 if result["status"]=="PASS" else 1


if __name__=="__main__":sys.exit(main())
