"""Aggregate actual renderer/native cases without inventing new observations."""
import argparse
import json
from pathlib import Path
from source_stamp import ROOT, source_stamp
from screen_evidence import inspect_native_frame, inspect_native_record


def combine(coordinator_path: Path, output: Path):
    if output.exists():
        raise FileExistsError("Refusing to overwrite previous evidence")
    coordinator = json.loads(coordinator_path.read_text(encoding="utf-8"))
    stamp = source_stamp()
    if coordinator.get("source_stamp") != stamp or coordinator.get("source_changed_during_run") is not False:
        raise ValueError("Coordinator evidence is not bound to unchanged current source")
    folder = coordinator_path.parent
    ui_path = folder / "user-journeys/report.json"
    native_path = folder / "native/native-smoke.json"
    ui = json.loads(ui_path.read_text(encoding="utf-8"))
    native = json.loads(native_path.read_text(encoding="utf-8"))
    # Derived validation of the preserved original image, not a new capture.
    # UIA can expose controls while a screenshot has only the window frame.
    native["original_recorded_status"] = native["status"]
    native["visual_capture"] = inspect_native_frame(Path(native.get("screenshot", "")))
    validation_errors = inspect_native_record(native)
    if validation_errors:
        native["status"] = "FAIL"
        native["evidence_failure"] = "; ".join(validation_errors)
    if ui.get("source_stamp") != stamp or ui.get("source_changed_during_run") is not False:
        raise ValueError("Renderer evidence is stale")
    completed = {row["name"] for row in coordinator["stages"]}
    if not {"real-user-journeys", "native-window"}.issubset(completed):
        raise ValueError("Missing actual renderer/native stage")
    # Never turn a FAIL green; retain original native status and image provenance.
    cases = ui["cases"] + [native]
    result = {"schema": "wise.ui-evidence.aggregate.v1", "source_stamp": stamp,
              "source_changed_during_run": False, "cases": cases,
              "ui_verdict": "PASS" if all(row["status"] == "PASS" for row in cases) else "FAIL",
              "mocked_api_responses": ui.get("mocked_api_responses"), "release_verdict": "NOT_READY",
              "sources": [str(coordinator_path.resolve()), str(ui_path.resolve()), str(native_path.resolve())],
              "limitations": ["Aggregates renderer and native stages in their documented owned runtimes; not a new combined user session",
                              "Does not certify provider answers, account OAuth, GPU/voice or installer",
                              "Native renderer previews do not certify the native frame/Acrylic"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--coordinator", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = combine(args.coordinator, args.output)
    print(json.dumps({"ui_verdict": result["ui_verdict"], "cases": len(result["cases"]), "output": str(args.output)}))
