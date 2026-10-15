"""Real vendor OAuth discovery/DCR, but never authorize an owner's account.

The browser-launch boundary is deliberately suppressed for background QA;
metadata, PKCE registration and callback listener are real, not mocked.
"""
from datetime import datetime,timezone
from pathlib import Path
import json
import os
import sys
import traceback


def main():
    root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root))
    output=root/'qa-results'/('service-oauth-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    output.mkdir();os.environ.update(WISE_RUNTIME_ROOT=str(output/'runtime'),WISE_CONFIG_DIR=str(output/'runtime/config'),AGENT_OS_SKIP_FIRST_BOOT_CHECK='1')
    from core.plugins.manager import PluginManager
    from core.mcp.registry import MCPRegistry
    from core.mcp import oauth
    from source_stamp import source_stamp
    stamp=source_stamp();memory=output/'runtime/memory';memory.mkdir(parents=True)
    manager=PluginManager(memory,MCPRegistry(memory));manager.launch=lambda state:False
    cases=[]
    for key in ('notion','linear','netlify'):
        case={'service':key,'status':'FAIL'}
        try:
            result=manager.connect_service(key)
            auth=result.get('authorization')
            assert result['connection']['status']=='AUTH_REQUIRED',result['connection']['status']
            assert auth and auth['status']=='PENDING',result.get('authorization_error','No OAuth flow')
            assert manager.poll(auth['id'])['status']=='PENDING'
            case.update(status='PASS',registration='Real vendor public OAuth registration and PKCE flow',account_authenticated=False,browser_launched=False)
            manager.cancel(auth['id']);assert manager.poll(auth['id'])['status']=='CANCELLED'
        except Exception:case['stack_trace']=traceback.format_exc()
        finally:
            if key in manager.installed:manager.disable(key,remove=True)
        cases.append(case);print(key,case['status'],flush=True)
    report={'status':'PASS' if all(c['status']=='PASS' for c in cases) else 'FAIL','cases':cases,
        'source_stamp':stamp,'source_changed_during_run':stamp!=source_stamp(),'account_authorizations':0,
        'scope':'Real public OAuth metadata/DCR/cancellation; browser launch intentionally suppressed; no completed account consent or token exchange'}
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(str(output/'report.json'),flush=True)
    return report['status']!='PASS'


if __name__=='__main__':raise SystemExit(main())
