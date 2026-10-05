"""QA-only preview of the exact owned WebView2, without desktop input.

The product launcher is imported unchanged. CapturePreviewAsync proves only the
renderer viewport: it does not certify the WinForms frame or DWM Acrylic.
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCOPE = "RENDERER_VIEWPORT_NOT_ACRYLIC_FRAME"
REQUEST_NAME = "native-renderer-request.json"
IMAGE_NAME = "native-renderer-settings.png"
RESULT_NAME = "native-renderer-result.json"


def owned_paths(output, runtime, qa_root=None):
    """Resolve both scopes, rejecting traversal, root targets and symlink escape."""
    qa_root = Path(qa_root or ROOT / "qa-results").resolve()
    output, runtime = Path(output).resolve(), Path(runtime).resolve()
    if output == qa_root or not output.is_relative_to(qa_root):
        raise ValueError("Capture output must be a descendant of qa-results")
    if runtime == qa_root or not runtime.is_relative_to(qa_root):
        raise ValueError("Capture requires an isolated runtime under qa-results")
    if runtime.name != "runtime" or not output.is_relative_to(runtime.parent):
        raise ValueError("Capture output and isolated runtime must belong to the same QA run")
    return output, runtime


def validate_request(request, *, token, hwnd, pid, runtime, url):
    expected = {"token": token, "window_handle": hwnd, "window_pid": pid,
                "runtime": str(runtime), "url": url, "output_file": IMAGE_NAME}
    if not token or request != expected:
        raise ValueError("Renderer capture request does not match the owned window identity")
    return expected


def capture_environment(env, output, runtime, token, url):
    """Do not let inherited WebView2 overrides target a personal profile."""
    result = dict(env, WISE_QA_NATIVE_CAPTURE_DIR=str(output),
                  WISE_QA_NATIVE_CAPTURE_TOKEN=token, WISE_QA_NATIVE_CAPTURE_URL=url,
                  WISE_DEBUG="0", WEBVIEW2_USER_DATA_FOLDER=str(runtime / "webview-profile"))
    for key in ("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER",
                "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER"):
        result.pop(key, None)
    return result


def verify_renderer_evidence(renderer, *, image_path, hwnd, pid, runtime, url):
    """Bind the actual preview bytes to the recorded owned native identity."""
    image_path, runtime = Path(image_path).resolve(), Path(runtime).resolve()
    expected = {"status": "PASS", "window_handle": hwnd, "window_pid": pid,
                "runtime": str(runtime), "url": url, "scope": SCOPE,
                "capture_api": "CoreWebView2.CapturePreviewAsync",
                "renderer": "Microsoft.Web.WebView2.WinForms.WebView2"}
    for key, value in expected.items():
        if renderer.get(key) != value:
            raise ValueError("Renderer evidence identity mismatch: " + key)
    if not renderer.get("profile") or Path(renderer["profile"]).resolve() != runtime / "webview-profile":
        raise ValueError("Renderer evidence does not use the owned profile")
    if not renderer.get("screenshot") or Path(renderer["screenshot"]).resolve() != image_path:
        raise ValueError("Renderer evidence screenshot path does not match the owned output")
    if not renderer.get("screenshot_sha256") or hashlib.sha256(image_path.read_bytes()).hexdigest() != renderer["screenshot_sha256"]:
        raise ValueError("Renderer evidence screenshot digest is missing or changed")
    from PIL import Image
    with Image.open(image_path) as image:
        if list(image.size) != renderer.get("viewport_size"):
            raise ValueError("Renderer evidence viewport dimensions do not match the image")
    return renderer


def _observe(window, output, runtime, token, expected_url):
    """One bounded request; invoke only capture on the existing UI thread."""
    result = {"status": "FAIL", "scope": SCOPE, "capture_api": "CoreWebView2.CapturePreviewAsync"}
    request_path, result_path = output / REQUEST_NAME, output / RESULT_NAME
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline and not window.events.closed.is_set():
        if request_path.is_file():
            break
        time.sleep(.1)
    else:
        return

    state = {}
    started = threading.Event()
    stream = None
    try:
        if request_path.stat().st_size > 4096:
            raise ValueError("Renderer capture request is too large")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        native = window.native
        hwnd, pid = int(native.Handle.ToInt64()), os.getpid()
        validate_request(request, token=token, hwnd=hwnd, pid=pid, runtime=runtime, url=expected_url)
        if not window.events.loaded.is_set():
            raise RuntimeError("Owned WebView2 page has not finished loading")
        from System import Action
        from System.IO import MemoryStream
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat

        stream = MemoryStream()
        state["stream"] = stream
        capture_deadline = time.monotonic() + 12

        def capture_on_ui_thread():
            try:
                if time.monotonic() >= capture_deadline:
                    raise TimeoutError("Owned capture callback started after its deadline")
                view = native.webview
                core = view.CoreWebView2
                if core is None or str(core.Source) != expected_url:
                    raise RuntimeError("Owned WebView2 URL/readiness does not match the QA page")
                if int(native.Handle.ToInt64()) != hwnd or native.IsDisposed:
                    raise RuntimeError("Owned native handle changed before capture")
                profile = Path(str(view.CreationProperties.UserDataFolder)).resolve()
                actual_profile = Path(str(core.Environment.UserDataFolder)).resolve()
                if profile != runtime / "webview-profile" or actual_profile != profile:
                    raise RuntimeError("WebView2 profile escaped the isolated runtime")
                result.update(window_handle=hwnd, window_pid=pid, runtime=str(runtime),
                              url=str(core.Source), renderer=str(view.GetType().FullName),
                              viewport_size=[int(view.Width), int(view.Height)],
                              profile=str(actual_profile), background_argb=int(view.DefaultBackgroundColor.ToArgb()))
                state["task"] = core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, state["stream"])
            except Exception as exc:
                state["error"] = str(exc)
            finally:
                started.set()

        # BeginInvoke schedules work without activating or focusing the form.
        action = Action(capture_on_ui_thread)
        native.BeginInvoke(action)
        if not started.wait(4):
            raise TimeoutError("Owned capture UI callback did not start")
        if "error" in state:
            raise RuntimeError(state["error"])
        task = state["task"]
        # This is a Python worker thread, never the WebView2 UI/message thread.
        while not task.IsCompleted and time.monotonic() < capture_deadline:
            time.sleep(.05)
        if not task.IsCompleted:
            # Leave the stream alive for the outstanding operation; the owned
            # host will be closed by its caller after the bounded failure.
            raise TimeoutError("Owned renderer capture exceeded 12 seconds")
        if task.IsCanceled or task.IsFaulted:
            raise RuntimeError("Owned renderer capture failed: " + str(task.Exception))
        image_path = output / IMAGE_NAME
        image_bytes = bytes(stream.ToArray())
        with image_path.open("xb") as image_file:
            image_file.write(image_bytes)
        result.update(status="PASS", screenshot=str(image_path),
                      screenshot_sha256=hashlib.sha256(image_bytes).hexdigest())
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        task = state.get("task")
        if stream is not None and started.is_set() and (task is None or task.IsCompleted):
            stream.Dispose()
        with result_path.open("x", encoding="utf-8") as result_file:
            json.dump(result, result_file, indent=2)


def main():
    output, runtime = owned_paths(os.environ["WISE_QA_NATIVE_CAPTURE_DIR"], os.environ["WISE_RUNTIME_ROOT"])
    token, expected_url = os.environ["WISE_QA_NATIVE_CAPTURE_TOKEN"], os.environ["WISE_QA_NATIVE_CAPTURE_URL"]
    if not token or not expected_url.startswith("http://127.0.0.1:") or not expected_url.endswith("/app/index.html"):
        raise ValueError("Native capture requires an owned loopback URL and request identity")
    sys.path.insert(0, str(ROOT))
    import wise_desktop
    import webview
    if wise_desktop._native_transparency_enabled():
        raise ValueError("QA renderer capture requires the default opaque native lifecycle")
    original_backdrop, original_start = wise_desktop._apply_windows_backdrop, webview.start

    def observed_backdrop(window):
        outcome = original_backdrop(window)
        threading.Thread(target=_observe, args=(window, output, runtime, token, expected_url), daemon=True).start()
        return outcome

    def isolated_start(*args, **kwargs):
        kwargs.update(private_mode=True, storage_path=str(runtime / "webview-profile"))
        return original_start(*args, **kwargs)

    wise_desktop._apply_windows_backdrop = observed_backdrop
    webview.start = isolated_start
    wise_desktop.main()


if __name__ == "__main__":
    main()
