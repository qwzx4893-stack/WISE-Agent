"""Owned Brave + actual WISE HTTP flows, no private accounts or model calls."""
from datetime import datetime,timezone
import json
import re
import traceback
import httpx
from playwright.sync_api import expect
from user_journeys import ROOT,BRAVE,Runtime,Evidence
from owned_browser import owned_playwright,browser_environment
from source_stamp import source_stamp

def main():
    out=ROOT/'qa-results'/('mcp-market-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));out.mkdir()
    runtime=Runtime(out);runtime.env['AGENT_OS_SKIP_FIRST_BOOT_CHECK']='1'
    evidence=Evidence(out,runtime,trace_visuals=False);stamp=source_stamp();catalog={};community={}
    try:
        runtime.start()
        with owned_playwright(out) as pw:
            browser=pw.chromium.launch(executable_path=str(BRAVE),headless=True,env=browser_environment(out))
            context=browser.new_context(viewport={'width':1440,'height':900},reduced_motion='reduce')
            page=context.new_page();step=evidence.step
            def stalled_startup():
                held=[]
                def hold(route):held.append(route)
                page.route('**/api/v2/sessions?*',hold)
                try:
                    step('Fault injection: hold session reads, not MCP responses',lambda:page.goto(runtime.url+'/app/#plugins'))
                    expect(page.locator('.plugins-heading h2')).to_have_text('MCP Market',timeout=5000)
                    assert held,'Session request must actually be held'
                finally:
                    for route in held:route.continue_()
                    page.unroute('**/api/v2/sessions?*',hold)
            evidence.case('market-navigation-does-not-wait-for-sessions',context,page,stalled_startup)
            def identity():
                nonlocal catalog
                page.goto(runtime.url+'/app/#plugins')
                expect(page.locator('.plugins-heading h2')).to_have_text('MCP Market',timeout=90000)
                expect(page.locator('.plugin-card').first).to_be_visible(timeout=90000)
                # Capture decoded artwork, not the temporary initials shown
                # while cold-network images are still loading.
                page.wait_for_function("()=>[...document.querySelectorAll('#pluginResults img')].filter(i=>{const r=i.getBoundingClientRect();return r.top<innerHeight&&r.bottom>0;}).every(i=>i.complete&&i.naturalWidth>0)",timeout=45000)
                expect(page.locator('[data-view="plugins"]')).to_contain_text('MCP Market')
                assert not page.locator('[data-plugin-action="bundle-import"],#pluginBundleFile').count()
                catalog=httpx.get(runtime.url+'/api/v2/plugins/connections',timeout=45).json()
                assert catalog['total']>=1000 and catalog['kind']=='mcp-market'
                assert not (runtime.root/'memory/plugins.json').exists()
            evidence.case('market-name-large-catalog-no-import-button',context,page,identity)
            def browse():
                step('Browse all',lambda:page.locator('.market-browse').click())
                expect(page.locator('.plugin-card').first).to_be_visible(timeout=30000)
                assert page.locator('.plugin-card').count()>=48
                step('Scroll without Show more',lambda:page.locator('#pluginsView').evaluate('(n)=>n.scrollTop=n.scrollHeight'))
                page.wait_for_function("document.querySelectorAll('.plugin-card').length>48",timeout=30000)
                assert not page.locator('[data-plugin-action="more"]').count()
            evidence.case('all-servers-infinite-scroll',context,page,browse)
            def community_detail():
                nonlocal community
                step('Search community namespaces',lambda:page.locator('#pluginSearch').fill('io.github'))
                rows=httpx.get(runtime.url+'/api/v2/plugins/connections',params={'query':'io.github'},timeout=45).json()['items'];assert rows
                community=rows[0]
                expect(page.locator('[data-plugin-id="'+community['id']+'"]')).to_be_visible(timeout=30000)
                step('Open registry listing',lambda:page.locator('[data-plugin-id="'+community['id']+'"]')).click()
                expect(page.locator('.market-detail h2')).to_have_text(community['display_name'],timeout=30000)
                expect(page.locator('.market-facts')).to_contain_text(community['publisher_id'])
                assert page.locator('[data-plugin-docs]').count()>0
                if community['can_connect']:expect(page.locator('#serviceConnectForm')).to_be_visible()
                else:assert not page.locator('#serviceConnectForm').count()
                assert not (runtime.root/'memory/plugins.json').exists()
            evidence.case('community-search-description-provenance',context,page,community_detail)
            def artwork():
                step('Load actual company and community artwork',lambda:None)
                for key in ('canva','cloudflare-docs','huggingface','prisma','neon','airtable','local-filesystem',community['id']):
                    response=httpx.get(runtime.url+'/api/v2/plugins/logos/'+key,timeout=45)
                    assert response.status_code==200 and response.headers.get('content-type','').startswith('image/')
                    if key in {'canva','cloudflare-docs','huggingface','prisma','neon','airtable'}:
                        assert b'text-anchor="middle" font-family="sans-serif" font-size="24"' not in response.content,'Known brand unexpectedly fell back to a monogram: '+key
                page.goto(runtime.url+'/app/#plugins/service/canva')
                expect(page.locator('.market-detail h2')).to_have_text('Canva',timeout=30000)
                page.wait_for_function("()=>document.querySelector('.plugin-detail-heading img')?.naturalWidth>0",timeout=45000)
                expect(page.locator('.market-facts')).to_be_visible()
                expect(page.locator('#serviceConnectForm [type="submit"]')).to_be_enabled()
            evidence.case('company-details-renderable-artwork',context,page,artwork)
            def cloud():
                page.goto(runtime.url+'/app/#plugins/service/cloudflare-docs')
                expect(page.locator('#serviceConnectForm')).to_be_visible(timeout=30000)
                step('Connect public MCP with the button',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                expect(page.locator('.service-connection')).to_have_attribute('aria-busy','false',timeout=90000)
                expect(page.locator('.service-status')).to_have_text(re.compile('Connected|متصل'))
                expect(page.locator('.service-tools')).to_be_visible()
                assert httpx.get(runtime.url+'/api/v2/plugins/integrations/cloudflare-docs',timeout=30).json()['tools']
            evidence.case('actual-public-connect',context,page,cloud)
            def compact():
                for width,height in ((1440,900),(1024,768),(800,650)):
                    page.set_viewport_size({'width':width,'height':height});page.goto(runtime.url+'/app/#plugins/service/canva')
                    expect(page.locator('.market-facts')).to_be_visible(timeout=30000)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(out/f'detail-{width}.png'))
                page.goto(runtime.url+'/app/#plugins');expect(page.locator('.plugin-card').first).to_be_visible(timeout=30000)
                page.screenshot(path=str(out/'market-800.png'))
            evidence.case('desktop-compact-detail-and-market-layout',context,page,compact)
            context.close();browser.close()
    except Exception:evidence.cases.append({'id':'harness','status':'FAIL','stack_trace':traceback.format_exc()})
    finally:runtime.stop()
    report=dict(status='PASS' if all(c['status']=='PASS' for c in evidence.cases) else 'FAIL',cases=evidence.cases,
        source_stamp=stamp,source_changed_during_run=stamp!=source_stamp(),catalog_total=catalog.get('total'),account_authorizations=0,paid_model_calls=0,
        browser='Browser plugin not available; owned headless Brave / Playwright',scope='Discovery/rendering and one actual public connection; not certification of every listing')
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(out/'report.json',flush=True)
    return report['status']!='PASS'

if __name__=='__main__':raise SystemExit(main())
