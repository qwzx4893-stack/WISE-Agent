"""Owned headless Brave, real WISE routes and public service connection.

Browser plugin not available; existing Playwright is used. No account consent,
user profile, paid model, external messages or downloaded plugin archives.
The owner explicitly requested durable QA screenshots/logs in qa-results.
"""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import re
import traceback
import httpx
from playwright.sync_api import expect
from user_journeys import ROOT, BRAVE, Runtime, Evidence
from owned_browser import owned_playwright, browser_environment
from source_stamp import source_stamp


def main():
    output=ROOT/'qa-results'/('service-connectors-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    output.mkdir();runtime=Runtime(output);runtime.env['AGENT_OS_SKIP_FIRST_BOOT_CHECK']='1'
    evidence=Evidence(output,runtime,trace_visuals=False);stamp=source_stamp();catalog={};artwork=[]
    try:
        runtime.start()
        with owned_playwright(output) as pw:
            browser=pw.chromium.launch(executable_path=str(BRAVE),headless=True,env=browser_environment(output))
            context=browser.new_context(viewport={'width':1440,'height':900},reduced_motion='reduce')
            page=context.new_page();step=evidence.step
            def opened():
                nonlocal catalog
                step('Open WISE service store',lambda:page.goto(runtime.url+'/app/#plugins'))
                expect(page.locator('.plugin-card').first).to_be_visible(timeout=90000)
                catalog=httpx.get(runtime.url+'/api/v2/plugins/connections',timeout=30).json()
                assert catalog['total']>=1000 and catalog['kind']=='mcp-market'
                assert page.title().startswith('WISE')
                assert all(row['kind'] in {'remote','local-app','local-package'} for row in catalog['items'])
                assert all(not row['download_required'] for row in catalog['items'] if row['connection_type'] in {'cloud','local'})
                assert not page.locator('[data-plugin-action="prepare"],#pluginInstallForm,.plugin-source-tabs').count()
                assert not (runtime.root/'memory/plugin-package-cache').exists()
                expect(page.locator('.plugin-section').first).to_be_visible()
            evidence.case('default-service-store-no-package-download',context,page,opened)
            def logos():
                nonlocal artwork
                def check(row):
                    response=httpx.get(runtime.url+row['logo'],timeout=45)
                    return {'service':row['id'],'url':row['logo'],'status':response.status_code,
                            'bytes':len(response.content),'media':response.headers.get('content-type','')}
                step('Fetch actual artwork for branded service identities; generic local adapters use neutral icons',lambda:None)
                artwork=list(ThreadPoolExecutor(max_workers=4).map(check,[row for row in catalog['items'] if row['logo']]))
                assert all(row['status']==200 and row['bytes']>20 and row['media'].startswith('image/') for row in artwork),artwork
                # Force ordinary scrolling/lazy loading; verify actual decoded
                # browser images, not the existence of an img element.
                host=page.locator('#pluginsView')
                height=host.evaluate('(node)=>node.scrollHeight')
                for y in range(0,height,500):
                    host.evaluate('(node,y)=>node.scrollTop=y',y);page.wait_for_timeout(150)
                page.wait_for_function("()=>[...document.querySelectorAll('#pluginResults .brand-logo img')].every(img=>img.complete&&img.naturalWidth>0)",timeout=45000)
                host.evaluate('(node)=>node.scrollTop=0')
            evidence.case('all-service-artwork-real-and-browser-decodable',context,page,logos)
            def canva():
                step('Search Canva',lambda:page.locator('#pluginSearch').fill('Canva'))
                expect(page.locator('[data-plugin-id="canva"]')).to_be_visible()
                step('Open service account connection',lambda:page.locator('[data-plugin-id="canva"]').click())
                expect(page.locator('#serviceConnectForm')).to_be_visible()
                expect(page.locator('.plugin-detail h2')).to_have_text('Canva')
                assert not page.locator('[data-plugin-action="prepare"],#pluginInstallForm').count()
                assert not (runtime.root/'memory/plugin-package-cache').exists()
                assert not (runtime.root/'memory/plugins.json').exists()
                expect(page.locator('#serviceConnectForm [type="submit"]')).to_be_enabled()
            evidence.case('canva-direct-connect-flow-no-install-step',context,page,canva)
            def cloud():
                step('Open public Cloudflare documentation connection',lambda:page.goto(runtime.url+'/app/#plugins/service/cloudflare-docs'))
                expect(page.locator('#serviceConnectForm')).to_be_visible()
                step('Connect as a normal user through the real button',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                expect(page.locator('.service-status')).to_have_text(re.compile('Connected|متصل'),timeout=90000)
                record=json.loads((runtime.root/'memory/plugins.json').read_text())['cloudflare-docs']
                detail=httpx.get(runtime.url+'/api/v2/plugins/integrations/cloudflare-docs',timeout=30).json()
                assert detail['status']=='CONNECTED' and len(detail['tools'])>0 and not detail['error']
                assert detail['server']==record['server']
                # The same registry feeds the agent's actual slash selection.
                commands=httpx.get(runtime.url+'/api/v2/chat/commands',timeout=30).json()['commands']
                assert any(record['server'] in json.dumps(command) for command in commands)
                assert not (runtime.root/'memory/plugin-package-cache').exists()
            evidence.case('real-user-cloud-connect-registers-agent-tools',context,page,cloud)
            def reopened():
                step('Restart only the isolated backend',lambda:(runtime.stop(),runtime.start()))
                step('Reopen service connection',lambda:page.goto(runtime.url+'/app/#plugins/service/cloudflare-docs'))
                expect(page.locator('#serviceConnectForm')).to_be_visible(timeout=90000)
                assert json.loads((runtime.root/'memory/plugins.json').read_text())['cloudflare-docs']['server']
                step('Reconnect persisted service without installing',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                expect(page.locator('.service-status')).to_have_text(re.compile('Connected|متصل'),timeout=90000)
            evidence.case('cloud-connection-persists-across-app-restart',context,page,reopened)
            def disconnect():
                page.once('dialog',lambda dialog:dialog.accept())
                step('Disconnect owned service using its real control',lambda:page.locator('[data-plugin-action="service-disconnect"]').click())
                expect(page.locator('.service-status')).to_have_text(re.compile('Available|متاحة'),timeout=30000)
                assert not json.loads((runtime.root/'memory/plugins.json').read_text())
                assert not any(cfg['name'].startswith('wise_') for cfg in json.loads((runtime.root/'memory/mcp_servers.json').read_text()))
            evidence.case('disconnect-removes-only-owned-local-connection',context,page,disconnect)
            def local():
                step('Open installed-app bridge, without touching owner Figma',lambda:page.goto(runtime.url+'/app/#plugins/service/figma-desktop'))
                expect(page.locator('#serviceConnectForm')).to_be_visible()
                expect(page.locator('[name="trusted_local"]')).not_to_be_checked()
                step('Try local access without consent',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                assert not page.locator('#serviceConnectForm').evaluate('(node)=>node.checkValidity()')
                assert not json.loads((runtime.root/'memory/plugins.json').read_text())
                assert not any(cfg['name'].startswith('wise_') for cfg in json.loads((runtime.root/'memory/mcp_servers.json').read_text()))
            evidence.case('local-app-access-requires-consent-and-launches-nothing',context,page,local)
            def token():
                step('Open Postman key-only service',lambda:page.goto(runtime.url+'/app/#plugins/service/postman'))
                expect(page.locator('#serviceConnectForm input[name="header_0"]')).to_be_visible()
                step('Submit without a credential',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                assert not page.locator('#serviceConnectForm').evaluate('(node)=>node.checkValidity()')
                assert not json.loads((runtime.root/'memory/plugins.json').read_text())
            evidence.case('token-only-service-has-real-required-field',context,page,token)
            def responsive():
                for width,height in ((1440,900),(1024,768),(800,650)):
                    step(f'Review {width} x {height}',lambda:page.set_viewport_size({'width':width,'height':height}))
                    page.goto(runtime.url+'/app/#plugins');expect(page.locator('.plugin-card').first).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(output/f'services-{width}.png'))
            evidence.case('responsive-service-browser-without-overflow',context,page,responsive)
            context.close();browser.close()
    except Exception:
        evidence.cases.append({'id':'harness','status':'FAIL','stack_trace':traceback.format_exc()})
    finally:runtime.stop()
    report={'status':'PASS' if all(case['status']=='PASS' for case in evidence.cases) else 'FAIL',
        'source_stamp':stamp,'source_changed_during_run':stamp!=source_stamp(),'cases':evidence.cases,'artwork':artwork,
        'catalog_total':catalog.get('total'),'scope':'Real Brave UI, public cloud connection and agent discovery; OAuth consent not performed on user accounts',
        'paid_model_calls':0,'account_authorizations':0,'browser':'Headless Brave / owned Playwright; Browser plugin not available'}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(str(output/'report.json'),flush=True)
    return report['status']!='PASS'


if __name__=='__main__':raise SystemExit(main())
