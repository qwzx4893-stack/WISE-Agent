"""Short owned-runtime sampler verification, not a soak or daily-use certificate."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
from process_resources import OwnedProcessSampler
from source_stamp import ROOT, source_stamp
from user_journeys import Runtime


def main():
    destination = ROOT / "qa-results" / ("resource-probe-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    destination.mkdir(parents=True)
    runtime = Runtime(destination)
    stamp = source_stamp()
    try:
        runtime.start()
        sampler = OwnedProcessSampler(runtime.process.pid)
        samples = []
        for index in range(4):
            httpx.get(runtime.url + "/health", timeout=5).raise_for_status()
            samples.append({"elapsed_s": index * 2, **sampler.sample()})
            if index < 3: time.sleep(2)
        result = {"verdict": "PASS", "source_stamp": stamp, "source_changed_during_run": stamp != source_stamp(),
                  "scope": "SHORT_SAMPLER_VERIFICATION_ONLY", "samples": samples,
                  "limitations": ["Not a 15-minute soak or prolonged stability measurement",
                                  "Backend tree only; no UI/browser or GPU attribution",
                                  "The earlier launcher-only samples are still unverified"]}
        (destination / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(destination / "report.json"), "process_counts": [len(row["processes"]) for row in samples],
                          "rss_mb": [row["rss_mb"] for row in samples]}))
    finally:
        runtime.stop()
        for handle in runtime.handles: handle.close()


if __name__ == "__main__": main()
