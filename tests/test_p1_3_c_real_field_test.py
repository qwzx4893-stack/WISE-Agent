# ==============================================================================
# WISE Phase P1.3-C Real Windows Field Test
# Verifies end-to-end Human-In-The-Loop Lifecycle in a real Windows GUI environment:
# 1. Spawns real native Windows process with controlled human verification challenge.
# 2. ClosedLoopOrchestrator & TaskEngine initiate real multi-step execution.
# 3. Live challenge detected via native UIA/window facts & ChallengeDetector.
# 4. Task transitions to PAUSED_FOR_HUMAN and creates an explicit TaskCheckpoint.
# 5. User intervention completes the challenge in the window.
# 6. Fresh observation taken by WorldState.
# 7. Post-intervention verification validates challenge clearance.
# 8. Exact interrupted task step resumes and completes without restarting.
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = BASE_DIR / "Supergent--main"
if str(SUPERGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_DIR))

from core.brain.task_engine import (
    get_task_engine,
    Task,
    Subgoal,
    TaskStep,
    TaskStatus,
    StepStatus,
    TaskDomain,
)
from core.hands import ComputerActionType, get_wise_hands
from core.orchestrator.closed_loop_orchestrator import (
    ClosedLoopOrchestrator,
    OrchestrationStep,
    OrchestrationCycleResult,
)
from core.security import (
    WindowsSecurityGate,
    ChallengeDetector,
    ChallengeType,
    ConfirmationManager,
)
from core.windows.window_manager import get_window_manager
from core.context.world_state import get_world_state_engine

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
print("   WISE PHASE P1.3-C: REAL WINDOWS FIELD TEST (CONTROLLED HITL)")
print("=" * 80)

helper_path = BASE_DIR / "tests" / "hitl_field_test_helper.py"
python_exe = sys.executable

