"""Public-source CI never invents coverage for omitted optional voice sources."""
import sys

import pytest

from qa.acceptance import source_regression as runner


@pytest.mark.parametrize("vendor_present", [False, True])
def test_source_ci_records_and_deselects_only_declared_controls(tmp_path, vendor_present):
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
    assert all(row["reason"] for row in omitted)


def test_source_ci_preserves_pytest_failure(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(runner.subprocess, "call", lambda args, **kw: calls.append((args, kw)) or 1)
    assert runner.main() == 1
    assert calls[0][1]["cwd"] == runner.APP
    assert '"explicit_omissions"' in capsys.readouterr().out
