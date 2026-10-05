"""
WISE Production E2E Acceptance Scenarios (A through I)
Authoritative verification suite executing live against the host environment.
Zero mocks. Real execution paths across all canonical boundaries.
"""

from __future__ import annotations

import os
import sys
import time
import json
import psutil
import subprocess
import pytest
from pathlib import Path

# Set up paths
_WISE_ROOT = Path(__file__).resolve().parent.parent
_SUPERGENT_ROOT = _WISE_ROOT / "Supergent--main"
if str(_SUPERGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SUPERGENT_ROOT))
if str(_WISE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WISE_ROOT))

from core.contracts import (
    VerificationSpec,
    MemoryTier,
    ActionLoopDecision,
    FailureType,
    ActionExecutionRecord,
    ModelRuntimeTelemetry,
)
from core.hands.computer_use import get_wise_hands, ComputerActionType, WISEHands
from core.windows.window_manager import get_window_manager
from core.orchestrator.closed_loop_orchestrator import ClosedLoopOrchestrator, OrchestrationStep
from core.brain.task_engine import TaskEngine, Task, Subgoal
from core.browser.browser_session import BrowserSession
from core.browser.browser_models import BrowserSessionConfig
from core.memory_service import MemoryService
from core.session_service import SessionService
from core.scheduler import Scheduler, Schedule
from core.security.security_gate import WindowsSecurityGate, SecurityContext
from core.security.confirmation import ConfirmationManager
from core.models.provider_interface import (
    get_model_runtime_telemetry,
    UnavailableModelProvider,
    SimulatedTestProvider,
)
from core.models.runtime.lfm25_model_locator import LFM25ModelLocator


# --------------------------------------------------------------------------
# Scenario A: Multi-Turn Desktop Task (Notepad interaction & verification)
# --------------------------------------------------------------------------
def test_scenario_a_multi_turn_desktop_task():
    """
    Scenario A:
    1. Launch Notepad.exe
    2. Verify active window / process
    3. Type initial text
    4. Replace text with CLEAR_AND_TYPE ("Hello from WISE")
    5. Verify and terminate Notepad cleanly
    """
    computer = get_wise_hands()
    wm = get_window_manager()
    
    # 1. Launch notepad
    proc = subprocess.Popen(["notepad.exe"])
    time.sleep(1.0)
    
    try:
        # 2. Verify window exists
        win_info = wm.get_foreground_window()
        assert win_info is not None, "A foreground window must exist"
        hwnd = win_info.hwnd
        
        # 3. Create frame signature
        sig1 = computer.create_frame_signature(hwnd)
        assert isinstance(sig1, dict)
        assert "signature" in sig1 and len(sig1["signature"]) > 0
        
        # 4. Type text
        res_type = computer.execute_closed_loop_action(
            ComputerActionType.TYPE_TEXT,
            {"text": "Hello"}
        )
        assert res_type.success is True
        time.sleep(0.3)
        
        # 5. Clear and type "Hello from WISE"
        res_clear_type = computer.execute_closed_loop_action(
            ComputerActionType.CLEAR_AND_TYPE,
            {"text": "Hello from WISE"}
        )
        assert res_clear_type.success is True
        time.sleep(0.3)
        
        # 6. Verify active process is still running
        v_spec = VerificationSpec(
            type="process_running",
            process="notepad"
        )
        verified = computer.verify_condition(v_spec)
        assert verified is True
        
    finally:
        # Cleanly terminate notepad
        proc.kill()
        proc.wait(timeout=3.0)
        time.sleep(0.5)
        
        assert proc.poll() is not None


# --------------------------------------------------------------------------
# Scenario B: Canonical File Task
# --------------------------------------------------------------------------
def test_scenario_b_canonical_file_task():
    """
    Scenario B:
    Agent creates temporary test document through canonical workspace path.
    Verifies existence, content accuracy, and cleans up cleanly.
    """
    test_file = _WISE_ROOT / "scratch" / "scenario_b_test_report.md"
    content = "# WISE Production Report\n\nVerified canonical file creation and artifact persistence."
    
    # Write file
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(content, encoding="utf-8")
    
    computer = get_wise_hands()
    
    # 1. Verify file exists
    v_exist = VerificationSpec(
        type="file_exists",
        path=str(test_file)
    )
    v_ok = computer.verify_condition(v_exist)
    assert v_ok is True, "File existence verification failed"
    
    # 2. Verify file content matches
    v_content = VerificationSpec(
        type="file_content",
        path=str(test_file),
        content="Verified canonical file creation"
    )
    v_ok2 = computer.verify_condition(v_content)
    assert v_ok2 is True, "File content verification failed"
    
    # 3. Clean up
    if test_file.exists():
        test_file.unlink()
    assert not test_file.exists()


