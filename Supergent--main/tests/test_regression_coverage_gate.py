"""An intentionally noninteractive test run is not a full release pass."""
import importlib.util
import sys
from pathlib import Path


def gate():
    directory = Path(__file__).resolve().parents[1] / "qa" / "acceptance"
    sys.path.insert(0, str(directory))
    try:
        spec = importlib.util.spec_from_file_location("coverage_quality_gate", directory / "quality_gate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(directory))


def report():
    return {"source_stamp": "actual", "source_changed_during_run": False, "summary": {"passed": 20}}


def test_matching_complete_regression_scope_can_pass():
    assert not gate().inspect_regression(report(), "actual")


def test_green_tests_with_excluded_native_gate_are_not_full_acceptance():
    data = report()
    data.update(excluded_scenarios=["native-application"], native_legacy_verdict="NOT_EVALUATED")
    errors = gate().inspect_regression(data, "actual")
    assert any("incomplete" in error for error in errors)
    assert any("not evaluated" in error for error in errors)


def test_deselected_tests_without_explicit_exclusion_metadata_still_fail():
    data = report()
    data["summary"]["deselected"] = 1
    assert gate().inspect_regression(data, "actual")


def test_stale_source_and_failures_never_pass():
    data = report()
    data["summary"]["failed"] = 1
    errors = gate().inspect_regression(data, "other")
    assert len(errors) == 2
