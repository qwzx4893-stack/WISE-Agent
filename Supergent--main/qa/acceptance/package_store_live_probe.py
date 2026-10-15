"""Install a real public plugin package and call its actual bundled MCP tool.

No paid model, account login, external messages or user-configured server.
"""
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def main():
    root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root))
    output=root/"qa-results"/("package-live-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"));output.mkdir()
    for name in list(os.environ):
        if any(word in name.upper() for word in ("API_KEY","TOKEN","SECRET","PASSWORD","NANGO")):os.environ.pop(name)
    os.environ.update(WISE_RUNTIME_ROOT=str(output/"runtime"),WISE_CONFIG_DIR=str(output/"runtime/config"),AGENT_OS_SKIP_FIRST_BOOT_CHECK="1")
    from source_stamp import source_stamp
    from core.plugins.store import PackageStore
    from core.plugins.manager import PluginManager
    from core.plugins.marketplace import read_marketplace,DEFAULT_MARKETPLACES
    from core.mcp.registry import MCPRegistry,build_mcp_tools
    memory=output/"runtime/memory";memory.mkdir(parents=True)
    registry=MCPRegistry(memory);components=PluginManager(memory,registry)
    store=PackageStore(memory,components);stamp=source_stamp()
    result={"package":"context7","status":"FAIL"};identifier=None
    try:
        snapshot=read_marketplace(DEFAULT_MARKETPLACES[0])
        store.cache={"time":0,"snapshots":[snapshot]}
        entry=next(e for e in snapshot["items"] if e["name"]=="context7")
        identifier=entry["id"];detail=store.detail(identifier)
        assert detail["plan"]["can_install"] and not detail["plan"]["unsupported"]
        store.install(identifier,detail["commit"],trust=True)
        assert not registry.list_servers(),"Install must not start a server"
        server=detail["plan"]["servers"][0]
        assert not server["local_execution"] and not server["fields"],"Only a keyless public connection is allowed in this probe"
        connected=store.connect(identifier,server["name"])
        result["connection_attempts"]=[{"status":connected["status"],"diagnostic":registry._errors.get(connected.get("server"),"")}]
        if connected["status"]=="FAILED":
            # One explicit retry of a keyless public handshake, not a replay of
            # a tool action. Preserve the failed attempt rather than conceal it.
            connected=store.connect(identifier,server["name"])
            result["connection_attempts"].append({"status":connected["status"],"diagnostic":registry._errors.get(connected.get("server"),"")})
        assert connected["status"]=="CONNECTED",connected["error"]
        method="resolve-library-id"
        callable_=build_mcp_tools(registry,auto_start=False)[f"mcp_{connected['server']}_"+re.sub(r"[^A-Za-z0-9_]","_",method)]
        reply=callable_(libraryName="FastAPI",query="Find official FastAPI path parameter documentation")
        assert isinstance(reply,dict) and reply.get("content") and not reply.get("isError"),"Read-only tool did not return content"
        result.update(status="PASS",commit=detail["commit"],connection=connected["status"],executed_tool=method,
            tool_count=len(connected["tools"]),response_preview=json.dumps(reply,ensure_ascii=False)[:1800])
    except Exception:result["stack_trace"]=traceback.format_exc()
    finally:
        if identifier in store.installed:store.disable(identifier,True)
        for row in registry.list_servers():registry.stop(row["name"])
        report={"case":result,"source_stamp":stamp,"source_changed_during_run":stamp!=source_stamp(),"paid_model_calls":0,"account_auth_completed":False}
        (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"status":result["status"],"report":str(output/"report.json")}),flush=True)
    return result["status"]!="PASS"


if __name__=="__main__":raise SystemExit(main())
