# ==============================================================================
# WISE Central Closed-Loop Orchestrator
# Architecture: User Intent → World State → Observe → Understand → Plan → Act →
# Observe Again → Verify → Recover → Update World State → Respond
# Strictly guarded by WindowsSecurityGate and ChallengeDetector
# Integrated with Human-In-The-Loop (HITL) Lifecycle and TaskEngine
# Zero singletons for new managers: Supports explicit dependency injection.
# ==============================================================================

from __future__ import annotations

import json
import time
import logging
import threading
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from core.contracts import ActionLoopDecision, FailureType, ActionExecutionRecord
from core.context.world_state import get_world_state_engine, WISEWorldState, StateItem
from core.context.fusion_engine import get_context_fusion_engine
from core.hands import get_wise_hands, WISEHands, ComputerActionType, ActionRecord
from core.security import (
    get_security_gate,
    WindowsSecurityGate,
    SecurityContext,
    ActionTier,
    HumanInterventionType,
)
from core.security.confirmation import ConfirmationManager
from core.security.challenge_detector import ChallengeDetector, ChallengeDetectionResult
from core.windows.computer_control import get_computer_control

LOG = logging.getLogger("WISE.Orchestrator")


@dataclass
class OrchestrationStep:
    action_type: ComputerActionType
    params: Dict[str, Any]
    verification_spec: Optional[Dict[str, Any]] = None
    max_retries: int = 2
    requires_human: bool = False
    human_intervention_type: Optional[str] = None
    human_intervention_reason: Optional[str] = None
    human_prompt: Optional[str] = None
    confirmation_token: Optional[str] = None
    description: str = ""



@dataclass
class OrchestrationCycleResult:
    intent: str
    success: bool
    steps_executed: int
    records: List[ActionRecord] = field(default_factory=list)
    recovery_triggered: bool = False
    verified: bool = True
    total_latency_ms: float = 0.0
    final_state_summary: str = ""
    error: Optional[str] = None
    needs_clarification: bool = False
    clarification_message: Optional[str] = None
    task_id: Optional[str] = None
    task: Optional[Any] = None
    paused_for_human: bool = False
    intervention_details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "success": self.success,
            "steps_executed": self.steps_executed,
            "recovery_triggered": self.recovery_triggered,
            "verified": self.verified,
            "total_latency_ms": round(self.total_latency_ms, 2),
            "records": [r.to_dict() for r in self.records],
            "error": self.error,
            "needs_clarification": self.needs_clarification,
            "clarification_message": self.clarification_message,
            "task_id": self.task_id,
            "paused_for_human": self.paused_for_human,
            "intervention_details": self.intervention_details,
        }


