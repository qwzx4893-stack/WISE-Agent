import importlib.util
import hashlib
from pathlib import Path

import pytest


@pytest.fixture
def host():
    path = Path(__file__).resolve().parents[1] / "qa/acceptance/native_renderer_host.py"
    spec = importlib.util.spec_from_file_location("native_renderer_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_renderer_paths_require_same_owned_qa_run(host, tmp_path):
    qa = tmp_path / "qa-results"
    runtime = qa / "run/runtime"
    output = qa / "run/native"
    assert host.owned_paths(output, runtime, qa) == (output.resolve(), runtime.resolve())
    for unsafe in (qa, tmp_path / "user-profile", qa / "run/../../outside", qa / "other-run/native"):
        with pytest.raises(ValueError):
            host.owned_paths(unsafe, runtime, qa)
    with pytest.raises(ValueError):
        host.owned_paths(output, tmp_path / "user-profile", qa)


def test_renderer_request_binds_handle_process_runtime_url_and_fixed_output(host, tmp_path):
    identity = dict(token="owned-run-token", hwnd=123, pid=456, runtime=tmp_path / "runtime",
                    url="http://127.0.0.1:60121/app/index.html")
    request = dict(token=identity["token"], window_handle=123, window_pid=456,
                   runtime=str(identity["runtime"]), url=identity["url"], output_file=host.IMAGE_NAME)
    assert host.validate_request(request, **identity) == request
    for key, wrong in {"token": "other", "window_handle": 999, "window_pid": 999,
                       "runtime": str(tmp_path / "production"), "url": "http://127.0.0.1:8765/app/index.html",
                       "output_file": "../../outside.png"}.items():
        with pytest.raises(ValueError):
            host.validate_request(dict(request, **{key: wrong}), **identity)
    with pytest.raises(ValueError):
        host.validate_request(dict(request, extra_output="other.png"), **identity)


def test_renderer_uniform_viewport_still_fails_closed(tmp_path):
    from PIL import Image
    path = Path(__file__).resolve().parents[1] / "qa/acceptance/screen_evidence.py"
    spec = importlib.util.spec_from_file_location("renderer_frame_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image = tmp_path / "blank-renderer.png"
    Image.new("RGB", (1166, 722), "#101014").save(image)
    assert module.inspect_native_frame(image)["verdict"] == "FAIL"


def test_renderer_environment_cannot_inherit_personal_profile_or_debugger(host, tmp_path):
    env = {"WEBVIEW2_USER_DATA_FOLDER": "personal-profile", "WISE_DEBUG": "true",
           "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--remote-debugging-port=9222",
           "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER": "personal-debug-pipe", "PYTHONUTF8": "1"}
    runtime = tmp_path / "runtime"
    result = host.capture_environment(env, tmp_path / "native", runtime, "token", "http://127.0.0.1:1/app/index.html")
    assert result["WEBVIEW2_USER_DATA_FOLDER"] == str(runtime / "webview-profile")
    assert result["WISE_DEBUG"] == "0"
    assert "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" not in result
    assert "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER" not in result
    assert result["PYTHONUTF8"] == "1"
    assert env["WEBVIEW2_USER_DATA_FOLDER"] == "personal-profile"


def test_renderer_evidence_rejects_wrong_identity_modified_png_and_missing_metadata(host, tmp_path):
    from PIL import Image
    runtime = tmp_path / "runtime"
    image_path = tmp_path / host.IMAGE_NAME
    Image.new("RGB", (1164, 721), "#101014").save(image_path)
    identity = dict(image_path=image_path, hwnd=123, pid=456, runtime=runtime,
                    url="http://127.0.0.1:60121/app/index.html")
    renderer = dict(status="PASS", window_handle=123, window_pid=456, runtime=str(runtime),
                    url=identity["url"], scope=host.SCOPE, capture_api="CoreWebView2.CapturePreviewAsync",
                    renderer="Microsoft.Web.WebView2.WinForms.WebView2", profile=str(runtime / "webview-profile"),
                    screenshot=str(image_path), screenshot_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
                    viewport_size=[1164, 721])
    assert host.verify_renderer_evidence(renderer, **identity) == renderer
    with pytest.raises(ValueError, match="window_pid"):
        host.verify_renderer_evidence(dict(renderer, window_pid=999), **identity)
    for missing in ("screenshot_sha256", "profile", "renderer", "window_handle", "viewport_size"):
        incomplete = dict(renderer)
        incomplete.pop(missing)
        with pytest.raises(ValueError):
            host.verify_renderer_evidence(incomplete, **identity)
    with pytest.raises(ValueError, match="screenshot path"):
        host.verify_renderer_evidence(dict(renderer, screenshot=str(tmp_path / "other.png")), **identity)
    Image.new("RGB", (1164, 721), "white").save(image_path)
    with pytest.raises(ValueError, match="digest"):
        host.verify_renderer_evidence(renderer, **identity)