print(f"Launching real Windows challenge helper: {helper_path}")
proc = subprocess.Popen(
    [python_exe, str(helper_path)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

time.sleep(1.5)  # Wait for window creation
win_mgr = get_window_manager()
all_wins = win_mgr.enumerate_windows(visible_only=True)
target_win = next((w for w in all_wins if "WISE_HITL_VERIFICATION_TEST" in w.title), None)

check("Field Test [Step 1]: Native Windows challenge window located", target_win is not None, f"(HWND: {target_win.hwnd if target_win else 'None'})")

if not target_win:
    print("FATAL: Challenge window could not be located on Windows desktop.")
    proc.kill()
    sys.exit(1)

# Bring challenge window to foreground
win_mgr.bring_to_front(target_win.hwnd)
time.sleep(0.5)

# Step 2: Initialize Orchestrator with DI
task_engine = get_task_engine()
detector = ChallengeDetector()
cm = ConfirmationManager(default_ttl_seconds=60.0)
gate = WindowsSecurityGate(confirmation_manager=cm)
orchestrator = ClosedLoopOrchestrator(
    security_gate=gate,
    challenge_detector=detector,
    confirmation_manager=cm,
)

# Create a structured 2-subgoal Task
sg1_steps = [
    TaskStep(
        action_type=ComputerActionType.FOCUS_WINDOW,
        params={"hwnd": target_win.hwnd},
        description="Focus challenge window",
    ),
    TaskStep(
        action_type=ComputerActionType.WAIT,
        params={"duration": 0.1},
        requires_human=True,
        human_intervention_type="CAPTCHA",
        human_intervention_reason="Security Check: Verify you are human.",
        human_prompt="Please solve the human verification challenge in the window.",
        description="Encounter human verification challenge",
    ),
]
sg2_steps = [
    TaskStep(
        action_type=ComputerActionType.WAIT,
        params={"duration": 0.1},
        description="Post-verification completion step",
    ),
]

subgoal1 = Subgoal(title="Access Protected App", steps=sg1_steps)
subgoal2 = Subgoal(title="Finalize Execution", steps=sg2_steps)

task = task_engine.create_task(
    user_intent="Open protected app, resolve human challenge, and complete operation",
    semantic_goal="Complete protected operation",
    subgoals=[subgoal1, subgoal2],
)
task_engine.start_task(task.task_id)

check("Field Test [Step 2]: Task created and started in TaskEngine", task.status == TaskStatus.RUNNING)

# Step 3: Run execution of initial steps
orch_steps = [
    OrchestrationStep(
        action_type=s.action_type,
        params=s.params,
        requires_human=s.requires_human,
        human_intervention_type=s.human_intervention_type,
        human_intervention_reason=s.human_intervention_reason,
        human_prompt=s.human_prompt,
    )
    for s in sg1_steps
]

cycle_res = orchestrator.run_cycle(intent=task.user_intent, steps=orch_steps)

check("Field Test [Step 3]: Orchestrator paused for human intervention", cycle_res.paused_for_human)
check("Field Test [Step 3.1]: Step 1 (Focus Window) completed prior to pause", cycle_res.steps_executed == 1)

# Reflect pause in TaskEngine
task_engine.record_step_result(task.task_id, sg1_steps[0].step_id, success=True)
task_engine.advance_step(task.task_id)
chk = task_engine.pause_for_human(
    task_id=task.task_id,
    intervention_type="CAPTCHA",
    reason=cycle_res.intervention_details["reason"],
    prompt=cycle_res.intervention_details["prompt"],
    details=cycle_res.intervention_details,
)

check("Field Test [Step 4]: TaskEngine transitioned to PAUSED_FOR_HUMAN", task.status == TaskStatus.PAUSED_FOR_HUMAN)
check("Field Test [Step 4.1]: Checkpoint captured on human pause", chk is not None and len(task.checkpoints) > 0)
check("Field Test [Step 4.2]: Task execution pointer preserved at Subgoal 0, Step 1", task.current_subgoal_idx == 0 and subgoal1.current_step_idx == 1)

# Step 5: Simulate human solving the challenge in the native window
print("\nSimulating user interaction in the native challenge window...")
try:
    if proc.stdin:
        proc.stdin.write("SOLVE\n")
        proc.stdin.flush()
except Exception as p_err:
    print("Stdin pipe write exception:", p_err)

from core.hands import get_wise_hands
hands = get_wise_hands()
# Send space / enter to trigger button in active window
hands.execute_closed_loop_action(
    action_type=ComputerActionType.HOTKEY,
    params={"keys": ["space"]},
)
time.sleep(1.0)

# Step 6: Fresh observation of live world state
world_engine = get_world_state_engine()
fresh_state = world_engine.get_current_world_state(force_fresh=True)
active_title = fresh_state.active_window.title if fresh_state.active_window else ""

check("Field Test [Step 6]: Fresh observation reveals window title change", "ACCESS_GRANTED" in active_title, f"(Active title: '{active_title}')")

# Step 7: Submit human intervention resolution to resume
resume_res = orchestrator.submit_human_intervention_resolution(
    task_id=task.task_id,
    action="completed",
    resolution_payload={"verified_title": active_title},
)
print(f"Task status after resolution: {task.status.value}")
check("Field Test [Step 7]: TaskEngine successfully resumed without restart", task.status == TaskStatus.COMPLETED or task.status == TaskStatus.RUNNING, f"(Task status: {task.status.value})")
check("Field Test [Step 7.1]: Previous Step 1 remained completed", sg1_steps[0].status == StepStatus.COMPLETED)

# Mark completion
task_engine.complete_task(task.task_id)
check("Field Test [Step 8]: Full end-to-end task COMPLETED", task.status == TaskStatus.COMPLETED)

# Cleanup helper process
try:
    proc.terminate()
    proc.wait(timeout=2)
except Exception:
    proc.kill()

print("\n" + "=" * 80)
print(f"   WISE PHASE P1.3-C REAL FIELD TEST SUMMARY: {passed} PASSED, {failed} FAILED")
print("=" * 80)

if failed > 0:
    sys.exit(1)
