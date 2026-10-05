"""Owned, keyless native observation for independently checking capture faults."""
from datetime import datetime, timezone
import argparse
import json

from user_journeys import ROOT, Runtime
from native_smoke import run_native
from source_stamp import source_stamp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=45)
    args = parser.parse_args()
    if not 0 <= args.seconds <= 55:
        parser.error("--seconds must be between 0 and 55")
    output = ROOT / "qa-results" / ("native-observation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir()
    runtime = Runtime(output)
    stamp = source_stamp()
    try:
        runtime.start()
        runtime.env["WISE_QA_OBSERVE_NATIVE_SECONDS"] = str(args.seconds)
        case = run_native(runtime.port, output, env=runtime.env)
        (output / "report.json").write_text(json.dumps({"source_stamp": stamp,
            "source_changed_during_run": stamp != source_stamp(), "case": case,
            "scope": "Independent passive capture diagnostic, not release acceptance"}, indent=2), encoding="utf-8")
    finally:
        runtime.stop()
        for handle in runtime.handles: handle.close()
    print(str(output), flush=True)


if __name__ == "__main__": main()