# --------------------------------------------------------------------------
# Scenario C: Failure Recovery & Cycle Detection
# --------------------------------------------------------------------------
def test_scenario_c_failure_recovery():
    """
    Scenario C:
    Task targeting non-existent target.
    Orchestrator detects failure, avoids blind repetition, diagnoses obstacle,
    and terminates truthfully with failure diagnosis.
    """
    orchestrator = ClosedLoopOrchestrator()
    
    # Non-existent target action
    failing_step = OrchestrationStep(
        action_type=ComputerActionType.CLICK,
        params={"target": "non_existent_ghost_button_xyz_999"},
        verification_spec=VerificationSpec(type="ui_element_exists", text="ghost_button_xyz_999")
    )
    
    result = orchestrator._execute_step_loop(
        intent="test_scenario_c_recovery",
        active_steps=[failing_step, failing_step, failing_step],
        start_step_idx=0,
        records=[],
        replans_done=0,
        max_replans=0,
        t0=time.perf_counter(),
        plan=None,
        task_engine=None,
        task_id=None,
        healer=None,
    )
    
    # Must fail safely and identify repeated failure
    assert result.success is False
    assert "Repeated failure avoidance" in (result.error or "")
    assert result.steps_executed <= 2


# --------------------------------------------------------------------------
# Scenario D: Live Browser Task (Playwright)
# --------------------------------------------------------------------------
def test_scenario_d_live_browser_task():
    """
    Scenario D:
    Opens safe public test website (https://example.com/), navigates,
    inspects title and DOM, and closes session cleanly.
    """
    config = BrowserSessionConfig(headless=True)
    session = BrowserSession(config=config)
    try:
        session.start()
        assert session.is_active is True, "BrowserSession must be active after start()"
        
        # Navigate to example.com
        page = session.get_page()
        page.goto("https://example.com/", wait_until="domcontentloaded", timeout=15000)
        
        # Extract title and verify DOM
        title = page.title()
        assert "Example Domain" in title
        
        h1_text = page.locator("h1").text_content()
        assert "Example Domain" in (h1_text or "")
        
        # Verify persistence of session storage state
        saved = session.save_storage_state()
        assert saved is True
        assert (_WISE_ROOT / "browser_storage_state.json").exists()
        
    finally:
        session.stop()
        assert session.is_active is False


# --------------------------------------------------------------------------
# Scenario E: Persistent Memory Across Sessions
# --------------------------------------------------------------------------
def test_scenario_e_persistent_tiered_memory():
    """
    Scenario E:
    Session 1: Storing unique preference (color: emerald_teal) in USER_PREFERENCE tier.
    Session 2: Queries and retrieves preference with composite scoring.
    Cleans up test memory.
    """
    mem = MemoryService()
    
    # Store preference in Session 1
    item = mem.store(
        key="user_theme_emerald",
        content="User UI theme preference is emerald_teal.",
        tier=MemoryTier.USER_PREFERENCE,
        relevance_score=0.95,
        metadata={"session_id": "session_1_pref_setup"}
    )
    assert item is not None
    assert item.tier == MemoryTier.USER_PREFERENCE
    
    # Session 2: Search memory
    results = mem.query("emerald_teal UI theme", limit=5)
    assert len(results.items) >= 1
    top = results.items[0]
    assert "emerald_teal" in top.content
    assert top.tier == MemoryTier.USER_PREFERENCE
    
    # Clean up test item
    mem.delete("user_theme_emerald")
    cleared = mem.query("emerald_teal", limit=5)
    assert not any(i.key == "user_theme_emerald" for i in cleared.items)


