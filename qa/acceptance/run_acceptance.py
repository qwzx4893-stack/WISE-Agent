"""Orchestrate WISE's evidence-producing production acceptance gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[2]
QA_ROOT = ROOT / "qa" / "acceptance"
NODE_ROOT = ROOT / "qa" / "tooling"
TOKEN_PATTERN = re.compile(r"sk-or-v1-[A-Za-z0-9_-]+|(?:api[_-]?key|authorization)\s*[:=]\s*\S+", re.I)
BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
JSON_SECRET_PATTERN = re.compile(r'''(["'](?:api[_-]?key|access_token|refresh_token|password|token)["']\s*:\s*)(["'])(.*?)(\2)''', re.I)


def redact(value: str) -> str:
    value = BEARER_PATTERN.sub("Bearer <redacted>", value)
    value = JSON_SECRET_PATTERN.sub(lambda match: match[1] + match[2] + "<redacted>" + match[2], value)
    return TOKEN_PATTERN.sub("<redacted>", value)


def wait_for_backend(base_url: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=3).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def run_stage(name: str, command: list[str], output_dir: Path, timeout: int, critical: bool = True) -> dict[str, object]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        stdout = redact(completed.stdout)
        stderr = redact(completed.stderr)
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = redact((exc.stdout or "") if isinstance(exc.stdout, str) else "")
        stderr = redact((exc.stderr or "") if isinstance(exc.stderr, str) else "")
        stderr += f"\nTimed out after {timeout}s"
        exit_code = 124
    (output_dir / f"{name}.stdout.log").write_text(stdout, encoding="utf-8")
    (output_dir / f"{name}.stderr.log").write_text(stderr, encoding="utf-8")
    return {
        "name": name,
        "critical": critical,
        "command": [Path(part).name if i == 0 else part for i, part in enumerate(command)],
        "exit_code": exit_code,
        "passed": exit_code == 0,
        "duration_s": round(time.perf_counter() - started, 2),
        "stdout_log": str(output_dir / f"{name}.stdout.log"),
        "stderr_log": str(output_dir / f"{name}.stderr.log"),
    }


def severity_counts(value: object) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            severity = str(node.get("severity") or node.get("issue_severity") or "").lower()
            if severity in counts:
                counts[severity] += 1
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return counts


def prepare_safe_openapi(base_url: str, destination: Path) -> None:
    """Write a GET-only schema containing endpoints proven to be read-only."""
    schema = httpx.get(f"{base_url}/openapi.json", timeout=30).json()
    safe_paths = {
        "/health",
        "/health/detailed",
        "/api/v2/settings",
        "/api/v2/system/telemetry",
        "/api/v2/system-integration",
        "/api/v2/voice/status",
        "/api/v2/models/external",
        "/api/v2/models/local",
        "/api/v2/tasks",
        "/api/v2/tasks/active",
        "/api/v2/sessions",
    }
    schema["paths"] = {
        path: {"get": operations["get"]}
        for path, operations in schema.get("paths", {}).items()
        if path in safe_paths and isinstance(operations, dict) and "get" in operations
    }
    destination.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_agentshield_input(destination: Path) -> None:
    """Stage credential-free MCP structure, never raw account/config folders.

    This is a limited structural review, not a scan of private policy contents
    or command arguments. Refuse a reused directory to avoid stale secret files.
    """
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("AgentShield staging directory must be empty")
    servers = {}
    executable_names = {"python", "python3", "node", "npx", "uv", "uvx", "docker", "bash", "pwsh", "powershell"}
    mcp_config = ROOT / "memory" / "mcp_servers.json"
    if mcp_config.exists():
        if mcp_config.stat().st_size > 512_000:
            raise ValueError("MCP staging input exceeds size limit")
        descriptors = json.loads(mcp_config.read_text(encoding="utf-8"))
        if not isinstance(descriptors, list):
            raise ValueError("Invalid MCP configuration structure")
        for item in descriptors[:100]:
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            entry = {}
            if item.get("command"):
                executable = str(item["command"]).replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe")
                entry["command"] = executable if executable in executable_names else "<custom-executable>"
            if isinstance(item.get("args"), list):
                entry["args"] = ["<redacted>" for _ in item["args"][:100]]
            for field in ("env", "headers"):
                if isinstance(item.get(field), dict):
                    entry[field] = {str(key): "<redacted>" for key in item[field]}
            if item.get("url"):
                try:
                    parsed = urlsplit(str(item["url"]))
                    hostname = parsed.hostname or "redacted.invalid"
                    path = parsed.path if parsed.path in {"", "/", "/mcp", "/sse", "/mcp/oauth"} else "/redacted"
                    entry["url"] = urlunsplit((parsed.scheme, hostname, path, "", ""))
                except ValueError:
                    entry["url"] = "https://redacted.invalid/mcp"
            # Stable synthetic names avoid copying arbitrary user input.
            servers["server-" + str(len(servers) + 1)] = entry
    (destination / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "SCAN_SCOPE.md").write_text(
        "Credential-free structural MCP review only. Arguments and credentials are redacted. "
        "Private policies, config folders and account data are deliberately not staged. "
        "No verdict from this scope proves whole-project security.\n", encoding="utf-8")


def write_markdown(report: dict[str, object], path: Path) -> None:
    verdict = report["verdict"]
    lines = [
        "# WISE production acceptance report",
        "",
        f"**Verdict:** {verdict}",
        f"**Started:** {report['started_at']}",
        f"**Duration:** {report['duration_s']} seconds",
        "",
        "| Stage | Critical | Result | Duration |",
        "| --- | --- | --- | --- |",
    ]
    for stage in report["stages"]:
        lines.append(
            f"| {stage['name']} | {'yes' if stage['critical'] else 'no'} | "
            f"{'PASS' if stage['passed'] else 'FAIL'} | {stage['duration_s']}s |"
        )
    lines.extend(["", "## Security triage", ""])
    triage = report.get("security_triage", {})
    for name, counts in triage.items():
        lines.append(f"- `{name}`: {counts}")
    lines.extend(["", "## Blocking stages", ""])
    blockers = [stage["name"] for stage in report["stages"] if stage["critical"] and not stage["passed"]]
    lines.append("- None" if not blockers else "\n".join(f"- {name}" for name in blockers))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--live", action="store_true", help="Run real provider scenarios through encrypted WISE configuration")
    parser.add_argument("--native", action="store_true", help="Launch and automate the native Windows window")
    parser.add_argument("--skip-full-tests", action="store_true")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = ROOT / "qa-results" / stamp
    output_dir.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    backend_proc: subprocess.Popen[str] | None = None

    if not wait_for_backend(args.base_url, timeout=2):
        port = int(args.base_url.rsplit(":", 1)[-1])
        backend_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api.server:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT,
            stdout=(output_dir / "backend.stdout.log").open("w", encoding="utf-8"),
            stderr=(output_dir / "backend.stderr.log").open("w", encoding="utf-8"),
            text=True,
        )
        if not wait_for_backend(args.base_url, timeout=180):
            backend_proc.kill()
            raise SystemExit(f"WISE backend did not become ready; see {output_dir}")

    stages: list[dict[str, object]] = []
    try:
        journey_command = [sys.executable, str(QA_ROOT / "user_journeys.py"), "--output", str(output_dir / "user-journeys")]
        if args.native: journey_command.append("--native")
        stages.append(run_stage("real-user-journeys", journey_command, output_dir, timeout=600, critical=True))
        if not args.skip_full_tests:
            stages.append(run_stage(
                "pytest-full",
                [sys.executable, "-m", "pytest", "tests", "-q", "--disable-warnings", "--maxfail=20", "--json-report", f"--json-report-file={output_dir / 'pytest-report.json'}"],
                output_dir,
                timeout=900,
                critical=True,
            ))
        stages.append(run_stage(
            "api-contract",
            [sys.executable, str(QA_ROOT / "api_contract.py"), "--base-url", args.base_url, "--output", str(output_dir / "api-contract.json")],
            output_dir,
            timeout=180,
            critical=True,
        ))
        safe_schema = output_dir / "safe-openapi.json"
        prepare_safe_openapi(args.base_url, safe_schema)
        stages.append(run_stage(
            "schemathesis-readonly",
            [
                str(ROOT / ".tooling/qa-venv/Scripts/schemathesis.exe"), "run", str(safe_schema), "--url", args.base_url,
                "--phases", "coverage,fuzzing", "--max-examples", "5", "--generation-deterministic", "--max-time", "120", "--workers", "1",
                "--checks", "not_a_server_error,status_code_conformance,response_schema_conformance",
                "--output-sanitize", "true", "--report", "json", "--report-dir", str(output_dir / "schemathesis"), "--no-color",
            ],
            output_dir,
            timeout=180,
            critical=True,
        ))
        stages.append(run_stage(
            "ui-audit",
            [sys.executable, str(QA_ROOT / "ui_audit.py"), "--base-url", args.base_url, "--output", str(output_dir / "ui")],
            output_dir,
            timeout=240,
            critical=True,
        ))
        if args.live:
            stages.append(run_stage(
                "live-agent",
                [sys.executable, str(QA_ROOT / "live_agent_eval.py"), "--base-url", args.base_url, "--output", str(output_dir / "live-agent.json")],
                output_dir,
                timeout=720,
                critical=True,
            ))
        if args.native:
            stages.append(run_stage(
                "native-window",
                [sys.executable, str(QA_ROOT / "native_smoke.py"), "--port", args.base_url.rsplit(":", 1)[-1], "--output", str(output_dir / "native")],
                output_dir,
                timeout=150,
                critical=True,
            ))
        stages.append(run_stage(
            "official-launcher-cold-start",
            [sys.executable, str(QA_ROOT / "startup_probe.py"), "--port", "8877", "--output", str(output_dir / "startup")],
            output_dir,
            timeout=150,
            critical=True,
        ))

        agentshield_report = output_dir / "agentshield.json"
        agentshield_input = output_dir / "agentshield-input"
        prepare_agentshield_input(agentshield_input)
        stages.append(run_stage(
            "agentshield",
            ["node", str(NODE_ROOT / "node_modules/ecc-agentshield/dist/index.js"), "scan", "--path", str(agentshield_input), "--format", "json", "--output", str(agentshield_report), "--taint", "--supply-chain"],
            output_dir,
            timeout=300,
            critical=False,
        ))
        bandit_report = output_dir / "bandit.json"
        stages.append(run_stage(
            "bandit",
            [
                sys.executable, "-m", "bandit", "-r", "api", "core", "wise_desktop.py",
                "-f", "json", "-o", str(bandit_report),
                "-x", "core/mcp/vendors,core/mcp/.venv,core/models/archived_moe,tools",
            ],
            output_dir,
            timeout=360,
            critical=False,
        ))
        pip_report = output_dir / "pip-audit.json"
        stages.append(run_stage(
            "pip-audit",
            [sys.executable, "-m", "pip_audit", "-r", "requirements.txt", "-f", "json", "-o", str(pip_report)],
            output_dir,
            timeout=600,
            critical=False,
        ))

        security_triage: dict[str, object] = {}
        for name, path in (("agentshield", agentshield_report), ("bandit", bandit_report)):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                counts = severity_counts(payload)
                if name == "agentshield":
                    files_scanned = int(payload.get("summary", {}).get("filesScanned", 0))
                    counts["files_scanned"] = files_scanned
                    if files_scanned == 0:
                        counts["coverage_error"] = "AgentShield matched no supported configuration files"
                security_triage[name] = counts
            except Exception as exc:
                security_triage[name] = {"parse_error": str(exc)}
        try:
            audit_payload = json.loads(pip_report.read_text(encoding="utf-8"))
            dependencies = audit_payload.get("dependencies", []) if isinstance(audit_payload, dict) else audit_payload
            vulnerable = sum(len(item.get("vulns", [])) for item in dependencies if isinstance(item, dict))
            security_triage["pip-audit"] = {"known_vulnerabilities": vulnerable}
        except Exception as exc:
            security_triage["pip-audit"] = {"parse_error": str(exc)}

        critical_failed = any(stage["critical"] and not stage["passed"] for stage in stages)
        security_blocker = any(
            isinstance(counts, dict) and (counts.get("critical", 0) > 0 or counts.get("high", 0) > 0)
            for counts in security_triage.values()
        ) or bool(security_triage.get("pip-audit", {}).get("known_vulnerabilities", 0))
        security_incomplete = any(
            isinstance(counts, dict) and ("parse_error" in counts or "coverage_error" in counts)
            for counts in security_triage.values()
        )
        # Skipped gates never constitute release evidence. UI success alone
        # cannot certify native behavior, real execution, voice or account auth.
        unverified = []
        if args.skip_full_tests: unverified.append("Full regression suite was skipped")
        if not args.live: unverified.append("Live provider execution was not tested")
        if not args.native: unverified.append("Native FlaUI automation was not tested")
        unverified.extend(["Real-account OAuth completion is not certified by automated public-server checks",
                           "Voice/GPU coexistence and long-running reliability have no acceptance evidence in this run"])
        verdict = "NOT_READY" if critical_failed or security_blocker or security_incomplete or unverified else "READY"
        report = {
            "schema_version": "wise.acceptance.v1",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(time.perf_counter() - started, 2),
            "verdict": verdict,
            "stages": stages,
            "security_triage": security_triage,
            "output_dir": str(output_dir),
            "live_provider_tested": args.live,
            "native_window_tested": args.native,
            "unverified_gates": unverified,
        }
        (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_markdown(report, output_dir / "REPORT.md")
        print(json.dumps({"verdict": verdict, "report": str(output_dir / "REPORT.md")}, ensure_ascii=False))
        return 0 if verdict == "READY" else 1
    finally:
        if backend_proc is not None:
            backend_proc.terminate()
            try:
                backend_proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                backend_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
