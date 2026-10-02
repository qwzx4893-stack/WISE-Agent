# ==============================================================================
# WISE Hands: Hierarchical Computer Use & Closed-Loop Execution Subsystem
# Architecture: Observe → Understand → Act → Observe Again → Verify → Recover
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import hashlib
import logging
import threading
import subprocess
from typing import Dict, Any, List, Optional, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum

from core.resource.resource_manager import ManagedWorker, WorkerPriority, get_resource_manager
from core.windows.input_driver import WindowsInputDriver, get_input_driver
from core.windows.window_manager import WISEWindowManager, get_window_manager, WindowInfo
from core.vision.vision_manager import WISEVisionManager, get_vision_manager, PerceptionLevel, PerceptionObservation
from core.vision.ui_tree import UIElementNode, get_ui_tree_extractor

LOG = logging.getLogger("WISE.Hands.ComputerUse")


class ComputerActionType(str, Enum):
    OPEN_APP = "open_app"
    FOCUS_WINDOW = "focus_window"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE_TEXT = "type_text"
    HOTKEY = "hotkey"
    DRAG_DROP = "drag_drop"
    SCROLL = "scroll"
    WAIT = "wait"
    CLOSE_WINDOW = "close_window"
    CLEAR_AND_TYPE = "clear_and_type"
    SELECT_DROPDOWN = "select_dropdown"

    # Browser Actions (P1.2)
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK = "browser_click"
    BROWSER_TYPE = "browser_type"
    BROWSER_TYPE_SENSITIVE = "browser_type_sensitive"
    BROWSER_CLEAR = "browser_clear"
    BROWSER_PRESS_KEY = "browser_press_key"
    BROWSER_SCROLL = "browser_scroll"
    BROWSER_BACK = "browser_back"
    BROWSER_FORWARD = "browser_forward"
    BROWSER_RELOAD = "browser_reload"
    BROWSER_SWITCH_TAB = "browser_switch_tab"
    BROWSER_NEW_TAB = "browser_new_tab"
    BROWSER_CLOSE_TAB = "browser_close_tab"
    BROWSER_WAIT = "browser_wait"
    BROWSER_EXTRACT = "browser_extract"
    BROWSER_EXTRACT_SENSITIVE = "browser_extract_sensitive"
    BROWSER_OBSERVE = "browser_observe"
    BROWSER_CLOSE = "browser_close"


@dataclass
class ActionRecord:
    action_type: ComputerActionType
    parameters: Dict[str, Any]
    timestamp: float
    perception_method: PerceptionLevel
    ui_tree_used: bool
    screenshot_used: bool
    ocr_used: bool
    vlm_used: bool
    action_result: Dict[str, Any]
    verification_result: bool
    latency_ms: float
    recovery_attempted: bool
    cpu_percent: float = 0.0
    ram_mb: float = 0.0
    vram_mb: float = 0.0
    pre_state_sig: Optional[str] = None
    post_state_sig: Optional[str] = None
    failure_type: Optional[str] = None

    @property
    def success(self) -> bool:
        return bool(self.verification_result and self.action_result.get("success", True) is not False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "parameters": self.parameters,
            "timestamp": self.timestamp,
            "perception_method": self.perception_method.value,
            "ui_tree_used": self.ui_tree_used,
            "screenshot_used": self.screenshot_used,
            "ocr_used": self.ocr_used,
            "vlm_used": self.vlm_used,
            "action_result": self.action_result,
            "verification_result": self.verification_result,
            "latency_ms": round(self.latency_ms, 2),
            "recovery_attempted": self.recovery_attempted,
            "cpu_percent": self.cpu_percent,
            "ram_mb": self.ram_mb,
            "vram_mb": self.vram_mb,
            "pre_state_sig": self.pre_state_sig,
            "post_state_sig": self.post_state_sig,
            "failure_type": self.failure_type,
        }


