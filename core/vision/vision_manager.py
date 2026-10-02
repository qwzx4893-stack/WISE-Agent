"""
WISE Vision Manager (WISEVisionManager).
Central governor for perception and visual understanding across all 4 levels:
  Level 1: UI Tree Automation (UIA / MSAA, zero-pixel, zero-VLM)
  Level 2: On-Demand Screen & Window Capture (Win32 GDI)
  Level 3: Local OCR Engine (Text extraction from images)
  Level 4: Heavy Vision Model (VLM, on-demand only)
Strictly enforces zero background capture and zero VRAM retention in IDLE.
"""

from __future__ import annotations

import gc
import os
import sys
import time
import logging
import threading
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from core.vision.ui_tree import (
    UITreeExtractor,
    UIElementNode,
    get_ui_tree_extractor,
)
from core.vision.screen_capture import (
    ScreenCaptureEngine,
    ScreenCaptureResult,
    get_screen_capture_engine,
)
from core.vision.ocr_engine import (
    WindowsOCREngine,
    OCRResult,
    get_ocr_engine,
)
from core.vision.vlm_engine import (
    VisualObservationResult,
    VisualElement,
    BaseVLMProvider,
    get_vlm_provider,
    DPIHelper,
    VisualDiffEngine,
)
from core.resource.resource_manager import (
    get_resource_manager,
    ManagedWorker,
    WorkerPriority,
    WorkerStatus,
)
from core.state.state_machine import get_state_machine, WiseState

LOG = logging.getLogger("wise.vision_manager")


class PerceptionLevel(str, Enum):
    LEVEL_1_UI_TREE = "LEVEL_1_UI_TREE"
    LEVEL_2_SCREEN_CAPTURE = "LEVEL_2_SCREEN_CAPTURE"
    LEVEL_3_OCR = "LEVEL_3_OCR"
    LEVEL_4_HEAVY_VLM = "LEVEL_4_HEAVY_VLM"


@dataclass
class PerceptionObservation:
    level_used: PerceptionLevel
    ui_tree: Optional[UIElementNode] = None
    capture: Optional[ScreenCaptureResult] = None
    ocr: Optional[OCRResult] = None
    vlm: Optional[VisualObservationResult] = None
    semantic_summary: str = ""
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level_used": self.level_used.value,
            "ui_tree": self.ui_tree.to_dict() if self.ui_tree else None,
            "capture": self.capture.to_dict() if self.capture else None,
            "ocr": self.ocr.to_dict() if self.ocr else None,
            "vlm": self.vlm.to_dict() if self.vlm else None,
            "semantic_summary": self.semantic_summary,
            "error": self.error,
            "timestamp": self.timestamp,
            "duration_ms": round(self.duration_ms, 2),
        }

    def format_prompt_block(self) -> str:
        """Renders dense high-signal observation text for LLM prompts."""
        parts = [f"#### [Visual Perception - {self.level_used.value}]"]
        if self.semantic_summary:
            parts.append(f"**Visual Summary**: {self.semantic_summary}")
        if self.ui_tree:
            parts.append("**Active UI Structure**:")
            parts.append(self.ui_tree.format_text_tree(indent=1))
        if self.ocr and self.ocr.full_text:
            parts.append(f"**Detected Text (OCR)**: {self.ocr.full_text}")
        if self.vlm:
            parts.append(f"**VLM Context**: {self.vlm.visual_description}")
            if self.vlm.active_dialog:
                parts.append(f"**Active Modal Dialog**: {self.vlm.active_dialog}")
            if self.vlm.elements:
                parts.append(f"**Detected Visual Elements ({len(self.vlm.elements)})**:")
                for el in self.vlm.elements[:5]:
                    parts.append(f"  - [{el.category.value}] '{el.name}' at {el.center_point} (conf: {el.confidence})")
        return "\n".join(parts)


