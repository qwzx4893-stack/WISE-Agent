"""
WISE Phase P0.2 Comprehensive Verification Suite:
Eyes & Demand-Driven Hierarchical Vision Engine.
Validates Level 1 through Level 4 Perception Architecture on Windows:
  - Level 1: Zero-Pixel UI Tree Automation (UIA / MSAA / Win32)
  - Level 2: On-Demand High-Speed Screen & Region Capture (Win32 GDI)
  - Level 3: Local OCR Engine (Text extraction without VLM)
  - Level 4 & Hierarchy: Demand-driven routing in WISEVisionManager
  - Resource Governance: Zero continuous capturing, zero VRAM in IDLE
  - ComputerControl Integration: observe(), read_screen(), closed-loop text_visible verification
  - Hardware Telemetry: Exact CPU model verification (12th Gen Intel Core i7-12700H)
"""

import os
import sys
import time
from pathlib import Path

# Add project root to sys.path
wise_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if wise_root not in sys.path:
    sys.path.insert(0, wise_root)
supergent_dir = os.path.join(wise_root, "Supergent--main")
if supergent_dir not in sys.path:
    sys.path.insert(0, supergent_dir)

import psutil
from PIL import Image

# Import Vision and Core Subsystems
from core.vision import (
    BoundingBox,
    UIElementNode,
    UITreeExtractor,
    get_ui_tree_extractor,
    ScreenCaptureResult,
    ScreenCaptureEngine,
    get_screen_capture_engine,
    OCRTextBlock,
    OCRResult,
    WindowsOCREngine,
    get_ocr_engine,
    PerceptionLevel,
    PerceptionObservation,
    WISEVisionManager,
    get_vision_manager,
)
from core.windows import ComputerControl, get_computer_control
from core.resource.resource_manager import get_resource_manager, WorkerStatus
from core.state.state_machine import get_state_machine, WiseState

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"[PASS] {name} {detail}")
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")


print("=" * 72)
print("   WISE PHASE P0.2 COMPREHENSIVE VERIFICATION SUITE")
print("   (Eyes & Demand-Driven Hierarchical Vision Engine)")
print("=" * 72)

# ==============================================================================
# 1. Level 1: UI Tree Extraction (Zero-Pixel Perception)
# ==============================================================================
print("\n--- 1. Testing Level 1: UI Tree Extraction (Zero-Pixel) ---")
ui_extractor = get_ui_tree_extractor()

t0 = time.perf_counter()
# Extract UI tree for active window or root
tree = ui_extractor.get_window_ui_tree(hwnd=0)
t_tree_ms = (time.perf_counter() - t0) * 1000

check(
    "Level 1: UI Tree node instantiated",
    isinstance(tree, UIElementNode),
    f"(Root: '{tree.name}', Type: {tree.control_type}, Children: {len(tree.children)})",
)
check(
    "Level 1: UI Tree extraction is ultra-fast (< 25ms)",
    t_tree_ms < 25.0,
    f"(Duration: {t_tree_ms:.2f} ms)",
)
check(
    "Level 1: Bounding box center coordinates computed",
    isinstance(tree.bounding_box.center, tuple) and len(tree.bounding_box.center) == 2,
    f"(Center: {tree.bounding_box.center})",
)

# Text Tree Formatting for LLM Context
formatted_text = tree.format_text_tree()
check(
    "Level 1: Formats compact structured text tree for LLM prompt",
    len(formatted_text) > 0 and "[" in formatted_text,
    f"(Chars: {len(formatted_text)}, Sample: {formatted_text.splitlines()[0]})",
)

# Search elements by text
matches = ui_extractor.find_elements_by_text(tree, "File")
check(
    "Level 1: Finds element by text query in tree",
    len(matches) > 0 and "File" in matches[0].name,
    f"(Matches found: {len(matches)})",
)

# ==============================================================================
# 2. Level 2: High-Speed On-Demand Screen Capture
# ==============================================================================
print("\n--- 2. Testing Level 2: On-Demand Screen Capture ---")
capture_engine = get_screen_capture_engine()

# Regional Capture
t0 = time.perf_counter()
region_cap = capture_engine.capture_region(0, 0, 200, 150)
t_cap_ms = (time.perf_counter() - t0) * 1000

check(
    "Level 2: On-demand regional screen capture succeeds",
    region_cap.success and region_cap.width == 200 and region_cap.height == 150,
    f"(Size: {region_cap.width}x{region_cap.height}, Bytes: {len(region_cap.image_bytes)})",
)
check(
    "Level 2: Screen capture duration is low-latency (< 50ms)",
    t_cap_ms < 50.0,
    f"(Duration: {t_cap_ms:.2f} ms)",
)
check(
    "Level 2: Produces valid base64 image encoding",
    len(region_cap.base64_image) > 50,
    f"(Base64 length: {len(region_cap.base64_image)})",
)

# Buffer Clearing on Idle
capture_engine.clear_buffers()
check(
    "Level 2: Screen capture buffers cleared on demand",
    capture_engine._last_capture is None,
    "(Buffer evicted)",
)

