"""Perception must fail closed when it cannot observe a live desktop."""

from __future__ import annotations


def test_screen_capture_never_reports_synthetic_pixels_as_live(monkeypatch, tmp_path):
    from core.vision.screen_capture import ScreenCaptureEngine

    engine = ScreenCaptureEngine(output_dir=tmp_path)
    monkeypatch.setattr(engine, "_is_win32", False)

    result = engine.capture_full_screen()

    assert result.success is False
    assert result.image_bytes == b""
    assert "interactive Windows desktop" in result.error_reason


def test_ui_tree_never_invents_controls_without_a_live_window(monkeypatch):
    from core.vision.ui_tree import UITreeExtractor

    extractor = UITreeExtractor()
    monkeypatch.setattr(extractor, "_is_win32", False)

    assert extractor.get_window_ui_tree(0) is None


def test_invalid_window_capture_does_not_substitute_the_full_desktop(monkeypatch, tmp_path):
    from core.vision.screen_capture import ScreenCaptureEngine

    engine = ScreenCaptureEngine(output_dir=tmp_path)
    monkeypatch.setattr(engine, "_is_win32", False)

    result = engine.capture_window(123)

    assert result.success is False
    assert result.image_bytes == b""


def test_vision_manager_stops_before_ocr_or_vlm_when_capture_fails(monkeypatch):
    from core.vision.screen_capture import ScreenCaptureResult
    from core.vision.vision_manager import WISEVisionManager

    manager = WISEVisionManager()
    monkeypatch.setattr(manager.ui_extractor, "get_window_ui_tree", lambda hwnd: None)
    monkeypatch.setattr(
        manager.screen_capture,
        "capture_full_screen",
        lambda save_to_disk=False: ScreenCaptureResult(False, 0, 0, error_reason="desktop unavailable"),
    )

    observation = manager.observe(scope="vlm")

    assert observation.capture is not None
    assert observation.error == "desktop unavailable"
    assert observation.vlm is None
