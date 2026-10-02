"""Run regression and bind its evidence to unchanged source content."""
import json
import subprocess
import sys
from source_stamp import ROOT,source_stamp

def main():
    stamp=source_stamp()
    output=ROOT/"qa-results/regression-final-20261001.json"
    process=subprocess.run([sys.executable,"-m","pytest","tests","-q","--disable-warnings",
        "--json-report","--json-report-file="+str(output)],cwd=ROOT)
    if output.exists():
        result=json.loads(output.read_text(encoding="utf-8"))
        result.update(source_stamp=stamp,source_changed_during_run=stamp != source_stamp())
        output.write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
    return process.returncode or (1 if stamp != source_stamp() else 0)

if __name__ == "__main__":raise SystemExit(main())
