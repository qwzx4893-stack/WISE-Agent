import importlib.util
import json
from pathlib import Path

import pytest


def reviewer(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "qa/acceptance"))
    spec = importlib.util.spec_from_file_location("qa_project_review", root / "qa/acceptance/review_project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "source_stamp", lambda: "checker-new-build")
    folder = tmp_path / "qa-results/owned"
    folder.mkdir(parents=True)
    runtime = tmp_path / ".tooling/owned-runtime"
    (runtime / "workspace").mkdir(parents=True)
    report = folder / "report.json"
    report.write_text(json.dumps({"runtime": str(runtime), "trials": 1, "source_stamp": "tested-old-build",
                                  "source_changed_during_run": True}), encoding="utf-8")
    (folder / "actual-tool-observations.ndjson").write_text("", encoding="utf-8")
    (runtime / "workspace/trial-1-analysis.json").write_text('{"row_count":3,"cve":"CVE-2021-44228"}')
    (runtime / "workspace/trial-1-research.md").write_text("2021-12-10 2026-10-02 0.999990000", encoding="utf-8")
    (folder / "trial-1-independent-reference.json").write_text(json.dumps({"kev": {"dateAdded": "2021-12-10"},
                       "epss": {"date": "2026-10-02", "epss": "0.999990000"}}))
    return module, folder, runtime, report


def test_missing_required_file_is_failure_report_not_unhandled_exception(monkeypatch, tmp_path):
    module, folder, runtime, report = reviewer(monkeypatch, tmp_path)
    output = folder / "new-review.json"
    result = module.review(report, output)
    assert result["automated_fact_verdict"] == "FAIL" and len(result["checked_claims"]) == 5
    assert result["findings"][0]["blocking"] and "follow-up" in result["findings"][0]["id"]
    assert result["source_stamp"] == "tested-old-build" and result["source_changed_during_run"] is True
    assert result["reviewer_application_stamp"] == "checker-new-build"
    assert not (runtime / "workspace/trial-1-follow-up.md").exists()
    with pytest.raises(FileExistsError): module.review(report, output)


@pytest.mark.parametrize("bad_json", ["{broken", "[]"])
def test_corrupt_artifact_keeps_remaining_independent_checks(monkeypatch, tmp_path, bad_json):
    module, folder, runtime, report = reviewer(monkeypatch, tmp_path)
    (runtime / "workspace/trial-1-analysis.json").write_text(bad_json)
    result = module.review(report, folder / "new-review.json")
    assert result["automated_fact_verdict"] == "FAIL" and len(result["checked_claims"]) == 3
    assert len(result["findings"]) == 2
