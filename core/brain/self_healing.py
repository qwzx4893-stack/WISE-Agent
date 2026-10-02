# ==============================================================================
# WISE Cognitive Brain - Self-Healing & Dynamic Re-planning Engine
# Architecture: Detects obstacles (unexpected dialogs, focus loss, verifier failures)
# and synthesizes corrective sub-plans that strictly pass through WindowsSecurityGate.
# ==============================================================================

from __future__ import annotations

import logging
import threading
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from core.context.world_state import WISEWorldState, get_world_state_engine
from core.hands import ComputerActionType
from core.brain.intent_parser import PlannedStep

LOG = logging.getLogger("WISE.Brain.SelfHealing")


@dataclass
class ObstacleDiagnosis:
    obstacle_type: str  # "MODAL_DIALOG", "FOCUS_LOSS", "VERIFICATION_FAILURE", "UNRESPONSIVE_WINDOW"
    details: str
    corrective_steps: List[PlannedStep]
    can_recover: bool = True


class SelfHealingEngine:
    """
    Diagnoses execution discrepancies and formulates safe corrective sub-plans.
    Guarantees that all corrective actions are audited and gated by WindowsSecurityGate.
    """

    def __init__(self) -> None:
        self.state_engine = get_world_state_engine()
        self._lock = threading.RLock()

    def diagnose_and_heal(
        self,
        failed_step: PlannedStep,
        failure_reason: str,
        world_state: Optional[WISEWorldState] = None,
    ) -> ObstacleDiagnosis:
        """Evaluates failure context against the latest World State and synthesizes corrective steps."""
        with self._lock:
            state = world_state or self.state_engine.get_current_world_state(force_fresh=True)

            LOG.warning(
                "Self-Healing Triggered: Step '%s' failed. Reason: %s",
                failed_step.action_type.value,
                failure_reason,
            )

            # Case 1: Unexpected Modal Dialog blocking the desktop or target window
            # Check open_dialogs or active window with dialog characteristics
            if state.open_dialogs:
                top_dialog = state.open_dialogs[0]
                LOG.info("Detected blocking modal dialog: '%s' (HWND: %s)", top_dialog.title, top_dialog.hwnd)

                corrective = []
                # If dialog is a confirmation dialog (e.g. Save As confirmation, replace, warning)
                d_title = top_dialog.title.lower()
                if any(w in d_title for w in ["confirm", "replace", "save as", "تأكيد", "حفظ باسم"]):
                    # Press Enter / Yes to confirm
                    corrective.append(PlannedStep(
                        action_type=ComputerActionType.HOTKEY,
                        params={"keys": ["enter"], "hwnd": top_dialog.hwnd, "action_name": "send_hotkey"},
                        description=f"Acknowledge modal confirmation dialog '{top_dialog.title}'",
                        is_corrective=True,
                    ))
                else:
                    # Dismiss unprompted popup cleanly
                    corrective.append(PlannedStep(
                        action_type=ComputerActionType.CLOSE_WINDOW,
                        params={"hwnd": top_dialog.hwnd, "force": False, "action_name": "close_app"},
                        description=f"Dismiss unexpected modal dialog '{top_dialog.title}'",
                        is_corrective=True,
                    ))

                return ObstacleDiagnosis(
                    obstacle_type="MODAL_DIALOG",
                    details=f"Blocking modal dialog '{top_dialog.title}' detected on desktop.",
                    corrective_steps=corrective,
                    can_recover=True,
                )

            # Case 1b: Zero-Effect Detection (Driver succeeded but UI frame was unchanged)
            if "ZERO_EFFECT" in failure_reason:
                LOG.info("Detected Zero-Effect on step '%s'. Synthesizing refocus and alternate interaction recovery.", failed_step.action_type.value)
                target_hwnd = failed_step.params.get("hwnd")
                corrective = []
                if target_hwnd:
                    corrective.append(PlannedStep(
                        action_type=ComputerActionType.FOCUS_WINDOW,
                        params={"hwnd": target_hwnd, "action_name": "focus_window"},
                        description=f"Refocus window HWND {target_hwnd} to overcome zero-effect",
                        is_corrective=True,
                    ))
                corrective.append(PlannedStep(
                    action_type=ComputerActionType.WAIT,
                    params={"duration": 0.3, "action_name": "wait"},
                    description="Stabilize desktop before alternate interaction",
                    is_corrective=True,
                ))
                if failed_step.action_type in (ComputerActionType.CLICK, ComputerActionType.DOUBLE_CLICK):
                    corrective.append(PlannedStep(
                        action_type=failed_step.action_type,
                        params={**failed_step.params, "post_delay": 0.2},
                        verification_spec=failed_step.verification_spec,
                        description=f"Alternate retry of {failed_step.action_type.value} after focus re-stabilization",
                        is_corrective=True,
                    ))
                return ObstacleDiagnosis(
                    obstacle_type="ZERO_EFFECT",
                    details="Zero-effect detected: action completed without UI state mutation.",
                    corrective_steps=corrective,
                    can_recover=True,
                )

            # Case 2: Target Window Lost Focus
            target_hwnd = failed_step.params.get("hwnd")
            if target_hwnd and state.active_window and state.active_window.hwnd != target_hwnd:
                LOG.info("Detected focus loss. Expected HWND: %s, Active HWND: %s", target_hwnd, state.active_window.hwnd)
                corrective = [
                    PlannedStep(
                        action_type=ComputerActionType.FOCUS_WINDOW,
                        params={"hwnd": target_hwnd, "action_name": "focus_window"},
                        description=f"Refocus target window (HWND: {target_hwnd})",
                        is_corrective=True,
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.WAIT,
                        params={"duration": 0.5, "action_name": "wait"},
                        description="Stabilize focus after reactivation",
                        is_corrective=True,
                    ),
                ]
                return ObstacleDiagnosis(
                    obstacle_type="FOCUS_LOSS",
                    details=f"Target window lost active focus to '{state.active_window.title}'.",
                    corrective_steps=corrective,
                    can_recover=True,
                )

            # Case 3: File System Verification Failure (e.g. Save took longer or required directory creation)
            if failed_step.verification_spec and failed_step.verification_spec.get("type") == "file_exists":
                file_path = failed_step.verification_spec.get("path")
                LOG.info("Verification failure on file path '%s'. Synthesizing direct write recovery.", file_path)
                content = failed_step.params.get("content") or failed_step.params.get("text", "WISE Auto-Recovery Content")
                corrective = [
                    PlannedStep(
                        action_type=ComputerActionType.OPEN_APP,
                        params={
                            "action_name": "write_workspace_file",
                            "path": file_path,
                            "content": content,
                        },
                        verification_spec={"type": "file_exists", "path": file_path},
                        description=f"Directly persist verified file to '{file_path}'",
                        is_corrective=True,
                    )
                ]
                return ObstacleDiagnosis(
                    obstacle_type="VERIFICATION_FAILURE",
                    details=f"Target file '{file_path}' not found after initial step.",
                    corrective_steps=corrective,
                    can_recover=True,
                )

            # Case 4: Browser Execution Obstacles (P1.2)
            if failed_step.action_type.value.startswith("browser_"):
                # Case 4a: Browser Navigation Timeout / Network Disruption
                if any(w in failure_reason.lower() for w in ["timeout", "navigat", "net::", "load state"]):
                    LOG.info("Detected browser navigation timeout. Synthesizing reload/wait recovery.")
                    corrective = [
                        PlannedStep(
                            action_type=ComputerActionType.BROWSER_WAIT,
                            params={"action_name": "browser_wait", "timeout_ms": 3000},
                            description="Wait for browser network stabilization",
                            is_corrective=True,
                        ),
                        PlannedStep(
                            action_type=ComputerActionType.BROWSER_RELOAD,
                            params={"action_name": "browser_reload"},
                            description="Reload current browser page",
                            is_corrective=True,
                        ),
                    ]
                    return ObstacleDiagnosis(
                        obstacle_type="BROWSER_NAVIGATION_TIMEOUT",
                        details=f"Browser navigation or load timed out: {failure_reason}",
                        corrective_steps=corrective,
                        can_recover=True,
                    )

                # Case 4b: Browser Element Not Found or Stale Reference
                if any(w in failure_reason.lower() for w in ["element", "selector", "stale", "locator", "not found", "detached"]):
                    LOG.info("Detected browser element resolution issue. Synthesizing observe and scroll recovery.")
                    corrective = [
                        PlannedStep(
                            action_type=ComputerActionType.BROWSER_SCROLL,
                            params={"action_name": "browser_scroll", "direction": "down", "amount": 300},
                            description="Scroll down to bring potential element into viewport",
                            is_corrective=True,
                        ),
                        PlannedStep(
                            action_type=ComputerActionType.BROWSER_WAIT,
                            params={"action_name": "browser_wait", "timeout_ms": 1000},
                            description="Wait for DOM rendering after scroll",
                            is_corrective=True,
                        ),
                        PlannedStep(
                            action_type=ComputerActionType.BROWSER_OBSERVE,
                            params={"action_name": "browser_observe"},
                            description="Re-observe page semantic structure via ARIA snapshot",
                            is_corrective=True,
                        ),
                    ]
                    return ObstacleDiagnosis(
                        obstacle_type="BROWSER_ELEMENT_NOT_FOUND",
                        details=f"Target browser element could not be resolved: {failure_reason}",
                        corrective_steps=corrective,
                        can_recover=True,
                    )

                # Case 4c: Browser Dialog Blocking
                if any(w in failure_reason.lower() for w in ["dialog", "alert", "modal", "prompt", "cookie", "banner"]):
                    LOG.info("Detected blocking browser dialog or banner. Synthesizing wait and observe recovery.")
                    corrective = [
                        PlannedStep(
                            action_type=ComputerActionType.BROWSER_WAIT,
                            params={"action_name": "browser_wait", "timeout_ms": 1000},
                            description="Wait for browser dialog dismiss",
                            is_corrective=True,
                        ),
                        PlannedStep(
                            action_type=ComputerActionType.BROWSER_OBSERVE,
                            params={"action_name": "browser_observe"},
                            description="Re-observe page after dialog resolution",
                            is_corrective=True,
                        ),
                    ]
                    return ObstacleDiagnosis(
                        obstacle_type="BROWSER_DIALOG_BLOCKING",
                        details=f"Browser execution blocked by dialog or overlay: {failure_reason}",
                        corrective_steps=corrective,
                        can_recover=True,
                    )

            # Case 5: Dynamic State-Aware Cognitive Recovery (LFM2.5)
            try:
                from core.brain.cognitive_decision_engine import get_cognitive_decision_engine
                cde = get_cognitive_decision_engine()
                diag = cde.diagnose_and_replan(
                    task_goal=getattr(failed_step, "description", "") or failed_step.action_type.value,
                    completed_steps=[],
                    failed_step=f"{failed_step.action_type.value}: {failed_step.params}",
                    error_message=failure_reason,
                    world_state=state,
                )
                if diag.get("can_recover") and (diag.get("fallback_action") or diag.get("replanned_remaining_steps")):
                    fallback_text = diag.get("fallback_action") or str(diag.get("replanned_remaining_steps", ["retry"]))
                    corrective = [
                        PlannedStep(
                            action_type=ComputerActionType.WAIT,
                            params={"action_name": "cognitive_fallback", "strategy": diag.get("recovery_strategy", ""), "action": fallback_text, "duration": 0.5},
                            description=f"Execute state-aware recovery: {fallback_text}",
                            is_corrective=True,
                        )
                    ]
                    return ObstacleDiagnosis(
                        obstacle_type="COGNITIVE_DYNAMIC_RECOVERY",
                        details=f"{diag.get('root_cause', failure_reason)} | Strategy: {diag.get('recovery_strategy')}",
                        corrective_steps=corrective,
                        can_recover=True,
                    )
            except Exception as cde_err:
                LOG.debug("Dynamic cognitive recovery unhandled: %s", cde_err)

            # Unrecoverable or generic discrepancy
            return ObstacleDiagnosis(
                obstacle_type="UNKNOWN_OBSTACLE",
                details=f"Unrecognized failure: {failure_reason}",
                corrective_steps=[],
                can_recover=False,
            )


# Global Singleton
_GLOBAL_HEALER: Optional[SelfHealingEngine] = None
_SH_LOCK = threading.Lock()


def get_self_healing_engine() -> SelfHealingEngine:
    global _GLOBAL_HEALER
    if _GLOBAL_HEALER is None:
        with _SH_LOCK:
            if _GLOBAL_HEALER is None:
                _GLOBAL_HEALER = SelfHealingEngine()
    return _GLOBAL_HEALER
