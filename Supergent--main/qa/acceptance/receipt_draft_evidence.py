"""Opt-in capture ONLY of the synthetic public receipt report's write draft."""
import hashlib
import json
import os
from pathlib import Path

from qa.acceptance.tool_evidence import ROOT, redact, _lock


def install(destination):
    destination = Path(destination).resolve()
    runtime = Path(os.environ.get("WISE_RUNTIME_ROOT", "")).resolve()
    if (ROOT / "qa-results" not in destination.parents or destination.suffix != ".ndjson"
            or runtime.parent != (ROOT / ".tooling").resolve() or not runtime.name.startswith("receipt-runtime-")):
        raise ValueError("Receipt-draft diagnostics require the explicitly owned synthetic runtime")
    from core.brain import capability_agent
    original = capability_agent.coalesce_model_call
    def observed(key, callback):
        response = original(key, callback)
        action = response.parsed_json
        if not isinstance(action, dict):
            try: action = json.loads(response.text)
            except (ValueError, TypeError): return response
        if not isinstance(action, dict): return response
        args = action.get("arguments")
        answer = action.get("answer")
        if isinstance(answer, str) and ("receipts.md" in answer or "CVE-2021-44228" in answer):
            record = {"kind": "final_answer_draft", "sha256": hashlib.sha256(answer.encode()).hexdigest(),
                      "chars": len(answer), "redacted_draft": redact(answer[:24000]),
                      "scope": "Explicit public-CVE synthetic report only; not arbitrary user prompts"}
            with _lock, destination.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if action.get("tool") not in {"native.write_file", "write_file"} or not isinstance(args, dict):
            return response
        content = args.get("content")
        if args.get("path") != "receipts.md" or not isinstance(content, str) or "CVE-2021-44228" not in content:
            return response
        record = {"path": "receipts.md", "sha256": hashlib.sha256(content.encode()).hexdigest(),
                  "chars": len(content), "redacted_draft": redact(content[:24000]),
                  "scope": "Explicit public-CVE synthetic report only; not arbitrary user files or model prompts"}
        with _lock, destination.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return response
    capability_agent.coalesce_model_call = observed
