"""Reject missing/stale acceptance evidence. Never infer whole-product readiness."""
from __future__ import annotations
import argparse
import json
import hashlib
from pathlib import Path
from source_stamp import ROOT,source_stamp

REQUIRED_UI = {"smoke-settings-navigation","regression-composer-centering",
    "fault-abrupt-restart-corrupt-file-xss","journey-session-rename-persistence",
    "journey-grouped-paginated-capability-browser","journey-messaging-real-schema-save-reopen",
    "smoke-real-mcp-import-auth-feedback-slash","native-flaui-lifecycle-settings"}
REQUIRED_LIVE = {"live-ui-arabic-greeting","live-ui-project-build-real-tools","live-ui-project-edit-real-tools",
    "live-ui-actual-skill-read-and-file-inspection","live-ui-real-multi-agent-one-writer-review",
    "live-ui-arabic-current-gemini-research","live-ui-selected-public-mcp-actual-tool","live-ui-project-save-restart"}
REQUIRED_PROJECT = {f"trial-{trial}-{name}" for trial in (1,2) for name in
    ("import-data-analysis","independent-structured-research","skill-code-repair-security-validation","follow-up-general-web-research")}
REQUIRED_PROJECT |= {"project-abrupt-restart-durable-artifacts","project-monitored-soak"}
MANUAL_GATES = ["Real-account MCP OAuth completion and persistent refresh",
    "Authorized external messaging delivery/incoming connector if required",
    "Voice interruption and voice/local-model GPU coexistence on target hardware",
    "Repeated independent live trials and prolonged monitored reliability",
    "Independent claim-level research grounding and current-vs-historical security guidance",
    "Native window frame/Acrylic visual review; renderer preview certifies only its viewport",
    "Clean Windows installation, distribution/update/rollback/uninstall preserving user data",
    "Project licensing and upstream redistribution rights before public binary/source distribution",
    "Target-hardware resource measurements including the actual backend children, UI and GPU"]


def inspect_report(report,required,stamp):
    failures=[]
    cases=report.get("cases",[])
    missing=required-{item.get("id") for item in cases}
    failures.extend("Missing scenario: "+name for name in sorted(missing))
    if report.get("source_stamp") != stamp or report.get("source_changed_during_run") is not False:
        failures.append("Evidence is stale/unversioned or source changed during the test")
    for case in cases:
        if case.get("status") != "PASS": failures.append("Scenario did not pass: "+str(case.get("id")))
        if not case.get("steps"): failures.append("Missing reproduction steps: "+str(case.get("id")))
        for key in ("screenshot","log"):
            if not case.get(key) or not Path(case[key]).is_file(): failures.append("Missing "+key+": "+str(case.get("id")))
        if case.get("id") == "native-flaui-lifecycle-settings":
            from screen_evidence import inspect_native_record
            failures.extend("Invalid native visual evidence: " + failure
                            for failure in inspect_native_record(case))
    return failures


def newest(pattern):
    files=list((ROOT/"qa-results").glob(pattern))
    if not files: raise ValueError("Missing evidence: "+pattern)
    return max(files,key=lambda path:path.stat().st_mtime)


def inspect_regression(regression, stamp):
    failures = []
    summary = regression.get("summary", {})
    if regression.get("source_stamp") != stamp:
        failures.append("Regression evidence is not bound to this build")
    if regression.get("source_changed_during_run") is not False:
        failures.append("Regression source changed during the run or lacks provenance")
    if summary.get("failed", 0) or summary.get("error", 0) or not summary.get("passed", 0):
        failures.append("Regression did not pass")
    if regression.get("excluded_scenarios") or summary.get("deselected", 0):
        failures.append("Regression coverage is incomplete: excluded/deselected scenarios remain unverified")
    if regression.get("native_legacy_verdict") == "NOT_EVALUATED":
        failures.append("Legacy native application scenario was not evaluated")
    return failures


def inspect_semantic_review(review, stamp, project_path):
    """Executed calls and plausible citations never replace answer correctness."""
    failures=[]
    reference = review.get("project_report")
    if isinstance(reference, dict):
        try:
            matches = (reference.get("file") == str(project_path.resolve())
                       and reference.get("sha256") == hashlib.sha256(project_path.read_bytes()).hexdigest())
        except OSError:
            matches = False
    else:
        matches = reference == str(project_path.resolve())
    if review.get("source_stamp") != stamp or not matches or review.get("source_changed_during_run") is True:
        failures.append("Semantic review is stale or is not bound to the selected project evidence")
    if review.get("research_verdict") != "PASS":
        failures.append("Independent research grounding did not pass: " + str(review.get("research_verdict", "MISSING")))
    for finding in review.get("findings", []):
        if finding.get("blocking") and finding.get("status") != "RESOLVED":
            failures.append("Unresolved acceptance finding: " + str(finding.get("id")))
    if not review.get("checked_claims"):
        failures.append("No independently checked research claims were recorded")
    elif any(row.get("verdict") not in {"PASS", "SUPPORTED"} for row in review["checked_claims"]):
        failures.append("Some independently checked claims remain unsupported or unverified")
    return failures


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--regression",type=Path,required=True)
    parser.add_argument("--ui",type=Path)
    parser.add_argument("--live",type=Path)
    parser.add_argument("--project",type=Path)
    parser.add_argument("--semantic-review",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); stamp=source_stamp()
    if args.output.exists(): parser.error("Refusing to overwrite prior acceptance evidence")
    failures=[]; sources=[]; counts={}; project_path=None
    for pattern,required,label in (("user-*/report.json",REQUIRED_UI,"ui"),("live-ui-*/report.json",REQUIRED_LIVE,"live"),
                                  ("project-ui-*/report.json",REQUIRED_PROJECT,"project")):
        try:
            path=getattr(args,label) or newest(pattern); report=json.loads(path.read_text(encoding="utf-8")); sources.append(str(path))
            failures.extend(inspect_report(report,required,stamp));counts[label]=len(report.get("cases",[]))
            if label == "ui" and report.get("mocked_api_responses") is not False:
                failures.append("UI API provenance is not verified")
            if label == "project":
                project_path=path
                if report.get("trials",0)<2 or report.get("soak_seconds",0)<900:
                    failures.append("Repeated project/15-minute monitored soak is missing")
                if report.get("verdict") != "PASS": failures.append("Project acceptance failed")
        except (ValueError,OSError) as exc: failures.append(str(exc))
    try:
        regression=json.loads(args.regression.read_text(encoding="utf-8"));summary=regression.get("summary",{})
        failures.extend(inspect_regression(regression,stamp))
        counts["regression"]=summary;sources.append(str(args.regression.resolve()))
    except (ValueError,OSError) as exc: failures.append(str(exc))
    execution_failures=list(failures)
    try:
        review_path=args.semantic_review
        review=json.loads(review_path.read_text(encoding="utf-8")); sources.append(str(review_path))
        if project_path is None: failures.append("No project is available for independent review")
        else: failures.extend(inspect_semantic_review(review,stamp,project_path))
    except (ValueError,OSError) as exc: failures.append("Independent semantic review is missing: "+str(exc))
    result={"schema":"wise.quality-gate.v1","source_stamp":stamp,"verified_scope_verdict":"FAIL" if failures else "PASS",
        "execution_scope_verdict":"FAIL" if execution_failures else "PASS",
        "release_verdict":"NOT_READY","evidence_failures":failures,"unverified_gates":MANUAL_GATES,"counts":counts,"sources":sources}
    path=args.output;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":raise SystemExit(main())