class WISEVisionManager:
    """Orchestrates demand-driven hierarchical vision and governs vision memory."""

    def __init__(self):
        self._lock = threading.RLock()
        self.ui_extractor = get_ui_tree_extractor()
        self.screen_capture = get_screen_capture_engine()
        self.ocr_engine = get_ocr_engine()
        self._vlm_provider: Optional[BaseVLMProvider] = None
        self._is_vlm_active = False

        # Register with WISEResourceManager
        self._register_with_resource_manager()

    def get_vlm_engine(self) -> BaseVLMProvider:
        if self._vlm_provider is None:
            self._vlm_provider = get_vlm_provider()
        return self._vlm_provider

    def set_vlm_engine(self, provider: BaseVLMProvider) -> None:
        with self._lock:
            self._vlm_provider = provider

    def _register_with_resource_manager(self) -> None:
        try:
            rm = get_resource_manager()
            worker = ManagedWorker(
                name="vision_engine",
                priority=WorkerPriority.NORMAL,
                is_essential_in_idle=False,
                start_fn=self.awaken,
                suspend_fn=self.sleep,
                resume_fn=self.awaken,
                stop_fn=self.sleep,
            )
            rm.register_worker(worker)
            LOG.info("Registered 'vision_engine' with WISEResourceManager.")
        except Exception as e:
            LOG.error("Failed to register vision_engine worker: %s", e)

    def awaken(self) -> bool:
        """Prepares vision subsystems for observation."""
        with self._lock:
            LOG.info("Vision Manager awakened on demand.")
            return True

    def sleep(self) -> bool:
        """Enforces zero-cost idle by releasing capture buffers and VLM memory."""
        with self._lock:
            LOG.info("Vision Manager entering IDLE: Flushing capture buffers and VRAM...")
            self.screen_capture.clear_buffers()
            self._is_vlm_active = False

            # Explicit garbage collection and CUDA cache release
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass
            return True

    def observe(
        self,
        hwnd: int = 0,
        scope: str = "auto",
        save_capture: bool = False,
        target_query: Optional[str] = None,
    ) -> PerceptionObservation:
        """Executes hierarchical perception:
        Level 1 (UI Tree) -> Level 2 (Capture) -> Level 3 (OCR) -> Level 4 (VLM).
        """
        t0 = time.perf_counter()

        with self._lock:
            # 1. Level 1: Structured UI Tree
            if scope in ("auto", "ui_tree"):
                tree = self.ui_extractor.get_window_ui_tree(hwnd)
                # If structured info contains interactive elements, satisfy request via Level 1
                if tree and (scope == "ui_tree" or tree.name or len(tree.children) > 0):
                    duration = (time.perf_counter() - t0) * 1000
                    return PerceptionObservation(
                        level_used=PerceptionLevel.LEVEL_1_UI_TREE,
                        ui_tree=tree,
                        semantic_summary=f"Active window: '{tree.name}' ({tree.control_type}, {len(tree.children)} child elements)",
                        duration_ms=duration,
                    )
                if scope == "ui_tree":
                    return PerceptionObservation(
                        level_used=PerceptionLevel.LEVEL_1_UI_TREE,
                        semantic_summary="UI accessibility tree is unavailable for the target window.",
                        error="No live UI Automation or Win32 element tree could be read.",
                        duration_ms=(time.perf_counter() - t0) * 1000,
                    )

            # 2. Level 2: On-Demand Screen Capture
            offset = (0, 0)
            window_size = (1920, 1080)
            if hwnd:
                try:
                    import ctypes
                    from ctypes import wintypes
                    r = wintypes.RECT()
                    if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
                        offset = (r.left, r.top)
                        window_size = (max(1, r.right - r.left), max(1, r.bottom - r.top))
                except Exception:
                    pass
                capture_res = self.screen_capture.capture_window(hwnd, save_to_disk=save_capture)
            else:
                capture_res = self.screen_capture.capture_full_screen(save_to_disk=save_capture)
                window_size = (capture_res.width, capture_res.height)

            if not capture_res.success or not capture_res.image_bytes:
                return PerceptionObservation(
                    level_used=PerceptionLevel.LEVEL_2_SCREEN_CAPTURE,
                    capture=capture_res,
                    semantic_summary="Live screen capture is unavailable.",
                    error=capture_res.error_reason or "Screen capture produced no pixels.",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

            if scope == "screen":
                duration = (time.perf_counter() - t0) * 1000
                return PerceptionObservation(
                    level_used=PerceptionLevel.LEVEL_2_SCREEN_CAPTURE,
                    capture=capture_res,
                    semantic_summary=f"Screen captured: {capture_res.width}x{capture_res.height} ({len(capture_res.image_bytes)} bytes)",
                    duration_ms=duration,
                )

            # 3. Level 3: Optical Character Recognition (OCR)
            ocr_res = None
            if capture_res.success and capture_res.image_bytes:
                ocr_res = self.ocr_engine.extract_text(capture_res.image_bytes, region_offset=offset)

            # If explicitly requesting OCR, or if OCR found text and scope is not explicitly VLM
            if scope == "ocr" or (scope == "auto" and ocr_res and ocr_res.full_text and not target_query):
                duration = (time.perf_counter() - t0) * 1000
                summary = f"Detected {len(ocr_res.blocks)} visual text blocks via OCR."
                return PerceptionObservation(
                    level_used=PerceptionLevel.LEVEL_3_OCR,
                    capture=capture_res,
                    ocr=ocr_res,
                    semantic_summary=summary,
                    duration_ms=duration,
                )

            # 4. Level 4: Heavy VLM Escalation (on demand)
            # Escalates to VLM if scope is "vlm" OR if OCR found no matching target in custom/canvas UI
            if scope in ("auto", "vlm"):
                self._is_vlm_active = True
                try:
                    vlm_engine = self.get_vlm_engine()
                    vlm_res = vlm_engine.analyze_image(
                        image_bytes=capture_res.image_bytes,
                        prompt=f"Inspect desktop window (HWND: {hwnd}) for UI elements, custom controls, and visual states.",
                        window_origin=offset,
                        window_size=window_size,
                        target_query=target_query,
                    )
                finally:
                    self._is_vlm_active = False

                duration = (time.perf_counter() - t0) * 1000
                return PerceptionObservation(
                    level_used=PerceptionLevel.LEVEL_4_HEAVY_VLM,
                    capture=capture_res,
                    ocr=ocr_res,
                    vlm=vlm_res,
                    semantic_summary=vlm_res.visual_description,
                    duration_ms=duration,
                )

            duration = (time.perf_counter() - t0) * 1000
            return PerceptionObservation(
                level_used=PerceptionLevel.LEVEL_3_OCR if ocr_res else PerceptionLevel.LEVEL_2_SCREEN_CAPTURE,
                capture=capture_res,
                ocr=ocr_res,
                semantic_summary="Visual observation complete.",
                duration_ms=duration,
            )

    def locate_visual_element(self, query: str, hwnd: int = 0) -> Optional[VisualElement]:
        """Locates an element visually via Level 4 VLM."""
        obs = self.observe(hwnd=hwnd, scope="vlm", target_query=query)
        if obs.vlm:
            return obs.vlm.find_element(query)
        return None

    def verify_visual_change(
        self,
        before_bytes: bytes,
        after_bytes: bytes,
        crop_box: Optional[Tuple[int, int, int, int]] = None,
        min_ratio: float = 0.005,
    ) -> bool:
        """Computes pixel diff between two captures to verify visual action execution."""
        ratio = VisualDiffEngine.compute_change_ratio(before_bytes, after_bytes, crop_box=crop_box)
        LOG.info("Visual diff change ratio: %.4f (threshold: %.4f)", ratio, min_ratio)
        return ratio >= min_ratio

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "is_vlm_active": self._is_vlm_active,
                "has_cached_captures": self.screen_capture._last_capture is not None,
            }


# Singleton
_GLOBAL_VISION_MGR: Optional[WISEVisionManager] = None
_VM_LOCK = threading.Lock()


def get_vision_manager() -> WISEVisionManager:
    global _GLOBAL_VISION_MGR
    if _GLOBAL_VISION_MGR is None:
        with _VM_LOCK:
            if _GLOBAL_VISION_MGR is None:
                _GLOBAL_VISION_MGR = WISEVisionManager()
    return _GLOBAL_VISION_MGR