class ClosedLoopOrchestrator:
    """
    Central cognitive execution engine for WISE.
    Orchestrates the Observe → Plan → Act → Verify → Recover cycle,
    updating the Unified World State at each checkpoint.
    """

    def __init__(
        self,
        security_context: Optional[SecurityContext] = None,
        security_gate: Optional[WindowsSecurityGate] = None,
        challenge_detector: Optional[ChallengeDetector] = None,
        confirmation_manager: Optional[ConfirmationManager] = None,
    ) -> None:
        self.state_engine = get_world_state_engine()
        self.fusion_engine = get_context_fusion_engine()
        self.hands = get_wise_hands()
        self.security_gate = security_gate or get_security_gate()
        self.challenge_detector = challenge_detector or ChallengeDetector()
        self.confirmation_manager = (
            confirmation_manager
            or getattr(self.security_gate, "confirmation_manager", None)
            or ConfirmationManager()
        )
        self.security_context = security_context or SecurityContext(
            caller="wise_orchestrator",
            is_untrusted_content=False,
            confirmed=False,
        )
        self._lock = threading.RLock()

    def _step_security_context(
        self,
        *,
        untrusted_content_seen: bool,
        confirmation_token: Optional[str] = None,
    ) -> SecurityContext:
        """Carry web-content taint through every later action in this run.

        Browser observations are data, never instructions.  The taint must be
        local to the execution run (not stored on the long-lived orchestrator)
        so an old page cannot poison an unrelated future task.
        """
        base = self.security_context
        return SecurityContext(
            caller=base.caller,
            is_untrusted_content=base.is_untrusted_content or untrusted_content_seen,
            confirmed=base.confirmed,
            elevation_requested=base.elevation_requested,
            session_id=base.session_id,
            confirmation_token=confirmation_token or base.confirmation_token,
            taint_sources=list(base.taint_sources) + (["browser"] if untrusted_content_seen else []),
        )

    @staticmethod
    def _record_contains_untrusted_web_content(record: ActionRecord) -> bool:
        return bool((record.action_result or {}).get("is_untrusted_web_content"))

    def run_cycle(
        self,
        intent: str,
        steps: List[OrchestrationStep],
    ) -> OrchestrationCycleResult:
        """
        Executes a complete cognitive cycle for the given intent:
        1. User Intent Ingestion
        2. World State Snapshot (Observe pre-state)
        3. Plan Formulation & Security Gate Approval
        4. Challenge Detection & Human Intervention Check
        5. Action Dispatch (Act)
        6. Observe Again (Post-State)
        7. Verification & Recovery
        8. Update World State
        9. Respond with Cycle Result
        """
        t0 = time.perf_counter()
        records: List[ActionRecord] = []
        recovery_triggered = False
        all_verified = True
        overall_success = True
        untrusted_content_seen = self.security_context.is_untrusted_content

        LOG.info("Initiating Closed-Loop Cycle for Intent: '%s'", intent)

        # 1 & 2. Observe initial World State
        initial_state = self.state_engine.get_current_world_state(force_fresh=True)
        plan_names = [f"{s.action_type.value}" for s in steps]
        self.state_engine.update_task_progress(
            intent=intent,
            current_step=0,
            total_steps=len(steps),
            active_plan=plan_names,
            verification_status="IN_PROGRESS",
        )

        action_signatures: List[str] = []
        for idx, step in enumerate(steps, 1):
            step_name = step.params.get("action_name") or step.action_type.value
            step_sig = f"{step.action_type.value}:{json.dumps(step.params, sort_keys=True, default=str)}"

            # 1. Check 3 identical consecutive actions
            if len(action_signatures) >= 2 and action_signatures[-1] == step_sig and action_signatures[-2] == step_sig:
                LOG.error("Loop detected: 3 identical consecutive actions attempted (%s). Aborting cycle.", step_sig)
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=idx - 1,
                    records=records,
                    recovery_triggered=True,
                    error=f"Loop detected: 3 identical consecutive actions ({step_name})",
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # 2. Check alternating 2-step cycle: [A, B, A, B]
            if len(action_signatures) >= 3 and step_sig == action_signatures[-2] and action_signatures[-1] == action_signatures[-3] and step_sig != action_signatures[-1]:
                LOG.error("Loop cycle detected: alternating pattern [A, B, A, B] without progress. Aborting cycle.")
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=idx - 1,
                    records=records,
                    recovery_triggered=True,
                    error="Loop cycle detected: alternating pattern [A, B, A, B] without progress",
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # 3. Check 3-step cycle: [A, B, C, A, B, C]
            if len(action_signatures) >= 5 and step_sig == action_signatures[-3] and action_signatures[-1] == action_signatures[-4] and action_signatures[-2] == action_signatures[-5]:
                LOG.error("Loop cycle detected: 3-step pattern [A, B, C, A, B, C] without progress. Aborting cycle.")
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=idx - 1,
                    records=records,
                    recovery_triggered=True,
                    error="Loop cycle detected: 3-step pattern [A, B, C, A, B, C] without progress",
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            action_signatures.append(step_sig)

            # Pre-step: Explicit step human requirement check
            if step.requires_human:
                LOG.warning("Step '%s' requires human intervention.", step_name)
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=idx - 1,
                    records=records,
                    paused_for_human=True,
                    intervention_details={
                        "action": step_name,
                        "intervention_type": step.human_intervention_type or "HUMAN_REQUIRED",
                        "reason": step.human_intervention_reason or f"Step '{step_name}' explicitly requires human action.",
                        "prompt": step.human_prompt or "Please complete the required interaction on screen.",
                    },
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # SecurityGate Evaluation
            step_ctx = self._step_security_context(
                untrusted_content_seen=untrusted_content_seen,
                confirmation_token=step.confirmation_token,
            )

            ev = self.security_gate.evaluate(
                action_name=step_name,
                params=step.params,
                context=step_ctx,
            )

            if ev.requires_human_intervention or (ev.requires_confirmation and not ev.allowed):
                LOG.warning("SecurityGate barrier triggered on step '%s': %s", step_name, ev.reason)
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=idx - 1,
                    records=records,
                    paused_for_human=True,
                    error=f"SecurityGate blocked '{step_name}': {ev.reason}",
                    intervention_details=ev.to_dict(),
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            if not ev.allowed:
                LOG.error("SecurityGate blocked step '%s': %s", step_name, ev.reason)
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=idx - 1,
                    records=records,
                    error=f"SecurityGate blocked '{step_name}': {ev.reason}",
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # Act, Observe Again, Verify, Recover via WISEHands
            rec = self.hands.execute_closed_loop_action(
                action_type=step.action_type,
                params=step.params,
                verification_condition=step.verification_spec,
                max_retries=step.max_retries,
                security_context=step_ctx,
            )
            records.append(rec)
            untrusted_content_seen = untrusted_content_seen or self._record_contains_untrusted_web_content(rec)

            if rec.recovery_attempted:
                recovery_triggered = True
            if not rec.verification_result:
                all_verified = False
                overall_success = False
                LOG.warning("Step '%s' verification failed.", step_name)

            # Update World State after each action
            self.state_engine.update_task_progress(
                intent=intent,
                current_step=idx,
                total_steps=len(steps),
                active_plan=plan_names,
                verification_status="VERIFIED" if all_verified else "UNVERIFIED",
            )

        # Update World State with final synthesized snapshot
        final_state = self.state_engine.get_current_world_state(force_fresh=True)
        final_summary = self.fusion_engine.fuse_world_state(final_state)

        total_latency = (time.perf_counter() - t0) * 1000

        return OrchestrationCycleResult(
            intent=intent,
            success=overall_success,
            steps_executed=len(steps),
            records=records,
            recovery_triggered=recovery_triggered,
            verified=all_verified,
            total_latency_ms=total_latency,
            final_state_summary=final_summary,
        )

    def orchestrate_intent(
        self,
        intent: str,
        max_replans: int = 3,
    ) -> OrchestrationCycleResult:
        """
        Autonomous Cognitive Execution Pipeline with HITL Governance:
        1. Ingest Natural Language Intent
        2. Ground against live World State
        3. Formulate Autonomous Plan via CognitivePlanner
        4. Detect Ambiguity / Insufficient Info -> Return honest clarification request
        5. Execute steps with SecurityGate, ChallengeDetector, and dynamic re-planning
        6. Pause cleanly on CAPTCHA / MFA / High-Impact confirmations without resetting progress
        7. Update World State and return synthesized result
        """
        t0 = time.perf_counter()
        from core.brain import get_cognitive_planner, get_self_healing_engine, PlannedStep

        planner = get_cognitive_planner()
        healer = get_self_healing_engine()

        # 1 & 2. Ground intent with live World State
        initial_state = self.state_engine.get_current_world_state(force_fresh=True)
        plan = planner.create_plan(intent, world_state=initial_state)

        # 3. Check for clarification requirement
        if plan.status == "NEEDS_CLARIFICATION":
            LOG.info("Intent requires user clarification: %s", plan.clarification_message)
            return OrchestrationCycleResult(
                intent=intent,
                success=False,
                steps_executed=0,
                needs_clarification=True,
                clarification_message=plan.clarification_message,
                final_state_summary=plan.clarification_message or "",
                total_latency_ms=(time.perf_counter() - t0) * 1000,
            )

        if plan.status == "RESTRICTED":
            LOG.warning("Intent blocked by policy: %s", plan.safety_warning)
            return OrchestrationCycleResult(
                intent=intent,
                success=False,
                steps_executed=0,
                error=plan.safety_warning,
                total_latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # Convert PlannedStep to OrchestrationStep
        active_steps = [
            OrchestrationStep(
                action_type=s.action_type,
                params=s.params,
                verification_spec=s.verification_spec,
                requires_human=getattr(s, "requires_human", False),
                human_intervention_type=getattr(s, "human_intervention_type", None),
                human_intervention_reason=getattr(s, "human_intervention_reason", None),
                human_prompt=getattr(s, "human_prompt", None),
                confirmation_token=getattr(s, "confirmation_token", None),
            )
            for s in plan.steps
        ]

        # Initialize Task in TaskEngine if available
        task_engine = None
        task_id = None
        if getattr(plan, "task", None):
            try:
                from core.brain.task_engine import get_task_engine
                task_engine = get_task_engine()
                task_id = plan.task.task_id
                task_engine.start_task(task_id)
            except Exception as te_err:
                LOG.warning("Failed to initialize task in TaskEngine: %s", te_err)

        effective_task_id = task_id or f"task_{int(t0 * 1000)}"
        provider = None
        try:
            from core.models.provider_interface import get_model_provider
            provider = get_model_provider()
            if hasattr(provider, "begin_task"):
                provider.begin_task(effective_task_id)
        except Exception as p_err:
            LOG.debug("Provider task pinning not active: %s", p_err)

        try:
            return self._execute_step_loop(
                intent=intent,
                active_steps=active_steps,
                start_step_idx=0,
                records=[],
                replans_done=0,
                max_replans=max_replans,
                t0=t0,
                plan=plan,
                task_engine=task_engine,
                task_id=task_id,
                healer=healer,
            )
        finally:
            if provider and hasattr(provider, "end_task"):
                try:
                    provider.end_task(effective_task_id)
                except Exception as p_end_err:
                    LOG.debug("Provider end_task error: %s", p_end_err)

    def _execute_step_loop(
        self,
        intent: str,
        active_steps: List[OrchestrationStep],
        start_step_idx: int,
        records: List[ActionRecord],
        replans_done: int,
        max_replans: int,
        t0: float,
        plan: Any,
        task_engine: Any,
        task_id: Optional[str],
        healer: Any,
    ) -> OrchestrationCycleResult:
        """Internal execution loop capable of clean resumption after human intervention."""
        recovery_triggered = False
        all_verified = True
        overall_success = True
        untrusted_content_seen = self.security_context.is_untrusted_content

        plan_names = [f"{s.action_type.value}" for s in active_steps]
        self.state_engine.update_task_progress(
            intent=intent,
            current_step=start_step_idx,
            total_steps=len(active_steps),
            active_plan=plan_names,
            verification_status="IN_PROGRESS",
        )

        step_idx = start_step_idx
        action_signatures: List[str] = []
        failed_signature_counts: Dict[str, int] = {}

        while step_idx < len(active_steps):
            step = active_steps[step_idx]
            step_name = step.params.get("action_name") or step.action_type.value
            step_sig = f"{step.action_type.value}:{json.dumps(step.params, sort_keys=True, default=str)}"

            # Stale-frame validation before coordinate-based actions
            if step.params.get("frame_signature"):
                if not self.hands.validate_frame_signature(step.params.get("frame_signature")):
                    LOG.warning("Stale frame detected before step '%s'. Refreshing frame signature.", step_name)
                    step.params["frame_signature"] = self.hands.create_frame_signature(step.params.get("hwnd"))

            # Loop & Failure Governance:
            # 1. Repeated failure avoidance: if this exact signature failed twice consecutively, do not attempt a third time with identical parameters
            if failed_signature_counts.get(step_sig, 0) >= 2:
                LOG.error("Repeated failure avoidance: step '%s' failed twice consecutively. Forbidding third duplicate attempt.", step_name)
                if task_engine and task_id:
                    task_engine.fail_task(task_id, error=f"Repeated failure avoidance: '{step_name}' failed twice consecutively")
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=step_idx,
                    records=records,
                    error=f"Repeated failure avoidance: step '{step_name}' failed twice consecutively with identical parameters",
                    task_id=task_id,
                    task=plan.task if getattr(plan, "task", None) else None,
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # 2. Check 3 identical consecutive actions
            if len(action_signatures) >= 2 and action_signatures[-1] == step_sig and action_signatures[-2] == step_sig:
                LOG.error("Loop detected: 3 identical consecutive actions attempted (%s). Aborting to prevent cycle.", step_sig)
                if task_engine and task_id:
                    task_engine.fail_task(task_id, error=f"Loop detected: 3 identical consecutive actions ({step_name})")
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=step_idx,
                    records=records,
                    error=f"Loop detected: 3 identical consecutive actions ({step_name})",
                    task_id=task_id,
                    task=plan.task if getattr(plan, "task", None) else None,
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # 3. Check alternating 2-step cycle: [A, B, A, B]
            if len(action_signatures) >= 3 and action_signatures[-2] == step_sig and action_signatures[-3] == action_signatures[-1]:
                LOG.warning("Alternating action loop [A, B, A, B] detected for '%s'. Escalating to replanning.", step_sig)
                if replans_done < max_replans:
                    replans_done += 1
                    recovery_triggered = True
                else:
                    if task_engine and task_id:
                        task_engine.fail_task(task_id, error="Loop cycle detected: alternating pattern [A, B, A, B] exceeded max replans")
                    return OrchestrationCycleResult(
                        intent=intent,
                        success=False,
                        steps_executed=step_idx,
                        records=records,
                        error="Loop cycle detected: alternating pattern [A, B, A, B] without progress",
                        task_id=task_id,
                        task=plan.task if getattr(plan, "task", None) else None,
                        total_latency_ms=(time.perf_counter() - t0) * 1000,
                    )

            # 4. Check 3-step cyclic loop: [A, B, C, A, B, C]
            if (
                len(action_signatures) >= 5
                and action_signatures[-3] == step_sig
                and action_signatures[-4] == action_signatures[-1]
                and action_signatures[-5] == action_signatures[-2]
            ):
                LOG.warning("3-step cyclic loop [A, B, C, A, B, C] detected for '%s'. Escalating to replanning.", step_sig)
                if replans_done < max_replans:
                    replans_done += 1
                    recovery_triggered = True
                else:
                    if task_engine and task_id:
                        task_engine.fail_task(task_id, error="Loop cycle detected: 3-step pattern [A, B, C, A, B, C] exceeded max replans")
                    return OrchestrationCycleResult(
                        intent=intent,
                        success=False,
                        steps_executed=step_idx,
                        records=records,
                        error="Loop cycle detected: 3-step pattern [A, B, C, A, B, C] without progress",
                        task_id=task_id,
                        task=plan.task if getattr(plan, "task", None) else None,
                        total_latency_ms=(time.perf_counter() - t0) * 1000,
                    )

            # 1. Step explicit human requirement
            if step.requires_human:
                if task_engine and task_id:
                    task_engine.pause_for_human(
                        task_id=task_id,
                        intervention_type=step.human_intervention_type or "HUMAN_REQUIRED",
                        reason=step.human_intervention_reason or f"Step '{step_name}' explicitly requires human action.",
                        prompt=step.human_prompt or "Please complete the required interaction on screen.",
                        details={"step_idx": step_idx, "step_name": step_name},
                    )
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=step_idx,
                    records=records,
                    paused_for_human=True,
                    intervention_details={
                        "step_idx": step_idx,
                        "action": step_name,
                        "intervention_type": step.human_intervention_type or "HUMAN_REQUIRED",
                        "reason": step.human_intervention_reason or f"Step '{step_name}' explicitly requires human action.",
                        "prompt": step.human_prompt or "Please complete the required interaction on screen.",
                    },
                    task_id=task_id,
                    task=plan.task if getattr(plan, "task", None) else None,
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # 2. SecurityGate Evaluation
            step_ctx = self._step_security_context(
                untrusted_content_seen=untrusted_content_seen,
                confirmation_token=step.confirmation_token,
            )

            ev = self.security_gate.evaluate(
                action_name=step_name,
                params=step.params,
                context=step_ctx,
            )

            # Check if SecurityGate requires human intervention or confirmation
            if ev.requires_human_intervention or (ev.requires_confirmation and not ev.allowed):
                if task_engine and task_id:
                    itype = ev.intervention_type.value if ev.intervention_type else "CONFIRMATION"
                    task_engine.pause_for_human(
                        task_id=task_id,
                        intervention_type=itype,
                        reason=ev.reason,
                        prompt=ev.reason,
                        details=ev.to_dict(),
                    )
                LOG.warning("Step '%s' PAUSED_FOR_HUMAN by SecurityGate: %s", step_name, ev.reason)
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=step_idx,
                    records=records,
                    paused_for_human=True,
                    intervention_details=ev.to_dict(),
                    task_id=task_id,
                    task=plan.task if getattr(plan, "task", None) else None,
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            if not ev.allowed:
                LOG.error("SecurityGate blocked step '%s': %s", step_name, ev.reason)
                if task_engine and task_id:
                    task_engine.fail_task(task_id, error=f"SecurityGate blocked: {ev.reason}")
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=step_idx,
                    records=records,
                    error=f"SecurityGate blocked '{step_name}': {ev.reason}",
                    task_id=task_id,
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # 3. Pre-Action Challenge Detection (Inspect World State)
            current_world = self.state_engine.get_current_world_state(force_fresh=False)
            ch_res = self.challenge_detector.evaluate_world_state(current_world)
            if ch_res.detected and ch_res.requires_human:
                if task_engine and task_id:
                    task_engine.pause_for_human(
                        task_id=task_id,
                        intervention_type=ch_res.challenge_type.value if ch_res.challenge_type else "SECURITY_CHALLENGE",
                        reason=ch_res.reason,
                        prompt=ch_res.prompt_to_user,
                        details=ch_res.to_dict(),
                    )
                LOG.warning("Active challenge detected before step '%s': %s", step_name, ch_res.reason)
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=step_idx,
                    records=records,
                    paused_for_human=True,
                    intervention_details=ch_res.to_dict(),
                    task_id=task_id,
                    task=plan.task if getattr(plan, "task", None) else None,
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # Record signature before action execution
            action_signatures.append(step_sig)

            # 4. Act via Hands
            rec = self.hands.execute_closed_loop_action(
                action_type=step.action_type,
                params=step.params,
                verification_condition=step.verification_spec,
                max_retries=step.max_retries,
                security_context=step_ctx,
            )
            records.append(rec)
            untrusted_content_seen = untrusted_content_seen or self._record_contains_untrusted_web_content(rec)

            # 5. Observe post-state
            current_world = self.state_engine.get_current_world_state(force_fresh=True)

            # Check if post-state revealed a new challenge (e.g. navigation landed on CAPTCHA / MFA)
            post_ch = self.challenge_detector.evaluate_world_state(current_world)
            if post_ch.detected and post_ch.requires_human:
                if task_engine and task_id:
                    task_engine.pause_for_human(
                        task_id=task_id,
                        intervention_type=post_ch.challenge_type.value if post_ch.challenge_type else "SECURITY_CHALLENGE",
                        reason=post_ch.reason,
                        prompt=post_ch.prompt_to_user,
                        details=post_ch.to_dict(),
                    )
                LOG.warning("Post-action challenge detected: %s", post_ch.reason)
                return OrchestrationCycleResult(
                    intent=intent,
                    success=False,
                    steps_executed=step_idx + 1,
                    records=records,
                    paused_for_human=True,
                    intervention_details=post_ch.to_dict(),
                    task_id=task_id,
                    task=plan.task if getattr(plan, "task", None) else None,
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

            # Update failure counts
            if not rec.verification_result:
                failed_signature_counts[step_sig] = failed_signature_counts.get(step_sig, 0) + 1
            else:
                failed_signature_counts[step_sig] = 0

            # Obstacle / Failure Check & Healing
            is_zero_effect = (rec.action_result.get("failure_type") == "ZERO_EFFECT" or 
                              (rec.action_result.get("success") and not rec.verification_result and rec.pre_state_sig == rec.post_state_sig))
            failure_reason = "ZERO_EFFECT: Action executed successfully but desktop state was unaffected" if is_zero_effect else rec.action_result.get("error", "Verification failure or modal dialog detected")

            needs_recovery = (not rec.verification_result) or bool(current_world.open_dialogs) or is_zero_effect
            if needs_recovery and replans_done < max_replans:
                LOG.info("Obstacle detected (%s). Triggering Self-Healing re-planning...", failure_reason)
                from core.brain import PlannedStep
                diagnosis = healer.diagnose_and_heal(
                    failed_step=PlannedStep(action_type=step.action_type, params=step.params, verification_spec=step.verification_spec),
                    failure_reason=failure_reason,
                    world_state=current_world,
                )

                if diagnosis.can_recover and diagnosis.corrective_steps:
                    recovery_triggered = True
                    replans_done += 1

                    for c_step in diagnosis.corrective_steps:
                        c_name = c_step.params.get("action_name") or c_step.action_type.value
                        c_ctx = self._step_security_context(
                            untrusted_content_seen=untrusted_content_seen,
                            confirmation_token=c_step.confirmation_token,
                        )
                        c_ev = self.security_gate.evaluate(
                            action_name=c_name,
                            params=c_step.params,
                            context=c_ctx,
                        )
                        if c_ev.allowed:
                            c_rec = self.hands.execute_closed_loop_action(
                                action_type=c_step.action_type,
                                params=c_step.params,
                                verification_condition=c_step.verification_spec,
                                security_context=c_ctx,
                            )
                            records.append(c_rec)
                        else:
                            LOG.warning("SecurityGate blocked corrective step '%s': %s", c_name, c_ev.reason)

                    if step.verification_spec:
                        verified_now = self.hands.verify_condition(step.verification_spec)
                        if verified_now:
                            rec.verification_result = True
                            failed_signature_counts[step_sig] = 0
                            LOG.info("Step '%s' successfully recovered and verified!", step_name)

            if not rec.verification_result:
                all_verified = False
                overall_success = False

            if task_engine and task_id:
                try:
                    task_engine.advance_step(task_id)
                except Exception as te_adv_err:
                    LOG.warning("Failed to advance step in TaskEngine: %s", te_adv_err)

            step_idx += 1
            self.state_engine.update_task_progress(
                intent=intent,
                current_step=step_idx,
                total_steps=len(active_steps),
                active_plan=plan_names,
                verification_status="VERIFIED" if all_verified else "UNVERIFIED",
            )

        if task_engine and task_id:
            try:
                if overall_success:
                    task_engine.complete_task(task_id)
                else:
                    task_engine.fail_task(task_id, error="One or more steps failed verification")
            except Exception as te_fin_err:
                LOG.warning("Failed to finalize task in TaskEngine: %s", te_fin_err)

        final_state = self.state_engine.get_current_world_state(force_fresh=True)
        final_summary = self.fusion_engine.fuse_world_state(final_state)
        total_latency = (time.perf_counter() - t0) * 1000

        return OrchestrationCycleResult(
            intent=intent,
            success=overall_success,
            steps_executed=step_idx,
            records=records,
            recovery_triggered=recovery_triggered,
            verified=all_verified,
            total_latency_ms=total_latency,
            final_state_summary=final_summary,
            task_id=task_id,
            task=plan.task if getattr(plan, "task", None) else None,
        )

    def submit_human_intervention_resolution(
        self,
        task_id: str,
        action: str = "completed",
        resolution_payload: Optional[Dict[str, Any]] = None,
        confirmation_token: Optional[str] = None,
        verification_condition: Optional[Dict[str, Any]] = None,
    ) -> OrchestrationCycleResult:
        """
        Processes human intervention completion (e.g. user finished CAPTCHA or approved payment).
        Forces fresh observation, verifies challenge resolution, and resumes the exact interrupted step.
        """
        t0 = time.perf_counter()
        from core.brain.task_engine import get_task_engine, TaskStatus
        task_engine = get_task_engine()
        task = task_engine.get_task(task_id)

        if not task:
            return OrchestrationCycleResult(
                intent="resume_task",
                success=False,
                steps_executed=0,
                error=f"Task '{task_id}' not found.",
                total_latency_ms=(time.perf_counter() - t0) * 1000,
            )

        if task.status not in (TaskStatus.PAUSED_FOR_HUMAN, TaskStatus.PAUSED):
            return OrchestrationCycleResult(
                intent=task.user_intent,
                success=False,
                steps_executed=0,
                error=f"Task '{task_id}' is not paused (current: {task.status.value}).",
                total_latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # Handle explicit cancellation/rejection
        action_lower = action.strip().lower()
        if action_lower in ("cancelled", "rejected", "abort", "cancel"):
            task_engine.resolve_human_intervention(task_id, action="cancelled")
            return OrchestrationCycleResult(
                intent=task.user_intent,
                success=False,
                steps_executed=0,
                error="Human intervention was cancelled or rejected by the user.",
                task_id=task_id,
                task=task,
                total_latency_ms=(time.perf_counter() - t0) * 1000,
            )

        if not task.subgoals or task.current_subgoal_idx >= len(task.subgoals):
            return OrchestrationCycleResult(intent=task.user_intent, success=False,
                steps_executed=0, error="No saved executable plan remains; send a new continuation request.",
                task_id=task_id, task=task, total_latency_ms=(time.perf_counter() - t0) * 1000)

        # Force fresh observation of live world state
        fresh_world = self.state_engine.get_current_world_state(force_fresh=True)

        # Verification Spec Check (if provided)
        if verification_condition:
            verified = self.hands.verify_condition(verification_condition)
            if not verified:
                LOG.warning("Post-intervention verification condition failed.")
                return OrchestrationCycleResult(
                    intent=task.user_intent,
                    success=False,
                    steps_executed=0,
                    paused_for_human=True,
                    error="Verification condition failed after human intervention.",
                    task_id=task_id,
                    task=task,
                    total_latency_ms=(time.perf_counter() - t0) * 1000,
                )

        # Verify challenge is cleared via ChallengeDetector
        ch_res = self.challenge_detector.evaluate_world_state(fresh_world)
        if ch_res.detected and ch_res.confidence >= 0.85:
            LOG.warning("Challenge still present after human intervention: %s", ch_res.reason)
            return OrchestrationCycleResult(
                intent=task.user_intent,
                success=False,
                steps_executed=0,
                paused_for_human=True,
                intervention_details=ch_res.to_dict(),
                error="Challenge is still active. Please complete the verification before resuming.",
                task_id=task_id,
                task=task,
                total_latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # Resume task in TaskEngine (transitions to RUNNING without resetting progress)
        resumed = (task_engine.resolve_human_intervention(task_id, action="completed", resolution_payload=resolution_payload)
                   if task.status == TaskStatus.PAUSED_FOR_HUMAN else task_engine.resume_task(task_id))
        if not resumed:
            return OrchestrationCycleResult(
                intent=task.user_intent,
                success=False,
                steps_executed=0,
                error=f"Failed to resume task '{task_id}' in TaskEngine.",
                task_id=task_id,
                total_latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # Build active steps from current subgoal/step
        current_sg = task.subgoals[task.current_subgoal_idx]
        current_step_idx = current_sg.current_step_idx

        active_steps: List[OrchestrationStep] = []
        for sg in task.subgoals:
            for s in sg.steps:
                is_current = (s == task.get_current_step())
                # If current step required human, that intervention is now satisfied
                if is_current and s.requires_human:
                    s.requires_human = False
                c_tok = confirmation_token if is_current else s.confirmation_token
                active_steps.append(
                    OrchestrationStep(
                        action_type=s.action_type,
                        params=s.params,
                        verification_spec=s.verification_spec,
                        max_retries=s.max_retries,
                        requires_human=False if is_current else s.requires_human,
                        human_intervention_type=s.human_intervention_type,
                        human_intervention_reason=s.human_intervention_reason,
                        human_prompt=s.human_prompt,
                        confirmation_token=c_tok,
                    )
                )

        # Calculate flattened step index
        flattened_idx = sum(len(task.subgoals[i].steps) for i in range(task.current_subgoal_idx)) + current_step_idx

        from core.brain import get_self_healing_engine
        healer = get_self_healing_engine()

        LOG.info(
            "Resuming Task '%s' execution at flattened step index %d (Subgoal %d, Step %d)",
            task_id,
            flattened_idx,
            task.current_subgoal_idx,
            current_step_idx,
        )

        return self._execute_step_loop(
            intent=task.user_intent,
            active_steps=active_steps,
            start_step_idx=flattened_idx,
            records=[],
            replans_done=0,
            max_replans=3,
            t0=t0,
            plan=None,
            task_engine=task_engine,
            task_id=task_id,
            healer=healer,
        )


# Global Singleton Orchestrator maintained for backward compatibility
_ORCHESTRATOR: Optional[ClosedLoopOrchestrator] = None
_ORC_LOCK = threading.Lock()


def get_closed_loop_orchestrator(security_context: Optional[SecurityContext] = None) -> ClosedLoopOrchestrator:
    global _ORCHESTRATOR
    if _ORCHESTRATOR is None:
        with _ORC_LOCK:
            if _ORCHESTRATOR is None:
                _ORCHESTRATOR = ClosedLoopOrchestrator(security_context=security_context)
    return _ORCHESTRATOR
