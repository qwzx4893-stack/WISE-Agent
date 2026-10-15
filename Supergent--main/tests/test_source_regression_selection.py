"""Public-source CI never invents coverage for omitted optional voice sources."""
import sys

import pytest

from qa.acceptance import source_regression as runner


@pytest.mark.parametrize("vendor_present", [False, True])
@pytest.mark.parametrize("browser_present", [False, True])
def test_source_ci_records_and_deselects_only_declared_controls(tmp_path, monkeypatch, vendor_present, browser_present):
    monkeypatch.setattr(runner, "_brave_available", lambda: browser_present)
    if vendor_present:
        for name in ("utils/checkpoint.py", "infer_v2_5.py"):
            path = tmp_path / "tools/index-tts/indextts" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic source, not a model")
    args, omissions = runner.command(tmp_path)
    assert args[:4] == [sys.executable, "-m", "pytest", "tests/"]
    omitted = [row for rows in omissions.values() for row in rows]
    assert [arg for arg in args if arg.startswith("--deselect=")] == [
        "--deselect=" + row["node"] for row in omitted]
    assert bool(omissions["optional_vendor"]) is not vendor_present
    assert [row["node"] for row in omissions["native"]] == list(runner.NATIVE_EXCLUSIONS)
    assert [row["node"] for row in omissions["browser"]] == ([] if browser_present else list(runner.BROWSER_CONTROLS))
    assert all(row["reason"] for row in omitted)


def test_browser_discovery_checks_paths_without_launching_or_replacing_browser(tmp_path, monkeypatch):
    from core.browser import browser_session
    path = tmp_path / "synthetic-browser.fixture"
    monkeypatch.setattr(browser_session, "_BRAVE_PATHS", (path,))
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner._brave_available() is False
    path.write_bytes(b"not an executable; discovery only")
    assert runner._brave_available() is True


def test_source_ci_preserves_pytest_failure(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(runner.subprocess, "call", lambda args, **kw: calls.append((args, kw)) or 1)
    assert runner.main() == 1
    assert calls[0][1]["cwd"] == runner.APP
    assert '"explicit_omissions"' in capsys.readouterr().out
