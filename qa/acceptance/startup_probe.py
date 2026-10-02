"""Measure cold startup through WISE's official desktop launcher."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=60.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    stdout_path = args.output / "launcher.stdout.log"
    stderr_path = args.output / "launcher.stderr.log"
    started = time.perf_counter()
    ready_at = None
    proc = None
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "wise_desktop.py"), "--background", "--port", str(args.port)],
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        deadline = started + max(args.budget + 45.0, 105.0)
        while time.perf_counter() < deadline:
            if proc.poll() is not None:
                break
            try:
                response = httpx.get(f"http://127.0.0.1:{args.port}/health", timeout=2.0)
                if response.status_code == 200:
                    ready_at = time.perf_counter()
                    break
            except Exception:
                pass
            time.sleep(0.5)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    elapsed = round((ready_at or time.perf_counter()) - started, 2)
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    launcher_reported_failure = "did not come online" in stderr_text.lower() or "failed to start" in stderr_text.lower()
    passed = ready_at is not None and elapsed <= args.budget and not launcher_reported_failure
    report = {
        "passed": passed,
        "ready": ready_at is not None,
        "startup_seconds": elapsed,
        "budget_seconds": args.budget,
        "launcher_reported_failure": launcher_reported_failure,
        "process_exit_code": proc.returncode if proc is not None else None,
        "stopped_by_probe_after_measurement": ready_at is not None,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    (args.output / "startup-probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