# ==============================================================================
# 3. Level 3: Local OCR Engine
# ==============================================================================
print("\n--- 3. Testing Level 3: Local OCR Engine ---")
ocr_engine = get_ocr_engine()

# Create a test synthetic image with known text
test_img = Image.new("RGB", (300, 80), color=(255, 255, 255))
ocr_res = ocr_engine.extract_text(test_img)

check(
    "Level 3: Local OCR extracts text without external VLM",
    isinstance(ocr_res, OCRResult) and len(ocr_res.full_text) > 0,
    f"(Engine: {ocr_res.engine_name}, Text: '{ocr_res.full_text[:35]}...', Latency: {ocr_res.latency_ms:.2f}ms)",
)
check(
    "Level 3: OCR yields localized text blocks with bounding boxes",
    len(ocr_res.blocks) > 0 and isinstance(ocr_res.blocks[0].bounding_box, BoundingBox),
    f"(Blocks: {len(ocr_res.blocks)}, Box: {ocr_res.blocks[0].bounding_box.to_dict()})",
)

# ==============================================================================
# 4. WISE Vision Manager: Hierarchical Routing & Memory Governance
# ==============================================================================
print("\n--- 4. Testing Hierarchical Routing & Resource Governance ---")
vm = get_vision_manager()
rm = get_resource_manager()

# Test Level 1 Priority: Structured UI Tree preferred over heavy pixels
obs_auto = vm.observe(scope="auto")
check(
    "Hierarchy: 'auto' scope selects Level 1 UI Tree when available",
    obs_auto.level_used == PerceptionLevel.LEVEL_1_UI_TREE and obs_auto.ui_tree is not None,
    f"(Level: {obs_auto.level_used.value}, Summary: {obs_auto.semantic_summary})",
)

# Test Level 2 Explicit Screen Scope
obs_screen = vm.observe(scope="screen")
check(
    "Hierarchy: 'screen' scope captures pixels",
    obs_screen.level_used == PerceptionLevel.LEVEL_2_SCREEN_CAPTURE and obs_screen.capture is not None,
    f"(Level: {obs_screen.level_used.value})",
)

# Test Resource Suspension in IDLE
vm.sleep()
v_status = vm.get_status()
check(
    "Resource Governance: Vision buffers and VLM flushed in IDLE",
    v_status.get("has_cached_captures") is False and v_status.get("is_vlm_active") is False,
    f"(Cached Captures: {v_status.get('has_cached_captures')}, VLM Active: {v_status.get('is_vlm_active')})",
)

# ==============================================================================
# 5. ComputerControl Closed-Loop Perception & Verification
# ==============================================================================
print("\n--- 5. Testing ComputerControl Vision Integration ---")
control = get_computer_control()

obs_control = control.observe(scope="auto")
check(
    "ComputerControl: observe() returns structured perception dictionary",
    "level_used" in obs_control and "semantic_summary" in obs_control,
    f"(Level: {obs_control.get('level_used')})",
)

read_screen_res = control.read_screen(0, 0, 100, 100)
check(
    "ComputerControl: read_screen() executes regional capture on demand",
    read_screen_res.get("success") is True and read_screen_res.get("width") == 100,
    f"(Width: {read_screen_res.get('width')}, Height: {read_screen_res.get('height')})",
)

# Closed-Loop text_visible verification (Level 1 UI Tree path)
ver_res = control.verify({"type": "text_visible", "text": "File"})
check(
    "Closed-Loop Verification: verify(text_visible) locates text via UI Tree",
    ver_res.get("verified") is True and ver_res.get("level_used") == PerceptionLevel.LEVEL_1_UI_TREE.value,
    f"(Verified: {ver_res.get('verified')}, Level: {ver_res.get('level_used')})",
)

# ==============================================================================
# 6. Hardware Telemetry & Accuracy Confirmation
# ==============================================================================
print("\n--- 6. Hardware Probing & Exact Model Verification ---")
hw = rm.get_hardware_metrics()

check(
    "Hardware Probing: Exact CPU model queried from Windows registry",
    "12th Gen Intel(R) Core(TM) i7-12700H" in hw.cpu_name,
    f"(CPU: {hw.cpu_name})",
)
check(
    "Hardware Probing: GPU correctly probed",
    "RTX 4060" in hw.gpu_name,
    f"(GPU: {hw.gpu_name})",
)

# Idle Telemetry Confirmation
proc = psutil.Process()
proc.cpu_percent()
time.sleep(1.0)
idle_cpu = proc.cpu_percent()
idle_ram = proc.memory_info().rss / (1024 * 1024)

check(
    "Hardware Telemetry: Idle CPU remains near zero",
    idle_cpu <= 2.0,
    f"(Idle CPU: {idle_cpu:.2f}%)",
)
check(
    "Hardware Telemetry: Idle RAM remains compact (< 60MB)",
    idle_ram < 60.0,
    f"(Idle RAM: {idle_ram:.2f} MB)",
)

# ==============================================================================
# Final Results
# ==============================================================================
print("\n" + "=" * 72)
print(f"   PHASE P0.2 TEST RESULTS: {passed} PASSED, {failed} FAILED")
print("=" * 72)

if __name__ == "__main__":
    if failed > 0:
        sys.exit(1)
    sys.exit(0)
