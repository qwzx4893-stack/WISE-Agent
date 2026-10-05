import hashlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from qa.acceptance.native_renderer_host import SCOPE
from qa.acceptance.screen_evidence import inspect_native_record


def native_record(tmp_path):
    image_path = tmp_path / "viewport.png"
    image = Image.new("RGB", (900, 640), "#151515")
    draw = ImageDraw.Draw(image)
    for index in range(20):
        draw.rectangle((100 + index * 20, 100, 120 + index * 20, 520),
                       fill=(index * 11, 100, 200 - index * 6))
    image.save(image_path)
    runtime = (tmp_path / "runtime").resolve()
    renderer = {"status": "PASS", "window_handle": 123, "window_pid": 789,
                "runtime": str(runtime), "profile": str(runtime / "webview-profile"),
                "url": "http://127.0.0.1:5555/app/index.html", "scope": SCOPE,
                "capture_api": "CoreWebView2.CapturePreviewAsync",
                "renderer": "Microsoft.Web.WebView2.WinForms.WebView2",
                "screenshot": str(image_path), "screenshot_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "viewport_size": [900, 640]}
    return {"screenshot": str(image_path), "screenshot_scope": SCOPE, "window_handle": 123,
            "window_pid": 789, "owned_process_ids": [788, 789], "runtime_root": str(runtime),
            "runtime_url": renderer["url"], "native_acrylic_frame_status": "UNVERIFIED",
            "renderer_capture": renderer}


def test_viewport_gate_requires_pixels_and_independent_launched_identity(tmp_path):
    assert not inspect_native_record(native_record(tmp_path))


@pytest.mark.parametrize("field,value", [
    ("window_pid", 999), ("owned_process_ids", [999]), ("window_handle", False),
    ("runtime_url", "http://127.0.0.1:9999/app/index.html"), ("screenshot_scope", "BROWSER"),
    ("native_acrylic_frame_status", "PASS"), ("renderer_capture", None),
])
def test_viewport_gate_rejects_foreign_or_overclaimed_metadata(tmp_path, field, value):
    record = native_record(tmp_path)
    record[field] = value
    assert inspect_native_record(record)


def test_modified_png_cannot_keep_its_old_native_provenance(tmp_path):
    record = native_record(tmp_path)
    path = Path(record["screenshot"])
    with path.open("ab") as handle:
        handle.write(b"modified-after-capture")
    assert any("digest" in error for error in inspect_native_record(record))


def test_legacy_gdi_record_is_not_relabelled_as_viewport_evidence(tmp_path):
    record = native_record(tmp_path)
    assert not inspect_native_record({"screenshot": record["screenshot"]})
    assert inspect_native_record({"screenshot": record["screenshot"], "screenshot_scope": SCOPE})
