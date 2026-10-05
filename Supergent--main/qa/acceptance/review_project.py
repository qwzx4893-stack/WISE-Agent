"""Bounded independent fact/evidence graders; no paid model or truth oracle.

This checks the synthetic project's stated facts against independent reference
captures and real tool-result observations. A separate reviewer must still read
the artifacts for broader semantic claims. It cannot certify all research.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from source_stamp import source_stamp, ROOT


def review(report_path, output):
    report_path=report_path.resolve()
    if output.exists(): raise FileExistsError("Refusing to overwrite prior review evidence")
    if ROOT/"qa-results" not in report_path.parents: raise ValueError("Owned QA report required")
    report=json.loads(report_path.read_text(encoding="utf-8"));root=Path(report["runtime"]).resolve()
    if ROOT/".tooling" not in root.parents: raise ValueError("Owned isolated runtime required")
    observations_path=report_path.parent/"actual-tool-observations.ndjson"
    observations=[json.loads(line) for line in observations_path.read_text(encoding="utf-8").splitlines()]
    page_evidence=[];source_observations=[]
    for row in observations:
        result=row["result"];payload=result.get("output")
        if isinstance(payload,str):
            try: payload=json.loads(payload)
            except ValueError: continue
        if not isinstance(payload,dict):continue
        if row["tool"]=="native.web_fetch" and result.get("success"):
            page_evidence.append({"url":payload.get("resolved_url"),**payload})
            source_observations.append({"url":payload.get("resolved_url"),"retrieved_at":payload.get("retrieved_at"),
                "sha256":hashlib.sha256(payload.get("page_text","").encode()).hexdigest(),
                "excerpt_truncated":payload.get("excerpt_truncated"),"scope":"ACTUAL_FETCHED_EXCERPT_NOT_FULL_PAGE"})
    claims=[];findings=[]
    def claim(artifact,identifier,expected,observed,passed,reference):
        claims.append({"artifact":str(artifact),"artifact_sha256":hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "claim_identifier":identifier,"expected":expected,"observed":observed,"reference":reference,
            "verdict":"PASS" if passed else "UNSUPPORTED"})
    def required_artifact(path, *, as_json=False):
        try:
            text=path.read_text(encoding="utf-8")
            return json.loads(text) if as_json else text
        except (OSError, ValueError) as exc:
            findings.append({"id":path.name+"-missing-or-corrupt","status":"OPEN","blocking":True,
                "artifact":str(path),"detail":"Required artifact is unavailable or corrupt: "+type(exc).__name__})
            return None
    for trial in range(1,report["trials"]+1):
        analysis=root/"workspace"/f"trial-{trial}-analysis.json"
        data=required_artifact(analysis,as_json=True)
        if isinstance(data,dict):
            claim(analysis,"CSV row count",3,data.get("row_count"),data.get("row_count")==3,"Independent synthetic CSV fixture")
            claim(analysis,"first actual CVE","CVE-2021-44228",data,"CVE-2021-44228" in json.dumps(data),"Independent synthetic CSV fixture")
        elif data is not None:
            findings.append({"id":analysis.name+"-invalid-schema","status":"OPEN","blocking":True,
                             "detail":"Required analysis must be a JSON object"})
        research=root/"workspace"/f"trial-{trial}-research.md"
        text=required_artifact(research)
        reference_path=report_path.parent/f"trial-{trial}-independent-reference.json"
        reference=required_artifact(reference_path,as_json=True)
        if text is not None and reference is not None:
            try:
                for key,value in (("KEV dateAdded",reference["kev"]["dateAdded"]),("EPSS score date",reference["epss"]["date"])):
                    claim(research,key,value,value if value in text else None,value in text,str(reference_path))
                values=[float(value) for value in re.findall(r"(?<![\d.])0\.\d+",text)]
                expected=float(reference["epss"]["epss"])
                claim(research,"exact unrounded EPSS probability",expected,values,
                    any(abs(value-expected)<1e-8 for value in values),str(reference_path))
            except (KeyError, TypeError, ValueError):
                findings.append({"id":reference_path.name+"-invalid-schema","status":"OPEN","blocking":True,
                                 "detail":"Independent reference schema is incomplete"})
        follow_up=root/"workspace"/f"trial-{trial}-follow-up.md"
        follow_text=required_artifact(follow_up)
        if follow_text is None: continue
        old_property="log4j2.formatMsgNoLookups"
        asserted=old_property in follow_text
        supported=any(old_property in row.get("page_text","") for row in page_evidence)
        if asserted and not supported:
            findings.append({"id":f"trial-{trial}-unverified-technical-property","status":"OPEN","blocking":True,
                "detail":"Exact historical technical property was asserted without support in actual fetched excerpts; not proof it never existed"})
        claim(follow_up,"No recurrence of unsupported historical property attribution",
            "Omitted unless supported by actual fetched evidence",{"asserted":asserted,"excerpt_support":supported},not asserted or supported,str(observations_path))
    result={"schema":"wise.semantic-review.v1","generated_at":datetime.now(timezone.utc).isoformat(),
        "source_stamp":report.get("source_stamp"),"source_changed_during_run":report.get("source_changed_during_run"),
        "reviewer_application_stamp":source_stamp(),"project_report":str(report_path),"research_verdict":"INCOMPLETE",
        "automated_fact_verdict":"PASS" if all(row["verdict"]=="PASS" for row in claims) and not findings else "FAIL",
        "checked_claims":claims,"findings":findings,"source_observations":source_observations,
        "scope":"Independent final-state/structured-fact and exact-token checks only; broader semantic reviewer sign-off still required",
        "actual_observations":str(observations_path)}
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open("x",encoding="utf-8") as handle:
        json.dump(result,handle,ensure_ascii=False,indent=2)
    print(json.dumps({"review":str(output),"automated_fact_verdict":result["automated_fact_verdict"],"claims":len(claims)}))
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=review(args.report,args.output)
    return 1 if result["automated_fact_verdict"] != "PASS" else 0


if __name__=="__main__":raise SystemExit(main())
