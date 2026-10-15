"""Bounded source/SCA scans with conservative, source-bound security evidence.

Local rules only; no cloud code upload, token validation, model calls or account
actions. Advisory requests transmit package names/versions, not project code.
Scanner observations are not confirmed exploits and cannot certify a product.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import site
import subprocess
import sys
from urllib.parse import quote

from packaging.version import InvalidVersion, Version

try:
    from .security_baseline import APP, WORKSPACE, inventory
except ImportError:  # Direct CLI execution.
    from security_baseline import APP, WORKSPACE, inventory

REQUIRED_EXTERNAL_GATES = (
    "isolated-windows-native", "official-uac", "clean-install-update-rollback",
    "native-2h-soak", "native-8h-soak", "runtime-privacy-egress",
    "voice-model-file-boundary", "connector-identity-consent",
)
SAFE_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LOCALAPPDATA",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "USERPROFILE",
            "APPDATA", "TEMP", "TMP", "LANG", "LC_ALL"}


def scrub(value):
    """Drop scanner source snippets/secret values; retain rules and locations."""
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items() if key.lower() not in
                {"secret", "match", "code", "lines", "metavars", "dataflow_trace", "fix"}}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def wsl_path(path):
    """Translate an absolute drive path, never shell syntax or a network share."""
    path = PureWindowsPath(str(path))
    if (not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive)
            or any(part in {".", ".."} or "\x00" in part for part in path.parts)):
        raise ValueError("WSL scanner requires an absolute local drive path")
    return "/mnt/" + path.drive[0].lower() + "/" + "/".join(path.parts[1:])


def semgrep_command(binary, staged, output, *, use_wsl=False):
    rules = APP / "qa/acceptance/security_rules.yml"
    flags = ["scan", "--config", str(rules), "--metrics=off", "--disable-version-check",
             "--no-git-ignore", "--timeout", "10", "--json", str(staged)]
    if not use_wsl:
        return [binary, *flags]
    if os.name != "nt":
        raise ValueError("Use the native Linux Semgrep executable outside Windows")
    scanner = APP / ".tooling/security/semgrep-linux-venv/bin/semgrep"
    if not scanner.is_file():
        return []  # Missing owned installation is a BLOCKED stage.
    temp = output / "tool-temp"
    temp.mkdir(exist_ok=True)
    flags[2], flags[-1] = wsl_path(rules), wsl_path(staged)
    # No login shell, no inherited model/account keys, no user Semgrep account.
    # WSL is only a static-scanner backend, not native Windows acceptance.
    return [str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/wsl.exe"),
            "-d", "Ubuntu", "--exec", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
            "SEMGREP_SEND_METRICS=off", "SEMGREP_ENABLE_VERSION_CHECK=false",
            "TMPDIR=" + wsl_path(temp),
            "SEMGREP_SETTINGS_FILE=" + wsl_path(temp / "semgrep-settings.yml"),
            "SEMGREP_LOG_FILE=" + wsl_path(temp / "semgrep.log"),
            wsl_path(scanner), *flags]


def evaluate(stages, initial, final, external_gates=(), findings=()):
    mandatory = {"bandit", "semgrep", "gitleaks-source", "gitleaks-history",
                 "pip-audit-runtime", "pip-audit-runtime-user", "pip-audit-qa", "pip-audit-voice", "pip-audit-tts", "npm-audit-qa"}
    blockers = ["stage:" + row["id"] for row in stages if row.get("status") != "PASS"]
    blockers.extend("missing-stage:" + name for name in sorted(mandatory - {row.get("id") for row in stages}))
    if not stages:
        blockers.append("No scans executed")
    if initial != final:
        blockers.append("Source changed during audit")
    for name in REQUIRED_EXTERNAL_GATES:
        # External/native evidence is reviewed separately; unknown/missing or
        # skipped gates never become success by implication.
        valid = False
        for row in external_gates:
            if row.get("id") != name or row.get("status") != "PASS" or row.get("source_sha256") != final:
                continue
            try:
                evidence = Path(row["evidence_path"]).resolve(strict=True)
                valid = (evidence.is_relative_to(APP / "qa-results") and evidence.is_file()
                         and hashlib.sha256(evidence.read_bytes()).hexdigest() == row.get("evidence_sha256"))
            except (KeyError, OSError, ValueError):
                valid = False
            if valid:
                break
        if not valid:
            blockers.append("unverified:" + name)
    blockers.extend("finding:" + row.get("id", "unknown") for row in findings
                    if row.get("status") not in {"FIXED", "FALSE_POSITIVE"})
    return {"release_verdict": "NOT_READY" if blockers else "SCOPED_SECURITY_GATE_PASS",
            "blockers": blockers, "absolute_zero_unknown_vulnerabilities_claim": False}


def stage(output, name, command, *, cwd=APP, timeout=300, report=None, count=None):
    if not command or not command[0] or not Path(command[0]).is_file():
        return {"id": name, "status": "BLOCKED", "reason": "Tool unavailable"}
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV}
    env.update(SEMGREP_SEND_METRICS="off", SEMGREP_ENABLE_VERSION_CHECK="false",
               PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1")
    temp = output / "tool-temp"
    temp.mkdir(exist_ok=True)
    env.update(TEMP=str(temp), TMP=str(temp))
    started = datetime.now(timezone.utc).isoformat()
    interrupted = False
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout,
                                encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        interrupted = True
        result = None
    except OSError as exc:
        return {"id": name, "status": "BLOCKED", "reason": type(exc).__name__, "started_at": started}
    row = {"id": name, "status": "INCOMPLETE" if interrupted else (
               "PASS" if result.returncode == 0 else "REVIEW_REQUIRED"),
           "returncode": None if interrupted else result.returncode, "started_at": started, "command": command}
    if interrupted:
        row["reason"] = "Bounded scan timeout; salvaged observations are not a completed scan"
    try:
        payload = json.loads(report.read_text(encoding="utf-8-sig") if report and report.is_file()
                             else result.stdout if result else "")
        clean = scrub(payload)
        destination = output / (name + ".json")
        _write(destination, clean)
        row["report"] = destination.name
        row["report_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        if count is not None:
            observed = count(clean)
            row["observations"] = observed
            if observed and not interrupted:
                row["status"] = "REVIEW_REQUIRED"
        if isinstance(clean, dict) and clean.get("errors"):
            row["status"] = "INCOMPLETE"
        if name == "bandit" and not clean.get("metrics", {}).get("_totals", {}).get("loc", 0):
            row["status"] = "INCOMPLETE"
            row["reason"] = "No source lines scanned"
        if name == "semgrep" and not clean.get("paths", {}).get("scanned"):
            row["status"] = "INCOMPLETE"
            row["reason"] = "No supported source files scanned"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        row["status"] = "INCOMPLETE"
        row["reason"] = "Missing/malformed JSON evidence; raw output deliberately not retained"
    finally:
        # Remove only our exact scanner-created raw artifact, also on timeout
        # or parse failure. Reports never retain source or credential snippets.
        if (report and report.parent.resolve() == output.resolve()
                and report.name.endswith("-raw.json") and report.is_file()
                and not report.is_symlink()):
            report.unlink()
    return row


def sbom(output, label, paths):
    components = []
    for distribution in importlib.metadata.distributions(path=[str(path) for path in paths if path.is_dir()]):
        name = distribution.metadata.get("Name", "")
        version = distribution.version
        if name:
            # Installed metadata is data, not pip syntax. A crafted Name or
            # Version must not turn an advisory query into another index/URL
            # request through the generated requirements inventory.
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name):
                raise ValueError("Invalid installed package name in " + label)
            try:
                Version(version)
            except InvalidVersion as exc:
                raise ValueError("Invalid installed package version in " + label) from exc
            if any(char.isspace() for char in version) or any(char in version for char in "#@;\\"):
                raise ValueError("Unsafe installed package version in " + label)
            components.append({"type": "library", "name": name, "version": version,
                               "purl": f"pkg:pypi/{quote(name.lower().replace('_', '-'))}@{quote(version)}"})
    components.sort(key=lambda item: (item["name"].casefold(), item["version"]))
    document = {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
                "metadata": {"timestamp": datetime.now(timezone.utc).isoformat(),
                             "component": {"type": "application", "name": "WISE-" + label}},
                "components": components}
    _write(output / ("sbom-" + label + ".json"), document)
    # Fully resolved inventory: pip-audit does not need to execute setup.py or
    # re-resolve arbitrary requirement URLs to inspect this environment.
    pins = output / ("installed-" + label + ".txt")
    pins.write_text("\n".join(sorted({row['name'] + '==' + row['version'] for row in components})) + "\n", encoding="utf-8")
    return pins, len(components)


def dependency_paths(*, use_wsl=False):
    """Separate installed locations; a venv overlay is not its inherited runtime.

    These are metadata inventories, not proof of the desktop's effective import
    precedence. In particular, user-site can shadow packages in base Python.
    """
    runtime = (Path(sys.base_prefix) / "Lib/site-packages" if os.name == "nt" else
               Path(sys.base_prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    local = (Path(sys.prefix) / "Lib/site-packages" if os.name == "nt" else
             Path(sys.prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    user = site.getusersitepackages()
    users = [user] if isinstance(user, str) else list(user)
    paths = {
        "runtime": [runtime],
        "runtime-user": [Path(path) for path in users],
        "qa": [local],
        "voice": [APP / ".voice-venv/Lib/site-packages"],
        "tts": [APP / "tools/index-tts/.venv/Lib/site-packages"],
    }
    if use_wsl:
        paths["scanner"] = list((APP / ".tooling/security/semgrep-linux-venv/lib").glob("python*/site-packages"))
    return paths


def history_identity():
    """Read-only Git scope stamp; never report ref names or commit contents."""
    git = shutil.which("git")
    if not git:
        return {"status": "UNVERIFIED", "reason": "Git unavailable"}
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV}
    commands = (("head", ["rev-parse", "HEAD"]),
                ("refs", ["for-each-ref", "--format=%(refname) %(objectname)"]),
                ("commits", ["rev-list", "--all", "--count"]))
    outputs = {}
    try:
        for name, args in commands:
            result = subprocess.run([git, "--no-pager", *args], cwd=WORKSPACE, env=env,
                                    capture_output=True, encoding="utf-8", errors="strict", timeout=30)
            if result.returncode or len(result.stdout) > 1024 * 1024:
                raise ValueError("Git identity unavailable")
            outputs[name] = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", outputs["head"]):
            raise ValueError("Invalid HEAD identity")
        if not re.fullmatch(r"[0-9]{1,12}", outputs["commits"]):
            raise ValueError("Invalid commit count")
        refs = sorted(outputs["refs"].splitlines())
        for row in refs:
            name, checksum = row.rsplit(" ", 1)
            if (not name.startswith("refs/") or any(char.isspace() for char in name)
                    or not re.fullmatch(r"[0-9a-f]{40,64}", checksum)):
                raise ValueError("Invalid ref identity")
        return {"status": "VERIFIED", "head": outputs["head"],
                "reachable_commit_count": int(outputs["commits"]), "ref_count": len(refs),
                "refs_sha256": hashlib.sha256("\n".join(refs).encode("utf-8")).hexdigest(),
                "scope": "Reachable --all refs only; prior exposures/unreachable objects are not revoked"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"status": "UNVERIFIED", "reason": "Bounded Git scope identity unavailable"}


def history_blockers(initial, final):
    if initial.get("status") != "VERIFIED" or final.get("status") != "VERIFIED":
        return ["Git history scope identity unverified"]
    if any(initial.get(key) != final.get(key) for key in
           ("head", "refs_sha256", "ref_count", "reachable_commit_count")):
        return ["Git history scope changed during audit"]
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--semgrep", type=Path)
    parser.add_argument("--semgrep-wsl", action="store_true",
                        help="Use the owned Semgrep installation in existing Ubuntu WSL; static scans only")
    parser.add_argument("--gitleaks", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(APP / "qa-results") or output.exists():
        parser.error("Use a new owned qa-results directory")
    output.mkdir(parents=True)
    initial = inventory()
    initial_history = history_identity()
    _write(output / "source-start.json", initial)
    staged = output / "scan-source"
    staged.mkdir()
    for row in initial["files"]:
        source = WORKSPACE / row["path"]
        target = staged / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise RuntimeError("Source changed while staging; restart without concurrent edits")
        target.write_bytes(data)
    stages = []
    stages.append(stage(output, "bandit", [sys.executable, "-m", "bandit", "-r", str(staged),
        "-f", "json", "-o", str(output / "bandit-raw.json")], report=output / "bandit-raw.json",
        count=lambda doc: len(doc.get("results", [])), timeout=600))
    semgrep = str(args.semgrep) if args.semgrep else shutil.which("semgrep")
    stages.append(stage(output, "semgrep", semgrep_command(semgrep, staged, output, use_wsl=args.semgrep_wsl),
        count=lambda doc: len(doc.get("results", [])), timeout=600))
    gitleaks = str(args.gitleaks) if args.gitleaks else shutil.which("gitleaks")
    for name, command in (("gitleaks-source", [gitleaks, "dir", str(staged)]),
                          ("gitleaks-history", [gitleaks, "git", str(WORKSPACE), "--log-opts=--all"])):
        raw = output / (name + "-raw.json")
        stages.append(stage(output, name, command + ["--redact=100", "--no-banner", "--report-format=json", "--report-path", str(raw)],
                            timeout=600, report=raw, count=lambda doc: len(doc)))
    inventories = []
    runtime_paths = dependency_paths(use_wsl=args.semgrep_wsl)
    for label, paths in runtime_paths.items():
        pins, number = sbom(output, label, paths)
        inventories.append({"environment": label, "components": number, "paths": [str(path) for path in paths]})
        if args.offline or not number:
            stages.append({"id": "pip-audit-" + label, "status": "UNVERIFIED", "reason": "Offline or environment absent"})
        else:
            stages.append(stage(output, "pip-audit-" + label, [sys.executable, "-m", "pip_audit", "-r", str(pins),
                "--no-deps", "--disable-pip", "--progress-spinner", "off", "--format", "json"], timeout=600,
                count=lambda doc: sum(len(row.get("vulns", [])) for row in doc.get("dependencies", []))))
    node = shutil.which("node")
    npm = Path(node).parent / "node_modules/npm/bin/npm-cli.js" if node else None
    if args.offline or not npm or not npm.is_file():
        stages.append({"id": "npm-audit-qa", "status": "UNVERIFIED", "reason": "Offline or npm unavailable"})
    else:
        for name in ("npm-userconfig", "npm-globalconfig"):
            (output / name).write_text("registry=https://registry.npmjs.org\n", encoding="utf-8")
        stages.append(stage(output, "npm-audit-qa", [node, str(npm), "audit", "--json", "--ignore-scripts",
            "--registry=https://registry.npmjs.org", "--userconfig=" + str(output / "npm-userconfig"),
            "--globalconfig=" + str(output / "npm-globalconfig")],
            cwd=APP / "qa/tooling", timeout=180,
            count=lambda doc: doc.get("metadata", {}).get("vulnerabilities", {}).get("total", 0)))
    final = inventory()
    final_history = history_identity()
    _write(output / "source-end.json", final)
    decision = evaluate(stages, initial["sha256"], final["sha256"])
    decision["blockers"].extend(history_blockers(initial_history, final_history))
    if decision["blockers"]:
        decision["release_verdict"] = "NOT_READY"
    report = {"schema": "wise.security-audit.v1", "source_sha256": initial["sha256"],
              "source_changed": initial["sha256"] != final["sha256"], "stages": stages,
              "history_scope": {"start": initial_history, "end": final_history},
              "inventories": inventories, "excluded_directories": initial["exclusions"],
              "auditor_environment": {"python": sys.executable,
                  "prefix": sys.prefix, "base_prefix": sys.base_prefix,
                  "user_site_enabled": site.ENABLE_USER_SITE,
                  "qa_scope": "Local overlay only; inherited base/user inventories reported separately",
                  "production_interpreter_verified": False},
              "coverage_limits": ["Scanner observations require independent reachability/impact triage",
                  "Installed MCP package environments, native binaries and GPU runtime require separate inventories",
                  "No real accounts, administrative actions, native-window or VM soak tests executed",
                  "Python runtime inventory uses this interpreter's base environment; launcher identity must be verified",
                  "QA local-overlay zero findings do not certify inherited base/user packages or import precedence",
                  "No claim that all market-listed third-party servers are safe"],
              **decision}
    _write(output / "report.json", report)
    print(json.dumps({"report": str(output / "report.json"), "verdict": report["release_verdict"],
                      "stages": [{key: value for key, value in row.items() if key in {"id", "status", "observations", "reason"}} for row in stages]}))
    return 1 if report["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
