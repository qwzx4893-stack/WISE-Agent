#!/usr/bin/env python3
"""
WISE Phase P0.0 Core Architecture Hardening Test Suite.
Verifies all foundational subsystems of the Persistent Intelligent Computer Layer:
1. State Machine (WiseStateMachine & ResourceProfile enforcement)
2. Resource Manager (WISEResourceManager, real hardware telemetry, worker suspension on IDLE)
3. Windows Event Bus (Native Win32 event hooks & Pub-Sub)
4. Computer World Model (OS, User, Windows, Apps, Hardware, Files, and WISE State)
5. Epistemic Truth Arbiter (Domain authority, provenance, freshness decay, conflict resolution)
6. Context Fusion Engine (Multi-source relevance gating & token budget optimization)
7. Windows Security Gate (5 tiers, prompt injection quarantine, system directory protection, audit log)
8. ComputerControl Abstraction (Unified capabilities, hierarchical execution, closed-loop verification)
"""

from __future__ import annotations

import os
import sys
import time
import json
from pathlib import Path

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = REPO_ROOT / "Supergent--main"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SUPERGENT_DIR))

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        print(f"[PASS] {name} {detail}")
        passed += 1
    else:
        print(f"[FAIL] {name} {detail}")
        failed += 1


print("=" * 72)
print("   WISE PHASE P0.0 CORE ARCHITECTURE HARDENING TEST SUITE")
print("   (Verification of Persistent Intelligent Computer Layer Subsystems)")
print("=" * 72)

# ==========================================================================
# 1. State Machine & Resource Profile Enforcement
# ==========================================================================
print("\n--- 1. Testing WiseStateMachine & Resource Profiles ---")
try:
    from core.state import WiseState, WiseStateMachine, ResourceProfile

    sm = WiseStateMachine()
    check("StateMachine initializes in BOOT state", sm.current_state == WiseState.BOOT)

    # Test invalid transition rejection (e.g. BOOT -> EXECUTING is invalid)
    invalid_res = sm.transition_to(WiseState.EXECUTING, "Illegal jump")
    check("Invalid transition rejected (BOOT -> EXECUTING)", invalid_res is False and sm.current_state == WiseState.BOOT)

    # Test legal transition path: BOOT -> INITIALIZING -> READY -> IDLE
    assert sm.transition_to(WiseState.INITIALIZING, "Starting subsystems")
    assert sm.transition_to(WiseState.READY, "Subsystems ready")
    assert sm.transition_to(WiseState.IDLE, "Entering low-power idle")
    check("Legal transition sequence (BOOT -> INITIALIZING -> READY -> IDLE)", sm.current_state == WiseState.IDLE)

    # Verify IDLE resource profile
    profile = sm.current_profile
    check("IDLE profile is_low_power=True", profile.is_low_power is True)
    check("IDLE profile heavy_models_allowed=False", profile.heavy_models_allowed is False)
    check("IDLE profile vision_active=False", profile.vision_active is False)
    check("IDLE profile max_cpu_percent_target <= 1.0", profile.max_cpu_percent_target <= 1.0)

    # Test transition listener
    received_transitions = []
    def on_trans(old, new, r):
        received_transitions.append((old, new, r))
    sm.add_listener(on_trans)
    sm.transition_to(WiseState.AWAKENING, "User wake event")
    check("Transition listener triggered on state change", len(received_transitions) == 1 and received_transitions[0][1] == WiseState.AWAKENING)
    sm.remove_listener(on_trans)

except Exception as e:
    check("WiseStateMachine tests", False, str(e))

