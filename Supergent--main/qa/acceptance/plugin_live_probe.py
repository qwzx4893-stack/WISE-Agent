"""Owned, keyless public MCP tool execution; no user accounts or model spend."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import traceback
import sys


def main():
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    output = root/"qa-results"/("plugin-live-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(parents=True)
    os.environ.update(WISE_RUNTIME_ROOT=str(output/"runtime"),WISE_CONFIG_DIR=str(output/"runtime/config"),AGENT_OS_SKIP_FIRST_BOOT_CHECK="1")
    from core.plugins.manager import PluginManager
    from core.mcp.registry import MCPRegistry, build_mcp_tools
    from source_stamp import source_stamp
    memory = output/"runtime/memory"
    memory.mkdir(parents=True)
    registry = MCPRegistry(memory)
    service = PluginManager(memory,registry)
    stamp = source_stamp()
    cases = []
    for key in ("cloudflare-docs", "context7"):
        result = {"extension":key,"status":"FAIL"}
        try:
            detail = service.connect(key)
            result.update(connection=detail["status"],tool_names=[t["name"] for t in detail["tools"]])
            assert detail["status"] == "CONNECTED", detail["error"]
            names = {t["name"] for t in detail["tools"]}
            if key == "context7":
                tool = "resolve-library-id"
                args = {"libraryName":"FastAPI","query":"Find official FastAPI path parameter documentation"}
            else:
                tool = next(n for n in names if "search" in n.lower())
                args = {"query":"Cloudflare Workers fetch handler API"}
            import re
            runner = build_mcp_tools(registry,auto_start=False)[f"mcp_{detail['server']}_"+re.sub(r"[^A-Za-z0-9_]","_",tool)]
            reply = runner(**args)
            assert isinstance(reply,dict) and reply.get("content") and not reply.get("isError"), "Public tool returned an error or no content"
            result.update(status="PASS",executed_tool=tool,arguments=args,response_preview=json.dumps(reply,ensure_ascii=False)[:3000])
        except Exception: result["stack_trace"] = traceback.format_exc()
        finally:
            if key in service.installed: service.disable(key,remove=True)
            cases.append(result)
            print(key,result["status"],flush=True)
    report = {"scope":"Real public MCP handshakes and read-only tool calls, no models, keys or account login",
              "cases":cases,"source_stamp":stamp,"source_changed_during_run":stamp!=source_stamp()}
    (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(str(output/"report.json"),flush=True)
    return any(c["status"] == "FAIL" for c in cases)


if __name__ == "__main__": raise SystemExit(main())