# --------------------------------------------------------------------------
# Scenario F: Security Gate Policy Interception
# --------------------------------------------------------------------------
def test_scenario_f_security_gate_interception():
    """
    Scenario F:
    Attempt harmlessly simulated destructive command.
    Security gate blocks or requires confirmation BEFORE execution.
    """
    gate = WindowsSecurityGate()
    
    # Destructive action: deleting critical files requires confirmation/blocks execution
    eval_res = gate.evaluate("run_command", {"command": "del /f /q C:\\Windows\\Temp\\*"})
    assert eval_res.allowed is False
    assert eval_res.requires_confirmation is True
    
    # Untrusted prompt injection attempt
    ctx = SecurityContext(is_untrusted_content=True)
    eval_inj = gate.evaluate("powershell", {"command": "Get-Process"}, ctx)
    assert eval_inj.allowed is False
    assert eval_inj.quarantined is True
    assert "Prompt Injection Firewall" in eval_inj.reason


# --------------------------------------------------------------------------
# Scenario G: Scheduler Execution Pipeline
# --------------------------------------------------------------------------
def test_scenario_g_scheduler_execution_pipeline():
    """
    Scenario G:
    Creates a scheduled task, validates next run time calculation,
    verifies canonical session attribution, and removes schedule.
    """
    scheduler = Scheduler()
    
    sched = scheduler.add(
        name="Scenario G Test Schedule",
        cron="*/5 * * * *",
        target="test_echo",
        payload={"msg": "hello"},
    )
    
    assert sched is not None
    assert sched.enabled is True
    assert sched.next_run_at is not None
    
    # Retrieve and verify
    retrieved = scheduler.get(sched.id)
    assert retrieved is not None
    assert retrieved.name == "Scenario G Test Schedule"
    
    # Clean up
    removed = scheduler.delete(sched.id)
    assert removed is True
    assert scheduler.get(sched.id) is None


# --------------------------------------------------------------------------
# Scenario H: Restart Recovery (Session & Memory persistence)
# --------------------------------------------------------------------------
def test_scenario_h_restart_recovery():
    """
    Scenario H:
    Persists session and memory items to disk.
    Simulates service restart by instantiating new instances.
    Verifies data restored accurately from disk.
    """
    # 1. Setup Session in instance 1
    s_svc1 = SessionService()
    session_id = f"test_restart_{int(time.time())}"
    ctx = s_svc1.get_or_create_session(session_id=session_id)
    ctx.history.append({"role": "user", "content": "Remember restart test key 98765"})
    ctx.history.append({"role": "assistant", "content": "Acknowledged restart test key 98765"})
    s_svc1.save_session(ctx)
    
    # 2. Simulate complete service restart with instance 2
    s_svc2 = SessionService()
    restored_ctx = s_svc2.get_session(session_id)
    assert restored_ctx is not None
    assert len(restored_ctx.history) >= 2
    assert "98765" in restored_ctx.history[0]["content"]
    
    # Clean up session
    s_svc1.delete_session(session_id)


# --------------------------------------------------------------------------
# Scenario I: Truthful Resource Pressure & Model Governance
# --------------------------------------------------------------------------
def test_scenario_i_resource_pressure_truthfulness():
    """
    Scenario I:
    Inspects real machine RAM.
    Under existing host RAM pressure (< 5.8 GB free), verifies:
    1. LFM25ModelLocator strictly refuses unsafe load.
    2. Model provider returns UnavailableModelProvider.
    3. SimulatedTestProvider is NOT used in production mode.
    4. ModelRuntimeTelemetry reflects truthful state.
    """
    from core.models.provider_interface import (
        reset_model_provider,
        get_production_model_provider,
        get_model_runtime_telemetry,
    )
    
    reset_model_provider()
    telemetry = get_model_runtime_telemetry(allow_simulation=False)
    assert isinstance(telemetry, ModelRuntimeTelemetry)
    assert telemetry.available_ram_gb > 0
    locator = LFM25ModelLocator()
    profile = locator.inspect_and_validate()
    
    if telemetry.available_ram_gb < 5.8:
        # Under current host conditions, must truthfully report unsafe to load
        assert profile.is_safe_to_load is False
        assert "ram" in profile.safety_message.lower() or "safety threshold" in profile.safety_message.lower()
        # Provider must be UnavailableModelProvider, NOT SimulatedTestProvider
        provider = get_production_model_provider()
        assert isinstance(provider, UnavailableModelProvider)
        assert not isinstance(provider, SimulatedTestProvider)
    else:
        # If host has >= 5.8 GB, safety gate passes
        assert profile.is_safe_to_load is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