# ==========================================================================
# 2. Resource Manager & Real Hardware Telemetry
# ==========================================================================
print("\n--- 2. Testing WISEResourceManager & Telemetry ---")
try:
    from core.resource import WISEResourceManager, ManagedWorker, WorkerStatus

    test_sm = WiseStateMachine()
    test_sm.transition_to(WiseState.INITIALIZING, "Init")
    test_sm.transition_to(WiseState.READY, "Ready")

    rm = WISEResourceManager(state_machine=test_sm)
    metrics = rm.get_hardware_metrics()

    check("Physical CPU metric queried", isinstance(metrics.cpu_percent, float) and metrics.cpu_percent >= 0.0, f"({metrics.cpu_percent}%)")
    check("Physical RAM metrics queried", metrics.ram_total_mb > 1000 and metrics.ram_used_mb > 500, f"({metrics.ram_used_mb}/{metrics.ram_total_mb}MB)")
    check("GPU detection probe executed", isinstance(metrics.gpu_available, bool), f"(Detected: {metrics.gpu_name})")
    check("Power status probe executed", isinstance(metrics.power_plugged, bool), f"(Plugged: {metrics.power_plugged})")

    # Test worker auto-suspension on IDLE
    worker_flag = {"active": False}
    worker = ManagedWorker(
        name="test_heavy_worker",
        start_fn=lambda: worker_flag.update({"active": True}),
        suspend_fn=lambda: worker_flag.update({"active": False}),
        is_essential_in_idle=False,
    )
    rm.register_worker(worker)
    worker.start()
    check("ManagedWorker started successfully", worker.status == WorkerStatus.RUNNING and worker_flag["active"] is True)

    # Transition to IDLE must automatically suspend worker
    test_sm.transition_to(WiseState.IDLE, "Zero-compute idle test")
    check("Worker auto-suspended upon entering IDLE", worker.status == WorkerStatus.SUSPENDED and worker_flag["active"] is False)

except Exception as e:
    check("WISEResourceManager tests", False, str(e))

# ==========================================================================
# 3. Windows Native Event Bus
# ==========================================================================
print("\n--- 3. Testing WindowsEventBus (Native Hooks & Pub-Sub) ---")
try:
    from core.windows import WindowsEventBus, WindowsEvent

    bus = WindowsEventBus()
    dispatched_events = []

    # Test wildcard subscription
    bus.subscribe("window.*", lambda ev: dispatched_events.append(ev))
    bus.publish(WindowsEvent(topic="window.foreground_changed", data={"hwnd": 1234, "title": "Test Terminal"}))
    bus.publish(WindowsEvent(topic="system.battery_low", data={"percent": 15}))  # Should not be caught

    check("EventBus Pub-Sub wildcard dispatch", len(dispatched_events) == 1 and dispatched_events[0].data["title"] == "Test Terminal")

    # Test native hook startup and clean shutdown
    bus_started = bus.start()
    check("WindowsEventBus hook thread starts without error", bus_started is True)
    time.sleep(0.3)
    bus.stop()
    check("WindowsEventBus unhooks cleanly", bus._running is False)

except Exception as e:
    check("WindowsEventBus tests", False, str(e))

# ==========================================================================
# 4. Computer World Model
# ==========================================================================
print("\n--- 4. Testing ComputerWorldModel ---")
try:
    from core.context import ComputerWorldModelManager, ComputerWorldModel

    wmm = ComputerWorldModelManager()
    wmm.refresh_active_window()
    wmm.refresh_open_windows()

    snapshot = wmm.get_snapshot()
    check("WorldModel captures OS attributes", snapshot["os"]["system"] == "Windows", f"({snapshot['os']['system']} {snapshot['os']['release']})")
    check("WorldModel captures User session", bool(snapshot["user_session"]["username"]), f"(User: {snapshot['user_session']['username']})")
    check("WorldModel resolves user directories", bool(snapshot["files"]["downloads_dir"]), f"(Downloads: {snapshot['files']['downloads_dir']})")
    check("WorldModel captures active/open applications", len(snapshot["open_windows"]) > 0, f"(Count: {len(snapshot['open_windows'])})")
    check("WorldModel incorporates live resource telemetry", "resources" in snapshot and snapshot["resources"]["ram_used_mb"] > 0)

    # Test prompt context formatting
    summary = wmm.get_prompt_context()
    check("WorldModel formats high-signal prompt context", "### Computer World Model Summary:" in summary and "Host" in summary)

except Exception as e:
    check("ComputerWorldModel tests", False, str(e))

