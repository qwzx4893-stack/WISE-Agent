"""Broad source CI, with explicit platform/vendor/native exclusions.

This is not the fail-closed security acceptance runner or native certification.
Unlike a developer checkout, a public checkout omits optional voice vendor code.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

from qa.acceptance.security_regression import selection_policy


APP = Path(__file__).resolve().parents[2]
NATIVE_EXCLUSIONS = (
    "tests/test_production_autonomy_harness.py::test_scenario_a_windows_application_control",
    "tests/test_production_autonomy_harness.py::test_zz_autonomy_metrics_summary",
)
BROWSER_CONTROLS = (
    "tests/test_production_autonomy_harness.py::test_scenario_c_browser_task",
    "tests/test_production_autonomy_harness.py::test_scenario_q_browser_crash_recovery",
)


def _brave_available():
    # Match the actual default browser launcher's discovery, without launching
    # a browser, borrowing an owner profile or silently replacing Brave.
    from core.browser.browser_session import _BRAVE_PATHS
    return any(path.is_file() for path in _BRAVE_PATHS) or bool(
        shutil.which("brave") or shutil.which("brave.exe"))


def command(app=APP):
    omissions = selection_policy(app)
    native = [{"node": node, "reason": "Requires separate owned native acceptance evidence"}
              for node in NATIVE_EXCLUSIONS]
    browser = [] if _brave_available() else [
        {"node": node, "reason": "Default Brave executable absent; browser journey NOT_VERIFIED, no alternate browser installed"}
        for node in BROWSER_CONTROLS]
    omitted = [row for rows in omissions.values() for row in rows] + native + browser
    args = [sys.executable, "-m", "pytest", "tests/", "-q", "--maxfail=0",
            "--junitxml=qa-results/ci.xml"]
    args.extend("--deselect=" + row["node"] for row in omitted)
    return args, {**omissions, "native": native, "browser": browser}


def main():
    args, omissions = command()
    print(json.dumps({"scope": "source-regression-not-product-acceptance",
                      "explicit_omissions": omissions}), flush=True)
    return subprocess.call(args, cwd=APP)


if __name__ == "__main__":
    raise SystemExit(main())