class WISEHands(ManagedWorker):
    """
    Closed-Loop Computer Use Engine for WISE.
    Governs hierarchical interaction with the operating system desktop,
    enforcing Observe -> Understand -> Act -> Observe -> Verify -> Recover.
    """

    def __init__(
        self,
        input_driver: Optional[WindowsInputDriver] = None,
        window_manager: Optional[WISEWindowManager] = None,
        vision_manager: Optional[WISEVisionManager] = None,
    ) -> None:
        super().__init__(name="wise_hands", priority=WorkerPriority.CRITICAL_ON_DEMAND)
        self.input_driver = input_driver or get_input_driver()
        self.window_mgr = window_manager or get_window_manager()
        self.vision_mgr = vision_manager or get_vision_manager()
        self.history: List[ActionRecord] = []
        self._lock = threading.RLock()
        self._active = False
        # Hands is deliberately a second, local enforcement boundary.  The
        # orchestrator also evaluates plans, but direct callers of this public
        # computer-control class must never bypass the SecurityGate.
        self.security_gate: Any = None

        # Browser subsystem dispatcher — lazily initialized on first browser action
        self._browser_dispatcher: Optional["BrowserActionDispatcher"] = None
        self._host_browser_dispatcher: Optional["BrowserActionDispatcher"] = None

        # Canonical TargetResolver (5-layer hierarchy & anti-stale frame protection)
        from core.hands.target_resolver import TargetResolver
        self.target_resolver = TargetResolver(
            vision_manager=self.vision_mgr,
            window_manager=self.window_mgr,
            input_driver=self.input_driver,
        )

        # Register with resource manager for lifecycle control
        get_resource_manager().register_worker(self)

    def start(self) -> None:
        with self._lock:
            self._active = True
            LOG.info("WISE Hands Subsystem activated.")

    def stop(self) -> None:
        with self._lock:
            self._active = False
            LOG.info("WISE Hands Subsystem stopped.")

    def suspend(self) -> None:
        with self._lock:
            self._active = False
            LOG.debug("WISE Hands Subsystem suspended on IDLE.")

    def resume(self) -> None:
        with self._lock:
            self._active = True
            LOG.debug("WISE Hands Subsystem resumed.")

    # ==========================================================================
    # Closed-Loop Execution Cycle
    # ==========================================================================

    def execute_closed_loop_action(
        self,
        action_type: ComputerActionType,
        params: Dict[str, Any],
        verification_condition: Optional[Dict[str, Any]] = None,
        max_retries: int = 2,
        security_context: Optional[Any] = None,
    ) -> ActionRecord:
        """
        Executes a complete Closed-Loop cycle:
        1. Observe (Pre-Action)
        2. Understand (Target element/coordinates resolution)
        3. Act (Dispatch SendInput / Window command)
        4. Observe Again (Post-Action)
        5. Verify (Evaluate condition)
        6. Recover (Attempt healing if verification failed)
        """
        t0 = time.perf_counter()

        # Import lazily: the security package also knows about world state,
        # which imports ActionRecord during application bootstrap.
        if self.security_gate is None:
            from core.security.security_gate import get_security_gate
            self.security_gate = get_security_gate()
        from core.security.security_gate import SecurityContext
        security_action = self._security_action_name(action_type, params)
        security_evaluation = self.security_gate.evaluate(
            security_action,
            params,
            security_context or SecurityContext(caller="wise_hands"),
        )
        if not security_evaluation.allowed:
            metrics = get_resource_manager().get_hardware_metrics()
            record = ActionRecord(
                action_type=action_type,
                parameters=dict(params),
                timestamp=time.time(),
                perception_method=PerceptionLevel.LEVEL_1_UI_TREE,
                ui_tree_used=False,
                screenshot_used=False,
                ocr_used=False,
                vlm_used=False,
                action_result={
                    "success": False,
                    "error": security_evaluation.reason,
                    "requires_confirmation": security_evaluation.requires_confirmation,
                    "security_evaluation": security_evaluation.to_dict(),
                },
                verification_result=False,
                latency_ms=(time.perf_counter() - t0) * 1000,
                recovery_attempted=False,
                cpu_percent=metrics.cpu_percent,
                ram_mb=metrics.ram_used_mb,
                vram_mb=metrics.gpu_memory_used_mb,
                failure_type="SECURITY_BLOCKED",
            )
            with self._lock:
                self.history.append(record)
            return record
        self.resume()

        # Telemetry probe
        rm = get_resource_manager()
        metrics = rm.get_hardware_metrics()

        ui_tree_used = False
        screen_used = False
        ocr_used = False
        vlm_used = False
        recovery_attempted = False
        last_level = PerceptionLevel.LEVEL_1_UI_TREE

        # 1. Pre-Observation & Resolution
        target_hwnd = params.get("hwnd")
        if not target_hwnd and "window_query" in params:
            w = self.window_mgr.find_window(params["window_query"])
            if w:
                target_hwnd = w.hwnd

        # Stale-frame validation if signature provided in params
        if params.get("frame_signature"):
            if not self.validate_frame_signature(params["frame_signature"]):
                LOG.warning("Stale frame detected before dispatching '%s'.", action_type.value)

        # Capture pre-action frame signature
        pre_sig_info = self.create_frame_signature(target_hwnd)
        pre_sig = pre_sig_info["signature"]

        # Automatic pre-action capture if visual_change verification requested
        v_dict = verification_condition.to_dict() if hasattr(verification_condition, "to_dict") else (verification_condition or {})
        if v_dict.get("type") == "visual_change":
            if not v_dict.get("before_bytes"):
                pre_obs = self.vision_mgr.observe(hwnd=target_hwnd or 0, scope="screen")
                if pre_obs.capture and pre_obs.capture.image_bytes:
                    if isinstance(verification_condition, dict):
                        verification_condition["before_bytes"] = pre_obs.capture.image_bytes
                    elif hasattr(verification_condition, "parameters"):
                        verification_condition.parameters["before_bytes"] = pre_obs.capture.image_bytes

        # 2. Act Dispatch
        act_res = self._dispatch_action(action_type, params, target_hwnd)
        ui_tree_used = act_res.get("ui_tree_used", False)
        screen_used = act_res.get("screen_used", False)
        ocr_used = act_res.get("ocr_used", False)
        vlm_used = act_res.get("vlm_used", False)
        if "level_used" in act_res:
            last_level = act_res["level_used"]

        # Small pause for UI settling
        time.sleep(params.get("post_delay", 0.1))

        # 3. Post-Observation & Verify
        verified = True
        if verification_condition:
            verified = self.verify_condition(verification_condition, target_hwnd)

            # 4. Recover if failed and retries remaining
            if not verified and max_retries > 0:
                LOG.warning("Verification failed for action '%s'. Initiating self-healing recovery.", action_type.value)
                recovery_attempted = True
                self._attempt_recovery(action_type, params, target_hwnd)
                # Re-dispatch action once
                act_res = self._dispatch_action(action_type, params, target_hwnd)
                time.sleep(0.15)
                verified = self.verify_condition(verification_condition, target_hwnd)

        # Capture post-action frame signature
        post_sig_info = self.create_frame_signature(target_hwnd)
        post_sig = post_sig_info["signature"]

        # Zero-effect detection: driver reported success but frame signature remained identical and verification failed
        failure_type: Optional[str] = None
        if not verified:
            if act_res.get("success", False) and pre_sig == post_sig:
                failure_type = "ZERO_EFFECT"
                act_res["failure_type"] = "ZERO_EFFECT"
                LOG.warning("Zero-effect detected for action '%s': driver reported success but desktop frame remained unchanged.", action_type.value)
            else:
                failure_type = act_res.get("failure_type") or "VERIFICATION_FAILURE"

        t_elapsed_ms = (time.perf_counter() - t0) * 1000

        record = ActionRecord(
            action_type=action_type,
            parameters=params,
            timestamp=time.time(),
            perception_method=last_level,
            ui_tree_used=ui_tree_used,
            screenshot_used=screen_used,
            ocr_used=ocr_used,
            vlm_used=vlm_used,
            action_result=act_res,
            verification_result=verified,
            latency_ms=t_elapsed_ms,
            recovery_attempted=recovery_attempted,
            cpu_percent=metrics.cpu_percent,
            ram_mb=metrics.ram_used_mb,
            vram_mb=metrics.gpu_memory_used_mb,
            pre_state_sig=pre_sig,
            post_state_sig=post_sig,
            failure_type=failure_type,
        )

        with self._lock:
            self.history.append(record)

        return record

    execute_action = execute_closed_loop_action

    @staticmethod
    def _security_action_name(action_type: ComputerActionType, params: Dict[str, Any]) -> str:
        """Map physical actions to their highest-impact security operation."""
        if action_type == ComputerActionType.OPEN_APP and "content" in params and "path" in params:
            return "write_file"
        if action_type == ComputerActionType.CLOSE_WINDOW and bool(params.get("force")):
            # A forced close may terminate a process and lose unsaved work.
            return "kill_process"
        return action_type.value

    def _dispatch_action(
        self,
        action_type: ComputerActionType,
        params: Dict[str, Any],
        hwnd: Optional[int],
    ) -> Dict[str, Any]:
        """Dispatches an action through the input driver or window manager."""
        res: Dict[str, Any] = {"success": False}

        try:
            if action_type == ComputerActionType.OPEN_APP:
                if "content" in params and "path" in params:
                    from core.windows.computer_control import get_computer_control
                    w_res = get_computer_control().write_file(params["path"], params.get("content", ""))
                    res["success"] = w_res.get("success", False)
                    res["file_details"] = w_res
                else:
                    app = params.get("app_path") or params.get("app_name", "")
                    from core.windows.computer_control import get_computer_control
                    open_res = get_computer_control().open_app(app)
                    res["success"] = open_res.get("success", False)
                    res["app_details"] = open_res

            elif action_type == ComputerActionType.FOCUS_WINDOW:
                target = hwnd or params.get("hwnd")
                if not target and "query" in params:
                    w = self.window_mgr.find_window(params["query"])
                    if w:
                        target = w.hwnd
                if target:
                    res["success"] = self.window_mgr.bring_to_front(target)
                    res["hwnd"] = target

            elif action_type in (ComputerActionType.CLICK, ComputerActionType.DOUBLE_CLICK, ComputerActionType.RIGHT_CLICK):
                x, y, level, uia, scr, ocr, vlm = self._resolve_target_coordinates(params, hwnd)
                res["level_used"] = level
                res["ui_tree_used"] = uia
                res["screen_used"] = scr
                res["ocr_used"] = ocr
                res["vlm_used"] = vlm

                if x is not None and y is not None:
                    if action_type == ComputerActionType.DOUBLE_CLICK:
                        res["success"] = self.input_driver.double_click(x, y)
                    elif action_type == ComputerActionType.RIGHT_CLICK:
                        res["success"] = self.input_driver.click("right", x, y)
                    else:
                        res["success"] = self.input_driver.click("left", x, y)
                    res["clicked_coords"] = (x, y)
                else:
                    res["error"] = "Target coordinates could not be resolved"

            elif action_type == ComputerActionType.TYPE_TEXT:
                text = params.get("text", "")
                count = self.input_driver.type_text(text, delay_per_char=params.get("char_delay", 0.01))
                res["success"] = (count == len(text))
                res["typed_count"] = count

            elif action_type == ComputerActionType.CLEAR_AND_TYPE:
                self.input_driver.hotkey("ctrl", "a")
                time.sleep(0.05)
                self.input_driver.press_key("backspace")
                time.sleep(0.05)
                text = params.get("text", "")
                count = self.input_driver.type_text(text, delay_per_char=params.get("char_delay", 0.01))
                res["success"] = (count == len(text))
                res["typed_count"] = count

            elif action_type == ComputerActionType.SELECT_DROPDOWN:
                x, y, level, uia, scr, ocr, vlm = self._resolve_target_coordinates(params, hwnd)
                if x is not None and y is not None:
                    self.input_driver.click("left", x, y)
                    time.sleep(0.15)
                val = params.get("value") or params.get("text", "")
                if val:
                    self.input_driver.type_text(val)
                    time.sleep(0.05)
                    self.input_driver.press_key("enter")
                    res["success"] = True
                    res["selected_value"] = val
                else:
                    res["success"] = True

            elif action_type == ComputerActionType.HOTKEY:
                keys = params.get("keys", [])
                if isinstance(keys, str):
                    keys = [k.strip() for k in keys.split("+")]
                res["success"] = self.input_driver.hotkey(*keys)
                res["keys"] = keys

            elif action_type == ComputerActionType.DRAG_DROP:
                from_x, from_y = params.get("from_x", 0), params.get("from_y", 0)
                to_x, to_y = params.get("to_x", 0), params.get("to_y", 0)
                res["success"] = self.input_driver.drag_and_drop(from_x, from_y, to_x, to_y, duration=params.get("duration", 0.2))

            elif action_type == ComputerActionType.SCROLL:
                delta = params.get("delta", -2)
                res["success"] = self.input_driver.scroll(delta, params.get("x"), params.get("y"))

            elif action_type == ComputerActionType.WAIT:
                duration = params.get("duration", 1.0)
                time.sleep(duration)
                res["success"] = True

            elif action_type == ComputerActionType.CLOSE_WINDOW:
                target = hwnd or params.get("hwnd")
                if not target and "app_name" in params:
                    w = self.window_mgr.find_window(params["app_name"])
                    if w:
                        target = w.hwnd
                if target:
                    res["success"] = self.window_mgr.close_window(target, force=params.get("force", False))
                elif params.get("app_name") or params.get("process"):
                    if not params.get("force"):
                        res["error"] = "A process-only close requires force=true and confirmation."
                    else:
                        proc_name = str(params.get("app_name") or params.get("process"))
                        # Argument-vector invocation prevents a process name
                        # from being interpreted as a shell command.
                        completed = subprocess.run(
                            ["taskkill", "/f", "/im", proc_name],
                            capture_output=True,
                            text=True,
                            timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        res["success"] = (completed.returncode == 0)
                        if completed.returncode != 0:
                            res["error"] = (completed.stderr or completed.stdout or
                                            f"Could not terminate process '{proc_name}'.").strip()

            elif action_type.value.startswith("browser_"):
                # Delegate all BROWSER_* actions to BrowserActionDispatcher
                res = self._dispatch_browser_action(action_type.value, params)

        except Exception as e:
            LOG.error("Error executing action %s: %s", action_type.value, e)
            res["error"] = str(e)

        return res

    def _dispatch_browser_action(self, action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate browser actions to BrowserActionDispatcher (lazy init)."""
        from core.browser import BrowserActionDispatcher, BrowserProfileMode, BrowserSessionConfig

        clean_params = dict(params)
        requested_mode = str(clean_params.pop("browser_mode", "isolated")).lower()
        use_host_browser = requested_mode in {"host", "host_cdp", "user", "device", "brave"}
        if use_host_browser:
            if self._host_browser_dispatcher is None:
                self._host_browser_dispatcher = BrowserActionDispatcher(
                    config=BrowserSessionConfig(profile_mode=BrowserProfileMode.HOST_CDP)
                )
            dispatcher = self._host_browser_dispatcher
        else:
            if self._browser_dispatcher is None:
                self._browser_dispatcher = BrowserActionDispatcher()
            dispatcher = self._browser_dispatcher

        # Check for sensitive field escalation before dispatch
        escalated_name = dispatcher.check_sensitive_escalation(action_name, clean_params)
        return dispatcher.dispatch(escalated_name, clean_params)

    def get_perception_snapshot(self, hwnd: Optional[int] = None):
        """Returns the canonical Unified Perception Snapshot from TargetResolver."""
        return self.target_resolver.capture_snapshot(hwnd)

    def _resolve_target_coordinates(
        self,
        params: Dict[str, Any],
        hwnd: Optional[int],
    ) -> Tuple[Optional[int], Optional[int], PerceptionLevel, bool, bool, bool, bool]:
        """
        Hierarchical target resolution using canonical TargetResolver:
        1. Browser DOM
        2. UI Automation / Tree text search (Level 1)
        3. Accessibility Tree (MSAA)
        4. Local OCR search (Level 3)
        5. VLM / Visual Grounding (Level 4, only when prior levels fail)
        6. Coordinate Fallback (Last resort, validated against window & frame signature)
        """
        target_text = (
            params.get("target_text")
            or params.get("text_query")
            or params.get("element_name")
            or params.get("locator")
            or params.get("query")
        )

        explicit_coords = None
        if "x" in params and "y" in params:
            explicit_coords = (int(params["x"]), int(params["y"]))

        expected_sig = params.get("expected_frame_signature") or params.get("frame_signature")

        # Resolve target via TargetResolver
        target, decision, err = self.target_resolver.resolve_target(
            target_query=target_text,
            hwnd=hwnd,
            explicit_coords=explicit_coords,
            expected_frame_signature=expected_sig,
        )

        if target and target.best_center:
            cx, cy = target.best_center
            src = target.source
            if src == "DOM":
                return (cx, cy, PerceptionLevel.LEVEL_1_UI_TREE, False, False, False, False)
            elif src in ("UIA", "ACCESSIBILITY"):
                return (cx, cy, PerceptionLevel.LEVEL_1_UI_TREE, True, False, False, False)
            elif src == "OCR":
                return (cx, cy, PerceptionLevel.LEVEL_3_OCR, True, True, True, False)
            elif src == "VLM":
                return (cx, cy, PerceptionLevel.LEVEL_4_HEAVY_VLM, True, True, True, True)
            else:
                return (cx, cy, PerceptionLevel.LEVEL_1_UI_TREE, False, False, False, False)

        LOG.warning("Target resolution failed: %s (decision: %s)", err, decision)
        return (None, None, PerceptionLevel.LEVEL_1_UI_TREE, False, False, False, False)


    def verify_condition(self, condition: Dict[str, Any], hwnd: Optional[int] = None) -> bool:
        """Evaluates closed-loop verification condition against system state."""
        if not condition:
            return True

        from core.contracts import normalize_verification_spec
        condition = normalize_verification_spec(condition)
        kind = condition.get("type", "")

        if kind == "file_exists":
            from pathlib import Path
            p = Path(os.path.expandvars(condition.get("path", "")))
            return p.is_file() or p.exists()

        elif kind == "file_content":
            from pathlib import Path
            p = Path(os.path.expandvars(condition.get("path", "")))
            if not p.is_file():
                return False
            expected_text = condition.get("content", "")
            try:
                return expected_text in p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return False

        elif kind == "window_exists":
            query = condition.get("title") or condition.get("query", "")
            wins = self.window_mgr.find_windows(query)
            return len(wins) > 0

        elif kind in ("window_closed", "window_disappeared"):
            target = condition.get("hwnd") or hwnd
            if not target:
                return False
            # A graceful close may complete asynchronously.  A hidden or
            # destroyed top-level window both satisfy the user's requested
            # visible-state change; process lifetime is a separate condition.
            for _ in range(12):
                info = self.window_mgr.get_window_info(int(target))
                if info is None or not info.is_visible:
                    return True
                time.sleep(0.1)
            return False

        elif kind in ("window_active", "active_window"):
            query = condition.get("title") or condition.get("query") or condition.get("expected_active_app", "")
            for _ in range(4):
                fore = self.window_mgr.get_foreground_window()
                if fore and query and query.lower() in fore.title.lower():
                    return True
                wins = self.window_mgr.find_windows(query)
                if wins:
                    return True
                time.sleep(0.25)
            return False

        elif kind in ("text_visible", "text_present"):
            target_text = condition.get("text", "")
            obs = self.vision_mgr.observe(hwnd=hwnd, scope="auto")
            if obs.ui_tree:
                matches = get_ui_tree_extractor().find_elements_by_text(obs.ui_tree, target_text)
                if matches:
                    return True
            if obs.ocr and target_text.lower() in obs.ocr.full_text.lower():
                return True
            return False

        elif kind == "process_running":
            import psutil
            name = (condition.get("process_name") or condition.get("process") or condition.get("name") or "").lower()
            if not name:
                return False
            return any(name in (p.info.get("name") or "").lower() for p in psutil.process_iter(["name"]))

        elif kind == "always_true":
            return True

        elif kind == "tool_result":
            return bool(condition.get("success", True))

        # Browser verification conditions (P1.2)
        elif kind == "url_match":
            expected = condition.get("url", "").lower()
            if self._browser_dispatcher and self._browser_dispatcher.is_active:
                state = self._browser_dispatcher.get_session_state()
                return expected in state.current_url.lower()
            return False

        elif kind == "page_loaded":
            if self._browser_dispatcher and self._browser_dispatcher.is_active:
                state = self._browser_dispatcher.get_session_state()
                return bool(state.current_url and state.is_active)
            return False

        elif kind == "text_on_page":
            expected = condition.get("text", "").lower()
            if self._browser_dispatcher and self._browser_dispatcher.is_active:
                try:
                    res = self._browser_dispatcher.dispatch("browser_extract", {"scope": "text"})
                    extracted = res.get("extracted_text", "").lower()
                    return expected in extracted
                except Exception:
                    pass
            return False

        elif kind == "browser_has_results":
            # Verify that the page contains meaningful content (non-empty extraction)
            if self._browser_dispatcher and self._browser_dispatcher.is_active:
                try:
                    res = self._browser_dispatcher.dispatch("browser_extract", {"scope": "text"})
                    extracted = res.get("extracted_text", "")
                    return len(extracted.strip()) > 50
                except Exception:
                    pass
            return False

        elif kind in ("process_terminated", "process_not_running", "process_exited", "process_dead"):
            import psutil
            name = (condition.get("process_name") or condition.get("process") or condition.get("name") or "").lower()
            pid = condition.get("pid")
            for _ in range(6):
                still_running = False
                for p in psutil.process_iter(["name", "pid"]):
                    try:
                        if pid and p.info.get("pid") == pid:
                            still_running = True
                            break
                        if name and name in (p.info.get("name") or "").lower():
                            still_running = True
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                if not still_running:
                    return True
                time.sleep(0.25)
            return False

        elif kind == "ui_element_exists":
            query = condition.get("text") or condition.get("name") or condition.get("query", "")
            obs = self.vision_mgr.observe(hwnd=hwnd, scope="auto")
            if obs.ui_tree:
                matches = get_ui_tree_extractor().find_elements_by_text(obs.ui_tree, query)
                if matches:
                    return True
            return False

        elif kind == "ui_element_disappeared":
            query = condition.get("text") or condition.get("name") or condition.get("query", "")
            for _ in range(4):
                obs = self.vision_mgr.observe(hwnd=hwnd, scope="auto")
                matches = get_ui_tree_extractor().find_elements_by_text(obs.ui_tree, query) if obs.ui_tree else []
                if not matches:
                    return True
                time.sleep(0.25)
            return False

        elif kind == "text_changed":
            old_text = condition.get("previous_text") or condition.get("old_text", "")
            obs = self.vision_mgr.observe(hwnd=hwnd, scope="auto")
            current_text = ""
            if obs.ocr and obs.ocr.full_text:
                current_text = obs.ocr.full_text
            elif obs.ui_tree:
                current_text = obs.ui_tree.format_text_tree()
            return old_text.strip() != current_text.strip()

        elif kind == "page_title":
            expected = condition.get("title", "").lower()
            if self._browser_dispatcher and self._browser_dispatcher.is_active:
                state = self._browser_dispatcher.get_session_state()
                return expected in state.title.lower()
            return False

        elif kind == "download_exists":
            from pathlib import Path
            expected_name = condition.get("filename")
            download_dir = Path(condition.get("directory") or (os.path.dirname(__file__) + "/../../downloads"))
            if download_dir.exists():
                for f in download_dir.iterdir():
                    if f.is_file() and f.stat().st_size > 0:
                        if not expected_name or expected_name.lower() in f.name.lower():
                            return True

        elif kind in ("artifact_valid", "artifact_exists"):
            from pathlib import Path
            p = Path(os.path.expandvars(condition.get("path") or condition.get("artifact_path", "")))
            if not p.is_file() or p.stat().st_size < condition.get("min_size_bytes", 1):
                return False
            patterns = condition.get("required_patterns", [])
            if patterns:
                try:
                    txt = p.read_text(encoding="utf-8", errors="ignore")
                    if not all(pat in txt for pat in patterns):
                        return False
                except Exception:
                    return False
            return True

        elif kind == "dom_element":
            selector = condition.get("selector", "")
            if self._browser_dispatcher and self._browser_dispatcher.is_active:
                try:
                    res = self._browser_dispatcher.dispatch("browser_observe", {})
                    for el in res.get("elements", []):
                        if selector.lower() in (el.get("name", "") + " " + el.get("role", "")).lower():
                            return True
                except Exception:
                    pass
            return False

        elif kind == "application_state":
            expected_key = condition.get("key")
            expected_val = condition.get("value")
            current_world = self.state_engine.get_current_world_state(force_fresh=True)
            if expected_key and hasattr(current_world, expected_key):
                return getattr(current_world, expected_key) == expected_val
            return True

        # Visual VLM verification conditions (P1.3-B)
        elif kind in ("visual_element", "visual_element_present"):
            target_name = condition.get("name") or condition.get("query", "")
            el = self.vision_mgr.locate_visual_element(target_name, hwnd=hwnd or 0)
            min_conf = float(condition.get("min_confidence", 0.60))
            return el is not None and el.confidence >= min_conf

        elif kind == "visual_change":
            before_bytes = condition.get("before_bytes", b"")
            crop_box = condition.get("crop_box")
            min_ratio = float(condition.get("min_ratio", 0.005))
            obs = self.vision_mgr.observe(hwnd=hwnd or 0, scope="screen")
            if obs.capture and obs.capture.image_bytes:
                return self.vision_mgr.verify_visual_change(
                    before_bytes=before_bytes,
                    after_bytes=obs.capture.image_bytes,
                    crop_box=crop_box,
                    min_ratio=min_ratio,
                )
            return False

        elif kind == "visual_dialog_present":
            obs = self.vision_mgr.observe(hwnd=hwnd or 0, scope="vlm")
            return bool(obs.vlm and obs.vlm.active_dialog)

        return False

    def create_frame_signature(self, hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Creates a cryptographic state signature of the active frame for stale-frame defense."""
        fore = self.window_mgr.get_foreground_window()
        target_hwnd = hwnd or (fore.hwnd if fore else 0)
        title = ""
        rect = (0, 0, 0, 0)
        if target_hwnd:
            w = self.window_mgr.get_window_info(target_hwnd)
            if w:
                title = w.title
                rect = w.rect
        sig = hashlib.sha256(f"{target_hwnd}_{title}_{rect}_{int(time.time() // 5)}".encode()).hexdigest()[:16]
        return {
            "hwnd": target_hwnd,
            "title": title,
            "rect": rect,
            "signature": sig,
            "timestamp": time.time(),
        }

    def validate_frame_signature(self, signature: Optional[Dict[str, Any]]) -> bool:
        """Validates that the active frame hasn't become stale (>5s old or window changed)."""
        if not signature:
            return True
        ts = signature.get("timestamp", 0)
        if time.time() - ts > 5.0:
            LOG.warning("Frame signature expired (stale frame > 5.0s)")
            return False
        expected_hwnd = signature.get("hwnd")
        current_fore = self.window_mgr.get_foreground_window()
        if expected_hwnd and current_fore and current_fore.hwnd != expected_hwnd:
            LOG.warning("Active foreground window changed from %s to %s", expected_hwnd, current_fore.hwnd)
            return False
        return True


    def _attempt_recovery(self, action_type: ComputerActionType, params: Dict[str, Any], hwnd: Optional[int]) -> None:
        """Self-healing recovery actions when a verification condition fails."""
        LOG.info("Attempting recovery for action: %s", action_type.value)
        # 1. Bring window to front again
        if hwnd:
            self.window_mgr.bring_to_front(hwnd)
            time.sleep(0.05)
        elif "window_query" in params:
            w = self.window_mgr.find_window(params["window_query"])
            if w:
                self.window_mgr.bring_to_front(w.hwnd)
                time.sleep(0.05)

        # 2. If dialog or modal blocking, press ESC or ENTER if requested
        if params.get("dismiss_dialog_on_fail"):
            self.input_driver.press_key("esc")
            time.sleep(0.05)


# Global Singleton Hands
_WISE_HANDS: Optional[WISEHands] = None


def get_wise_hands() -> WISEHands:
    global _WISE_HANDS
    if _WISE_HANDS is None:
        _WISE_HANDS = WISEHands()
    return _WISE_HANDS


get_hands = get_wise_hands
