"""Actual Context7 discovery/public-client registration, not human login."""
import json
import sys
from datetime import datetime,timezone
from urllib.parse import urlsplit,parse_qs
from source_stamp import ROOT,source_stamp

sys.path.insert(0,str(ROOT))
from core.mcp.registry import MCPRegistry,MCPServerConfig
from core.mcp.oauth import start

def main():
    output=ROOT/"qa-results"/("oauth-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir();registry=MCPRegistry(root=output)
    registry._configs=[MCPServerConfig(name="qa-context7",transport="http",url="https://mcp.context7.com/mcp",enabled=False)]
    registry._save()
    report={"source_stamp":source_stamp(),"human_account_login":False,"credentials_used":False}
    try:
        result=start("qa-context7",registry)
        parsed=urlsplit(result["auth_url"]);query=parse_qs(parsed.query)
        assert parsed.scheme == "https" and query["code_challenge_method"] == ["S256"]
        assert query["resource"] == ["https://mcp.context7.com"]
        assert registry._find("qa-context7").url == "https://mcp.context7.com/mcp"
        report.update(status="PASS",authorization_origin=f"{parsed.scheme}://{parsed.netloc}",
            resource=query["resource"][0],pkce="S256",scope="Discovery + public-client registration + authorization URL; NOT account login")
    except Exception as exc:
        report.update(status="FAIL",error=type(exc).__name__+": "+str(exc)[:250])
    finally:registry.stop_all()
    (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report));return 0 if report["status"]=="PASS" else 1

if __name__ == "__main__":raise SystemExit(main())
