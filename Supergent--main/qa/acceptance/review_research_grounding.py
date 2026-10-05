"""Independently check a concrete unsupported claim from the final project.

This is a narrow claim-level grader, NOT a semantic certification of all claims.
It never treats URLs, tool calls or absent known mistakes as a complete PASS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import httpx
from source_stamp import ROOT, source_stamp

sys.path.insert(0, str(ROOT))
from core.public_http import PublicTransport

URLS = ("https://logging.apache.org/security.html",
        "https://github.com/cisagov/log4j-affected-db/blob/develop/README.md")
PROPERTY = "log4j2.formatMsgNoLookups"


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.excluded, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}: self.excluded += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}: self.excluded = max(0, self.excluded - 1)

    def handle_data(self, value):
        if not self.excluded and value.strip(): self.parts.append(value.strip())


def full_official_page(url, destination):
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False, transport=PublicTransport()) as client:
        with client.stream("GET", url, headers={"User-Agent": "WISE acceptance source review/1.0"}) as response:
            response.raise_for_status()
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 1_000_000: raise ValueError("Independent source review exceeds its byte budget")
    parser = VisibleText()
    parser.feed(payload.decode("utf-8", errors="replace"))
    text = "\n".join(parser.parts)
    destination.write_text(text, encoding="utf-8")
    return {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(payload).hexdigest(), "snapshot": str(destination),
            "full_visible_text_chars": len(text), "property_present": PROPERTY.lower() in text.lower()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project_path, workspace = args.project.resolve(), args.workspace.resolve()
    if (ROOT / "qa-results").resolve() not in project_path.parents:
        raise ValueError("An owned QA project report is required")
    if (ROOT / ".tooling").resolve() not in workspace.parents or workspace.name != "workspace":
        raise ValueError("An owned isolated test workspace is required")
    project = json.loads(project_path.read_text(encoding="utf-8"))
    if project["source_stamp"] != source_stamp() or project["source_changed_during_run"]:
        raise ValueError("The project report is not bound to the current build")
    if str(workspace.parent) not in (project_path.parent / "backend.log").read_text(encoding="utf-8"):
        raise ValueError("Workspace is not attributable to the selected test backend")
    destination = args.output.resolve()
    if (ROOT / "qa-results").resolve() not in destination.parents:
        raise ValueError("Use a new owned QA evidence directory")
    destination.mkdir(parents=True, exist_ok=False)
    observations = [full_official_page(url, destination / f"grounding-source-{i+1}.txt") for i, url in enumerate(URLS)]
    checked, findings = [], []
    for trial in (1, 2):
        artifact = workspace / f"trial-{trial}-follow-up.md"
        text = artifact.read_text(encoding="utf-8")
        present = PROPERTY.lower() in text.lower()
        supported = any(row["property_present"] for row in observations)
        checked.append({"artifact": str(artifact), "claim_identifier": PROPERTY,
                        "artifact_asserts_identifier": present, "cited_sources_contain_identifier": supported,
                        "verdict": "UNSUPPORTED_ATTRIBUTION" if present and not supported else "UNVERIFIED",
                        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})
        if present and not supported:
            case = next(row for row in project["cases"] if row["id"] == f"trial-{trial}-follow-up-general-web-research")
            findings.append({"id": f"research-attribution-trial-{trial}", "status": "OPEN", "blocking": True,
                "summary": "The generated artifact attributes a mitigation property to current cited official guidance, but the independently fetched full pages do not contain that property.",
                "scope": "Unsupported attribution; not proof that the property never existed historically",
                "steps": case["steps"] + ["Compare the claimed property with independently saved full official page text"],
                "artifact": str(artifact), "screenshot": case["screenshot"], "trace": case["trace"], "log": case["log"]})
    findings.append({"id": "soak-resource-scope", "status": "UNVERIFIED", "blocking": False,
        "summary": "Check the selected project's declared measurement scope. Backend-tree samples alone cannot certify UI/GPU resource use or day-long reliability.",
        "scope": project.get("resource_measurement_scope", "UNDECLARED")})
    result = {"schema": "wise.semantic-review.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_stamp": source_stamp(), "project_report": str(project_path),
        "research_verdict": "FAIL" if any(row["blocking"] for row in findings) else "UNVERIFIED",
        "checked_claims": checked, "source_observations": observations, "findings": findings,
        "limitations": ["A narrow independent attribution check; not a review of every research claim",
                        "Current generic safety cannot be inferred from historical vulnerability fixes",
                        "No application fix or model retraining is implied by this review"]}
    (destination / "semantic-review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"research_verdict": result["research_verdict"], "findings": [row["id"] for row in findings]}))


if __name__ == "__main__": main()
