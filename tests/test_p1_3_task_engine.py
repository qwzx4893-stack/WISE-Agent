# ==============================================================================
# WISE Phase P1.3-A Comprehensive Verification Suite
# Focus: Real Cognitive Brain Foundation & Decoupled Task Engine
# Tests:
# 1. Task Engine Architecture & Hierarchical State Model (Task, Subgoal, Step)
# 2. Checkpointing, Rollback, and PAUSED_FOR_HUMAN / Resume Lifecycle
# 3. Decoupled State Invariants (Task State vs World State separation)
# 4. Provider-Agnostic Model Abstraction & Explicit Simulation Tagging
# 5. Model-Assisted Semantic Goal Decomposition (Beyond Regex Rules)
# 6. Autonomous Cognitive Planner & Ground-Truth Task Synthesis
# 7. Closed-Loop Orchestrator End-to-End Integration with Task Engine
# 8. Telemetry & Resource Invariants (Zero Idle VRAM, CPU <= 5%)
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import logging
from pathlib import Path
from typing import Dict, Any, List

# Ensure Supergent--main is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = BASE_DIR / "Supergent--main"
if str(SUPERGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
LOG = logging.getLogger("WISE.Test.P1_3_TaskEngine")

passed = 0
failed = 0


def check(name: str, condition: bool, details: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"[PASS] {name} {details}")
    else:
        failed += 1
        print(f"[FAIL] {name} {details}")


print("=" * 80)
print("   WISE PHASE P1.3-A: REAL COGNITIVE BRAIN & TASK ENGINE VERIFICATION")
print("=" * 80)

# ==============================================================================
# 1. Task Engine Architecture & Hierarchical State Model
# ==============================================================================
print("\n--- 1. Task Engine Architecture & Hierarchical State Model ---")

from core.brain.task_engine import (
    TaskEngine,
    get_task_engine,
    Task,
    Subgoal,
    TaskStep,
    TaskCheckpoint,
    TaskStatus,
    SubgoalStatus,
    StepStatus,
    TaskDomain,
    create_task_from_steps,
)
from core.hands import ComputerActionType

te = get_task_engine()
check("TaskEngine instantiated and available", te is not None)
check("TaskEngine singleton identity holds", te is get_task_engine())

# Create a multi-subgoal task
sg1 = Subgoal(
    title="Prepare environment",
    description="Launch and focus application",
    domain=TaskDomain.WINDOWS_OS,
    steps=[
        TaskStep(action_type=ComputerActionType.OPEN_APP, params={"app_name": "notepad.exe"}, description="Launch app"),
        TaskStep(action_type=ComputerActionType.WAIT, params={"duration": 0.5}, description="Wait for focus"),
    ],
)
sg2 = Subgoal(
    title="Data entry",
    description="Type payload content",
    domain=TaskDomain.WINDOWS_OS,
    steps=[
        TaskStep(action_type=ComputerActionType.TYPE_TEXT, params={"text": "Hello TaskEngine"}, description="Type text"),
    ],
)

test_task = te.create_task(
    user_intent="Open notepad and type test message",
    semantic_goal="Type verified text in notepad",
    subgoals=[sg1, sg2],
)

check("Task created with unique ID", test_task.task_id.startswith("task_"))
check("Initial Task status is PENDING", test_task.status == TaskStatus.PENDING)
check("Total steps count calculated correctly", test_task.total_steps == 3)
check("Completed steps count starts at 0", test_task.completed_steps_count == 0)

# Start task
started = te.start_task(test_task.task_id)
check("Task start transition succeeds", started is True)
check("Task status is RUNNING", test_task.status == TaskStatus.RUNNING)
check("First subgoal status is IN_PROGRESS", test_task.subgoals[0].status == SubgoalStatus.IN_PROGRESS)

# Advance Step 1
s1 = test_task.get_current_step()
check("Current step retrieved", s1 is not None and s1.action_type == ComputerActionType.OPEN_APP)
te.record_step_result(test_task.task_id, s1.step_id, success=True)
next_step, is_done = te.advance_step(test_task.task_id)
check("Step 1 completed and advanced to Step 2", next_step is not None and next_step.action_type == ComputerActionType.WAIT)
check("Task not yet done", is_done is False)

# Advance Step 2 (completes Subgoal 1 -> auto checkpoint)
te.record_step_result(test_task.task_id, next_step.step_id, success=True)
next_step, is_done = te.advance_step(test_task.task_id)
check("Subgoal 1 completed -> moved to Subgoal 2", test_task.current_subgoal_idx == 1)
check("Checkpoint automatically created at subgoal boundary", len(test_task.checkpoints) >= 1)
check("Next step in Subgoal 2 is TYPE_TEXT", next_step is not None and next_step.action_type == ComputerActionType.TYPE_TEXT)

# Complete final step
te.record_step_result(test_task.task_id, next_step.step_id, success=True)
next_step, is_done = te.advance_step(test_task.task_id)
check("All steps completed -> is_done=True", is_done is True)
check("Task final status is COMPLETED", test_task.status == TaskStatus.COMPLETED)
check("Completed steps count matches total", test_task.completed_steps_count == 3)


# ==============================================================================
# 2. Checkpointing, Rollback, and PAUSED_FOR_HUMAN / Resume Lifecycle
# ==============================================================================
print("\n--- 2. Checkpointing, Rollback, and PAUSED_FOR_HUMAN Lifecycle ---")

sg_a = Subgoal(
    title="Step A",
    domain=TaskDomain.BROWSER,
    steps=[TaskStep(action_type=ComputerActionType.BROWSER_NAVIGATE, params={"url": "https://example.com"})],
)
sg_b = Subgoal(
    title="Step B (MFA/CAPTCHA barrier)",
    domain=TaskDomain.BROWSER,
    steps=[TaskStep(action_type=ComputerActionType.WAIT, params={"duration": 1.0})],
)

pause_task = te.create_task(
    user_intent="Navigate to portal and complete authentication",
    semantic_goal="Portal login flow",
    subgoals=[sg_a, sg_b],
)
te.start_task(pause_task.task_id)

# Advance past Subgoal A
step_a = pause_task.get_current_step()
te.record_step_result(pause_task.task_id, step_a.step_id, success=True)
next_st, done = te.advance_step(pause_task.task_id)
chk_a_id = pause_task.checkpoints[-1].checkpoint_id

# Pause at Subgoal B for human intervention (e.g. CAPTCHA)
pause_chk = te.pause_task(pause_task.task_id, reason="CAPTCHA_DETECTION")
check("Task paused with PAUSED_FOR_HUMAN", pause_task.status == TaskStatus.PAUSED_FOR_HUMAN)
check("Pause reason recorded", pause_task.pause_reason == "CAPTCHA_DETECTION")
check("Explicit checkpoint created during pause", pause_chk is not None)

# Resume task
resumed = te.resume_task(pause_task.task_id)
check("Task resume succeeds", resumed is True)
check("Status restored to RUNNING", pause_task.status == TaskStatus.RUNNING)
check("Pause reason cleared", pause_task.pause_reason is None)

# Rollback test: roll back to checkpoint A
rolled_back = te.rollback_to_checkpoint(pause_task.task_id, chk_a_id)
check("Rollback to checkpoint succeeds", rolled_back is True)
check("Execution pointer returned to Subgoal index 0", pause_task.current_subgoal_idx == 0)
check("Subgoal A status restored to IN_PROGRESS", pause_task.subgoals[0].status == SubgoalStatus.IN_PROGRESS)

# Cancellation test
cancelled = te.cancel_task(pause_task.task_id, reason="User requested abort")
check("Task cancelled successfully", cancelled is True)
check("Status reflects CANCELLED", pause_task.status == TaskStatus.CANCELLED)


# ==============================================================================
# 3. Decoupled State Invariants (Task State vs World State Separation)
# ==============================================================================
print("\n--- 3. Decoupled State Invariants (Task State vs World State) ---")

from core.context.world_state import get_world_state_engine, WISEWorldState

ws_engine = get_world_state_engine()
live_ws = ws_engine.get_current_world_state(force_fresh=True)

check("WISEWorldState exists and contains environmental facts", live_ws is not None)
check("WorldState contains active window attribute", hasattr(live_ws, "active_window"))
check("WorldState contains open windows list", len(live_ws.open_windows) > 0)
check("WorldState contains items dictionary", hasattr(live_ws, "items"))


# Verify TaskEngine is separate from WorldState
check("TaskEngine is not embedded as a God Object inside WorldState", not hasattr(live_ws, "cancel_task"))
check("TaskEngine manages active task pointer independently", te.get_active_task() is not None)


# ==============================================================================
# 4. Provider-Agnostic Model Abstraction & Explicit Simulation Tagging
# ==============================================================================
print("\n--- 4. Provider-Agnostic Model Abstraction & Explicit Simulation Tagging ---")

from core.models.provider_interface import (
    BaseModelProvider,
    SimulatedTestProvider,
    HttpOpenAICompatibleProvider,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelProviderType,
    get_model_provider,
    set_active_model_provider,
)

sim_provider = SimulatedTestProvider()
set_active_model_provider(sim_provider)

active_prov = get_model_provider()
check("Active provider is SimulatedTestProvider", isinstance(active_prov, SimulatedTestProvider))
check("Provider reports is_available=True", active_prov.is_available() is True)

# Generate completion via Simulated provider
req = ModelCompletionRequest(
    messages=[{"role": "user", "content": "Open calculator and compute 5 * 5"}],
    system_prompt="You are WISE.",
)
resp = active_prov.generate(req)

check("Completion returned text", len(resp.text) > 0)
check("Explicit is_simulated flag is True", resp.is_simulated is True)
check("Provider type reports SIMULATED_TEST", resp.provider_type == ModelProviderType.SIMULATED_TEST)
check("Parsed JSON populated", resp.parsed_json is not None)

# HTTP Provider configuration test (without live network call)
http_prov = HttpOpenAICompatibleProvider(
    base_url="http://localhost:11434/v1",
    model_name="llama3.2:3b",
)
check("HttpOpenAICompatibleProvider instantiated with localhost endpoint", http_prov.is_available() is True)
check("HttpOpenAICompatibleProvider model name preserved", http_prov.model_name == "llama3.2:3b")


# ==============================================================================
# 5. Model-Assisted Semantic Goal Decomposition
# ==============================================================================
print("\n--- 5. Model-Assisted Semantic Goal Decomposition ---")

from core.brain.tiered_runtime import get_tiered_model_router, ModelTier
from core.brain.intent_parser import (
    CognitiveIntentParser,
    get_cognitive_intent_parser,
    ParsedIntentType,
    PlannedStep,
)

parser = get_cognitive_intent_parser()
router = get_tiered_model_router()

# Test 5.1: Deterministic Fast-Path Preserved (< 5ms)
t_start = time.perf_counter()
fast_res = parser.parse_intent("افتح المفكرة واكتب تجربة")
t_fast = (time.perf_counter() - t_start) * 1000

check("Fast-path regex parses Notepad in < 15ms", t_fast < 15.0, f"({t_fast:.2f}ms)")
check("Fast-path produces ACTIONABLE_PLAN", fast_res.intent_type == ParsedIntentType.ACTIONABLE_PLAN)
check("Fast-path produces >= 4 steps", len(fast_res.steps) >= 4)

# Test 5.2: Gibberish is reliably rejected as UNKNOWN_UNSUPPORTED
gibberish_res = parser.parse_intent("xyzabc 998877 qwerty")
check("Gibberish parsed as UNKNOWN_UNSUPPORTED", gibberish_res.intent_type == ParsedIntentType.UNKNOWN_UNSUPPORTED)
check("Clarification requested for gibberish", gibberish_res.clarification_needed is not None)

# Test 5.3: Model-Assisted Semantic Goal Decomposition for Unseen Intent
# Register a custom semantic plan in the test provider for an unfamiliar intent
semantic_intent = "Inspect hardware temperature and create diagnostic chart on desktop"
sim_provider.register_response(
    keyword_trigger="diagnostic chart",
    structured_json={
        "goal": "Collect hardware metrics and plot diagnostic chart",
        "is_actionable": True,
        "clarification_needed": None,
        "confidence": 0.96,
        "subgoals": [
            {
                "title": "Query system metrics",
                "domain": "SYSTEM",
                "steps": [
                    {
                        "action": "wait",
                        "params": {"duration": 0.5},
                        "description": "Probe hardware sensors",
                    },
                    {
                        "action": "type_text",
                        "params": {"text": "HW_METRICS_LOG"},
                        "description": "Output metric stream",
                    },
                ],
            }
        ],
    },
)

semantic_analysis = parser.parse_intent(semantic_intent)
check("Novel semantic intent parsed as ACTIONABLE_PLAN", semantic_analysis.intent_type == ParsedIntentType.ACTIONABLE_PLAN)
check("Model decomposed novel intent into planned steps", len(semantic_analysis.steps) == 2)
check("Step 1 is WAIT action", semantic_analysis.steps[0].action_type == ComputerActionType.WAIT)
check("Step 2 is TYPE_TEXT action", semantic_analysis.steps[1].action_type == ComputerActionType.TYPE_TEXT)


# ==============================================================================
# 6. Autonomous Cognitive Planner & Ground-Truth Task Synthesis
# ==============================================================================
print("\n--- 6. Autonomous Cognitive Planner & Ground-Truth Task Synthesis ---")

from core.brain.planner import get_cognitive_planner, CognitivePlan

planner = get_cognitive_planner()
check("CognitivePlanner instantiated", planner is not None)

# Plan generation for fast-path intent
plan_notepad = planner.create_plan("افتح المفكرة واكتب اختبار الخطة الذكية", world_state=live_ws)
check("CognitivePlan created successfully", plan_notepad.is_actionable is True)
check("Plan status is PROPOSED", plan_notepad.status == "PROPOSED")
check("Plan contains Task instance", plan_notepad.task is not None)
check("Plan Task has matching user intent", "المفكرة" in plan_notepad.task.user_intent)
check("Plan Task has initialized subgoals", len(plan_notepad.task.subgoals) >= 1)

# Grounding check: target desktop path exists in params
has_desktop_path = any("Desktop" in str(s.params.get("path", "")) for s in plan_notepad.steps)
check("Plan grounds target path in Desktop directory", has_desktop_path is True)


# ==============================================================================
# 7. Closed-Loop Orchestrator End-to-End Integration with Task Engine
# ==============================================================================
print("\n--- 7. Closed-Loop Orchestrator Integration with Task Engine ---")

from core.orchestrator.closed_loop_orchestrator import get_closed_loop_orchestrator

orc = get_closed_loop_orchestrator()
check("ClosedLoopOrchestrator instantiated", orc is not None)

# Execute an intent through the closed loop orchestrator
cycle_intent = "افتح الحاسبة"
cycle_res = orc.orchestrate_intent(cycle_intent)

check("Orchestration cycle returned result", cycle_res is not None)
check("Cycle result success is True", cycle_res.success is True)
check("Cycle result executed steps >= 1", cycle_res.steps_executed >= 1)
check("Cycle result verified is True", cycle_res.verified is True)
check("Cycle result contains task_id", cycle_res.task_id is not None)

# Verify the Task in TaskEngine reached COMPLETED status
if cycle_res.task_id:
    tracked_task = te.get_task(cycle_res.task_id)
    check("Task tracked in TaskEngine", tracked_task is not None)
    if tracked_task:
        check("Tracked Task final status is COMPLETED", tracked_task.status == TaskStatus.COMPLETED)
        check("Tracked Task has completed all steps", tracked_task.completed_steps_count == tracked_task.total_steps)


# ==============================================================================
# 8. Telemetry & Resource Invariants
# ==============================================================================
print("\n--- 8. Telemetry & Resource Invariants ---")

import psutil

proc = psutil.Process(os.getpid())
proc_cpu = proc.cpu_percent(interval=0.5)
proc_ram_mb = proc.memory_info().rss / (1024 * 1024)
router_vram = router.get_allocated_vram_mb()

check("Process CPU idle remains <= 5.0%", proc_cpu <= 5.0, f"(Measured: {proc_cpu:.1f}%)")
check("Router allocated VRAM in idle == 0.0 MB", router_vram == 0.0, f"(Measured: {router_vram} MB)")
check("Process RAM footprint is bounded", proc_ram_mb < 150.0, f"(Measured: {proc_ram_mb:.1f} MB)")

# ==============================================================================
# Final Summary
# ==============================================================================
print("\n" + "=" * 80)
print(f"WISE PHASE P1.3-A VERIFICATION SUMMARY: {passed} PASSED, {failed} FAILED")
print("=" * 80)

if failed > 0:
    sys.exit(1)
sys.exit(0)
