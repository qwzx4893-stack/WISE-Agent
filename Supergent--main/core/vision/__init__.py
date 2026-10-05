"""WISE Vision & Hierarchical Perception Subsystem."""

from .ui_tree import (
    BoundingBox,
    UIElementNode,
    UITreeExtractor,
    get_ui_tree_extractor,
)
from .screen_capture import (
    ScreenCaptureResult,
    ScreenCaptureEngine,
    get_screen_capture_engine,
)
from .ocr_engine import (
    OCRTextBlock,
    OCRResult,
    WindowsOCREngine,
    get_ocr_engine,
)
from .vlm_engine import (
    VisualElementCategory,
    VisualElement,
    VisualObservationResult,
    BaseVLMProvider,
    SimulatedVLMProvider,
    HttpOpenAIVisionProvider,
    get_vlm_provider,
    set_active_vlm_provider,
    DPIHelper,
    VisualDiffEngine,
)
from .vision_manager import (
    PerceptionLevel,
    PerceptionObservation,
    WISEVisionManager,
    get_vision_manager,
)

__all__ = [
    "BoundingBox",
    "UIElementNode",
    "UITreeExtractor",
    "get_ui_tree_extractor",
    "ScreenCaptureResult",
    "ScreenCaptureEngine",
    "get_screen_capture_engine",
    "OCRTextBlock",
    "OCRResult",
    "WindowsOCREngine",
    "get_ocr_engine",
    "VisualElementCategory",
    "VisualElement",
    "VisualObservationResult",
    "BaseVLMProvider",
    "SimulatedVLMProvider",
    "HttpOpenAIVisionProvider",
    "get_vlm_provider",
    "set_active_vlm_provider",
    "DPIHelper",
    "VisualDiffEngine",
    "PerceptionLevel",
    "PerceptionObservation",
    "WISEVisionManager",
    "get_vision_manager",
]
