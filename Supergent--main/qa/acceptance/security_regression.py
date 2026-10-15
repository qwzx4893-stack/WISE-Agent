"""Offline synthetic regressions; passing them never certifies production readiness.

Raw pytest output/failure payloads are discarded. Only validated test identities,
counts and statuses are retained. The Python network guard permits loopback
event-loop plumbing; it is not an OS sandbox or an egress certification.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from qa.acceptance.security_baseline import APP, inventory

SUITES = (
    "tests/test_ingress_security_contract.py",
    "tests/test_oauth_security.py",
    "tests/test_mcp_inbound_authority.py",
    "tests/test_secret_storage_security.py",
    "tests/test_security_policy_fail_closed.py",
    "tests/test_preview_security_contract.py",
    "tests/test_archive_extraction_verification.py",
    "tests/test_uac_security_contract.py",
    "tests/test_voice_load_security.py",
    "tests/test_security_audit_gate.py",
    "tests/test_native_bridge_security.py",
    "tests/test_native_runtime_window_ownership.py",
    "tests/test_security_boundary_properties.py",
    "tests/test_skill_tls_security.py",
    "tests/test_file_result_trust_security.py",
    "tests/test_mcp_outbound_authority_security.py",
    "tests/test_mcp_owner_retry.py",
    "tests/test_mcp_owner_confirmation_api.py",
    "tests/test_voice_reference_security.py",
    "tests/test_telemetry_render_security.py",
    "tests/test_publication_redaction.py",
    "tests/test_source_regression_selection.py",
    "tests/test_structured_write_evidence.py",
    "tests/test_security_regression_evidence.py",
)
PLATFORM_CONTROLS = (
    "tests/test_secret_storage_security.py::test_windows_junction_parent_rejected",
    "tests/test_security_policy_fail_closed.py::test_healthy_policy_keeps_confirmed_system_modification",
)
OPTIONAL_VENDOR_CONTROLS = (
    "tests/test_voice_load_security.py::test_checkpoint_restricted_load_preserves_state_dict_semantics[False]",
    "tests/test_voice_load_security.py::test_checkpoint_restricted_load_preserves_state_dict_semantics[True]",
    "tests/test_voice_load_security.py::test_checkpoint_preserves_optional_yaml_metadata",
    "tests/test_voice_load_security.py::test_checkpoint_rejection_propagates_without_unrestricted_retry",
)
OPTIONAL_REFERENCE_VENDOR_CONTROLS = (
    "tests/test_voice_reference_security.py::test_actual_worker_model_installs_finite_reference_decode",
    "tests/test_voice_reference_security.py::test_full_actual_job_validates_then_uses_guard_without_changing_inference_options",
    "tests/test_voice_reference_security.py::test_unsupported_model_contract_cannot_enter_cached_model_state",
)
SAFE_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LOCALAPPDATA",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "USERPROFILE",
            "APPDATA", "TEMP", "TMP", "LANG", "LC_ALL", "HOME"}
READINESS_LIMITS = ["No isolated native desktop or official UAC acceptance",
                   "Native bridge fixtures use library dispatch and fake windows, not Qt/GTK or real renderers",
                   "Voice loader fixtures use mocked Torch; optional vendor fixtures omitted explicitly if checkout absent",
                   "No production connector/private-data egress acceptance",
                   "No clean-install/update/rollback or native soak evidence",
                   "No live model or broad prompt-injection certification"]


def selection_policy(app, *, windows=None):
    """Record every deliberate omission; never turn missing vendor coverage into SKIP/PASS."""
    if windows is None:
        windows = os.name == "nt"
    platform = [] if windows else [
        {"node": node, "reason": "Windows-specific policy or NTFS fixture; this platform is not Windows"}
        for node in PLATFORM_CONTROLS]
    optional = []
    for relative, nodes in (
        ("tools/index-tts/indextts/utils/checkpoint.py", OPTIONAL_VENDOR_CONTROLS),
        ("tools/index-tts/indextts/infer_v2_5.py", OPTIONAL_REFERENCE_VENDOR_CONTROLS),
    ):
        if not (Path(app) / relative).is_file():
            optional.extend({"node": node,
                             "reason": "Optional ignored vendor source absent: " + relative + "; no clone or download attempted"}
                            for node in nodes)
    return {"platform": platform, "optional_vendor": optional}


def _local_address(address):
    if not isinstance(address, tuple):
        return True  # Local AF_UNIX/pipe address, not an IP destination.
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def pytest_sessionstart(session):
    """Activated only by this runner's explicit -p argument and environment."""
    if os.environ.get("WISE_SECURITY_REGRESSION_OFFLINE") != "1":
        return
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_sendto, original_resolve = socket.socket.sendto, socket.getaddrinfo

    def check(address):
        if not _local_address(address):
            raise PermissionError("Offline security regressions prohibit remote networking")

    def connect(instance, address):
        check(address)
        return original_connect(instance, address)

    def connect_ex(instance, address):
        check(address)
        return original_connect_ex(instance, address)

    def sendto(instance, *args):
        check(args[-1])
        return original_sendto(instance, *args)

    def resolve(host, *args, **kwargs):
        if host is not None:
            check((host, 0))
        return original_resolve(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = resolve


def sanitized_junit(raw, destination, expected):
    """Rebuild bounded JUnit without messages, properties, stdout or input values."""
    data = raw.read_bytes()
    if len(data) > 8 * 1024 * 1024 or b"<!DOCTYPE" in data.upper():
        raise ValueError("Invalid JUnit document")
    root = ET.fromstring(data)
    if root.tag not in {"testsuites", "testsuite"}:
        raise ValueError("Invalid JUnit root")
    suites = list(root.iter("testsuite"))
    counts = dict(tests=0, failures=0, errors=0, skipped=0)
    observed = set()
    output = ET.Element("testsuites")
    clean = ET.SubElement(output, "testsuite", name="focused-security-regressions")
    if not suites:
        raise ValueError("Empty JUnit document")
    for suite in suites:
        cases = list(suite.findall("testcase"))
        actual = dict(tests=len(cases), failures=0, errors=0, skipped=0)
        for case in cases:
            classname = case.get("classname", "")
            name = case.get("name", "").split("[", 1)[0]
            if not re.fullmatch(r"(?:tests\.)?test_[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", classname):
                classname = "redacted"
            if not re.fullmatch(r"test_[A-Za-z0-9_]+", name):
                name = "redacted"
            observed.update(set(classname.split(".")) & set(expected))
            item = ET.SubElement(clean, "testcase", classname=classname, name=name)
            statuses = [kind for kind in ("failure", "error", "skipped") if case.find(kind) is not None]
            if len(statuses) > 1:
                raise ValueError("Ambiguous testcase status")
            if statuses:
                kind = statuses[0]
                actual[{"failure": "failures", "error": "errors", "skipped": "skipped"}[kind]] += 1
                ET.SubElement(item, kind, message="Details omitted from sanitized evidence")
        declared = {key: int(suite.attrib[key]) for key in counts}
        if declared != actual or any(value < 0 for value in declared.values()):
            raise ValueError("Inconsistent JUnit counts")
        for key in counts:
            counts[key] += actual[key]
    for key, value in counts.items():
        clean.set(key, str(value))
    ET.ElementTree(output).write(destination, encoding="utf-8", xml_declaration=True)
    return counts, sorted(set(expected) - observed)


def evaluate(returncode, counts, initial, final, *, missing=(), error=None):
    blockers = []
    if error:
        blockers.append(error)
    if returncode != 0:
        blockers.append("pytest did not complete successfully")
    if not counts or counts.get("tests", 0) <= 0:
        blockers.append("No executed test evidence")
    elif any(counts.get(key, 0) for key in ("failures", "errors", "skipped")):
        blockers.append("Failure, error or skip in selected regressions")
    if missing:
        blockers.append("Missing selected suite coverage")
    if not initial or not final:
        blockers.append("Source identity unavailable")
    elif initial != final:
        blockers.append("Source changed during regressions")
    return {"regression_verdict": "FAIL" if blockers else "PASS",
            "regression_blockers": blockers, "release_verdict": "NOT_READY",
            "readiness_blockers": READINESS_LIMITS,
            "absolute_zero_unknown_vulnerabilities_claim": False}


def _write(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _source(output, label):
    snapshot = inventory()
    if (not snapshot.get("files") or not re.fullmatch(r"[a-f0-9]{64}", snapshot.get("sha256", ""))):
        raise ValueError("Source inventory unavailable")
    _write(output / ("source-" + label + ".json"), snapshot)
    return snapshot["sha256"]


def run(output, *, timeout=600):
    output = Path(output).resolve()
    if not output.is_relative_to(APP / "qa-results") or output.exists():
        raise ValueError("Use a new owned qa-results directory")
    if timeout <= 0 or timeout > 1800:
        raise ValueError("Timeout must be within 1..1800 seconds")
    output.mkdir(parents=True)
    initial = final = None
    counts, missing, returncode, error = None, list(SUITES), None, None
    omissions = selection_policy(APP)
    try:
        initial = _source(output, "start")
        with tempfile.TemporaryDirectory(prefix="owned-regression-", dir=output) as temporary:
            temporary = Path(temporary)
            raw = temporary / "junit-raw.xml"
            env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV}
            env.update(PYTHONPATH=str(APP), PYTHONDONTWRITEBYTECODE="1",
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONIOENCODING="utf-8",
                       AGENT_OS_ROOT=str(APP), TEMP=str(temporary), TMP=str(temporary),
                       WISE_SECURITY_REGRESSION_OFFLINE="1", HF_HUB_OFFLINE="1",
                       TRANSFORMERS_OFFLINE="1", PIP_NO_INDEX="1")
            command = [sys.executable, "-m", "pytest", *SUITES, "-q", "--tb=no",
                       "-p", "qa.acceptance.security_regression", "-o", "xfail_strict=true",
                       "--junitxml=" + str(raw), "--basetemp=" + str(temporary / "pytest")]
            command.extend("--deselect=" + row["node"]
                           for rows in omissions.values() for row in rows)
            # Raw output may contain private values, so it never becomes an artifact.
            with tempfile.TemporaryFile() as discarded:
                result = subprocess.run(command, cwd=APP, env=env, stdout=discarded,
                                        stderr=discarded, timeout=timeout)
            returncode = result.returncode
            counts, missing = sanitized_junit(raw, output / "junit.xml",
                                             [Path(suite).stem for suite in SUITES])
    except Exception as exc:
        error = "Incomplete regression evidence: " + type(exc).__name__
    finally:
        try:
            final = _source(output, "end")
        except Exception as exc:
            error = "Source evidence unavailable: " + type(exc).__name__
    verdict = evaluate(returncode, counts, initial, final, missing=missing, error=error)
    report = {"schema": "wise.security-regression.v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "python": sys.version.split()[0], "platform": sys.platform,
              "source_start_sha256": initial, "source_end_sha256": final,
              "counts": counts, "selected_suites": list(SUITES), "missing_suites": missing,
              "explicit_omissions": omissions, "returncode": returncode,
              "limitations": ["Synthetic contract fixtures only", "Raw output and diagnostic text omitted",
                              "Python network guard permits loopback; not an OS sandbox"], **verdict}
    _write(output / "pytest-sanitized.json", {"returncode": returncode, "counts": counts,
                                             "regression_blockers": verdict["regression_blockers"]})
    report["evidence_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in output.iterdir() if path.is_file()}
    _write(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        report = run(args.output, timeout=args.timeout)
    except (OSError, ValueError) as exc:
        parser.error(type(exc).__name__ + ": invalid output or run configuration")
    print(json.dumps({"regression_verdict": report["regression_verdict"],
                      "release_verdict": report["release_verdict"], "counts": report["counts"]}))
    return 0 if report["regression_verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
