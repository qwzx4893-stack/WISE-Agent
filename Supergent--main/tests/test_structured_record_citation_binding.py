"""Actual rejected public-CVE draft regression, with strict negative bindings."""
import json

import pytest

from core.web_research import attach_observed_record_date_citations, validate_grounded_answer

CVE = "CVE-2021-44228"
CISA = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
FIRST = "https://api.first.org/data/v1/epss?cve=CVE-2021-44228&limit=5"
CISA_TIME = "2026-10-04T14:20:01.180639+00:00"
FIRST_TIME = "2026-10-04T14:20:04.080817+00:00"

# Exact first rejected draft observed in the isolated real UI diagnostic
# 20261004T141934Z; it is public synthetic evidence, not a private user file.
RAW = f"""# CVE-2021-44228 Analysis

## CISA Known Exploited Vulnerabilities (KEV)

- **Date Added**: 2021-12-10
- **Vendor Project**: Apache
- **Product**: Log4j2
- **Vulnerability Name**: Apache Log4j2 Remote Code Execution Vulnerability
- **Short Description**: Apache Log4j2 contains a vulnerability where JNDI features do not protect against attacker-controlled JNDI-related endpoints, allowing for remote code execution.
- **Required Action**: For all affected software assets for which updates exist, the only acceptable remediation actions are: 1) Apply updates; OR 2) remove affected assets from agency networks.
- **Due Date**: 2021-12-24
- **Known Ransomware Campaign Use**: Known

## FIRST Exploit Prediction Scoring System (EPSS)

- **EPSS Score**: 0.999990000
- **Percentile**: 1.000000000
- **Score Date**: 2026-10-03

## Source URLs and Retrieval Dates

- **CISA KEV Source**: {CISA} (Retrieved: {CISA_TIME})
- **FIRST EPSS Source**: {FIRST} (Retrieved: {FIRST_TIME})

## Notes

- The EPSS score represents the probability that the CVE will be exploited in the near term.
- The KEV listing indicates that this vulnerability has been actively exploited in ransomware campaigns.
"""


def actual_evidence():
    data = []
    for resource, url, timestamp, record in (
            ("cisa-known-exploited-vulnerabilities", CISA, CISA_TIME,
             {"cveID": CVE, "dateAdded": "2021-12-10", "dueDate": "2021-12-24"}),
            ("first-epss", FIRST, FIRST_TIME,
             {"cve": CVE, "epss": "0.999990000", "percentile": "1.000000000", "date": "2026-10-03"})):
        data.append({"url": url, "retrieved_at": timestamp, "evidence_kind": "source_api_data",
                     "page_text": json.dumps({"resource_id": resource, "records": [record],
                                              "provenance": {"retrieved_at": timestamp}})})
    return data


@pytest.mark.parametrize("corrected", [False, True])
def test_actual_raw_and_partially_corrected_drafts_bind_only_exact_record_dates(corrected):
    raw = RAW
    if corrected:
        # Exact second proposed write: model fixed two citations but left the
        # actual CISA dueDate uncited, so the unchanged guard still rejected it.
        raw = raw.replace("**Date Added**: 2021-12-10", f"**Date Added**: 2021-12-10 (Source: {CISA}, Retrieved: {CISA_TIME})")
        raw = raw.replace("**Score Date**: 2026-10-03", f"**Score Date**: 2026-10-03 (Source: {FIRST}, Retrieved: {FIRST_TIME})")
    evidence = actual_evidence()
    raw_errors = validate_grounded_answer(raw, evidence)
    citation_error = "An exact technical claim has no adjacent retrieved-source citation"
    assert citation_error in raw_errors
    bound = attach_observed_record_date_citations(raw, evidence, "Read analysis.json and research its first CVE", "report.md")
    assert f"**Due Date**: 2021-12-24 [Source](<{CISA}>)" in bound
    if not corrected:
        assert f"**Date Added**: 2021-12-10 [Source](<{CISA}>)" in bound
        assert f"**Score Date**: 2026-10-03 [Source](<{FIRST}>)" in bound
    # Binding repairs placement, not the historical draft's unqualified EPSS
    # interpretation. The newer semantic guard must not be bypassed to make
    # a date-binding test pass. Preserve the original actual RAW above.
    bound_errors = validate_grounded_answer(bound, evidence)
    assert citation_error not in bound_errors
    assert set(bound_errors) == set(raw_errors) - {citation_error}
    assert any("EPSS" in error for error in bound_errors)
    assert attach_observed_record_date_citations(bound, evidence, "Research " + CVE, "report.md") == bound


@pytest.mark.parametrize("body,goal", [
    ("## CISA KEV\n\n- **Date Added**: 2021-12-10", "Research first CVE"),
    ("## CISA KEV\n\n- **Date Added**: 2021-12-11", CVE),
    ("## CISA KEV\n\n- **Date Added**: 2021-12-24", CVE),
    ("## CISA KEV\n\n- **Due Date**: 2021-12-10", CVE),
    ("## CISA KEV\n\n- **Due Date**: 2021-12-24", CVE + " and CVE-2021-45046"),
    ("## CVE-2021-45046 CISA KEV\n\n- **Due Date**: 2021-12-24", CVE),
    ("## FIRST EPSS\n\n- **Date Added**: 2021-12-10", CVE),
    ("## FIRST EPSS\n\n- **Due Date**: 2021-12-24", CVE),
    ("## FIRST EPSS\n\n- dueDate: 2021-12-24", CVE),
    ("## Project plan\n\n- **Due Date**: 2021-12-24", CVE),
    ("## CISA KEV\n\n## Project plan\n\n- **Due Date**: 2021-12-24", CVE),
    ("## CISA KEV\n\n- **Project Due Date**: 2021-12-24", CVE),
    ("## CISA KEV\n\n- **Launch Date**: 2021-12-24", CVE),
    ("## CISA KEV\n\n- **Unknown Field**: 2021-12-24", CVE),
    ("## CISA and FIRST\n\n- **Due Date**: 2021-12-24", CVE),
    ("## CISA KEV\n\nThe date added to our own project was 2021-12-10.", CVE),
])
def test_new_generic_labels_never_launder_ambiguous_dates_or_other_contexts(body, goal):
    assert attach_observed_record_date_citations(body, actual_evidence(), goal, "report.md") == body


def test_missing_actual_dueDate_or_unknown_adapter_does_not_create_citation():
    body = "## CISA KEV\n\n- **Due Date**: 2021-12-24"
    for resource in ("cisa-known-exploited-vulnerabilities", "unknown"):
        payload = {"resource_id": resource, "records": [{"cveID": CVE, "dateAdded": "2021-12-24"}]}
        evidence = [{"url": CISA, "evidence_kind": "source_api_data", "page_text": json.dumps(payload)}]
        assert attach_observed_record_date_citations(body, evidence, CVE, "report.md") == body


def test_canonical_dueDate_uses_record_value_not_renamed_dateAdded():
    body = "dueDate: 2021-12-24"
    bound = attach_observed_record_date_citations(body, actual_evidence(), CVE, "report.md")
    assert bound == body + f" [Source](<{CISA}>)"
    assert validate_grounded_answer(bound, actual_evidence()) == []
