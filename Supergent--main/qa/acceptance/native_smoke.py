"""PID-bound FlaUI test. Never matches a user window by title or sends input."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_native(port, output, *, env=None):
    output.mkdir(parents=True, exist_ok=True)
    record = {"id": "native-flaui-lifecycle-settings", "status": "PASS", "steps": []}
    log_path = output / "native.log"
    record["log"] = str(log_path)
    automation = window = proc = None
    owned_children = []
    started = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            if not env or not env.get("WISE_RUNTIME_ROOT"):
                raise RuntimeError("Native automation requires an explicitly isolated runtime; production targeting is forbidden")
            from native_renderer_host import IMAGE_NAME, REQUEST_NAME, RESULT_NAME, SCOPE, capture_environment, owned_paths, verify_renderer_evidence
            output, isolated_runtime = owned_paths(output, env["WISE_RUNTIME_ROOT"])
            capture_token = uuid.uuid4().hex
            expected_url = f"http://127.0.0.1:{port}/app/index.html"
            record["runtime_root"] = str(isolated_runtime)
            record["runtime_url"] = expected_url
            launch_env = capture_environment(env, output, isolated_runtime, capture_token, expected_url)
            import clr
            assemblies = ROOT / ".tooling/flaui"
            for name in ("Interop.UIAutomationClient", "FlaUI.Core", "FlaUI.UIA3"):
                clr.AddReference(str(assemblies / (name + ".dll")))
            from FlaUI.Core import Application
            from FlaUI.Core.AutomationElements import Window
            from FlaUI.Core.Capturing import Capture
            from FlaUI.Core.Definitions import WindowVisualState
            from FlaUI.UIA3 import UIA3Automation
            from System import TimeSpan
            record["steps"].append("Launch own native WISE process against isolated backend")
            proc = subprocess.Popen([sys.executable, str(ROOT / "qa/acceptance/native_renderer_host.py"), "--native-only", "--port", str(port)],
                                    cwd=ROOT, env=launch_env, stdout=log, stderr=subprocess.STDOUT)
            record["owned_pid"] = proc.pid
            automation = UIA3Automation()
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline and proc.poll() is None:
                import psutil
                owned_children = psutil.Process(proc.pid).children(recursive=True)
                # Windows venv python.exe is a redirector: the actual WinForms
                # window belongs to its child python process, not the shim.
                for owned_pid in [proc.pid] + [child.pid for child in owned_children]:
                    candidate = automation.GetDesktop().FindFirstChild(automation.ConditionFactory.ByProcessId(owned_pid))
                    if candidate is not None:
                        window = Window(candidate.FrameworkAutomationElement); break
                if window is not None: break
                time.sleep(.5)
            if window is None:
                raise RuntimeError("Owned native window did not appear")
            record["owned_process_ids"] = [proc.pid] + [child.pid for child in owned_children]
            assert window.Properties.ProcessId.Value in record["owned_process_ids"]
            record["window_pid"] = window.Properties.ProcessId.Value
            record["steps"].append("Verify window PID equals launched PID")
            pattern = window.Patterns.Window.Pattern
            record["steps"].append("Minimize and restore via UI Automation Window pattern")
            pattern.SetWindowVisualState(WindowVisualState.Minimized)
            time.sleep(.5)
            assert pattern.WindowVisualState.Value == WindowVisualState.Minimized
            pattern.SetWindowVisualState(WindowVisualState.Normal)
            time.sleep(.5)
            transform = window.Patterns.Transform.Pattern
            record["steps"].append("Resize own window to 1180x760 via Transform pattern")
            transform.Resize(1180, 760)
            time.sleep(1)
            bounds = window.BoundingRectangle
            assert abs(bounds.Width - 1180) <= 16 and abs(bounds.Height - 760) <= 16
            record["bounds"] = [bounds.X, bounds.Y, bounds.Width, bounds.Height]
            # Chromium accessible DOM is queried by semantic accessible names.
            settings = None
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and settings is None:
                for name in ("الإعدادات", "Settings"):
                    settings = window.FindFirstDescendant(automation.ConditionFactory.ByName(name))
                    if settings is not None: break
                if settings is None: time.sleep(.5)
            if settings is None: raise RuntimeError("Settings control is absent from native accessibility tree")
            record["steps"].append("Invoke real Settings control through FlaUI (no physical mouse)")
            settings.Patterns.Invoke.Pattern.Invoke()
            time.sleep(1)
            close = None
            for name in ("إغلاق الإعدادات", "Close settings"):
                close = window.FindFirstDescendant(automation.ConditionFactory.ByName(name))
                if close is not None: break
            assert close is not None, "Settings dialog did not expose its close control"
            observe_seconds = int(env.get("WISE_QA_OBSERVE_NATIVE_SECONDS", "0"))
            if not 0 <= observe_seconds <= 55: raise ValueError("Native observation window is bounded to 55 seconds")
            if observe_seconds:
                record["window_handle"] = window.Properties.NativeWindowHandle.Value.ToInt64()
                (output / "native-observation.json").write_text(json.dumps({
                    "window_handle": record["window_handle"], "window_pid": record["window_pid"],
                    "runtime": env["WISE_RUNTIME_ROOT"], "scope": "OWNED_SYNTHETIC_QA_WINDOW_ONLY"
                }), encoding="utf-8")
                time.sleep(observe_seconds)
            from screen_evidence import inspect_native_frame
            record["settings_accessibility_status"] = "PASS"
            record["window_handle"] = window.Properties.NativeWindowHandle.Value.ToInt64()
            renderer_deadline = time.monotonic() + 15
            request_pending = output / (REQUEST_NAME + ".pending")
            with request_pending.open("x", encoding="utf-8") as capture_request:
                json.dump({"token": capture_token, "window_handle": record["window_handle"],
                           "window_pid": record["window_pid"], "runtime": str(isolated_runtime),
                           "url": expected_url, "output_file": IMAGE_NAME}, capture_request)
            request_pending.rename(output / REQUEST_NAME)
            record["capture_attempts"] = []
            # FlaUI 5 Capture.Element copies desktop GDI pixels within the UIA
            # rectangle. Preserve those frames separately from renderer proof.
            for attempt in range(5):
                shot = output / f"native-settings-{attempt + 1}.png"
                Capture.Element(window).ToFile(str(shot))
                record["screenshot"] = str(shot)
                record["visual_capture"] = inspect_native_frame(shot)
                record["capture_attempts"].append({"screenshot": str(shot), **record["visual_capture"]})
                if record["visual_capture"]["verdict"] == "NOT_BLANK": break
                if attempt < 4: time.sleep(.5)
            record["desktop_gdi_capture_status"] = record["visual_capture"]["verdict"]
            record["desktop_gdi_screenshot"] = record["screenshot"]
            record["desktop_gdi_visual_capture"] = dict(record["visual_capture"])
            renderer = None
            while time.monotonic() < renderer_deadline:
                try:
                    renderer = json.loads((output / RESULT_NAME).read_text(encoding="utf-8"))
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                time.sleep(.1)
            if renderer is None:
                raise TimeoutError("Owned renderer capture did not return within 15 seconds")
            record["renderer_capture"] = renderer
            if renderer["status"] != "PASS":
                raise RuntimeError(renderer.get("error", "Owned renderer capture failed"))
            verify_renderer_evidence(renderer, image_path=output / IMAGE_NAME, hwnd=record["window_handle"],
                                     pid=record["window_pid"], runtime=isolated_runtime, url=expected_url)
            renderer["visual_capture"] = inspect_native_frame(output / IMAGE_NAME)
            if renderer["visual_capture"]["verdict"] != "NOT_BLANK":
                raise RuntimeError("Owned renderer viewport is uniform or invalid")
            record["native_evidence_scope"] = "OWNED_LIFECYCLE_SETTINGS_ACCESSIBILITY_AND_RENDERER_VIEWPORT"
            record["screenshot"] = renderer["screenshot"]
            record["visual_capture"] = dict(renderer["visual_capture"])
            record["screenshot_scope"] = SCOPE
            record["native_acrylic_frame_status"] = "UNVERIFIED"
            record["steps"].append("Capture exact owned WebView2 viewport without activation; retain desktop GDI separately")
            record["steps"].append("Close Settings via Invoke and close ONLY owned window")
            close.Patterns.Invoke.Pattern.Invoke()
            # Use FlaUI's title-bar close-button Invoke when available,
            # matching a real user's X; WindowPattern.Close is a fallback.
            window.Close()
            proc.wait(timeout=15)
        except Exception:
            record.update(status="FAIL", stack_trace=traceback.format_exc())
            log.write(record["stack_trace"])
            if window is not None:
                try:
                    from FlaUI.Core.Capturing import Capture
                    shot = output / "native-failure.png"
                    Capture.Element(window).ToFile(str(shot))
                    record["screenshot"] = str(shot)
                    tree = []
                    def read_property(item, name):
                        try: return str(getattr(item, name))
                        except Exception: return None
                    for item in window.FindAllDescendants():
                        tree.append({"name":read_property(item,"Name"), "automation_id":read_property(item,"AutomationId"), "control_type":read_property(item,"ControlType")})
                    (output / "accessibility-tree.json").write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as exc:
                    record["capture_error"] = str(exc)
        finally:
            for child in reversed(owned_children):
                try:
                    if child.is_running(): child.terminate()
                except Exception: pass
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
            if automation is not None: automation.Dispose()
    record["duration_s"] = round(time.perf_counter() - started, 2)
    record["passed"] = record["status"] == "PASS"
    (output / "native-smoke.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{record['status']} {record['id']}", flush=True)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return 0 if run_native(args.port, args.output, env=dict(os.environ))["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
