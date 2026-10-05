"""
WISE Phase P0.1 Comprehensive Verification Suite:
Deep Windows Integration & Embedded Native Model Runtime Architecture.
Validates Tests A through K against real Windows APIs and P0.1 subsystems:
  Test A: Startup & Task Scheduler / Registry Integration
  Test B: Interactive User Session (Session ID > 0, Console desktop)
  Test C: Session Lock/Unlock Event Handling
  Test D: System Sleep/Wake Power Event Handling
  Test E: Event Bus Awareness (Zero-polling native dispatch)
  Test F: Filesystem Governor (Traversal defense, System protection, Whitelist)
  Test G: Permission & Onboarding Mode (SecurityGate integration)
  Test H: Idle Resource Telemetry (CPU, RAM, GPU VRAM)
  Test I: Wake Latency (Microsecond state transition)
  Test J: Worker Supervisor & Crash Recovery Watchdog
  Test K: Native Model Runtime Lifecycle & VRAM Flush on IDLE
"""

import os
import gc
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

# Import P0.0 and P0.1 subsystems
from core.state.state_machine import WiseStateMachine, WiseState, get_state_machine
from core.resource.resource_manager import get_resource_manager, ManagedWorker, WorkerStatus, WorkerPriority
from core.resource.worker_supervisor import get_worker_supervisor
from core.windows.event_bus import get_event_bus, WindowsEvent
from core.windows.session_daemon import get_session_daemon, OnboardingMode, WTS_SESSION_LOCK, WTS_SESSION_UNLOCK, PBT_APMSUSPEND, PBT_APMRESUMEAUTOMATIC
from core.windows.filesystem_governor import get_filesystem_governor, FileOperation
from core.security.security_gate import get_security_gate, ActionTier, SecurityContext
from core.context.world_model import get_world_model_manager
from core.models.model_runtime import BaseModelRuntime, EmbeddedLocalRuntime, RuntimeBackend, InferenceConfig
from core.models.model_manager import get_model_manager

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
print("   WISE PHASE P0.1 COMPREHENSIVE VERIFICATION SUITE")
print("   (Deep Windows Integration & Native Model Runtime Architecture)")
print("=" * 72)

# ==============================================================================
# Test A & B: Windows Interactive Session & Startup Daemon
# ==============================================================================
print("\n--- Tests A & B: Windows Interactive Session & Startup ---")
session_daemon = get_session_daemon()
audit = session_daemon.get_audit()

check(
    "Test B: Interactive User Session Detected",
    audit.is_interactive_session,
    f"(Session ID: {audit.session_id}, User: {audit.current_user})",
)
check(
    "Test B: Session ID != 0 (Not isolated Session 0)",
    audit.session_id > 0,
    f"(Session ID: {audit.session_id})",
)
check(
    "Test A: Startup status queryable via Win32/Registry",
    isinstance(audit.has_autostart_configured, bool),
    f"(Configured: {audit.has_autostart_configured}, Method: {audit.autostart_method})",
)
check(
    "Test G: Onboarding Mode evaluated",
    audit.onboarding_mode in (OnboardingMode.LIMITED_MODE, OnboardingMode.FULL_INTEGRATED_MODE),
    f"(Mode: {audit.onboarding_mode.value})",
)

# ==============================================================================
# Tests C & D: Session Lock/Unlock & Power Broadcast Notifications
# ==============================================================================
print("\n--- Tests C & D: Session & Power Event Handling ---")
bus = get_event_bus()
wm = get_world_model_manager()

# Verify Lock Event
session_daemon.notify_session_event(WTS_SESSION_LOCK, audit.session_id)
time.sleep(0.05)
snapshot_locked = wm.get_snapshot()
check(
    "Test C: Session LOCK event dispatched and reflected in WorldModel",
    wm.model.user_session.is_locked is True,
    f"(is_locked={wm.model.user_session.is_locked})",
)

# Verify Unlock Event
session_daemon.notify_session_event(WTS_SESSION_UNLOCK, audit.session_id)
time.sleep(0.05)
check(
    "Test C: Session UNLOCK event dispatched and reflected in WorldModel",
    wm.model.user_session.is_locked is False,
    f"(is_locked={wm.model.user_session.is_locked})",
)

# Verify Sleep Power Event
session_daemon.notify_power_event(PBT_APMSUSPEND)
time.sleep(0.05)
check(
    "Test D: Power SLEEP event updates WorldModel power state",
    wm.model.system_power_state == "SLEEP",
    f"(system_power_state={wm.model.system_power_state})",
)