# ==========================================================================
# 5. Epistemic Truth Arbiter & Conflict Resolution
# ==========================================================================
print("\n--- 5. Testing EpistemicTruthArbiter ---")
try:
    from core.context import EpistemicTruthArbiter, EpistemicDomain, SourceType

    arbiter = EpistemicTruthArbiter()

    # Fact with decay
    f1 = arbiter.record_fact(
        key="gpu_temp",
        value=55.0,
        domain=EpistemicDomain.SYSTEM_STATE,
        source_type=SourceType.OS_HARDWARE_TELEMETRY,
        provenance="nvidia-smi",
        confidence=1.0,
        halflife_seconds=60.0,
    )
    check("Fact record with provenance and freshness", f1.freshness >= 0.99 and f1.composite_score >= 0.9)

    # Conflict on SYSTEM_STATE: OS Hardware Telemetry vs Internet
    arbiter.record_fact(
        key="system_ram_free",
        value="4GB",
        domain=EpistemicDomain.SYSTEM_STATE,
        source_type=SourceType.INTERNET_SEARCH,
        provenance="web:spec_sheet",
    )
    f_win = arbiter.record_fact(
        key="system_ram_free",
        value="1.5GB",
        domain=EpistemicDomain.SYSTEM_STATE,
        source_type=SourceType.OS_HARDWARE_TELEMETRY,
        provenance="psutil:virtual_memory",
    )
    check("TruthArbiter SYSTEM_STATE: Hardware overrode Internet", f_win.value == "1.5GB" and f_win.source_type == SourceType.OS_HARDWARE_TELEMETRY)

    # Conflict on USER_INTENT: User Input vs Memory
    arbiter.record_fact(
        key="output_format",
        value="csv",
        domain=EpistemicDomain.USER_INTENT,
        source_type=SourceType.LONG_TERM_MEMORY,
        provenance="qmd:owner_profile",
    )
    f_intent = arbiter.record_fact(
        key="output_format",
        value="json",
        domain=EpistemicDomain.USER_INTENT,
        source_type=SourceType.DIRECT_USER_INPUT,
        provenance="user_prompt",
    )
    check("TruthArbiter USER_INTENT: Direct User Input overrode Memory", f_intent.value == "json" and f_intent.source_type == SourceType.DIRECT_USER_INPUT)

except Exception as e:
    check("EpistemicTruthArbiter tests", False, str(e))

# ==========================================================================
# 6. Context Fusion Engine
# ==========================================================================
print("\n--- 6. Testing ContextFusionEngine ---")
try:
    from core.context import ContextFusionEngine

    fusion = ContextFusionEngine()
    fused = fusion.fuse(
        user_intent="Analyze the downloaded financial spreadsheet in Downloads",
        local_files=["financial_q3.xlsx", "notes.txt", "system_log.txt"],
        memory_candidates=[
            {"title": "Financial Spreadsheet Schema", "snippet": "Columns for revenue and expense"},
            {"title": "Gaming profile", "snippet": "Steam game preferences"},
        ],
        web_candidates=[
            {"title": "Financial Spreadsheet GAAP standards", "snippet": "Rules for revenue recognition", "url": "https://gaap.org"},
            {"title": "Weather forecast", "snippet": "Rain in London", "url": "https://weather.com"},
        ],
        relevance_threshold=0.1,
    )

    check("ContextFusion: relevant local file retained", "financial_q3.xlsx" in fused.relevant_local_files)
    check("ContextFusion: relevant memory kept, irrelevant pruned", len(fused.relevant_memory) == 1 and fused.relevant_memory[0]["title"] == "Financial Spreadsheet Schema")
    check("ContextFusion: relevant web kept, irrelevant pruned", len(fused.relevant_web_findings) == 1 and "Financial Spreadsheet GAAP" in fused.relevant_web_findings[0]["title"])

    md_prompt = fused.to_markdown_prompt(max_tokens=1000)
    check("ContextFusion: serializes dense high-signal markdown", "[WISE Fused World State]" in md_prompt and "financial_q3.xlsx" in md_prompt)

except Exception as e:
    check("ContextFusionEngine tests", False, str(e))

