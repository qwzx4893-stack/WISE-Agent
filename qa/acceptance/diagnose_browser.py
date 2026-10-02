"""Read-only diagnostics against a fresh, isolated app instance."""
from pathlib import Path
from datetime import datetime,timezone
from playwright.sync_api import sync_playwright,expect
from user_journeys import Runtime,ROOT,BRAVE
import httpx

output=ROOT/"qa-results"/("diagnose-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
output.mkdir(); runtime=Runtime(output)
try:
    runtime.start()
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=str(BRAVE),headless=True)
        page=browser.new_page(viewport={"width":1440,"height":900})
        page.on("pageerror",lambda e:print("JS",str(e),flush=True))
        page.on("requestfailed",lambda r:print("FAILED",r.url,r.failure,flush=True))
        page.on("request",lambda r:print("REQUEST",r.url,flush=True) if "knowledge" in r.url or "capabilities" in r.url else None)
        page.on("response",lambda r:print("RESPONSE",r.status,r.url,flush=True) if "knowledge" in r.url or "capabilities" in r.url else None)
        page.goto(runtime.url+"/app/",wait_until="networkidle")
        print("HTTPX",httpx.get(runtime.url+"/api/v2/capabilities/browse/resources",timeout=20).status_code,flush=True)
        print("FETCH",page.evaluate("async()=>{try{const r=await fetch('/api/v2/capabilities/browse/resources',{signal:AbortSignal.timeout(4000)});return [r.status,(await r.text()).slice(0,100)]}catch(e){return String(e)}}"),flush=True)
        page.locator("#settingsButton").click()
        expect(page.locator("#settingsContent")).to_have_attribute("aria-busy","false")
        page.locator('[data-settings-tab="knowledge"]').click()
        expect(page.locator("#capabilityKind")).to_be_visible(timeout=30000)
        page.locator("#capabilityKind").select_option("resources")
        page.wait_for_timeout(10000)
        print("UI",page.locator("#capabilityResults").inner_text(),flush=True)
        print("STATE",page.evaluate("({version:capabilityBrowser.version,kind:capabilityBrowser.kind, connected:document.getElementById('capabilityResults').isConnected})"),flush=True)
        response=httpx.get(runtime.url+"/api/v2/capabilities/browse/resources",timeout=20)
        print("API",response.status_code,response.text[:200],flush=True)
        page.screenshot(path=str(output/"capability-browser.png"))
        page.locator("#closeSettings").click();page.locator('[data-view="messaging"]').click()
        page.wait_for_timeout(3000);print("MESSAGING",page.locator("#messagingContent").inner_text()[:350],flush=True)
        page.screenshot(path=str(output/"messaging.png"))
        page.locator("#messagingSearch").fill("Telegram")
        page.locator('[data-messaging-service="tgram"]').click()
        expect(page.locator('[name="bot_token"]')).to_be_visible()
        page.screenshot(path=str(output/"telegram-form.png"))
        print("VISUAL_EVIDENCE",str(output),flush=True)
        browser.close()
finally:
    runtime.stop()
    for handle in runtime.handles:handle.close()
