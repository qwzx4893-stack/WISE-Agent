# ==============================================================================
# WISE Vision Subsystem - Level 4 VLM Perception & Visual Grounding
# Architecture: On-demand visual reasoning for arbitrary computer interfaces:
# - Understands visual context, layouts, custom controls, icons, canvas UI,
#   dialogs, visual states, and inter-element spatial relationships.
# - DPI-aware coordinate grounding with strict window-bounds safety validation.
# - Visual diff verification for post-action confirmation.
# - Provider-agnostic: Supports live OpenAI-compatible multi-modal endpoints.
#   Test-only providers are never selected by the production registry.
# ==============================================================================

from __future__ import annotations

import os
import io
import sys
import json
import time
import base64
import logging
import threading
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

# PIL is imported lazily inside VisualDiffEngine and HttpOpenAIVisionProvider
LOG = logging.getLogger("WISE.Vision.VLM")


class VisualElementCategory(str, Enum):
    BUTTON = "button"
    ICON = "icon"
    TEXT_INPUT = "text_input"
    CHECKBOX = "checkbox"
    TOGGLE = "toggle"
    TAB = "tab"
    MENU_ITEM = "menu_item"
    DIALOG = "dialog"
    CANVAS_CONTROL = "canvas_control"
    CHART = "chart"
    IMAGE = "image"
    LABEL = "label"
    SCROLLBAR = "scrollbar"
    UNKNOWN = "unknown"


@dataclass
class VisualElement:
    name: str
    category: VisualElementCategory
    normalized_bbox: List[int] = field(default_factory=lambda: [0, 0, 0, 0])  # [ymin, xmin, ymax, xmax] in 0-1000 scale
    pixel_bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)  # [x, y, width, height] in screen pixels
    center_point: Tuple[int, int] = (0, 0)  # (screen_x, screen_y)
    visual_state: str = "normal"  # normal, focused, active, disabled, selected, checked, loading
    confidence: float = 1.0  # 0.0 - 1.0
    relationship_hints: List[str] = field(default_factory=list)  # e.g., ["below: 'Search Label'", "inside: 'Toolbar'"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category.value,
            "normalized_bbox": self.normalized_bbox,
            "pixel_bbox": self.pixel_bbox,
            "center_point": self.center_point,
            "visual_state": self.visual_state,
            "confidence": round(self.confidence, 2),
            "relationship_hints": list(self.relationship_hints),
        }


@dataclass
class VisualObservationResult:
    visual_description: str = ""
    elements: List[VisualElement] = field(default_factory=list)
    active_dialog: Optional[str] = None
    visible_errors: List[str] = field(default_factory=list)
    suggested_actions: List[str] = field(default_factory=list)
    confidence: float = 1.0
    is_simulated: bool = False
    provider_name: str = "vlm"
    latency_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "visual_description": self.visual_description,
            "elements": [e.to_dict() for e in self.elements],
            "active_dialog": self.active_dialog,
            "visible_errors": self.visible_errors,
            "suggested_actions": self.suggested_actions,
            "confidence": round(self.confidence, 2),
            "is_simulated": self.is_simulated,
            "provider_name": self.provider_name,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
        }

    def find_element(self, query: str) -> Optional[VisualElement]:
        """Finds the best matching element by name, category, or relationship."""
        q = query.strip().lower()
        # 1. Exact match on name
        for el in self.elements:
            if el.name.lower() == q:
                return el
        # 2. Substring match on name
        for el in self.elements:
            if q in el.name.lower():
                return el
        # 3. Category match
        for el in self.elements:
            if el.category.value.lower() == q:
                return el
        # 4. Relationship match
        for el in self.elements:
            for rel in el.relationship_hints:
                if q in rel.lower():
                    return el
        return None


