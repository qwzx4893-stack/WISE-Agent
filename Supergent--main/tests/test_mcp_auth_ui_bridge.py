"""Offline JS bridge contracts, not proof of human OAuth account login."""
import json
from pathlib import Path
import subprocess


def test_authorization_bridge_accepts_native_bool_and_preserves_manual_link():
    source = Path(__file__).resolve().parents[1] / "ui/wise_web/integrations.js"
    script = r'''
const fs=require('fs'), vm=require('vm');
const notices=[], links=[];
const host={isConnected:false,append(){},setAttribute(){}};
const context={URL, document:{addEventListener(){},createElement(tag){const node={...host};if(tag==='a')links.push(node);return node;},createTextNode(t){return t;}},
 window:{pywebview:{api:{open_external_url:async()=>true}}},state:{},L:(ar,en)=>en,
 $:id=>id==='mcpAuthFeedback'?null:id==='settingsContent'?{prepend(){}}:{hidden:false},toast:(...args)=>notices.push(args)};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
 await context.openMcpAuthorization({ok:true,auth_url:'https://login.example/authorize',state:'offline'});
 if(notices.length)throw Error('Boolean native success incorrectly reported as failure');
 if(links[0].href!=='https://login.example/authorize')throw Error('Missing manual authorization link');
 context.window.pywebview.api.open_external_url=async()=>false;
 await context.openMcpAuthorization({ok:true,auth_url:'https://login.example/authorize',state:'offline'});
 if(notices.length!==1)throw Error('Native failure was silently ignored');
 let rejected=false;
 try{await context.openMcpAuthorization({ok:true,auth_url:'http://unsafe.example',state:'offline'});}catch(e){rejected=true;}
 if(!rejected)throw Error('Unsafe auth URL accepted');
 rejected=false;
 try{await context.openMcpAuthorization({ok:false});}catch(e){rejected=true;}
 if(!rejected)throw Error('Missing auth link silently ignored');
 process.stdout.write('PASS');
})().catch(e=>{process.stderr.write(String(e));process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "PASS"
