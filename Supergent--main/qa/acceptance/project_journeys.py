"""Repeated real-user project, final-state graders and owned-runtime soak.

Real UI/API/provider, synthetic local project. Never scan remote hosts, send
messages or close the user's existing application. Explicit additional budget
uses per-generation accounting and reservations, not whole-key usage deltas.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import re
import shutil
import sys
import time
import uuid
from datetime import datetime,timezone
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright,expect
from user_journeys import ROOT,BRAVE,Runtime,Evidence
from source_stamp import source_stamp
from process_resources import OwnedProcessSampler
from owned_browser import owned_playwright, browser_environment
from actionability_probe import install_probe, observe_actionability

CSV=b"software,cve\nApache Log4j,CVE-2021-44228\nOutlook,CVE-2023-23397\nOpenSSL,CVE-2022-3602\n"
PYTHON=b"import hashlib\n\ndef digest(value):\n    return hashlib.md5(value.encode()).hexdigest()\n\ndef parse_payload(value):\n    return eval(value)\n"

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--trials",type=int,default=2);parser.add_argument("--soak-seconds",type=int,default=900)
    parser.add_argument("--model",default="qwen/qwen3-coder-flash")
    parser.add_argument("--max-additional-usd",type=float,required=True)
    parser.add_argument("--budget-ledger",type=Path,default=ROOT/"qa/results/additional-budget-20261002.json")
    parser.add_argument("--capture-public-writes", action="store_true",
                        help="Observe only the exact synthetic public CISA/FIRST/Log4Shell write proposals")
    parser.add_argument("--compact-trace", action="store_true",
                        help="Retain actions/network and final case screenshot, not continuous polling visuals")
    parser.add_argument("--only-follow-up", action="store_true",
                        help="Focused diagnostic only: skips analysis, structured sources and code repair; not full project acceptance")
    args=parser.parse_args()
    if not 1<=args.trials<=3 or not 0<=args.soak_seconds<=3600: raise ValueError("Bounded trials/soak required")
    output=ROOT/"qa-results"/("project-ui-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"));output.mkdir(parents=True)
    private=ROOT/".tooling"/("project-ui-runtime-"+uuid.uuid4().hex)
    runtime=Runtime(output,runtime_root=private,app_target="qa.acceptance.instrumented_app:app");evidence=Evidence(output,runtime,trace_visuals=not args.compact_trace);initial=source_stamp();responses=[];resource_samples=[]
    runner_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    sys.path.insert(0,str(ROOT))
    from core.llm.keystore import KeyStore
    key=KeyStore(path=ROOT/"memory/keys.json",secret_path=ROOT/"memory/.keystore_secret").get_for_provider("openrouter",reveal=True)
    if not key: raise RuntimeError("Configured live QA credential is unavailable")
    model=next(row for row in httpx.get("https://openrouter.ai/api/v1/models",timeout=20).json()["data"] if row["id"]==args.model)
    from live_budget import LiveBudget
    budget=LiveBudget(args.budget_ledger,api_key=key["api_key"])
    before=budget.initialize(additional_usd=args.max_additional_usd,model=model["id"],pricing=model["pricing"])
    runtime.env["WISE_QA_BUDGET_PATH"]=str(args.budget_ledger.resolve())
    runtime.env["WISE_QA_TOOL_EVIDENCE_PATH"]=str(output/"actual-tool-observations.ndjson")
    if args.capture_public_writes:
        runtime.env["WISE_QA_STRUCTURED_WRITE_EVIDENCE_PATH"]=str(output/"structured-write-observations.ndjson")
    started=time.monotonic(); active_turn=None; owned_turn_ids=[]
    try:
        for name in ("keys.json",".keystore_secret"):
            source=ROOT/"memory"/name
            if source.exists(): shutil.copy2(source,private/"memory"/name)
        runtime.start()
        response=httpx.post(runtime.url+"/api/v2/models/external",json={"provider_id":"openrouter","model":model["id"],"set_as_active":True},timeout=30)
        response.raise_for_status();assert response.json().get("ok")
        with owned_playwright(output) as pw:
            browser=pw.chromium.launch(executable_path=str(BRAVE),headless=True,env=browser_environment(output))
            context=browser.new_context(viewport={"width":1440,"height":900});page=context.new_page()
            install_probe(page)
            page.goto(runtime.url+"/app/",wait_until="networkidle")
            def send(text):
                nonlocal active_turn
                # Normal mode performs one preflight classification before its
                # hard-capped 12-turn executor. Keep both bounded and charged;
                # the shared dollar ledger still admits every dispatch first.
                turn_id=budget.begin_turn(max_requests=13)
                owned_turn_ids.append(turn_id)
                active_turn=turn_id
                evidence.step("Type project request: "+text,lambda:page.locator("#prompt").fill(text))
                with page.expect_response(lambda r:r.url.endswith("/api/v2/chat/events") and r.request.method=="POST",timeout=240000) as pending:
                    evidence.step("Submit through WISE Send button",lambda:page.locator("#sendButton").click())
                request_key=pending.value.request.headers["idempotency-key"]
                deadline=time.monotonic()+240;completed=None
                while time.monotonic()<deadline:
                    status=httpx.get(runtime.url+"/api/v2/chat/requests/"+request_key,timeout=10).json()
                    if status.get("state")=="COMPLETED": completed=status["response"];break
                    if status.get("state")=="UNKNOWN": raise AssertionError("Request failed or expired")
                    page.wait_for_timeout(250)
                assert completed is not None,"No completed provider response"
                budget.end_turn(turn_id)
                active_turn=None
                responses.append(completed)
                (output/"responses.json").write_text(json.dumps(responses,ensure_ascii=False,indent=2),encoding="utf-8")
                expect(page.locator("#prompt")).to_be_enabled(timeout=240000)
                assert not completed.get("error"),completed.get("error")
                assert completed.get("model_name") and completed.get("execution_metrics",{}).get("model_requests",0)>0
                return completed
            def calls(result): return {row.get("tool") for row in result.get("working_items",[]) if row.get("success") and not row.get("duplicate_prevented")}
            def reference_json(url,params=None):
                with httpx.stream("GET",url,params=params,timeout=20,trust_env=False,follow_redirects=False) as response:
                    response.raise_for_status();data=bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data)>5_000_000: raise ValueError("Independent reference exceeds size bound")
                    return json.loads(data)
            for trial in range(1,args.trials+1):
                prefix=f"trial-{trial}"
                evidence.step("Start an independent project chat",lambda:page.locator('[data-view="chat"]').click())
                def import_analysis():
                    evidence.step("Import synthetic assets.csv through WISE file picker",lambda:page.locator("#fileInput").set_input_files({"name":"assets.csv","mimeType":"text/csv","buffer":CSV}))
                    result=send(f"Inspect the attached assets.csv using DuckDB's local CSV adapter. Create {prefix}-analysis.json with the actual row_count, column names, and the first row's CVE. Verify the saved file. Do not use shell, execute code, scan networks or send messages.")
                    data=json.loads((private/"workspace"/f"{prefix}-analysis.json").read_text(encoding="utf-8"))
                    assert data["row_count"]==3 and "CVE-2021-44228" in json.dumps(data)
                    assert "intelligence.duckdb" in calls(result),"No actual upstream DuckDB execution"
                    assert "native.write_file" in calls(result) and "native.read_file" in calls(result)
                if not args.only_follow_up:
                    evidence.case(prefix+"-import-data-analysis",context,page,import_analysis)
                def structured_research():
                    result=send(f"Read {prefix}-analysis.json. Research its first CVE using CISA Known Exploited Vulnerabilities and FIRST EPSS query adapters. Create {prefix}-research.md containing observed facts, the KEV dateAdded, the exact unrounded EPSS score and score date, exact source URLs and retrieval dates. Distinguish known exploitation from predicted exploitation probability. Verify the saved file. Report unavailable sources honestly; no network scans or external messages.")
                    text=(private/"workspace"/f"{prefix}-research.md").read_text(encoding="utf-8")
                    assert "CVE-2021-44228" in text and "cisa.gov" in text and "first.org" in text
                    assert {"source.cisa-known-exploited-vulnerabilities","source.first-epss"}.issubset(calls(result)),"Missing complementary actual sources"
                    kev=next(row for row in reference_json("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")["vulnerabilities"] if row["cveID"]=="CVE-2021-44228")
                    epss=reference_json("https://api.first.org/data/v1/epss",{"cve":"CVE-2021-44228"})["data"][0]
                    assert kev["dateAdded"] in text and epss["date"] in text,"Observed dates do not match independent source retrieval"
                    values=[float(value) for value in re.findall(r"(?<![\d.])0\.\d+",text)]
                    assert any(abs(value-float(epss["epss"]))<1e-8 for value in values),"EPSS probability does not match independent source"
                    (output/f"{prefix}-independent-reference.json").write_text(json.dumps({"kev":kev,"epss":epss},indent=2),encoding="utf-8")
                if not args.only_follow_up:
                    evidence.case(prefix+"-independent-structured-research",context,page,structured_research)
                def security_repair():
                    evidence.step("Import synthetic Python source through WISE file picker",lambda:page.locator("#fileInput").set_input_files({"name":"sample.py","mimeType":"text/x-python","buffer":PYTHON}))
                    result=send("Read the indexed code-review-checklist skill, apply its relevant code/security checks, then inspect only the attached sample.py. Replace weak MD5 with SHA256 and unsafe eval with json.loads, preserving digest(value) and parse_payload(value). Edit the actual attached workspace file, not a new copy. Run Bandit on the modified file and read it back. Do not execute Python/shell, access other files, scan networks or send messages.")
                    assert "code-review-checklist" in result["execution_metrics"].get("skills_read",[]),"Skill was not actually read"
                    imported=sorted((private/"workspace/attachments").glob("*sample.py"),key=lambda path:path.stat().st_mtime_ns)
                    assert imported,"Missing imported source file"
                    code=imported[-1].read_text(encoding="utf-8");tree=ast.parse(code)
                    assert {"digest","parse_payload"}.issubset({node.name for node in tree.body if isinstance(node,ast.FunctionDef)})
                    call_names={node.func.attr if isinstance(node.func,ast.Attribute) else node.func.id if isinstance(node.func,ast.Name) else "" for node in ast.walk(tree) if isinstance(node,ast.Call)}
                    assert "sha256" in call_names and "loads" in call_names and not {"eval","md5"}.intersection(call_names)
                    # Independent behavior grader, not agent-run Python. Admit
                    # only two side-effect-free functions and their whitelisted
                    # stdlib operations before executing any generated AST.
                    allowed_nodes=(ast.Module,ast.Import,ast.alias,ast.FunctionDef,ast.arguments,ast.arg,
                        ast.Return,ast.Call,ast.Name,ast.Load,ast.Attribute,ast.Constant)
                    assert all(isinstance(node,allowed_nodes) for node in ast.walk(tree)),"Generated source has unapproved executable structures"
                    assert all(isinstance(node,(ast.Import,ast.FunctionDef)) for node in tree.body)
                    assert all(node.name in {"digest","parse_payload"} and not node.decorator_list for node in tree.body if isinstance(node,ast.FunctionDef))
                    imports={alias.asname or alias.name:alias.name for node in tree.body if isinstance(node,ast.Import) for alias in node.names}
                    assert set(imports.values())<={"hashlib","json"}
                    assert all(node.id in {"value",*imports} for node in ast.walk(tree) if isinstance(node,ast.Name))
                    assert all(node.attr in {"encode","sha256","hexdigest","loads"} for node in ast.walk(tree) if isinstance(node,ast.Attribute))
                    modules={"hashlib":hashlib,"json":json}
                    namespace={"__builtins__":{"__import__":lambda name,*args,**kwargs:modules[name]}}
                    exec(compile(tree,"<strictly-admitted-synthetic-qa-source>","exec"),namespace)
                    assert namespace["digest"]("WISE")==hashlib.sha256(b"WISE").hexdigest(),"Digest behavior is wrong despite valid-looking syntax"
                    assert namespace["parse_payload"]('{"ok":true}')=={"ok":True},"JSON parse behavior is wrong"
                    assert calls(result).intersection({"native.security_scan","intelligence.bandit"}),"No actual defensive scanner"
                if not args.only_follow_up:
                    evidence.case(prefix+"-skill-code-repair-security-validation",context,page,security_repair)
                def follow_up():
                    result=send(f"For this project, search the general internet for current official guidance on mitigating Log4Shell from CISA and Apache Log4j. Fetch at least one accessible official page rather than relying only on snippets. If CISA refuses page access, do not bypass it: use an accessible official Apache page and clearly record the CISA limitation. Create {prefix}-follow-up.md with citations and uncertain details, including the inaccessible CISA source if applicable. Verify that file. Do not limit the search to our preinstalled catalog. No shell, network scans or messages.")
                    text=(private/"workspace"/f"{prefix}-follow-up.md").read_text(encoding="utf-8")
                    assert "https://" in text and "cisa.gov" in text
                    assert {"native.web_search","native.web_fetch"}.issubset(calls(result)),"General search/fetched-page evidence missing"
                evidence.case(prefix+"-follow-up-general-web-research",context,page,follow_up)
            def restart():
                files={str(path.relative_to(private/"workspace")):hashlib.sha256(path.read_bytes()).hexdigest() for path in (private/"workspace").rglob("*") if path.is_file()}
                evidence.step("Abruptly stop ONLY owned backend",lambda:runtime.stop(abrupt=True))
                evidence.step("Reopen same isolated project",runtime.start)
                evidence.step("Reload WISE",lambda:page.reload(wait_until="networkidle"))
                assert files=={str(path.relative_to(private/"workspace")):hashlib.sha256(path.read_bytes()).hexdigest() for path in (private/"workspace").rglob("*") if path.is_file()}
                expect(page.locator("#sessionList")).not_to_be_empty()
            evidence.case("project-abrupt-restart-durable-artifacts",context,page,restart)
            def soak():
                evidence.step(f"Exercise owned UI/backend for {args.soak_seconds} seconds; no paid turns",lambda:None)
                sampler=OwnedProcessSampler(runtime.process.pid)
                sampler.sample() # Prime all existing CPU counters, including launcher children.
                deadline=time.monotonic()+args.soak_seconds
                while time.monotonic()<deadline:
                    expect(page.locator("#prompt")).to_be_visible()
                    try:
                        page.locator("#settingsButton").click()
                        expect(page.locator("#settingsModal")).to_be_visible()
                        page.locator("#closeSettings").click()
                        expect(page.locator("#settingsModal")).to_be_hidden()
                    except Exception:
                        # Observe BEFORE screenshot/reload can resume paint. No
                        # retry or force-click: the original action stays failed.
                        (output/"soak-actionability-failure.json").write_text(
                            json.dumps(observe_actionability(page),ensure_ascii=False,indent=2),encoding="utf-8")
                        raise
                    httpx.get(runtime.url+"/health",timeout=5).raise_for_status()
                    resource_samples.append({"elapsed_s":round(time.monotonic()-started,1),**sampler.sample()})
                    print(f"SOAK {len(resource_samples)} samples",flush=True)
                    page.wait_for_timeout(min(20000,max(1,int((deadline-time.monotonic())*1000))))
                if resource_samples: assert resource_samples[-1]["rss_mb"]-resource_samples[0]["rss_mb"]<96,"Memory growth exceeds short-soak guardrail"
            evidence.case("project-monitored-soak",context,page,soak)
            context.close();browser.close()
        after=budget.summary()
        owned_cost=budget.summarize_turns(owned_turn_ids)
        successful={row["tool"] for result in responses for row in result.get("working_items",[]) if row.get("success") and not row.get("duplicate_prevented")}
        skills={name for result in responses for name in result.get("execution_metrics",{}).get("skills_read",[])}
        report={"schema":"wise.project-acceptance.v1","runtime":str(private),"cases":evidence.cases,"verdict":"FAIL" if any(case["status"]!="PASS" for case in evidence.cases) else "PASS",
            "source_stamp":initial,"source_changed_during_run":initial!=source_stamp(),"trials":args.trials,"soak_seconds":args.soak_seconds,
            "acceptance_scope":"FOCUSED_FOLLOW_UP_DIAGNOSTIC_ONLY" if args.only_follow_up else "REPEATED_PROJECT",
            "runner_sha256_at_start":runner_digest,
            "runner_changed_during_run":runner_digest!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "distinct_actual_tools":sorted(successful),"actual_skills":sorted(skills),"model":model["id"],"resource_samples":resource_samples,
            "resource_measurement_scope":"OWNED_BACKEND_PROCESS_TREE; excludes UI/browser/GPU; summed working sets",
            "budget_before":before,"budget_after":after,
            "shared_ledger_cost_delta":after["attributed_generation_cost_usd"]-before["attributed_generation_cost_usd"],
            "owned_turn_accounting":owned_cost,
            "attributed_generation_cost_delta":owned_cost["attributed_generation_cost_usd"],
            "limitations":["Short soak/repeated project is not proof of day-long reliability or optimal capability coverage","No real-account integrations, voice/GPU, network scans or outgoing messages tested"]}
        (output/"responses.json").write_text(json.dumps(responses,ensure_ascii=False,indent=2),encoding="utf-8")
        (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"report":str(output/"report.json"),"verdict":report["verdict"],"tools":sorted(successful),"skills":sorted(skills)}),flush=True)
        return 0 if report["verdict"]=="PASS" and not report["source_changed_during_run"] else 1
    finally:
        runtime.stop()
        if active_turn is not None:
            budget.end_turn(active_turn)  # Release only our owned turn, never its unresolved cost reserves.
        for name in ("keys.json",".keystore_secret"):
            target=private/"memory"/name
            if target.exists() and target.resolve().parent==(private/"memory").resolve(): target.unlink()
        for handle in runtime.handles: handle.close()

if __name__=="__main__": raise SystemExit(main())
