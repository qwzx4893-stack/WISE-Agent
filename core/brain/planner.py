# ==============================================================================
# WISE Cognitive Brain - Autonomous Planner
# Architecture: Synthesizes user intent with live WISEWorldState into an
# actionable, ground-truth-anchored CognitivePlan.
# ==============================================================================

from __future__ import annotations

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from core.context.world_state import WISEWorldState, get_world_state_engine
from core.hands import ComputerActionType
from core.brain.intent_parser import (
    CognitiveIntentParser,
    PlannedStep,
    IntentAnalysisResult,
    ParsedIntentType,
)
from core.brain.task_engine import Task, create_task_from_steps

LOG = logging.getLogger("WISE.Brain.Planner")


@dataclass
class CognitivePlan:
    intent: str
    status: str  # "PROPOSED", "NEEDS_CLARIFICATION", "RESTRICTED", "EXECUTING", "COMPLETED", "FAILED"
    steps: List[PlannedStep] = field(default_factory=list)
    clarification_message: Optional[str] = None
    safety_warning: Optional[str] = None
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)
    replan_count: int = 0
    task: Optional[Task] = None

    @property
    def is_actionable(self) -> bool:
        return self.status in ("PROPOSED", "EXECUTING") and len(self.steps) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "clarification_message": self.clarification_message,
            "safety_warning": self.safety_warning,
            "confidence": self.confidence,
            "replan_count": self.replan_count,
            "task_id": self.task.task_id if self.task else None,
        }



class AutonomousCognitivePlanner:
    """
    Formulates dynamic, ground-truth grounded execution plans.
    Continuously inspects live WISEWorldState to avoid duplicate actions or stale assumptions.
    """

    def __init__(self, parser: Optional[CognitiveIntentParser] = None) -> None:
        self.parser = parser or CognitiveIntentParser()
        self.state_engine = get_world_state_engine()
        self._lock = threading.RLock()

    def create_plan(
        self,
        intent: str,
        world_state: Optional[WISEWorldState] = None,
    ) -> CognitivePlan:
        """Translates user intent into a validated CognitivePlan grounded in the current World State."""
        with self._lock:
            state = world_state or self.state_engine.get_current_world_state(force_fresh=True)
            analysis = self.parser.parse(intent)

            # Case 1: Ambiguous or Insufficient Information
            if analysis.intent_type == ParsedIntentType.AMBIGUOUS_INSUFFICIENT:
                return CognitivePlan(
                    intent=intent,
                    status="NEEDS_CLARIFICATION",
                    clarification_message=analysis.clarification_needed,
                    confidence=analysis.confidence,
                )

            # Case 2: Restricted / Unsafe Intent
            if analysis.intent_type == ParsedIntentType.RESTRICTED_UNSAFE:
                return CognitivePlan(
                    intent=intent,
                    status="RESTRICTED",
                    safety_warning=analysis.safety_warning,
                    confidence=1.0,
                )

            # Case 3: Unsupported
            if analysis.intent_type == ParsedIntentType.UNKNOWN_UNSUPPORTED:
                return CognitivePlan(
                    intent=intent,
                    status="NEEDS_CLARIFICATION",
                    clarification_message=analysis.clarification_needed,
                    confidence=analysis.confidence,
                )

            # Case 4: Actionable Plan -> Ground with World State
            steps = list(analysis.steps)

            # Check if target app is ALREADY open and active in World State
            if steps and steps[0].action_type == ComputerActionType.OPEN_APP:
                app_target = steps[0].params.get("app_name", "").lower()
                matching_win = None
                for win in state.open_windows:
                    if app_target.replace(".exe", "") in win.title.lower() or app_target in win.class_name.lower():
                        matching_win = win
                        break

                if matching_win:
                    LOG.info("Application already running on desktop: '%s'. Optimizing plan with FOCUS_WINDOW.", matching_win.title)
                    # Replace OPEN_APP with FOCUS_WINDOW to avoid redundant instances
                    steps[0] = PlannedStep(
                        action_type=ComputerActionType.FOCUS_WINDOW,
                        params={"hwnd": matching_win.hwnd},
                        verification_spec={"type": "window_active", "title": matching_win.title},
                        description=f"Focus existing window '{matching_win.title}'",
                    )

            # Browser world-state grounding (P1.2):
            # If first step is BROWSER_NAVIGATE and Edge is already open in World State,
            # the BrowserActionDispatcher will reuse the existing session (lazy init handles this).
            # Log the optimization for audit trail.
            if steps and steps[0].action_type == ComputerActionType.BROWSER_NAVIGATE:
                edge_open = any(
                    "edge" in (win.title.lower() + " " + win.class_name.lower())
                    for win in state.open_windows
                )
                if edge_open:
                    LOG.info("Microsoft Edge already detected in World State. Browser session will reuse existing context.")

            task = create_task_from_steps(user_intent=intent, steps=steps, semantic_goal=intent)

            return CognitivePlan(
                intent=intent,
                status="PROPOSED",
                steps=steps,
                confidence=analysis.confidence,
                task=task,
            )

    def adapt_plan_to_obstacle(
        self,
        current_plan: CognitivePlan,
        failed_step_idx: int,
        corrective_steps: List[PlannedStep],
    ) -> CognitivePlan:
        """Injects corrective steps mid-flight when an obstacle is encountered."""
        with self._lock:
            new_steps = list(current_plan.steps[:failed_step_idx])
            new_steps.extend(corrective_steps)
            new_steps.extend(current_plan.steps[failed_step_idx:])

            return CognitivePlan(
                intent=current_plan.intent,
                status="EXECUTING",
                steps=new_steps,
                confidence=current_plan.confidence,
                replan_count=current_plan.replan_count + 1,
            )


# Global Singleton Planner
_GLOBAL_PLANNER: Optional[AutonomousCognitivePlanner] = None
_GP_LOCK = threading.Lock()


def get_cognitive_planner() -> AutonomousCognitivePlanner:
    global _GLOBAL_PLANNER
    if _GLOBAL_PLANNER is None:
        with _GP_LOCK:
            if _GLOBAL_PLANNER is None:
                _GLOBAL_PLANNER = AutonomousCognitivePlanner()
    return _GLOBAL_PLANNER