# Verify Wake Power Event
session_daemon.notify_power_event(PBT_APMRESUMEAUTOMATIC)
time.sleep(0.05)
check(
    "Test D: Power WAKE event updates WorldModel power state",
    wm.model.system_power_state == "ACTIVE",
    f"(system_power_state={wm.model.system_power_state})",
)

# ==============================================================================
# Test E: Windows Event Awareness (Pub-Sub without Polling)
# ==============================================================================
print("\n--- Test E: Windows Native Event Awareness ---")
received_events = []
def _on_test_event(ev: WindowsEvent):
    received_events.append(ev)

bus.subscribe("window.test_awareness", _on_test_event)
bus.publish(WindowsEvent(
    topic="window.test_awareness",
    data={"action": "foreground_switch", "app": "notepad.exe"},
    source="test_suite",
))
check(
    "Test E: Native Event Bus publishes and dispatches without polling",
    len(received_events) == 1 and received_events[0].data.get("app") == "notepad.exe",
    f"(Events captured: {len(received_events)})",
)

# ==============================================================================
# Test F: Filesystem Governor & Traversal Protection
# ==============================================================================
print("\n--- Test F: Filesystem Governor & Traversal Defense ---")
gov = get_filesystem_governor()

# 1. Canonical path resolution & traversal attempt
traversal_path = r"C:\Windows\..\Windows\System32\cmd.exe"
canon = gov.canonicalize_path(traversal_path)
check(
    "Test F: Resolves traversal '..' to canonical absolute path",
    ".." not in canon and "system32" in canon,
    f"(Canonical: {canon})",
)

# 2. System Directory Modification Blocked
sys_write_dec = gov.evaluate_operation(FileOperation.WRITE, r"C:\Windows\System32\malicious.dll")
check(
    "Test F: System directory write escalated to DESTRUCTIVE",
    sys_write_dec.tier == ActionTier.DESTRUCTIVE and sys_write_dec.requires_confirmation is True,
    f"(Tier: {sys_write_dec.tier.value}, RequiresConfirmation: {sys_write_dec.requires_confirmation})",
)

# 3. Sensitive Credentials Protection
ssh_path = os.path.join(str(Path.home()), ".ssh", "id_rsa")
cred_dec = gov.evaluate_operation(FileOperation.READ, ssh_path)
check(
    "Test F: Sensitive SSH key read is blocked",
    cred_dec.allowed is False and cred_dec.tier == ActionTier.ADMINISTRATIVE,
    f"(Allowed: {cred_dec.allowed}, Tier: {cred_dec.tier.value})",
)

# 4. Whitelisted Workspace Path Allowed
ws_file = os.path.join(str(gov.workspace_root), "test_allowed.txt")
ws_dec = gov.evaluate_operation(FileOperation.WRITE, ws_file)
check(
    "Test F: Whitelisted Workspace write allowed with LOW_RISK",
    ws_dec.allowed is True and ws_dec.tier == ActionTier.LOW_RISK,
    f"(Allowed: {ws_dec.allowed}, Tier: {ws_dec.tier.value})",
)

# ==============================================================================
# Test G: Permission & Security Gate Integration
# ==============================================================================
print("\n--- Test G: Security Gate & Governor Deep Integration ---")
gate = get_security_gate()

# Reading a whitelisted workspace file through SecurityGate
gate_eval_read = gate.evaluate("read_file", {"path": ws_file})
check(
    "Test G: SecurityGate allows workspace read through Governor",
    gate_eval_read.allowed is True and gate_eval_read.tier == ActionTier.READ,
    f"(Tier: {gate_eval_read.tier.value})",
)

# Attempting to write into System32 with untrusted content -> Quarantined by Firewall
gate_eval_untrusted = gate.evaluate(
    "write_file",
    {"path": r"C:\Windows\System32\drivers\etc\hosts"},
    context=SecurityContext(is_untrusted_content=True),
)
check(
    "Test G: Untrusted injection targeting system directory QUARANTINED",
    gate_eval_untrusted.allowed is False and gate_eval_untrusted.quarantined is True,
    f"(Allowed: {gate_eval_untrusted.allowed}, Quarantined: {gate_eval_untrusted.quarantined})",
)

# ==============================================================================
# Test J: Worker Supervisor & Self-Healing Watchdog
# ==============================================================================
print("\n--- Test J: Worker Supervisor Crash Recovery ---")
supervisor = get_worker_supervisor()
rm = get_resource_manager()

worker_crashed = False
worker_restarted = False

def _mock_worker_start():
    global worker_restarted
    worker_restarted = True

