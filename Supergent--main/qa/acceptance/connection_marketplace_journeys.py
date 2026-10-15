"""Owned Brave UI, actual public MCP and actual installed filesystem MCPB.

No mocked service responses, account authorization, inherited provider keys,
paid models, user browser profiles, or external messages. Browser plugin not
available: use the project's owned Playwright runtime. Evidence outside source.
"""
import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import traceback
import zipfile
import httpx
from playwright.sync_api import expect
from user_journeys import ROOT,BRAVE,Runtime,Evidence
from owned_browser import owned_playwright,browser_environment
from source_stamp import source_stamp


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    runtime=Runtime(out);runtime.env['AGENT_OS_SKIP_FIRST_BOOT_CHECK']='1'
    evidence=Evidence(out,runtime,trace_visuals=False);bundle_id='';catalog={};stamp=source_stamp()
    try:
        runtime.start()
        with owned_playwright(out) as pw:
            browser=pw.chromium.launch(executable_path=str(BRAVE),headless=True,env=browser_environment(out))
            context=browser.new_context(viewport={'width':1440,'height':900},reduced_motion='reduce')
            page=context.new_page();step=evidence.step
            def connection_finished(timeout=90000):
                # A reconnect may initially display the previous Connected
                # state. Do not pass the journey or navigate away until the
                # POST and the refreshed detail have both finished.
                expect(page.locator('.service-connection')).to_have_attribute('aria-busy','false',timeout=timeout)
                expect(page.locator('.service-status')).to_have_text(re.compile('Connected|متصل'),timeout=1000)
            def browse():
                nonlocal catalog
                step('Open the actual WISE connection marketplace',lambda:page.goto(runtime.url+'/app/#plugins'))
                expect(page.locator('.plugin-card').first).to_be_visible(timeout=90000)
                catalog=httpx.get(runtime.url+'/api/v2/plugins/connections',timeout=30).json()
                assert catalog['total']>=1000 and page.title().startswith('WISE')
                assert not page.locator('[data-plugin-action="bundle-import"],#pluginBundleFile').count()
                assert not page.locator('.plugin-source-tabs,#pluginInstallForm').count()
                assert not (runtime.root/'memory/plugin-package-cache').exists()
            evidence.case('store-identity-and-cloud-local-separation',context,page,browse)
            def local_consent():
                step('Open Local files',lambda:page.goto(runtime.url+'/app/#plugins/service/local-filesystem'))
                expect(page.locator('#serviceConnectForm input[name="directory"]')).to_be_visible()
                step('Try without trust',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                assert not page.locator('#serviceConnectForm').evaluate('(n)=>n.checkValidity()')
                assert not (runtime.root/'memory/plugins.json').exists()
            evidence.case('local-package-consent-before-installation',context,page,local_consent)
            def local_install():
                workspace=runtime.root/'workspace';(workspace/'evidence.txt').write_text('WISE real local MCP acceptance',encoding='utf-8')
                step('Choose a QA-owned folder',lambda:page.locator('[name="directory"]').fill(str(workspace)))
                page.locator('[name="trusted_local"]').check()
                step('Install the pinned reference filesystem server using the real UI',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                connection_finished(timeout=300000)
                detail=httpx.get(runtime.url+'/api/v2/plugins/integrations/local-filesystem',timeout=30).json()
                assert detail['status']=='CONNECTED' and len(detail['tools'])>0
                expect(page.locator('.service-tools')).to_be_visible()
                assert not page.locator('#serviceOAuthClient').count()
                cfg=next(c for c in json.loads((runtime.root/'memory/mcp_servers.json').read_text()) if c['name']==detail['server'])
                assert cfg['require_confirmation'] and not cfg['auto_approve'] and cfg['transport']=='stdio'
            evidence.case('real-user-pinned-local-install-and-handshake',context,page,local_install)
            if evidence.cases[-1]['status']!='PASS':raise RuntimeError('Dependent MCPB journeys blocked by failed real package installation; failure evidence retained')
            def bundle_import():
                nonlocal bundle_id
                # Bundle the actual official npm server installed above, not
                # a fake MCP implementation or a generated model response.
                root=next((runtime.root/'memory/mcp-runtimes').iterdir())
                manifest=dict(manifest_version='0.3',name='qa-reference-filesystem',display_name='QA reference filesystem bundle',version='1.0.0',
                    description='Acceptance bundle of the actual MCP reference filesystem server',author={'name':'MCP reference server / WISE QA packaging'},
                    server=dict(type='node',entry_point='node_modules/@modelcontextprotocol/server-filesystem/dist/index.js',
                        mcp_config=dict(command='node',args=['${__dirname}/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js','${user_config.folders}'])),
                    user_config={'folders':dict(type='directory',title='Allowed folders',multiple=True,required=True)})
                buffer=io.BytesIO()
                with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr('manifest.json',json.dumps(manifest))
                    for file in (root/'node_modules').rglob('*'):
                        if file.is_file() and not file.is_symlink() and '.bin' not in file.parts:
                            archive.write(file,file.relative_to(root).as_posix())
                # Bundle button intentionally removed from Market. Preserve
                # backend compatibility tests, not an invented visible chooser.
                response=step('Import real bundle through supported backend API',lambda:httpx.post(runtime.url+'/api/v2/plugins/bundles',content=buffer.getvalue(),headers={'X-Wise-Action':'plugins','Content-Type':'application/octet-stream'},timeout=90))
                response.raise_for_status();bundle_id=response.json()['id']
                page.goto(runtime.url+'/app/#plugins/service/'+bundle_id)
                expect(page.locator('.plugin-detail h2')).to_have_text('QA reference filesystem bundle',timeout=60000)
                assert page.locator('.service-status').inner_text() in {'Available','متاحة'}
                bundle_id=page.url.split('/')[-1]
                page.locator('[name="folders"]').fill(str(runtime.root/'workspace'));page.locator('[name="trusted_local"]').check()
                step('Trust and connect the actual MCPB',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                connection_finished()
                detail=httpx.get(runtime.url+'/api/v2/plugins/integrations/'+bundle_id,timeout=30).json()
                assert detail['bundle_sha256'] and len(detail['tools'])>0
                probe="""import json,sys
from pathlib import Path
from core.mcp.registry import MCPRegistry,build_mcp_tools
reg=MCPRegistry(Path(sys.argv[1]))
try:
    assert reg.start(sys.argv[2])['alive']
    tools=build_mcp_tools(reg,auto_start=False)
    runner=next(fn for name,fn in tools.items() if name.endswith('_read_text_file'))
    result=runner(path=sys.argv[3])
    assert 'WISE real local MCP acceptance' in json.dumps(result)
    print('PASS actual canonical MCP read_text_file')
finally:reg.stop_all()
"""
                step('Read the QA-owned file through the canonical MCP agent tool wrapper',lambda:subprocess.run(
                    [sys.executable,'-c',probe,str(runtime.root/'memory'),detail['server'],str(runtime.root/'workspace/evidence.txt')],
                    cwd=ROOT,env=runtime.env,check=True,timeout=90,capture_output=True,text=True))
            evidence.case('real-mcpb-import-review-configure-and-connect',context,page,bundle_import)
            if evidence.cases[-1]['status']!='PASS':raise RuntimeError('Restart journey blocked by a failed real MCPB import; failure evidence retained')
            def restart():
                step('Restart only the owned WISE runtime',lambda:(runtime.stop(),runtime.start()))
                page.goto(runtime.url+'/app/#plugins/service/'+bundle_id)
                expect(page.locator('#serviceConnectForm')).to_be_visible(timeout=90000)
                assert not page.locator('[name="folders"]').count()
                page.locator('[name="trusted_local"]').check()
                step('Reconnect saved bundle without re-entering settings',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                connection_finished()
            evidence.case('real-bundle-restart-persistence-and-reconnect',context,page,restart)
            def cloud():
                page.goto(runtime.url+'/app/#plugins/service/cloudflare-docs');expect(page.locator('#serviceConnectForm')).to_be_visible(timeout=30000)
                step('Connect public Cloudflare MCP using the real button',lambda:page.locator('#serviceConnectForm [type="submit"]').click())
                connection_finished()
                expect(page.locator('.service-tools')).to_be_visible()
            evidence.case('real-public-cloud-mcp-connect',context,page,cloud)
            def app_readiness():
                page.goto(runtime.url+'/app/#plugins/service/figma-desktop');expect(page.locator('[data-plugin-action="service-probe"]')).to_be_visible()
                step('Check bridge readiness without granting application access',lambda:page.locator('[data-plugin-action="service-probe"]').click())
                expect(page.locator('.service-readiness')).not_to_be_empty()
                assert not page.locator('[name="trusted_local"]').is_checked()
                detail=httpx.get(runtime.url+'/api/v2/plugins/integrations/figma-desktop',timeout=30).json()
                assert not detail['installed'] and not detail['tools']
            evidence.case('application-probe-does-not-pretend-to-connect',context,page,app_readiness)
            def responsive():
                for width,height in ((1440,900),(1024,768),(800,650)):
                    page.set_viewport_size({'width':width,'height':height});page.goto(runtime.url+'/app/#plugins')
                    expect(page.locator('.plugin-card').first).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(out/f'marketplace-{width}.png'))
                page.locator('#pluginSearch').fill('Playwright')
                expect(page.locator('[data-plugin-id="local-playwright"]')).to_be_visible()
            evidence.case('desktop-compact-layout-and-real-search',context,page,responsive)
            context.close();browser.close()
    except Exception:
        evidence.cases.append({'id':'harness','status':'FAIL','stack_trace':traceback.format_exc()})
    finally:runtime.stop()
    report=dict(status='PASS' if all(c['status']=='PASS' for c in evidence.cases) else 'FAIL',cases=evidence.cases,
        source_stamp=stamp,source_changed_during_run=stamp!=source_stamp(),
        catalog_total=catalog.get('total'),account_authorizations=0,paid_model_calls=0,
        scope='Actual cloud/local MCP, official filesystem package and real MCPB through owned Brave; account consent is not tested',
        browser='Browser plugin not available; owned headless Brave / Playwright')
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(out/'report.json',flush=True)
    return report['status']!='PASS'


if __name__=='__main__':raise SystemExit(main())