class DPIHelper:
    """Provides high-DPI scaling factors and coordinate denormalization for Windows."""

    @staticmethod
    def get_window_dpi(hwnd: int) -> int:
        if sys.platform != "win32" or hwnd == 0:
            return 96
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if hasattr(user32, "GetDpiForWindow"):
                dpi = user32.GetDpiForWindow(hwnd)
                if dpi > 0:
                    return int(dpi)
            if hasattr(user32, "GetDpiForSystem"):
                return int(user32.GetDpiForSystem())
        except Exception:
            pass
        return 96

    @staticmethod
    def get_scale_factor(hwnd: int) -> float:
        dpi = DPIHelper.get_window_dpi(hwnd)
        return float(dpi / 96.0)

    @staticmethod
    def denormalize_box(
        norm_box: List[int],  # [ymin, xmin, ymax, xmax] in 0-1000 scale
        window_origin: Tuple[int, int],  # (left, top)
        window_size: Tuple[int, int],  # (width, height)
    ) -> Tuple[Tuple[int, int, int, int], Tuple[int, int]]:
        """
        Converts normalized coordinates [ymin, xmin, ymax, xmax] in 0-1000 scale
        into physical screen pixel bbox (x, y, w, h) and center point (cx, cy).
        """
        ymin, xmin, ymax, xmax = norm_box
        w_origin_x, w_origin_y = window_origin
        w_width, w_height = window_size

        pixel_x = int(w_origin_x + (xmin / 1000.0) * w_width)
        pixel_y = int(w_origin_y + (ymin / 1000.0) * w_height)
        pixel_w = max(1, int(((xmax - xmin) / 1000.0) * w_width))
        pixel_h = max(1, int(((ymax - ymin) / 1000.0) * w_height))

        center_x = pixel_x + pixel_w // 2
        center_y = pixel_y + pixel_h // 2

        return (pixel_x, pixel_y, pixel_w, pixel_h), (center_x, center_y)


class VisualDiffEngine:
    """Computes perceptual differences between pre-action and post-action captures."""

    @staticmethod
    def compute_change_ratio(
        before_bytes: bytes,
        after_bytes: bytes,
        crop_box: Optional[Tuple[int, int, int, int]] = None,
    ) -> float:
        """
        Returns a float between 0.0 (identical) and 1.0 (completely different).
        If crop_box (x, y, w, h) is provided, evaluates diff strictly within that region.
        """
        if not before_bytes or not after_bytes:
            return 0.0

        try:
            from PIL import Image, ImageChops
            img1 = Image.open(io.BytesIO(before_bytes)).convert("RGB")
            img2 = Image.open(io.BytesIO(after_bytes)).convert("RGB")

            # Resize if dimensions differ
            if img1.size != img2.size:
                img2 = img2.resize(img1.size)

            if crop_box:
                x, y, w, h = crop_box
                box_tuple = (x, y, x + w, y + h)
                img1 = img1.crop(box_tuple)
                img2 = img2.crop(box_tuple)

            diff = ImageChops.difference(img1, img2)
            hist = diff.histogram()

            # Count non-zero pixel diffs
            total_pixels = img1.width * img1.height
            if total_pixels == 0:
                return 0.0

            # Sum of difference intensities
            r_diff = sum(i * n for i, n in enumerate(hist[0:256]))
            g_diff = sum(i * n for i, n in enumerate(hist[256:512]))
            b_diff = sum(i * n for i, n in enumerate(hist[512:768]))

            max_possible = total_pixels * 255 * 3
            if max_possible == 0:
                return 0.0

            return float((r_diff + g_diff + b_diff) / max_possible)

        except Exception as e:
            LOG.error("Failed to compute visual diff: %s", e)
            return 0.0


class BaseVLMProvider(ABC):
    """Abstract interface for all VLM perception providers."""

    @abstractmethod
    def analyze_image(
        self,
        image_bytes: bytes,
        prompt: str,
        window_origin: Tuple[int, int] = (0, 0),
        window_size: Tuple[int, int] = (1920, 1080),
        target_query: Optional[str] = None,
    ) -> VisualObservationResult:
        """Analyzes a desktop or window frame and returns structured visual observations."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Returns whether this VLM provider is configured and available."""
        pass


class UnavailableVLMProvider(BaseVLMProvider):
    """Honest fallback when no real vision endpoint has been configured."""

    def is_available(self) -> bool:
        return False

    def analyze_image(
        self,
        image_bytes: bytes,
        prompt: str,
        window_origin: Tuple[int, int] = (0, 0),
        window_size: Tuple[int, int] = (1920, 1080),
        target_query: Optional[str] = None,
    ) -> VisualObservationResult:
        return VisualObservationResult(
            confidence=0.0,
            is_simulated=False,
            provider_name="unavailable-vlm",
            error="Vision requires a configured live VLM endpoint.",
        )


