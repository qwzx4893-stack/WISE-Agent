"""Synthetic runner evidence checks; never dispatch the selected suites recursively."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from qa.acceptance import security_regression as runner


def junit(*, status="", declared="1", classname="tests.test_fixture", name="test_safe[PRIVATE_INPUT]"):
    result = f'<{status} message="PRIVATE_MESSAGE">PRIVATE_TEXT</{status}>' if status else ""
    counters = {"failures": "0", "errors": "0", "skipped": "0"}
    if status:
        counters[{"failure": "failures", "error": "errors", "skipped": "skipped"}[status]] = "1"
    return (f'<testsuites><testsuite tests="{declared}" failures="{counters["failures"]}" '
            f'errors="{counters["errors"]}" skipped="{counters["skipped"]}">'
            '<properties><property name="api_key" value="PRIVATE_SECRET"/></properties>'
            f'<testcase classname="{classname}" name="{name}">{result}'
            '<system-out>PRIVATE_STDOUT</system-out><system-err>PRIVATE_STDERR</system-err>'
            '</testcase></testsuite></testsuites>')


@pytest.mark.parametrize("status", ["", "failure", "error", "skipped"])
def test_junit_sanitizer_drops_diagnostics_inputs_and_properties(tmp_path, status):
    raw, clean = tmp_path / "raw.xml", tmp_path / "clean.xml"
    raw.write_text(junit(status=status))
    counts, missing = runner.sanitized_junit(raw, clean, ["test_fixture"])
    document = clean.read_text()
    assert "PRIVATE" not in document
    assert counts["tests"] == 1 and missing == []
    assert ET.fromstring(document).find("testsuite/testcase").attrib == {
        "classname": "tests.test_fixture", "name": "test_safe"}


@pytest.mark.parametrize("content", ["<broken", "<other/>", "<testsuites/>",
                                      junit(declared="2"), junit(declared="-1"),
                                      '<!DOCTYPE x [<!ENTITY leak "PRIVATE">]><testsuites/>'])
def test_bad_or_empty_junit_is_rejected(tmp_path, content):
    raw = tmp_path / "raw.xml"
    raw.write_text(content)
    with pytest.raises((ValueError, ET.ParseError)):
        runner.sanitized_junit(raw, tmp_path / "clean.xml", ["test_fixture"])


@pytest.mark.parametrize("returncode,counts,initial,final,missing,error", [
    (1, {"tests": 1}, "same", "same", (), None),
    (0, {"tests": 0}, "same", "same", (), None),
    (0, {"tests": 1, "skipped": 1}, "same", "same", (), None),
    (0, {"tests": 1, "failures": 1}, "same", "same", (), None),
    (0, {"tests": 1, "errors": 1}, "same", "same", (), None),
    (0, {"tests": 1}, "before", "after", (), None),
    (0, {"tests": 1}, None, "same", (), None),
    (0, {"tests": 1}, "same", "same", ("test_missing",), None),
    (0, {"tests": 1}, "same", "same", (), "evidence missing"),
])
def test_incomplete_execution_cannot_pass(returncode, counts, initial, final, missing, error):
    result = runner.evaluate(returncode, counts, initial, final, missing=missing, error=error)
    assert result["regression_verdict"] == "FAIL"
    assert result["release_verdict"] == "NOT_READY"


def test_success_is_only_regression_pass():
    result = runner.evaluate(0, {"tests": 1}, "same", "same")
    assert result["regression_verdict"] == "PASS"
    assert result["release_verdict"] == "NOT_READY"
    assert result["readiness_blockers"]
    assert result["absolute_zero_unknown_vulnerabilities_claim"] is False


def test_fixed_runner_includes_tls_and_file_trust_contracts():
    assert "tests/test_skill_tls_security.py" in runner.SUITES
    assert "tests/test_file_result_trust_security.py" in runner.SUITES
    assert "tests/test_mcp_outbound_authority_security.py" in runner.SUITES
    assert "tests/test_mcp_owner_retry.py" in runner.SUITES
    assert "tests/test_mcp_owner_confirmation_api.py" in runner.SUITES
    assert "tests/test_voice_reference_security.py" in runner.SUITES
    assert "tests/test_telemetry_render_security.py" in runner.SUITES
    assert "tests/test_publication_redaction.py" in runner.SUITES
    assert "tests/test_source_regression_selection.py" in runner.SUITES
    assert len(runner.SUITES) == len(set(runner.SUITES))


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("vendor_present", [False, True])
def test_omission_policy_records_exact_optional_and_platform_nodes(tmp_path, windows, vendor_present):
    if vendor_present:
        vendor = tmp_path / "tools/index-tts/indextts/utils/checkpoint.py"
        vendor.parent.mkdir(parents=True)
        vendor.write_text("# synthetic optional source")
        inference = tmp_path / "tools/index-tts/indextts/infer_v2_5.py"
        inference.write_text("# synthetic optional source")
    omissions = runner.selection_policy(tmp_path, windows=windows)
    assert [row["node"] for row in omissions["platform"]] == (
        [] if windows else list(runner.PLATFORM_CONTROLS))
    assert [row["node"] for row in omissions["optional_vendor"]] == (
        [] if vendor_present else list(runner.OPTIONAL_VENDOR_CONTROLS + runner.OPTIONAL_REFERENCE_VENDOR_CONTROLS))
    assert all(row["reason"] for rows in omissions.values() for row in rows)
    assert len(runner.OPTIONAL_VENDOR_CONTROLS) == 4
    assert len(runner.OPTIONAL_REFERENCE_VENDOR_CONTROLS) == 3


@pytest.mark.parametrize("checkpoint_present", [False, True])
def test_optional_vendor_sources_are_checked_independently(tmp_path, checkpoint_present):
    relative = ("tools/index-tts/indextts/utils/checkpoint.py" if checkpoint_present
                else "tools/index-tts/indextts/infer_v2_5.py")
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_text("# synthetic optional source")
    expected = (runner.OPTIONAL_REFERENCE_VENDOR_CONTROLS if checkpoint_present
                else runner.OPTIONAL_VENDOR_CONTROLS)
    omitted = runner.selection_policy(tmp_path, windows=True)["optional_vendor"]
    assert [row["node"] for row in omitted] == list(expected)


@pytest.fixture
def fake_execution(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(runner, "APP", app)
    monkeypatch.setattr(runner, "SUITES", ("tests/test_fixture.py",))
    stamp = "a" * 64
    monkeypatch.setattr(runner, "inventory", lambda: {
        "sha256": stamp, "files": [{"path": "core/fixture.py", "sha256": stamp}]})
    executions = []

    def execute(command, **kwargs):
        executions.append((command, kwargs))
        raw = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        raw.write_text(junit())
        kwargs["stdout"].write(b"PRIVATE_RAW_OUTPUT")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", execute)
    return app / "qa-results" / "owned", executions


def test_runner_captures_source_and_only_sanitized_evidence(fake_execution, monkeypatch):
    output, executions = fake_execution
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE_OWNER_KEY")
    report = runner.run(output)
    assert report["regression_verdict"] == "PASS"
    assert report["release_verdict"] == "NOT_READY"
    assert report["source_start_sha256"] == report["source_end_sha256"]
    assert len(executions) == 1
    assert "OPENAI_API_KEY" not in executions[0][1]["env"]
    assert executions[0][1]["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    omitted = report["explicit_omissions"]["optional_vendor"]
    assert [row["node"] for row in omitted] == list(runner.OPTIONAL_VENDOR_CONTROLS + runner.OPTIONAL_REFERENCE_VENDOR_CONTROLS)
    assert all("--deselect=" + row["node"] in executions[0][0] for row in omitted)
    assert not list(output.glob("owned-regression-*"))
    for path in output.iterdir():
        assert "PRIVATE" not in path.read_text()
    for name, stamp in report["evidence_sha256"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == stamp


@pytest.mark.parametrize("kind", ["timeout", "oserror", "malformed", "missing", "skip", "source_change"])
def test_runner_faults_cannot_become_success(fake_execution, monkeypatch, kind):
    output, executions = fake_execution
    execute = runner.subprocess.run
    if kind == "source_change":
        snapshots = iter(["a" * 64, "b" * 64])
        monkeypatch.setattr(runner, "inventory", lambda: {
            "sha256": next(snapshots), "files": [{"path": "core/fixture.py"}]})

    def fail(command, **kwargs):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output="PRIVATE_TIMEOUT")
        if kind == "oserror":
            raise OSError("PRIVATE_ERROR")
        result = execute(command, **kwargs)
        raw = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        if kind == "malformed":
            raw.write_text("PRIVATE_MALFORMED")
        elif kind == "missing":
            raw.unlink()
        elif kind == "skip":
            raw.write_text(junit(status="skipped"))
        return result

    monkeypatch.setattr(runner.subprocess, "run", fail)
    report = runner.run(output)
    assert report["regression_verdict"] == "FAIL"
    assert report["release_verdict"] == "NOT_READY"
    assert not list(output.glob("owned-regression-*"))
    for path in output.iterdir():
        assert "PRIVATE" not in path.read_text()


def test_existing_or_unowned_output_is_rejected(fake_execution):
    output, executions = fake_execution
    with pytest.raises(ValueError):
        runner.run(output.parents[1] / "outside")
    output.mkdir(parents=True)
    with pytest.raises(ValueError):
        runner.run(output)
    assert executions == []


@pytest.mark.parametrize("phase", ["start", "end"])
def test_inventory_failure_never_awards_success(fake_execution, monkeypatch, phase):
    output, executions = fake_execution
    snapshot = runner.inventory
    calls = []

    def unavailable():
        calls.append(1)
        if (phase == "start" and len(calls) == 1) or (phase == "end" and len(calls) == 2):
            raise OSError("PRIVATE_INVENTORY_DETAIL")
        return snapshot()

    monkeypatch.setattr(runner, "inventory", unavailable)
    result = runner.run(output)
    assert result["regression_verdict"] == "FAIL"
    assert result["release_verdict"] == "NOT_READY"
    assert "PRIVATE" not in (output / "report.json").read_text()
    if phase == "start":
        assert executions == []


def test_python_network_guard_rejects_remote_and_retains_local_plumbing(monkeypatch):
    monkeypatch.setenv("WISE_SECURITY_REGRESSION_OFFLINE", "1")
    # Restore the session's socket guard automatically; no real socket is used.
    calls = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: calls.append(addr))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, addr: calls.append(addr) or 0)
    monkeypatch.setattr(socket.socket, "sendto", lambda self, *args: calls.append(args[-1]))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: calls.append(args[0]))
    runner.pytest_sessionstart(None)
    instance = object()
    for method in (socket.socket.connect, socket.socket.connect_ex):
        with pytest.raises(PermissionError):
            method(instance, ("203.0.113.1", 443))
        method(instance, ("127.0.0.1", 1))
    with pytest.raises(PermissionError):
        socket.socket.sendto(instance, b"synthetic", ("203.0.113.1", 443))
    with pytest.raises(PermissionError):
        socket.getaddrinfo("example.invalid", 443)
    assert calls == [("127.0.0.1", 1), ("127.0.0.1", 1)]
