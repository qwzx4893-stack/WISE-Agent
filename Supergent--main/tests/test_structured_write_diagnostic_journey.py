"""Offline failure-recording and field-presence grading; no live dispatch."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "qa/acceptance"))
    return importlib.import_module("qa.acceptance.structured_write_diagnostic_journey")


def test_failed_case_persists_recursive_json_log_and_original_failure(tmp_path, runner):
    class Page:
        def __init__(self):
            self.listeners = {}
        def on(self, name, callback):
            self.listeners[name] = callback
        def remove_listener(self, name, callback):
            assert self.listeners.pop(name) is callback
        def screenshot(self, **kwargs):
            # Test-fixture acknowledgement only, not actual UI acceptance.
            assert kwargs["full_page"]
    page = Page()
    evidence = runner.NarrowEvidence(tmp_path, SimpleNamespace())
    def failed():
        page.listeners["console"](SimpleNamespace(type="error", text="Synthetic failure sk-or-v1-example123"))
        raise AssertionError("Original synthetic artifact missing")
    evidence.case("synthetic-failure", None, page, failed)
    assert evidence.cases[0]["status"] == "FAIL"
    assert "Original synthetic artifact missing" in evidence.cases[0]["stack_trace"]
    assert "DISABLED_CONTINUOUS_CAPTURE" in evidence.cases[0]["trace_profile"]
    log = json.loads((tmp_path / "synthetic-failure.log.json").read_text(encoding="utf-8"))
    assert log["page_errors"] == [] and len(log["console"]) == 1
    assert "sk-or-v1-example123" not in log["console"][0]["text"]
    assert not page.listeners and not list(tmp_path.glob("*.trace.zip"))


def test_success_case_persists_dict_log_without_typeerror(tmp_path, runner):
    page = SimpleNamespace(on=lambda *args: None, remove_listener=lambda *args: None, screenshot=lambda **kwargs: None)
    evidence = runner.NarrowEvidence(tmp_path, SimpleNamespace())
    evidence.case("synthetic-pass", None, page, lambda: None)
    assert evidence.cases[0]["status"] == "PASS"
    assert json.loads((tmp_path / "synthetic-pass.log.json").read_text(encoding="utf-8"))["console"] == []
