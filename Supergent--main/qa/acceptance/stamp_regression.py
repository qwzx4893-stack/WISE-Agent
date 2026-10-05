"""Run regression and bind its evidence to unchanged source content."""
import json
import subprocess
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from source_stamp import ROOT,source_stamp

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-interactive-native", action="store_true",
                        help="Do not launch legacy Notepad scenario; retain explicit uncovered native/summary gates")
    args = parser.parse_args()
    stamp=source_stamp()
    output=args.output or ROOT/"qa-results"/("regression-final-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+".json")
    if output.exists():
        parser.error("Refusing to overwrite earlier regression evidence")
    output.parent.mkdir(parents=True,exist_ok=True)
    excluded = (["tests/test_production_autonomy_harness.py::test_scenario_a_windows_application_control",
                 "tests/test_production_autonomy_harness.py::test_zz_autonomy_metrics_summary"]
                if args.no_interactive_native else [])
    process=subprocess.run([sys.executable,"-m","pytest","tests","-q","--disable-warnings",
        "--json-report","--json-report-file="+str(output),
        *["--deselect="+node for node in excluded]],cwd=ROOT)
    if output.exists():
        result=json.loads(output.read_text(encoding="utf-8"))
        result.update(source_stamp=stamp,source_changed_during_run=stamp != source_stamp(),
                      excluded_scenarios=excluded, coverage_scope=("NONINTERACTIVE_REGRESSION_ONLY" if excluded else "FULL_REGRESSION"),
                      native_legacy_verdict="NOT_EVALUATED" if excluded else "SEE_TEST_RESULTS")
        output.write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
    return process.returncode or (1 if stamp != source_stamp() else 0)

if __name__ == "__main__":raise SystemExit(main())
