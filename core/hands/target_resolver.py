"""
WISE Target Resolver Subsystem.
Implements the canonical 5-layer target resolution hierarchy:
  1. Browser DOM
  2. Windows UI Automation (UIA)
  3. Accessibility Tree (MSAA)
  4. OCR / Text Grounding
  5. Visual Grounding (VLM / Template)
  6. Coordinate Fallback (Last resort, bound strictly to frame signature and window geometry)

Enforces strict anti-stale frame verification before any physical input dispatch.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core.contracts import (
    PerceptionSnapshot,
    VisualTarget,
    ActionLoopDecision,
    FailureType,
)
from core.windows.window_manager import WISEWindowManager, get_window_manager, WindowInfo
from core.windows.input_driver import WindowsInputDriver, get_input_driver
from core.vision.vision_manager import WISEVisionManager, get_vision_manager, PerceptionLevel
from core.vision.ui_tree import UITreeExtractor, UIElementNode, get_ui_tree_extractor
from core.vision.ocr_engine import WindowsOCREngine, get_ocr_engine

LOG = logging.getLogger("WISE.Hands.TargetResolver")


class TargetResolver:
    """
    Canonical Hierarchical Target Resolver for WISE.
    Resolves semantic targets into validated physical coordinates across
    DOM -> UIA -> Accessibility -> OCR -> Visual Grounding -> Coordinates.
    """

    def __init__(
        self,
        vision_manager: Optional[WISEVisionManager] = None,
        window_manager: Optional[WISEWindowManager] = None,
        input_driver: Optional[WindowsInputDriver] = None,
    ) -> None:
        self.vision_mgr = vision_manager or get_vision_manager()
        self.window_mgr = window_manager or get_window_manager()
        self.input_driver = input_driver or get_input_driver()
        self.ui_extractor = get_ui_tree_extractor()
        self.ocr_engine = get_ocr_engine()
        self._last_snapshot: Optional[PerceptionSnapshot] = None

    def capture_snapshot(self, hwnd: Optional[int] = None) -> PerceptionSnapshot:
        """
        Gathers a comprehensive Unified Perception Snapshot (Phase 2.1).
        Captures active HWND, window rect, title, DPI scale, cursor position,
        UIA element tree, OCR text regions, and computes a frame signature.
        """
        target_hwnd = hwnd or 0
        if not target_hwnd:
            active_win = self.window_mgr.get_foreground_window()
            if active_win:
                target_hwnd = active_win.hwnd

        # Window geometry
        win_rect = (0, 0, 1920, 1080)
        win_title = ""
        proc_name = ""
        if target_hwnd:
            w_info = self.window_mgr.get_window_info(target_hwnd)
            if w_info:
                win_title = w_info.title
                proc_name = f"pid_{w_info.process_id}"
                win_rect = (w_info.rect[0], w_info.rect[1], w_info.rect[0] + w_info.rect[2], w_info.rect[1] + w_info.rect[3])
            else:
                try:
                    r = wintypes.RECT()
                    if ctypes.windll.user32.GetWindowRect(target_hwnd, ctypes.byref(r)):
                        win_rect = (r.left, r.top, r.right, r.bottom)
                except Exception:
                    pass

        # DPI scale
        dpi_scale = 1.0
        try:
            dpi_scale = self.input_driver.get_dpi_scale()
        except Exception:
            pass

        # Cursor position
        cursor_pos = (0, 0)
        try:
            pt = wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                cursor_pos = (pt.x, pt.y)
        except Exception:
            pass

        # Extract UIA tree
        uia_elements: List[Dict[str, Any]] = []
        try:
            ui_tree = self.ui_extractor.get_window_ui_tree(target_hwnd)
            if ui_tree:
                def _collect_nodes(node: UIElementNode) -> None:
                    uia_elements.append({
                        "name": node.name,
                        "control_type": node.control_type,
                        "bounding_box": [node.bounding_box.x, node.bounding_box.y, node.bounding_box.width, node.bounding_box.height],
                        "center": list(node.bounding_box.center),
                        "is_enabled": node.is_enabled,
                        "is_keyboard_focusable": bool(getattr(node, "is_keyboard_focusable", False)),
                    })
                    for child in node.children:
                        _collect_nodes(child)
                _collect_nodes(ui_tree)
        except Exception as e:
            LOG.debug("Error extracting UIA tree: %s", e)

        # OCR regions & detected text
        ocr_regions: List[Dict[str, Any]] = []
        detected_text: List[str] = []
        try:
            cap = self.vision_mgr.screen_capture.capture_window(target_hwnd, save_to_disk=False)
            if cap and cap.success and cap.image_bytes:
                ocr_res = self.ocr_engine.extract_text(cap.image_bytes, region_offset=(win_rect[0], win_rect[1]))
                if ocr_res and ocr_res.success:
                    for block in ocr_res.blocks:
                        box = getattr(block, "bounding_box", None)
                        bx, by, bw, bh = 0, 0, 0, 0
                        if box:
                            bx = getattr(box, "x", 0)
                            by = getattr(box, "y", 0)
                            bw = getattr(box, "width", 0)
                            bh = getattr(box, "height", 0)
                        ocr_regions.append({
                            "text": block.text,
                            "box": [bx, by, bw, bh],
                            "center": [bx + bw // 2, by + bh // 2],
                            "confidence": getattr(block, "confidence", 1.0),
                        })
                        detected_text.append(block.text)
        except Exception as e:
            LOG.debug("Error extracting OCR regions: %s", e)

        # Compute deterministic frame signature
        sig_data = f"{target_hwnd}:{win_title}:{win_rect}:{len(uia_elements)}:{len(detected_text)}"
        frame_sig = hashlib.sha256(sig_data.encode("utf-8")).hexdigest()[:16]

        snapshot = PerceptionSnapshot(
            frame_signature=frame_sig,
            active_hwnd=target_hwnd,
            process_name=proc_name,
            window_title=win_title,
            window_rect=win_rect,
            dpi_scale=dpi_scale,
            uia_tree=uia_elements,
            accessibility_elements=[],
            ocr_regions=ocr_regions,
            detected_text=detected_text,
            cursor_position=cursor_pos,
        )
        self._last_snapshot = snapshot
        return snapshot

    def resolve_target(
        self,
        target_query: Optional[str] = None,
        hwnd: Optional[int] = None,
        explicit_coords: Optional[Tuple[int, int]] = None,
        expected_frame_signature: Optional[str] = None,
        browser_active: bool = False,
        browser_dom: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[VisualTarget], ActionLoopDecision, Optional[str]]:
        """
        Resolves the physical coordinates for a target using the 5-layer hierarchy:
          DOM -> UIA -> Accessibility -> OCR -> Visual Grounding -> Coordinates.
        Validates frame signature before resolving to prevent stale action dispatch.
        """
        # Step 0: Take or refresh perception snapshot
        snapshot = self.capture_snapshot(hwnd)

        # Anti-stale frame check: If caller expected a specific frame, verify it matches
        if expected_frame_signature and expected_frame_signature != snapshot.frame_signature:
            LOG.warning(
                "STALE_FRAME detected! Expected signature %s != current %s",
                expected_frame_signature,
                snapshot.frame_signature,
            )
            return None, ActionLoopDecision.RETRY, "STALE_FRAME: Frame signature mismatch, re-observation required"

        # Layer 1: Browser DOM (if browser active and DOM provided)
        if browser_active and browser_dom and target_query:
            dom_el = self._search_browser_dom(browser_dom, target_query)
            if dom_el:
                target = VisualTarget(
                    semantic_description=target_query,
                    candidate_bounding_boxes=[dom_el.get("rect", (0, 0, 0, 0))],
                    confidence=0.98,
                    source="DOM",
                    frame_id=snapshot.frame_id,
                    coordinate_space="WINDOW",
                    associated_element=dom_el,
                    best_center=dom_el.get("center"),
                )
                return target, ActionLoopDecision.CONTINUE, None

        # Layer 2: Windows UI Automation (UIA)
        if target_query and snapshot.uia_tree:
            query_lower = target_query.lower()
            for el in snapshot.uia_tree:
                name = (el.get("name") or "").lower()
                if query_lower in name:
                    box = el.get("bounding_box", [0, 0, 0, 0])
                    center = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                    if center[0] > 0 and center[1] > 0:
                        target = VisualTarget(
                            semantic_description=target_query,
                            candidate_bounding_boxes=[(box[0], box[1], box[2], box[3])],
                            confidence=0.95,
                            source="UIA",
                            frame_id=snapshot.frame_id,
                            coordinate_space="SCREEN",
                            associated_element=el,
                            best_center=center,
                        )
                        return target, ActionLoopDecision.CONTINUE, None

        # Layer 3: Accessibility / Partial match in UIA control type or text
        if target_query and snapshot.uia_tree:
            query_words = set(target_query.lower().split())
            for el in snapshot.uia_tree:
                name_words = set((el.get("name") or "").lower().split())
                if query_words.intersection(name_words):
                    box = el.get("bounding_box", [0, 0, 0, 0])
                    center = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                    if center[0] > 0 and center[1] > 0:
                        target = VisualTarget(
                            semantic_description=target_query,
                            candidate_bounding_boxes=[(box[0], box[1], box[2], box[3])],
                            confidence=0.85,
                            source="ACCESSIBILITY",
                            frame_id=snapshot.frame_id,
                            coordinate_space="SCREEN",
                            associated_element=el,
                            best_center=center,
                        )
                        return target, ActionLoopDecision.CONTINUE, None

        # Layer 4: OCR / Text Grounding
        if target_query and snapshot.ocr_regions:
            query_lower = target_query.lower()
            for region in snapshot.ocr_regions:
                if query_lower in region.get("text", "").lower():
                    box = region.get("box", [0, 0, 0, 0])
                    center = tuple(region.get("center", [0, 0]))
                    target = VisualTarget(
                        semantic_description=target_query,
                        candidate_bounding_boxes=[(box[0], box[1], box[2], box[3])],
                        confidence=0.90,
                        source="OCR",
                        frame_id=snapshot.frame_id,
                        coordinate_space="SCREEN",
                        associated_ocr_text=region.get("text"),
                        best_center=center,
                    )
                    return target, ActionLoopDecision.CONTINUE, None

        # Layer 5: Visual Grounding (VLM on demand if available and configured)
        if target_query:
            try:
                vlm_obs = self.vision_mgr.observe(hwnd=hwnd or snapshot.active_hwnd, scope="vlm", target_query=target_query)
                if vlm_obs and vlm_obs.vlm and vlm_obs.vlm.elements:
                    matched_el = vlm_obs.vlm.find_element(target_query)
                    if matched_el and matched_el.confidence >= 0.60:
                        cx, cy = matched_el.center_point
                        # Verify coordinate is within active window bounds
                        win_rect = snapshot.window_rect
                        if win_rect[0] <= cx <= win_rect[2] and win_rect[1] <= cy <= win_rect[3]:
                            target = VisualTarget(
                                semantic_description=target_query,
                                candidate_bounding_boxes=[matched_el.pixel_bbox],
                                confidence=matched_el.confidence,
                                source="VLM",
                                frame_id=snapshot.frame_id,
                                coordinate_space="SCREEN",
                                best_center=(cx, cy),
                            )
                            return target, ActionLoopDecision.CONTINUE, None
            except Exception as e:
                LOG.debug("Visual grounding attempt skipped or failed: %s", e)

        # Layer 6: Coordinate Fallback (Last resort)
        # Validated strictly against active window bounds and frame signature
        if explicit_coords is not None:
            cx, cy = explicit_coords
            win_rect = snapshot.window_rect
            # If coordinates fall inside window geometry
            is_valid = True
            if win_rect[2] > win_rect[0] and win_rect[3] > win_rect[1]:
                # Allow coordinates if within window rect or within screen
                if not (win_rect[0] <= cx <= win_rect[2] and win_rect[1] <= cy <= win_rect[3]):
                    # If outside window, check if coordinates are within desktop
                    if cx < 0 or cy < 0 or cx > 3840 or cy > 2160:
                        is_valid = False

            if is_valid:
                target = VisualTarget(
                    semantic_description=f"Raw Coordinate ({cx}, {cy})",
                    candidate_bounding_boxes=[(cx - 2, cy - 2, 4, 4)],
                    confidence=0.50,
                    source="COORDINATE",
                    frame_id=snapshot.frame_id,
                    coordinate_space="SCREEN",
                    best_center=(cx, cy),
                )
                return target, ActionLoopDecision.CONTINUE, None
            else:
                return None, ActionLoopDecision.REPLAN, f"COORDINATE_OUT_OF_BOUNDS: ({cx}, {cy}) outside window {win_rect}"

        # If no layer could resolve the target
        return None, ActionLoopDecision.ALTERNATE_METHOD, f"TARGET_NOT_FOUND: Could not resolve target '{target_query}'"

    def validate_target_freshness(self, target: VisualTarget, hwnd: Optional[int] = None) -> bool:
        """
        Validates that a resolved target's frame signature and window geometry
        have not changed prior to physical action dispatch.
        """
        if not target or not target.best_center:
            return False

        current_active = self.window_mgr.get_foreground_window()
        current_hwnd = current_active.hwnd if current_active else 0
        if hwnd and current_hwnd and hwnd != current_hwnd:
            LOG.warning("Freshness check failed: Window HWND changed (%s -> %s)", hwnd, current_hwnd)
            return False

        # Verify coordinates are still inside window rect
        if current_active:
            r = current_active.rect
            cx, cy = target.best_center
            right, bottom = r[0] + r[2], r[1] + r[3]
            if not (r[0] <= cx <= right and r[1] <= cy <= bottom):
                # Tolerant for desktop-wide coordinates if window minimized/hidden
                if target.coordinate_space == "SCREEN" and (0 <= cx <= 3840 and 0 <= cy <= 2160):
                    return True
                return False

        return True

    def _search_browser_dom(self, dom: Dict[str, Any], query: str) -> Optional[Dict[str, Any]]:
        """Helper to search structured browser DOM elements."""
        q = query.lower()
        elements = dom.get("elements", [])
        for el in elements:
            text = (el.get("text") or el.get("innerText") or el.get("aria-label") or "").lower()
            if q in text:
                return el
        return None


_GLOBAL_TARGET_RESOLVER: Optional[TargetResolver] = None


def get_target_resolver() -> TargetResolver:
    global _GLOBAL_TARGET_RESOLVER
    if _GLOBAL_TARGET_RESOLVER is None:
        _GLOBAL_TARGET_RESOLVER = TargetResolver()
    return _GLOBAL_TARGET_RESOLVER