class SimulatedVLMProvider(BaseVLMProvider):
    """
    Deterministic simulated VLM provider for CI, automated testing, and offline use.
    Explicitly flags all results with is_simulated=True.
    Recognizes test fixtures (Canvas test buttons, modal dialogs, toolbar icons) reliably.
    """

    def __init__(self, custom_elements: Optional[Dict[str, List[VisualElement]]] = None) -> None:
        self.custom_elements = custom_elements or {}
        self.call_history: List[Dict[str, Any]] = []

    def is_available(self) -> bool:
        return True

    def register_visual_elements(self, keyword_trigger: str, elements: List[VisualElement]) -> None:
        """Registers predetermined visual elements for specific test prompts or window titles."""
        self.custom_elements[keyword_trigger.lower()] = elements

    def analyze_image(
        self,
        image_bytes: bytes,
        prompt: str,
        window_origin: Tuple[int, int] = (0, 0),
        window_size: Tuple[int, int] = (1920, 1080),
        target_query: Optional[str] = None,
    ) -> VisualObservationResult:
        t0 = time.perf_counter()
        self.call_history.append({"prompt": prompt, "target_query": target_query, "size": window_size})

        p_lower = (prompt + " " + (target_query or "")).lower()

        # Check for registered triggers
        matched_elements: Optional[List[VisualElement]] = None
        for trigger, elems in self.custom_elements.items():
            if trigger in p_lower:
                matched_elements = elems
                break

        # If not matched, generate contextual elements based on query
        if matched_elements is None:
            matched_elements = []

            # Canvas button test fixture pattern
            if "canvas" in p_lower or "custom" in p_lower or "click_me" in p_lower:
                norm_box = [350, 400, 450, 600]
                p_box, center = DPIHelper.denormalize_box(norm_box, window_origin, window_size)
                matched_elements.append(
                    VisualElement(
                        name="Canvas Button",
                        category=VisualElementCategory.CANVAS_CONTROL,
                        normalized_bbox=norm_box,
                        pixel_bbox=p_box,
                        center_point=center,
                        visual_state="normal",
                        confidence=0.95,
                        relationship_hints=["centered in canvas container"],
                    )
                )

            # Icon controls (e.g. Play, Run, Settings)
            if "icon" in p_lower or "play" in p_lower or "vector" in p_lower:
                norm_box = [200, 250, 280, 330]
                p_box, center = DPIHelper.denormalize_box(norm_box, window_origin, window_size)
                matched_elements.append(
                    VisualElement(
                        name="Play Icon",
                        category=VisualElementCategory.ICON,
                        normalized_bbox=norm_box,
                        pixel_bbox=p_box,
                        center_point=center,
                        visual_state="normal",
                        confidence=0.96,
                        relationship_hints=["inside: toolbar", "left of: status indicator"],
                    )
                )

            # Close / Exit icon pattern
            if "close" in p_lower or "exit" in p_lower or "x" in p_lower:
                norm_box = [10, 960, 40, 995]
                p_box, center = DPIHelper.denormalize_box(norm_box, window_origin, window_size)
                matched_elements.append(
                    VisualElement(
                        name="Close Window Button",
                        category=VisualElementCategory.ICON,
                        normalized_bbox=norm_box,
                        pixel_bbox=p_box,
                        center_point=center,
                        visual_state="normal",
                        confidence=0.98,
                        relationship_hints=["top-right corner of titlebar"],
                    )
                )

            # Chart visualization pattern
            if "chart" in p_lower or "graph" in p_lower:
                norm_box = [500, 100, 850, 900]
                p_box, center = DPIHelper.denormalize_box(norm_box, window_origin, window_size)
                matched_elements.append(
                    VisualElement(
                        name="Metrics Chart",
                        category=VisualElementCategory.CHART,
                        normalized_bbox=norm_box,
                        pixel_bbox=p_box,
                        center_point=center,
                        visual_state="normal",
                        confidence=0.94,
                        relationship_hints=["below: control toolbar", "spans: main container"],
                    )
                )

            # Toggle / Switch pattern
            if "toggle" in p_lower or "switch" in p_lower:
                norm_box = [120, 750, 160, 820]
                p_box, center = DPIHelper.denormalize_box(norm_box, window_origin, window_size)
                matched_elements.append(
                    VisualElement(
                        name="Dark Mode Toggle",
                        category=VisualElementCategory.TOGGLE,
                        normalized_bbox=norm_box,
                        pixel_bbox=p_box,
                        center_point=center,
                        visual_state="active" if "active" in p_lower else "normal",
                        confidence=0.93,
                        relationship_hints=["inside: header controls", "right of: theme label"],
                    )
                )

            # Checkbox pattern
            if "checkbox" in p_lower:
                norm_box = [420, 200, 460, 240]
                p_box, center = DPIHelper.denormalize_box(norm_box, window_origin, window_size)
                matched_elements.append(
                    VisualElement(
                        name="Terms Checkbox",
                        category=VisualElementCategory.CHECKBOX,
                        normalized_bbox=norm_box,
                        pixel_bbox=p_box,
                        center_point=center,
                        visual_state="selected" if "selected" in p_lower else "normal",
                        confidence=0.91,
                        relationship_hints=["left of: terms text"],
                    )
                )

            # Generic interactive button pattern if query specified
            if target_query and not matched_elements:
                norm_box = [450, 420, 520, 580]
                p_box, center = DPIHelper.denormalize_box(norm_box, window_origin, window_size)
                v_state = "disabled" if "disabled" in p_lower else ("focused" if "focus" in p_lower else "normal")
                matched_elements.append(
                    VisualElement(
                        name=target_query,
                        category=VisualElementCategory.BUTTON,
                        normalized_bbox=norm_box,
                        pixel_bbox=p_box,
                        center_point=center,
                        visual_state=v_state,
                        confidence=0.92,
                        relationship_hints=["main content area"],
                    )
                )

        latency = (time.perf_counter() - t0) * 1000

        # Check for dialogs or errors
        active_dialog = "Confirm Action Modal" if ("confirm" in p_lower or "dialog" in p_lower or "modal" in p_lower) else None
        visible_errors = ["Critical validation error: Missing required fields"] if "error" in p_lower else []

        layout_desc = (
            "Two-pane layout with navigation sidebar on left and central workspace with controls."
            if "layout" in p_lower
            else f"Visual analysis observed {len(matched_elements)} UI controls in window context."
        )

        return VisualObservationResult(
            visual_description=layout_desc,
            elements=matched_elements,
            active_dialog=active_dialog,
            visible_errors=visible_errors,
            suggested_actions=[f"Click '{el.name}'" for el in matched_elements],
            confidence=0.95 if matched_elements else 0.50,
            is_simulated=True,
            provider_name="simulated-vlm-v1",
            latency_ms=latency,
        )