mock_worker = ManagedWorker(
    name="test_resilient_worker",
    priority=WorkerPriority.LOW_BACKGROUND,
    is_essential_in_idle=True,
    start_fn=_mock_worker_start,
)
rm.register_worker(mock_worker)
supervisor.register_worker("test_resilient_worker")

# Trigger crash report
recovered = supervisor.report_crash("test_resilient_worker", RuntimeError("Simulated process crash"))
check(
    "Test J: Supervisor accepts crash report and schedules recovery",
    recovered is True,
    "(Recovery scheduled)",
)
# Wait for recovery backoff
time.sleep(1.5)
health = supervisor.get_health_summary().get("test_resilient_worker", {})
check(
    "Test J: Supervisor successfully self-healed and restarted worker",
    health.get("crash_count", 0) >= 1 and worker_restarted is True,
    f"(Crash Count: {health.get('crash_count')}, Restarted: {worker_restarted})",
)

# ==============================================================================
# Test K: Embedded Native Model Runtime & VRAM Governor
# ==============================================================================
print("\n--- Test K: Embedded Native Model Runtime & VRAM Governor ---")
model_mgr = get_model_manager()

# 1. Awaken Native Model Runtime
awaken_ok = model_mgr.awaken()
check(
    "Test K: Native Model Runtime awakens on demand",
    awaken_ok is True,
    f"(Active: {model_mgr._is_active}, Model: {model_mgr._current_model_path})",
)

# 2. In-process synchronous inference test
infer_res = model_mgr.generate("Explain Windows Session Architecture")
check(
    "Test K: Native in-process inference executed successfully",
    len(infer_res.text) > 0 and infer_res.tokens_generated > 0,
    f"(Tokens: {infer_res.tokens_generated}, Latency: {infer_res.latency_ms:.2f}ms)",
)

# 3. Enter IDLE -> Model Suspends & VRAM / RAM Flushed
model_mgr.sleep()
mem_after_sleep = model_mgr.get_status()
check(
    "Test K: Model Runtime unloads on IDLE with ZERO VRAM footprint",
    mem_after_sleep.get("vram_used_mb") == 0.0 and mem_after_sleep.get("is_loaded") is False,
    f"(VRAM: {mem_after_sleep.get('vram_used_mb')}MB, Loaded: {mem_after_sleep.get('is_loaded')})",
)

# ==============================================================================
# Tests H & I: Real Telemetry & Latency (Idle CPU, RAM, GPU, Wake)
# ==============================================================================
print("\n--- Tests H & I: Hardware Telemetry & Microsecond Latency ---")
sm = get_state_machine()

# Ensure Core is in IDLE
if sm.current_state != WiseState.IDLE:
    sm.transition_to(WiseState.INITIALIZING, reason="reset")
    sm.transition_to(WiseState.READY, reason="ready")
    sm.transition_to(WiseState.IDLE, reason="test idle")

# Sample CPU over 1 second
proc = psutil.Process()
proc.cpu_percent()
time.sleep(1.0)
idle_cpu = proc.cpu_percent()
idle_ram_mb = proc.memory_info().rss / (1024 * 1024)
hw = rm.get_hardware_metrics()

check(
    "Test H: Idle CPU utilization near zero",
    idle_cpu <= 2.0,
    f"(Measured: {idle_cpu:.2f}%)",
)
check(
    "Test H: Idle RAM footprint remains compact (< 60MB)",
    idle_ram_mb < 60.0,
    f"(Measured RSS: {idle_ram_mb:.2f} MB)",
)
check(
    "Test H: GPU VRAM has zero heavy models in IDLE",
    model_mgr.get_status().get("vram_used_mb") == 0.0,
    f"(Active Model VRAM: {model_mgr.get_status().get('vram_used_mb')} MB)",
)

# Test I: Wake Latency
t_wake_start = time.perf_counter_ns()
sm.transition_to(WiseState.AWAKENING, reason="User voice activation")
t_wake_us = (time.perf_counter_ns() - t_wake_start) / 1000
check(
    "Test I: State Machine Wake Latency is instantaneous (< 100 µs)",
    t_wake_us < 100.0,
    f"(Measured Wake Latency: {t_wake_us:.2f} µs)",
)

# Return to IDLE
sm.transition_to(WiseState.REASONING, reason="plan")
sm.transition_to(WiseState.IDLE, reason="task complete")

# ==============================================================================
# Final Results
# ==============================================================================
print("\n" + "=" * 72)
print(f"   PHASE P0.1 TEST RESULTS: {passed} PASSED, {failed} FAILED")
print("=" * 72)

if __name__ == "__main__":
    if failed > 0:
        sys.exit(1)
    sys.exit(0)