# ==========================================================================
# 7. Windows Security Gate & Anti-Prompt-Injection
# ==========================================================================
print("\n--- 7. Testing WindowsSecurityGate (5 Tiers & Anti-Injection) ---")
try:
    from core.security import WindowsSecurityGate, ActionTier, SecurityContext

    test_audit_path = REPO_ROOT / "logs" / "test_p0_security_audit.jsonl"
    if test_audit_path.exists():
        test_audit_path.unlink()

    gate = WindowsSecurityGate(audit_log_path=test_audit_path)

    # 1. READ action
    ev_read = gate.evaluate("read_file", {"path": "notes.txt"})
    check("SecurityGate: READ action automatically allowed", ev_read.allowed is True and ev_read.tier == ActionTier.READ)

    # 2. LOW_RISK action
    ev_low = gate.evaluate("click", {"x": 100, "y": 200})
    check("SecurityGate: LOW_RISK action automatically allowed", ev_low.allowed is True and ev_low.tier == ActionTier.LOW_RISK)

    # 3. ADMINISTRATIVE action unconfirmed vs confirmed
    ev_admin_unconf = gate.evaluate("modify_registry", {"key": "HKCU\\Software\\WISE"})
    check("SecurityGate: ADMINISTRATIVE action requires confirmation", ev_admin_unconf.allowed is False and ev_admin_unconf.requires_confirmation is True)

    ev_admin_conf = gate.evaluate("modify_registry", {"key": "HKCU\\Software\\WISE"}, SecurityContext(confirmed=True))
    check("SecurityGate: ADMINISTRATIVE action allowed when confirmed", ev_admin_conf.allowed is True)

    # 4. DESTRUCTIVE action unconfirmed vs confirmed
    ev_dest_unconf = gate.evaluate("delete_file", {"path": "temp.txt"})
    check("SecurityGate: DESTRUCTIVE action blocked without confirmation", ev_dest_unconf.allowed is False and ev_dest_unconf.requires_confirmation is True)

    ev_dest_conf = gate.evaluate("delete_file", {"path": "temp.txt"}, SecurityContext(confirmed=True))
    check("SecurityGate: DESTRUCTIVE action allowed when confirmed", ev_dest_conf.allowed is True)

    # 5. Protected system directory escalation
    ev_sys_dir = gate.evaluate("write_file", {"path": "C:/Windows/System32/config.sys"})
    check("SecurityGate: Protected Windows system directory escalated to DESTRUCTIVE", ev_sys_dir.allowed is False and ev_sys_dir.tier == ActionTier.DESTRUCTIVE)

    # 6. Prompt Injection Firewall Quarantine
    ev_injection = gate.evaluate(
        "delete_file",
        {"path": "C:/user_data/important.db"},
        SecurityContext(is_untrusted_content=True, confirmed=True),  # Attacker injected confirmed=True
    )
    check("SecurityGate: Untrusted web/file content QUARANTINED from DESTRUCTIVE", ev_injection.allowed is False and ev_injection.quarantined is True)

    # 7. Audit log existence and validity
    check("SecurityGate: Immutable audit log written to disk", test_audit_path.exists() and test_audit_path.stat().st_size > 0)
    if test_audit_path.exists():
        test_audit_path.unlink()

except Exception as e:
    check("WindowsSecurityGate tests", False, str(e))

# ==========================================================================
# 8. Unified ComputerControl Abstraction & Closed-Loop Verification
# ==========================================================================
print("\n--- 8. Testing ComputerControl Unified Interface ---")
try:
    from core.windows import ComputerControl

    control = ComputerControl()

    # 1. State query
    state_res = control.get_state(scope="summary")
    check("ComputerControl.get_state() returns summary and active window", "summary" in state_res and "active_window" in state_res)

    # 2. File write, read, verify, move
    docs_dir = Path.home() / "Documents"
    test_p0_file = docs_dir / "wise_p0_test_artifact.txt"
    test_p0_moved = docs_dir / "wise_p0_test_artifact_moved.txt"

    write_res = control.write_file(str(test_p0_file), "WISE_P0_ZERO_IDLE_ACTIVE_2026")
    check("ComputerControl.write_file() executed successfully", write_res.get("success") is True)

    read_res = control.read_file(str(test_p0_file))
    check("ComputerControl.read_file() matches written content", read_res.get("success") is True and "WISE_P0_ZERO_IDLE_ACTIVE_2026" in read_res.get("content", ""))

    # 3. Closed-loop verification
    ver_res = control.verify({"type": "file_exists", "path": str(test_p0_file)})
    check("ComputerControl.verify(file_exists) confirms actual file on disk", ver_res.get("verified") is True)

    # 4. Move file
    move_res = control.move_file(str(test_p0_file), str(test_p0_moved))
    check("ComputerControl.move_file() succeeds", move_res.get("success") is True)
    check("Verify moved file exists", test_p0_moved.exists() and not test_p0_file.exists())

    # 5. PowerShell command execution
    cmd_res = control.run_command("$v = 5 * 5; Write-Output \"MATH_RESULT:$v\"")
    check("ComputerControl.run_command() runs native PowerShell", cmd_res.get("success") is True and "MATH_RESULT:25" in cmd_res.get("stdout", ""))

    # Clean up test artifacts
    test_p0_moved.unlink(missing_ok=True)
    test_p0_file.unlink(missing_ok=True)

except Exception as e:
    check("ComputerControl tests", False, str(e))

# ==========================================================================
# Final Results
# ==========================================================================
print("\n" + "=" * 72)
print(f"   PHASE P0.0 TEST RESULTS: {passed} PASSED, {failed} FAILED")
print("=" * 72)

if __name__ == "__main__":
    if failed > 0:
        sys.exit(1)
    sys.exit(0)
