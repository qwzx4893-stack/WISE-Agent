"""Regression coverage for the live UIA/VLM target-resolution path."""

from __future__ import annotations

from core.vision.screen_capture import ScreenCaptureResult
from core.vision.ui_tree import BoundingBox, UIElementNode
from core.vision.vision_manager import PerceptionLevel, PerceptionObservation
from core.vision.vlm_engine import VisualElement, VisualElementCategory, VisualObservationResult


class _Window:
    title = "Settings"
    process_id = 100
    rect = (10, 20, 400, 300)
    hwnd = 42


class _Windows:
    def get_foreground_window(self):
        return _Window()

    def get_window_info(self, hwnd):
        return _Window() if hwnd == 42 else None


class _Input:
    def get_dpi_scale(self):
        return 1.0


class _Screen:
    def capture_window(self, hwnd, save_to_disk=False):
        return ScreenCaptureResult(False, 0, 0, error_reason="not needed for UIA test")


class _Vision:
    def __init__(self, vlm=None):
        self.screen_capture = _Screen()
        self._vlm = vlm

    def observe(self, **kwargs):
        return PerceptionObservation(
            level_used=PerceptionLevel.LEVEL_4_HEAVY_VLM,
            vlm=self._vlm,
        )


class _UIA:
    def __init__(self, root):
        self._root = root

    def get_window_ui_tree(self, hwnd):
        return self._root


def _resolver(root, vlm=None):
    from core.hands.target_resolver import TargetResolver

    resolver = TargetResolver(
        vision_manager=_Vision(vlm),
        window_manager=_Windows(),
        input_driver=_Input(),
    )
    resolver.ui_extractor = _UIA(root)
    return resolver


def test_capture_snapshot_keeps_live_uia_coordinates_and_resolves_target():
    root = UIElementNode(
        hwnd=42,
        name="Settings",
        control_type="Window",
        class_name="Window",
        bounding_box=BoundingBox(10, 20, 400, 300),
        children=[UIElementNode(
            hwnd=43,
            name="Save changes",
            control_type="Button",
            class_name="Button",
            bounding_box=BoundingBox(100, 150, 120, 30),
        )],
    )
    resolver = _resolver(root)

    snapshot = resolver.capture_snapshot(42)
    target, decision, error = resolver.resolve_target("save changes", hwnd=42)

    assert len(snapshot.uia_tree) == 2
    assert snapshot.uia_tree[1]["bounding_box"] == [100, 150, 120, 30]
    assert decision.value == "CONTINUE"
    assert error is None
    assert target.source == "UIA"
    assert target.best_center == (160, 165)


def test_vlm_target_uses_its_real_pixel_bbox():
    vlm = VisualObservationResult(elements=[VisualElement(
        name="Custom button",
        category=VisualElementCategory.BUTTON,
        pixel_bbox=(90, 120, 80, 40),
        center_point=(130, 140),
        confidence=0.9,
    )])
    resolver = _resolver(None, vlm)

    target, decision, error = resolver.resolve_target("custom button", hwnd=42)

    assert decision.value == "CONTINUE"
    assert error is None
    assert target.source == "VLM"
    assert target.candidate_bounding_boxes == [(90, 120, 80, 40)]
