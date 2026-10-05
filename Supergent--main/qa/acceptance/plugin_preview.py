"""Owned keyless QA preview; stop on stdin, never reuse the user's backend."""
from datetime import datetime, timezone
import json
import sys
from user_journeys import ROOT, Runtime

output = ROOT / "qa-results" / ("plugins-review-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
output.mkdir()
runtime = Runtime(output)
try:
    runtime.start()
    print(json.dumps({"url":runtime.url + "/app/#plugins", "output":str(output)}), flush=True)
    sys.stdin.readline()
finally: runtime.stop()