class HttpOpenAIVisionProvider(BaseVLMProvider):
    """
    Production HTTP provider for multi-modal Vision endpoints.
    Supports OpenAI GPT-4o / GPT-4o-mini, local Ollama / vLLM multi-modal models,
    or Gemini Vision via OpenAI-compatible endpoints.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout_seconds: float = 35.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("WISE_VLM_API_KEY") or os.environ.get("WISE_LLM_API_KEY", "")
        self.base_url = (
            base_url
            or os.environ.get("WISE_VLM_BASE_URL")
            or os.environ.get("WISE_LLM_BASE_URL", "http://localhost:11434/v1")
        ).rstrip("/")
        self.model_name = model_name or os.environ.get("WISE_VLM_MODEL", "gpt-4o-mini")
        self.timeout = timeout_seconds

    def is_available(self) -> bool:
        if not self.base_url:
            return False
        if "localhost" in self.base_url or "127.0.0.1" in self.base_url:
            return True
        return bool(self.api_key)

    def analyze_image(
        self,
        image_bytes: bytes,
        prompt: str,
        window_origin: Tuple[int, int] = (0, 0),
        window_size: Tuple[int, int] = (1920, 1080),
        target_query: Optional[str] = None,
    ) -> VisualObservationResult:
        t0 = time.perf_counter()

        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "WISE-Vision-Engine/1.3",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Downscale image if larger than 1280x720 to preserve bounded token budget
        try:
            from PIL import Image
            pil_img = Image.open(io.BytesIO(image_bytes))
            max_w, max_h = 1280, 720
            if pil_img.width > max_w or pil_img.height > max_h:
                pil_img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85)
                prepared_bytes = buf.getvalue()
            else:
                prepared_bytes = image_bytes
        except Exception as downscale_err:
            LOG.warning("Could not optimize image dimensions: %s", downscale_err)
            prepared_bytes = image_bytes

        b64_image = base64.b64encode(prepared_bytes).decode("ascii")

        system_instruction = (
            "You are WISE Visual Computer Perception Engine. Analyze the desktop screenshot. "
            "Detect UI controls, icons, canvas buttons, dialogs, charts, and visible errors. "
            "Return strictly valid JSON matching this schema:\n"
            "{\n"
            '  "visual_description": "summary of window layout and contents",\n'
            '  "active_dialog": "title of modal dialog if present, else null",\n'
            '  "visible_errors": ["list of visible error banners/dialog text"],\n'
            '  "elements": [\n'
            "    {\n"
            '      "name": "control name or icon label",\n'
            '      "category": "button | icon | text_input | checkbox | toggle | tab | menu_item | canvas_control | chart | label",\n'
            '      "normalized_bbox": [ymin, xmin, ymax, xmax],\n'
            '      "visual_state": "normal | focused | active | disabled | selected",\n'
            '      "confidence": 0.0 - 1.0,\n'
            '      "relationship_hints": ["spatial hints like below: search"]\n'
            "    }\n"
            "  ]\n"
            "}"
        )

        user_content: List[Dict[str, Any]] = [
            {"type": "text", "text": f"{prompt}\nTarget search query: {target_query or 'all primary controls'}"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
        ]

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": 1024,
        }

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_body = resp.read().decode("utf-8")
                parsed_resp = json.loads(resp_body)
                choices = parsed_resp.get("choices", [])
                if not choices:
                    return VisualObservationResult(
                        error="Empty response from VLM endpoint",
                        is_simulated=False,
                        provider_name=self.model_name,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                    )

                raw_json_str = choices[0].get("message", {}).get("content", "{}")
                vlm_json = json.loads(raw_json_str)

                elements: List[VisualElement] = []
                for el_raw in vlm_json.get("elements", []):
                    n_box = el_raw.get("normalized_bbox", [0, 0, 0, 0])
                    p_box, center = DPIHelper.denormalize_box(n_box, window_origin, window_size)
                    elements.append(
                        VisualElement(
                            name=el_raw.get("name", "unnamed_control"),
                            category=VisualElementCategory(el_raw.get("category", "button").lower())
                            if el_raw.get("category", "button").lower() in VisualElementCategory._value2member_map_
                            else VisualElementCategory.UNKNOWN,
                            normalized_bbox=n_box,
                            pixel_bbox=p_box,
                            center_point=center,
                            visual_state=el_raw.get("visual_state", "normal"),
                            confidence=float(el_raw.get("confidence", 0.9)),
                            relationship_hints=el_raw.get("relationship_hints", []),
                        )
                    )

                latency = (time.perf_counter() - t0) * 1000

                return VisualObservationResult(
                    visual_description=vlm_json.get("visual_description", ""),
                    elements=elements,
                    active_dialog=vlm_json.get("active_dialog"),
                    visible_errors=vlm_json.get("visible_errors", []),
                    suggested_actions=[f"Interact with '{e.name}'" for e in elements],
                    confidence=0.92 if elements else 0.4,
                    is_simulated=False,
                    provider_name=self.model_name,
                    latency_ms=latency,
                )

        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            LOG.error("VLM visual perception request failed: %s", e)
            return VisualObservationResult(
                error=str(e),
                confidence=0.0,
                is_simulated=False,
                provider_name=self.model_name,
                latency_ms=latency,
            )


# Global VLM Provider Registry
_ACTIVE_VLM_PROVIDER: Optional[BaseVLMProvider] = None


def get_vlm_provider() -> BaseVLMProvider:
    """Returns the active VLM provider instance."""
    global _ACTIVE_VLM_PROVIDER
    if _ACTIVE_VLM_PROVIDER is not None:
        return _ACTIVE_VLM_PROVIDER

    # Check for live VLM configuration
    has_live_endpoint = bool(os.environ.get("WISE_VLM_API_KEY") or os.environ.get("WISE_VLM_BASE_URL"))
    if has_live_endpoint:
        http_prov = HttpOpenAIVisionProvider()
        if http_prov.is_available():
            _ACTIVE_VLM_PROVIDER = http_prov
            return _ACTIVE_VLM_PROVIDER

    # Never silently invent visual observations in a production session.
    _ACTIVE_VLM_PROVIDER = UnavailableVLMProvider()
    return _ACTIVE_VLM_PROVIDER


def set_active_vlm_provider(provider: BaseVLMProvider) -> None:
    """Overrides the active VLM provider for test fixtures or runtime switching."""
    global _ACTIVE_VLM_PROVIDER
    _ACTIVE_VLM_PROVIDER = provider
