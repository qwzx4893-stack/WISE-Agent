"""Stamp deterministic extension regression evidence before and after pytest."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from source_stamp import ROOT, source_stamp

TESTS = ["test_mcp_market.py","test_mcp_bundles.py","test_service_connectors.py","test_plugin_packages.py","test_capability_skill_resolution.py","test_capability_agent_regressions.py","test_plugins.py","test_ui_connection_contracts.py","test_mcp_oauth_flow.py","test_mcp_configuration_import.py",
    "test_mcp_security.py","test_mcp_server_and_bridge.py","test_mcp_settings_and_oauth.py","test_final_capability_contracts.py",
    "test_desktop_source_package.py","test_acceptance_evidence_integrity.py","test_wise_web_shell.py"]

def main():
    out=ROOT/"qa-results"/("plugins-regression-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out.mkdir(parents=True)
    stamp=source_stamp()
    env={k:v for k,v in os.environ.items() if not any(word in k.upper() for word in ("API_KEY","TOKEN","SECRET","PASSWORD","NANGO"))}
    env.update(PYTHONUTF8="1",AGENT_OS_SKIP_FIRST_BOOT_CHECK="1")
    command=[sys.executable,"-m","pytest",*["tests/"+t for t in TESTS],"-q",f"--basetemp={out/'temp'}",f"--junitxml={out/'results.xml'}"]
    with (out/"pytest.log").open("w",encoding="utf-8") as handle:
        result=subprocess.run(command,cwd=ROOT,env=env,stdout=handle,stderr=subprocess.STDOUT)
    import xml.etree.ElementTree as ET
    suite=ET.parse(out/"results.xml").find("testsuite")
    report={"status":"PASS" if result.returncode==0 else "FAIL","source_stamp":stamp,"source_changed_during_run":stamp!=source_stamp(),
        "tests":suite.attrib,"logs":str(out/"pytest.log"),"junit":str(out/"results.xml"),"scope":"Deterministic regression; not real-account OAuth or long-term whole-product certification"}
    (out/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report),flush=True)
    return result.returncode

if __name__=="__main__": raise SystemExit(main())
