"""Opt-in, bounded diagnostics for the synthetic public CISA/FIRST project.

Observe the actual write proposal before/after citation binding and the actual
grounding verdict. Do not change completion requests, guards, evidence or writes.
No arbitrary task, imported file, prompt, account or credential is captured.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from qa.acceptance.tool_evidence import ROOT, _lock, redact

LOG = logging.getLogger("WISE.QA.StructuredWriteObservation")
CVE = "CVE-2021-44228"


def public_goal(trial):
    return (f"Read trial-{trial}-analysis.json. Research its first CVE using CISA Known Exploited Vulnerabilities "
            f"and FIRST EPSS query adapters. Create trial-{trial}-research.md containing observed facts, the KEV "
            "dateAdded, the exact unrounded EPSS score and score date, exact source URLs and retrieval dates. "
            "Distinguish known exploitation from predicted exploitation probability. Verify the saved file. "
            "Report unavailable sources honestly; no network scans or external messages.")


def follow_up_goal(trial):
    return ("For this project, search the general internet for current official guidance on mitigating Log4Shell "
            "from CISA and Apache Log4j. Fetch at least one accessible official page rather than relying only on "
            "snippets. If CISA refuses page access, do not bypass it: use an accessible official Apache page and "
            f"clearly record the CISA limitation. Create trial-{trial}-follow-up.md with citations and uncertain "
            "details, including the inaccessible CISA source if applicable. Verify that file. Do not limit the "
            "search to our preinstalled catalog. No shell, network scans or messages.")


def owned_destination(destination):
    destination = Path(destination).resolve()
    runtime = Path(os.environ.get("WISE_RUNTIME_ROOT", "")).resolve()
    if (not os.environ.get("WISE_QA_BUDGET_PATH") or ROOT / "qa-results" not in destination.parents
            or destination.suffix != ".ndjson" or runtime.parent != (ROOT / ".tooling").resolve()
            or not runtime.name.startswith("project-ui-runtime-")):
        raise ValueError("Structured-write diagnostics require an opted-in owned synthetic project runtime")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def public_records(evidence):
    """Only the actual single-CVE adapter records from two fixed public APIs."""
    rows = []
    for item in evidence:
        if not isinstance(item, dict) or item.get("evidence_kind") != "source_api_data":
            continue
        try:
            payload = json.loads(item.get("page_text", ""))
            parsed = urlsplit(item.get("url", ""))
            query = parse_qs(parsed.query, strict_parsing=True)
            port = parsed.port
        except (ValueError, TypeError):
            continue
        if (not isinstance(payload, dict) or parsed.scheme != "https" or parsed.username or parsed.password
                or port or parsed.fragment):
            continue
        resource = payload.get("resource_id")
        if resource == "cisa-known-exploited-vulnerabilities":
            if parsed.hostname != "www.cisa.gov" or parsed.path != "/sites/default/files/feeds/known_exploited_vulnerabilities.json" or query:
                continue
            fields = ("cveID", "dateAdded", "shortDescription", "requiredAction", "dueDate")
        elif resource == "first-epss":
            if (parsed.hostname != "api.first.org" or parsed.path != "/data/v1/epss"
                    or set(query) - {"cve", "limit"} or query.get("cve") != [CVE]
                    or ("limit" in query and (len(query["limit"]) != 1 or not re.fullmatch(r"\d{1,2}", query["limit"][0])))):
                continue
            fields = ("cve", "epss", "percentile", "date")
        else:
            continue
        records = payload.get("records")
        if not isinstance(records, list):
            continue
        selected = [{key: str(record[key])[:4000] for key in fields if key in record}
                    for record in records[:100] if isinstance(record, dict)
                    and str(record.get("cveID") or record.get("cve", "")).upper() == CVE]
        if selected:
            rows.append({"resource_id": resource, "url": item["url"], "records": selected[:3],
                         "retrieved_at": str(item.get("retrieved_at", ""))[:60]})
        if len(rows) == 4:
            break
    return rows


def public_page_records(evidence):
    rows = []
    for item in evidence:
        if not isinstance(item, dict) or item.get("evidence_kind") != "page_excerpt":
            continue
        try:
            parsed = urlsplit(item.get("resolved_url") or item.get("url", ""))
            if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port
                    or parsed.query or parsed.fragment or parsed.hostname not in {"www.cisa.gov", "logging.apache.org"}):
                continue
        except (TypeError, ValueError):
            continue
        rows.append({key: (str(item[key])[:7000] if key == "page_text" else str(item[key])[:1000])
                     for key in ("url", "resolved_url", "title", "page_text", "retrieved_at", "excerpt_scope") if key in item})
        if len(rows) == 4:
            break
    return rows


def write_record(destination, record):
    line = json.dumps(redact(record), ensure_ascii=False)
    if len(line.encode("utf-8")) > 250000:
        raise ValueError("Structured write observation exceeds its bounded evidence budget")
    with _lock, destination.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def scanner_observation(tool, arguments, result, runtime):
    """No source body, line, snippet, arbitrary scanner result or raw error."""
    if (tool not in {"intelligence.bandit", "native.security_scan"} or not isinstance(arguments, dict)
            or (tool == "native.security_scan" and arguments.get("scanner") != "bandit")):
        return None
    path = arguments.get("path")
    if not isinstance(path, str) or ".." in Path(path).parts:
        return None
    workspace = runtime / "workspace"
    target = Path(path) if Path(path).is_absolute() else workspace / path
    try:
        if any(part.is_symlink() or part.is_junction() for part in (runtime, workspace, workspace / "attachments", target)):
            return None
        resolved = target.resolve()
        if (resolved.parent != (workspace / "attachments").resolve()
                or not re.fullmatch(r"\d{10,25}-sample\.py", resolved.name) or not resolved.is_file()):
            return None
    except (ValueError, OSError):
        return None
    data = result.to_dict()
    if not isinstance(data, dict):
        return None
    output = data.get("output")
    actual = output if isinstance(output, dict) else {}
    count = actual.get("finding_count")
    count = count if type(count) is int and count >= 0 else None
    success = data.get("success") is True
    correct_scanner = actual.get("scanner") == "bandit"
    status = ("FAILED" if not success else "UNVERIFIED_SCHEMA" if not correct_scanner or count is None
              else "COMPLETED_ZERO_FINDINGS" if count == 0 else "COMPLETED_FINDINGS")
    fields = {}
    for key in ("files_submitted", "files_skipped"):
        value = actual.get(key)
        fields[key] = value if type(value) is int and value >= 0 else None
    return {"kind": "synthetic_bandit_observation", "tool": tool,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "target": "workspace/attachments/<synthetic-import>-sample.py", "owned_workspace_verified": True,
            "scan_status": status, "success": success, "scanner_verified": correct_scanner,
            "finding_count": count, **fields,
            "offline": actual.get("offline") if type(actual.get("offline")) is bool else None,
            "truncated": actual.get("truncated") if type(actual.get("truncated")) is bool else None,
            "error_present": bool(data.get("error")),
            "result_sha256": hashlib.sha256(json.dumps(data, ensure_ascii=False, default=str, sort_keys=True).encode()).hexdigest(),
            "scope": "Owned synthetic sample.py only; no code, finding snippets/locations, raw errors or arbitrary scans"}


def install(destination):
    destination = owned_destination(destination)
    from core.brain import capability_agent
    from core import web_research
    original_run = capability_agent.CapabilityAgent._run
    original_bind = web_research.attach_observed_record_date_citations
    original_guard = web_research.validate_grounded_answer
    state = threading.local()

    def run(self, goal, *args, **kwargs):
        previous = getattr(state, "active", None)
        exact_request = kwargs.get("trusted_goal") if kwargs.get("trusted_goal") is not None else goal
        scope = next(((trial, kind) for trial in range(1, 4) for kind, make_goal in
                      (("research", public_goal), ("follow-up", follow_up_goal)) if exact_request == make_goal(trial)), None)
        state.active = {"trial": scope[0], "kind": scope[1], "pending": None, "sequence": 0} if scope else None
        try:
            return original_run(self, goal, *args, **kwargs)
        finally:
            state.active = previous

    def bind(content, evidence, request, path):
        bound = original_bind(content, evidence, request, path)
        active = getattr(state, "active", None)
        if active is not None:
            active["pending"] = None
            make_goal = public_goal if active["kind"] == "research" else follow_up_goal
            if (request == make_goal(active["trial"]) and path == f"trial-{active['trial']}-{active['kind']}.md"
                    and isinstance(content, str) and (active["kind"] == "follow-up" or CVE in content)
                    and isinstance(bound, str)):
                active["pending"] = (content, bound, path)
        return bound

    def guard(answer, evidence):
        errors = original_guard(answer, evidence)
        active = getattr(state, "active", None)
        pending = active.get("pending") if active is not None else None
        if pending is not None and isinstance(answer, str) and answer == pending[1]:
            active["pending"] = None
            active["sequence"] += 1
            try:
                raw, bound, path = pending
                # Only the explicit public-CVE draft is captured. The goal and
                # model/system prompts are deliberately not written anywhere.
                write_record(destination, {
                    "kind": "synthetic_structured_write_grounding", "path": path,
                    "draft_number": active["sequence"], "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "bound_sha256": hashlib.sha256(bound.encode()).hexdigest(),
                    "raw_chars": len(raw), "bound_chars": len(bound),
                    "redacted_raw_draft": raw[:24000], "redacted_bound_draft": bound[:24000],
                    "capture_truncated": len(raw) > 24000 or len(bound) > 24000,
                    "guard_errors": list(errors), "public_source_records": public_records(evidence),
                    "public_page_records": public_page_records(evidence) if active["kind"] == "follow-up" else [],
                    "scope": "Exact isolated synthetic public-CVE project write only; no arbitrary files, prompts or accounts"})
            except Exception:
                LOG.warning("Structured-write observation unavailable; actual grounding verdict preserved")
        return errors

    capability_agent.CapabilityAgent._run = run
    web_research.attach_observed_record_date_citations = bind
    web_research.validate_grounded_answer = guard

    from core.capability_router import CapabilityRouter
    original_execute = CapabilityRouter.execute
    runtime = Path(os.environ["WISE_RUNTIME_ROOT"]).resolve()
    def execute(self, id_or_name, *args, **kwargs):
        result = original_execute(self, id_or_name, *args, **kwargs)
        try:
            arguments = args[0] if args else kwargs.get("arguments", {})
            row = scanner_observation(id_or_name, arguments, result, runtime)
            if row is not None:
                write_record(destination.with_name("synthetic-bandit-observations.ndjson"), row)
        except Exception:
            LOG.warning("Synthetic scanner observation unavailable; actual tool result preserved")
        return result
    CapabilityRouter.execute = execute
