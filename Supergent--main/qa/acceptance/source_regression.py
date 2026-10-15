"""Broad source CI, with explicit platform/vendor/native exclusions.

This is not the fail-closed security acceptance runner or native certification.
Unlike a developer checkout, a public checkout omits optional voice vendor code.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from qa.acceptance.security_regression import selection_policy


APP = Path(__file__).resolve().parents[2]
NATIVE_EXCLUSIONS = (
    "tests/test_production_autonomy_harness.py::test_scenario_a_windows_application_control",
    "tests/test_production_autonomy_harness.py::test_zz_autonomy_metrics_summary",
)


def command(app=APP):
    omissions = selection_policy(app)
    native = [{"node": node, "reason": "Requires separate owned native acceptance evidence"}
              for node in NATIVE_EXCLUSIONS]
    omitted = [row for rows in omissions.values() for row in rows] + native
    args = [sys.executable, "-m", "pytest", "tests/", "-q", "--maxfail=0",
            "--junitxml=qa-results/ci.xml"]
    args.extend("--deselect=" + row["node"] for row in omitted)
    return args, {**omissions, "native": native}


def main():
    args, omissions = command()
    print(json.dumps({"scope": "source-regression-not-product-acceptance",
                      "explicit_omissions": omissions}), flush=True)
    return subprocess.call(args, cwd=APP)


if __name__ == "__main__":
    raise SystemExit(main())
