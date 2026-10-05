import importlib.util
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


def load_check():
    path = Path(__file__).resolve().parents[1] / "qa/acceptance/screen_evidence.py"
    spec = importlib.util.spec_from_file_location("native_frame_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.inspect_native_frame


def test_native_frame_with_only_title_and_border_is_not_visual_pass(tmp_path):
    image = Image.new("RGB", (1180, 760), "#111111")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1179, 37), fill="#555555")
    draw.text((20, 12), "WISE settings", fill="white")
    draw.rectangle((0, 0, 1179, 759), outline="#666666", width=8)
    path = tmp_path / "blank-body.png"
    image.save(path)
    assert load_check()(path)["verdict"] == "FAIL"


def test_nonblank_native_capture_is_not_semantic_ui_certification(tmp_path):
    image = Image.new("RGB", (1180, 760), "#111111")
    draw = ImageDraw.Draw(image)
    for i in range(16):
        draw.rectangle((40 + i * 20, 100, 57 + i * 20, 700), fill=(i * 15, 180, 220))
    path = tmp_path / "synthetic-nonblank.png"
    image.save(path)
    result = load_check()(path)
    assert result["verdict"] == "NOT_BLANK"
    assert "semantic visual review still required" in result["reason"]


@pytest.mark.parametrize("kind", ["missing", "corrupt", "tiny"])
def test_invalid_native_frames_fail_closed(tmp_path, kind):
    path = tmp_path / "invalid.png"
    if kind == "corrupt":
        path.write_bytes(b"not an image")
    elif kind == "tiny":
        Image.new("RGB", (64, 48), "white").save(path)
    assert load_check()(path)["verdict"] == "FAIL"
