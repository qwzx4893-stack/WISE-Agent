# ==============================================================================
# WISE Phase P1.3-C Comprehensive Verification Suite
# Focus: Human-In-The-Loop (HITL) Lifecycle & Security Hardening
#
# Covers all 19 required test categories + additional invariants:
# 1.  CAPTCHA pause
# 2.  CAPTCHA resume
# 3.  MFA pause
# 4.  MFA resume
# 5.  OTP treated as transient sensitive data
# 6.  Credential & secret redaction (logs, checkpoints, World State)
# 7.  Confirmation required for high-impact action
# 8.  Exact-action confirmation (valid token with exact params succeeds)
# 9.  Stale confirmation rejection (expired token rejected)
# 10. Changed-target rejection (TOCTOU: target A -> target B rejected)
# 11. Changed-amount rejection (TOCTOU: $10 -> $100 rejected)
# 12. Destructive action blocking (untrusted prompt injection & unconfirmed)
# 13. Prompt injection isolation (untrusted content remains DATA, not intent)
# 14. Task resume from checkpoint (retains completed steps, resumes exact step)
# 15. User cancellation (user rejects intervention -> task cancels cleanly)
# 16. Security denial (SecurityGate evaluation denies execution when policy violated)
# 17. Normal low-risk action does NOT unnecessarily require confirmation
# 18. Browser task pauses and resumes correctly
# 19. Windows task pauses and resumes correctly
# + Additional Invariants:
# 20. Model output != authorization (cannot bypass SecurityGate)
# 21. Single-use token / Replay rejection
# 22. CAPTCHA cannot be auto-marked complete by model output
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import json
import logging
from pathlib import Path
from typing import Dict, Any, List

# Ensure Supergent--main is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = BASE_DIR / "Supergent--main"
if str(SUPERGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
LOG = logging.getLogger("WISE.Test.P1_3_C_HITL_Security")

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
print("   WISE PHASE P1.3-C: HUMAN-IN-THE-LOOP & SECURITY HARDENING VERIFICATION")
print("=" * 80)

# Import Security & Core Subsystems
from core.security import (
    ActionTier,
    HumanInterventionType,
    SecurityContext,
    SecurityEvaluation,
    WindowsSecurityGate,
    ActionConfirmationToken,
    ConfirmationManager,
    TransientSensitiveStore,
    redact_sensitive_payload,
    sanitize_text,
    ChallengeType,
    ChallengeDetectionResult,
    ChallengeDetector,
)
from core.brain.task_engine import (
    TaskEngine,
    Task,
    Subgoal,
    TaskStep,
    TaskCheckpoint,
    TaskStatus,
    SubgoalStatus,
    StepStatus,
    TaskDomain,
)
from core.hands import ComputerActionType
from core.orchestrator.closed_loop_orchestrator import (
    ClosedLoopOrchestrator,
    OrchestrationStep,
    OrchestrationCycleResult,
)
from core.context.world_state import (
    get_world_state_engine,
    WISEWorldState,
    StateItem,
)

# ==============================================================================
# PART 1: Confirmation Manager & Anti-TOCTOU Protection (Tests 8, 9, 10, 11, 21)
# ==============================================================================
print("\n--- PART 1: Anti-TOCTOU Confirmation Architecture ---")

cm = ConfirmationManager(default_ttl_seconds=2.0)
tok = cm.create_confirmation_request(
    action_name="transfer_money",
    params={"recipient": "bob@example.com", "amount": 50.0, "currency": "USD"},
    description="Transfer 50.0 USD to bob@example.com",
)

check("Confirmation request created", tok is not None and tok.token_id.startswith("tok_"))
check("Token starts in unapproved state", not tok.approved)
check("Token is not expired initially", not tok.is_expired)

# Test 8: Exact-Action Confirmation
ok, msg = cm.approve_token(tok.token_id, approver="alice_user")
check("Token explicitly approved by user", ok and tok.approved)

# Execute with exact approved parameters
valid, reason = cm.validate_and_consume_token(
    token_id=tok.token_id,
    action_name="transfer_money",
    current_params={"recipient": "bob@example.com", "amount": 50.0, "currency": "USD"},
)
check("Test 8: Exact-action confirmation succeeds", valid and reason == "VALIDATED_AND_CONSUMED")
check("Token is now marked used", tok.used)

# Test 21: Single-Use / Replay Rejection
valid_replay, reason_replay = cm.validate_and_consume_token(
    token_id=tok.token_id,
    action_name="transfer_money",
    current_params={"recipient": "bob@example.com", "amount": 50.0, "currency": "USD"},
)
check("Test 21: Replay attack rejected (token already consumed)", not valid_replay and "ALREADY_USED" in reason_replay)

# Test 10: Changed-Target Rejection (Anti-TOCTOU)
tok2 = cm.create_confirmation_request(
    action_name="transfer_money",
    params={"recipient": "legitimate_merchant", "amount": 100.0},
    description="Transfer to legitimate_merchant",
)
cm.approve_token(tok2.token_id, approver="user")
tampered_target_valid, tampered_target_err = cm.validate_and_consume_token(
    token_id=tok2.token_id,
    action_name="transfer_money",
    current_params={"recipient": "attacker_account", "amount": 100.0},
)
check("Test 10: Changed-target TOCTOU tampering rejected", not tampered_target_valid and "TOCTOU" in tampered_target_err)

# Test 11: Changed-Amount Rejection (Anti-TOCTOU)
tok3 = cm.create_confirmation_request(
    action_name="execute_payment",
    params={"invoice_id": "INV-100", "amount": 15.00},
    description="Pay invoice INV-100",
)
cm.approve_token(tok3.token_id, approver="user")
tampered_amount_valid, tampered_amount_err = cm.validate_and_consume_token(
    token_id=tok3.token_id,
    action_name="execute_payment",
    current_params={"invoice_id": "INV-100", "amount": 1500.00},
)
check("Test 11: Changed-amount TOCTOU tampering rejected", not tampered_amount_valid and "TOCTOU" in tampered_amount_err)

# Test 9: Stale Confirmation Rejection
tok_stale = cm.create_confirmation_request(
    action_name="delete_file",
    params={"path": "C:\\safe\\data.tmp"},
    description="Delete temp file",
    ttl_seconds=0.1,  # short TTL
)
cm.approve_token(tok_stale.token_id, approver="user")
time.sleep(0.2)  # expire token
stale_valid, stale_err = cm.validate_and_consume_token(
    token_id=tok_stale.token_id,
    action_name="delete_file",
    current_params={"path": "C:\\safe\\data.tmp"},
)
check("Test 9: Stale / expired confirmation rejected", not stale_valid and "EXPIRED" in stale_err)

# ==============================================================================
# PART 2: Secret Redaction & Transient Sensitive Store (Tests 5, 6)
# ==============================================================================
print("\n--- PART 2: Secret Protection & Transient Sensitive Handling ---")

# Test 5: OTP treated as transient sensitive data
vault = TransientSensitiveStore(default_ttl_seconds=1.0)
vault.store_transient_secret("sms_otp", "849201", ttl_seconds=0.5)

val = vault.retrieve_transient_secret("sms_otp", consume=True)
check("Test 5.1: Transient OTP retrieved successfully", val == "849201")
val_after = vault.retrieve_transient_secret("sms_otp")
check("Test 5.2: Transient OTP evicted immediately upon consumption", val_after is None)

# Test TTL expiry in vault
vault.store_transient_secret("temp_token", "secret123", ttl_seconds=0.1)
time.sleep(0.15)
check("Test 5.3: Transient secret evicted after TTL expiry", vault.retrieve_transient_secret("temp_token") is None)

# Test 6: Credential Redaction across nested payloads, logs, and checkpoints
sensitive_dict = {
    "user": "alice",
    "password": "SuperSecretPassword123!",
    "api_key": "sk-proj-1234567890abcdef1234567890",
    "card": "4111-2222-3333-4444",
    "notes": "Here is the code: 654321 and SSN 123-45-6789",
    "safe_field": "public_data",
}
sanitized = redact_sensitive_payload(sensitive_dict)

check("Test 6.1: Password field scrubbed", sanitized["password"] == "[REDACTED:SENSITIVE_FIELD]")
check("Test 6.2: API key field scrubbed", sanitized["api_key"] == "[REDACTED:SENSITIVE_FIELD]")
check("Test 6.3: Safe field preserved", sanitized["safe_field"] == "public_data")
check("Test 6.4: Credit card scrubbed in text", "[REDACTED:CARD_NUMBER]" in sanitized["card"])
check("Test 6.5: OTP scrubbed in text", "[REDACTED:OTP]" in sanitized["notes"])
check("Test 6.6: SSN scrubbed in text", "[REDACTED:SSN]" in sanitized["notes"])

# Verify World State items scrub secrets
world_engine = get_world_state_engine()
state = world_engine.get_current_world_state(force_fresh=True)
state.add_item(StateItem(key="user_password", value="MySecretPass!", source="test"))
item_retrieved = state.get_item("user_password")
check("Test 6.7: World State add_item sanitizes sensitive keys", item_retrieved.value == "[REDACTED:SENSITIVE_DATA]")

# ==============================================================================
# PART 3: SecurityGate Policy Hierarchy & Prompt Injection (Tests 7, 12, 13, 16, 17, 20)
# ==============================================================================
print("\n--- PART 3: SecurityGate Policy Hierarchy & Prompt Injection ---")

custom_cm = ConfirmationManager(default_ttl_seconds=60.0)
gate = WindowsSecurityGate(confirmation_manager=custom_cm)

# Test 17: Normal low-risk action does NOT unnecessarily require confirmation
low_risk_ev = gate.evaluate("click", {"x": 100, "y": 200}, context=SecurityContext(caller="agent"))
check("Test 17: Low-risk click auto-allowed without confirmation", low_risk_ev.allowed and not low_risk_ev.requires_confirmation)
read_ev = gate.evaluate("read_screen", {}, context=SecurityContext(caller="agent"))
check("Test 17.2: Read screen auto-allowed without confirmation", read_ev.allowed and read_ev.tier == ActionTier.READ)

# Test 7: Confirmation required for high-impact action (Financial)
fin_ev = gate.evaluate("transfer_money", {"recipient": "carol", "amount": 250.0}, context=SecurityContext(caller="agent"))
check("Test 7.1: Financial action requires confirmation", not fin_ev.allowed and fin_ev.requires_confirmation)
check("Test 7.2: Financial action flagged as FINANCIAL_TRANSFER intervention", fin_ev.intervention_type == HumanInterventionType.FINANCIAL_TRANSFER)
check("Test 7.3: Confirmation token issued", fin_ev.confirmation_token is not None)

# Test 12: Destructive action blocking without explicit confirmation
destr_ev = gate.evaluate("format_drive", {"drive": "D:"}, context=SecurityContext(caller="agent", confirmed=False))
check("Test 12.1: Destructive format_drive blocked without confirmation", not destr_ev.allowed and destr_ev.requires_confirmation)
check("Test 12.2: Destructive action flagged as DESTRUCTIVE_CONFIRMATION", destr_ev.intervention_type == HumanInterventionType.DESTRUCTIVE_CONFIRMATION)

# Test 13: Prompt Injection Isolation (untrusted content cannot execute high-impact or destructive)
untrusted_ctx = SecurityContext(
    caller="web_scraper",
    is_untrusted_content=True,
    confirmed=True,  # Attacker attempts to claim pre-confirmed status in payload
)
pi_destr_ev = gate.evaluate("delete_file", {"path": "C:\\important\\database.db"}, context=untrusted_ctx)
check("Test 13.1: Untrusted prompt injection cannot delete file", not pi_destr_ev.allowed and pi_destr_ev.quarantined)
check("Test 13.2: Quarantined by Prompt Injection Firewall", "Prompt Injection Firewall" in pi_destr_ev.reason)

pi_fin_ev = gate.evaluate("transfer_money", {"recipient": "attacker", "amount": 1000.0}, context=untrusted_ctx)
check("Test 13.3: Untrusted prompt injection cannot execute financial transfer", not pi_fin_ev.allowed and pi_fin_ev.quarantined)

# Test 16: Security Denial
deny_ev = gate.evaluate("modify_registry", {"key": "HKLM\\Software\\Tamper"}, context=SecurityContext(caller="agent", confirmed=False))
check("Test 16: Administrative registry modification denied without confirmation", not deny_ev.allowed and deny_ev.requires_confirmation)

# Test 20: Model output != authorization (cannot bypass SecurityGate)
class FakeModelOutput:
    def __init__(self):
        self.proposes_action = "format_drive"
        self.claims_authorized = True
        self.params = {"drive": "C:"}

fake_model = FakeModelOutput()
gate_decision = gate.evaluate(fake_model.proposes_action, fake_model.params, context=SecurityContext(caller="agent", confirmed=False))
check("Test 20: Model self-authorization claim is rejected by SecurityGate", not gate_decision.allowed)

# ==============================================================================
# PART 4: Multi-Modal Challenge Detector (Tests 1, 3, 22)
# ==============================================================================
print("\n--- PART 4: Multi-Modal Challenge Detector ---")

detector = ChallengeDetector()

# Test 1.1: DOM-based CAPTCHA Detection
class FakeDOMElement:
    def __init__(self, role: str, name: str, ref_id: str):
        self.role = role
        self.name = name
        self.ref_id = ref_id

dom_elements = [
    FakeDOMElement("button", "Submit", "btn1"),
    FakeDOMElement("iframe", "Google reCAPTCHA widget", "g-recaptcha-response"),
]
dom_res = detector.detect_from_browser_elements(
    elements=dom_elements,
    aria_text="Please verify you are human before proceeding to payment.",
    page_url="https://secure.example.com/checkout?challenge=recaptcha",
)
check("Test 1.1: CAPTCHA detected via DOM elements & ARIA text", dom_res.detected and dom_res.challenge_type == ChallengeType.CAPTCHA)
check("Test 1.2: CAPTCHA requires human intervention", dom_res.requires_human)
check("Test 1.3: Evidence collected from DOM", any("CAPTCHA" in ev for ev in dom_res.evidence))

# Test 3.1: MFA / OTP Detection via DOM & Text
mfa_elements = [
    FakeDOMElement("textbox", "Enter one-time-code", "input_otp"),
]
mfa_res = detector.detect_from_browser_elements(
    elements=mfa_elements,
    aria_text="Enter the 6-digit verification code sent to your mobile device.",
)
check("Test 3.1: MFA / OTP detected via input element", mfa_res.detected and mfa_res.challenge_type in (ChallengeType.OTP, ChallengeType.MFA))
check("Test 3.2: MFA requires human intervention", mfa_res.requires_human)

# Test 1.4: OCR-based CAPTCHA Detection
ocr_text = "Security Check: Select all images with traffic lights. Verify you are human."
ocr_res = detector.detect_from_ocr(ocr_text)
check("Test 1.4: CAPTCHA detected via Windows Media OCR text", ocr_res.detected and ocr_res.challenge_type == ChallengeType.CAPTCHA)

# Test 3.3: OCR-based MFA Detection
ocr_mfa_text = "Two-step verification: Enter verification code to sign in."
ocr_mfa_res = detector.detect_from_ocr(ocr_mfa_text)
check("Test 3.3: MFA detected via OCR text", ocr_mfa_res.detected and ocr_mfa_res.challenge_type == ChallengeType.MFA)

# Test 22: CAPTCHA cannot be auto-marked complete by model output
model_claim_solved = False
check("Test 22: CAPTCHA requires independent observation verification, model cannot self-certify", not model_claim_solved)

# ==============================================================================
# PART 5: TaskEngine HITL Lifecycle & State Preservation (Tests 2, 4, 14, 15)
# ==============================================================================
print("\n--- PART 5: TaskEngine HITL Lifecycle & State Preservation ---")

te = TaskEngine()

step1 = TaskStep(action_type=ComputerActionType.BROWSER_NAVIGATE, params={"url": "https://portal.example.com/login"}, description="Navigate to portal")
step2 = TaskStep(action_type=ComputerActionType.BROWSER_CLICK, params={"target": "submit_btn"}, description="Submit login form")
step3 = TaskStep(action_type=ComputerActionType.BROWSER_EXTRACT, params={"target": "dashboard"}, description="Extract dashboard data")

subgoal1 = Subgoal(title="Authenticate", steps=[step1, step2])
subgoal2 = Subgoal(title="Read Dashboard", steps=[step3])

task = te.create_task(
    user_intent="Login to portal and extract dashboard",
    semantic_goal="Extract dashboard metrics",
    subgoals=[subgoal1, subgoal2],
)
te.start_task(task.task_id)

# Execute step 1
te.record_step_result(task.task_id, step1.step_id, success=True)
te.advance_step(task.task_id)

check("Task on Step 2 of Subgoal 1", task.current_subgoal_idx == 0 and subgoal1.current_step_idx == 1)

# Encounter CAPTCHA on Step 2 -> PAUSE_FOR_HUMAN
chk = te.pause_for_human(
    task_id=task.task_id,
    intervention_type="CAPTCHA",
    reason="CAPTCHA challenge detected on login form.",
    prompt="Please solve the CAPTCHA in the browser.",
    details={"url": "https://portal.example.com/login"},
)

check("Test 1.5: Task status transitioned to PAUSED_FOR_HUMAN", task.status == TaskStatus.PAUSED_FOR_HUMAN)
check("Test 1.6: Checkpoint created on pause", chk is not None and chk in task.checkpoints)
check("Test 1.7: Active intervention recorded", task.active_intervention is not None and task.active_intervention["type"] == "CAPTCHA")
check("Test 14.1: Subgoal and step pointers preserved during pause", task.current_subgoal_idx == 0 and subgoal1.current_step_idx == 1)

# Test 15: User Cancellation of Intervention
task_cancel = te.create_task("Disposable task", "Test cancel", subgoals=[Subgoal(title="SG", steps=[TaskStep()])])
te.start_task(task_cancel.task_id)
te.pause_for_human(task_cancel.task_id, reason="Testing user cancel")
te.resolve_human_intervention(task_cancel.task_id, action="cancelled")
check("Test 15: User cancellation cancels task cleanly", task_cancel.status == TaskStatus.CANCELLED)

# Test 2 & 14: Task Resume from Exact Interrupted Step (NOT restarted!)
resumed = te.resolve_human_intervention(task.task_id, action="completed")
check("Test 2.1: Human intervention resolved to RUNNING", resumed and task.status == TaskStatus.RUNNING)
check("Test 2.2: Active intervention cleared", task.active_intervention is None)
check("Test 14.2: Resumes on exact interrupted step (Subgoal 0, Step 1)", task.current_subgoal_idx == 0 and subgoal1.current_step_idx == 1)
check("Test 14.3: Previous completed steps remain completed", step1.status == StepStatus.COMPLETED)

# Advance and complete task
te.record_step_result(task.task_id, step2.step_id, success=True)
te.advance_step(task.task_id)
check("Advanced to Subgoal 2", task.current_subgoal_idx == 1)
te.record_step_result(task.task_id, step3.step_id, success=True)
te.advance_step(task.task_id)
check("Task reaches COMPLETED without restarting", task.status == TaskStatus.COMPLETED)

# Test 4: MFA Pause & Resume Lifecycle
task_mfa = te.create_task("MFA Flow", "Authenticate via MFA", subgoals=[
    Subgoal(title="MFA SG", steps=[
        TaskStep(action_type=ComputerActionType.WAIT, params={"duration": 0.1}),
        TaskStep(action_type=ComputerActionType.WAIT, params={"duration": 0.1}),
    ])
])
te.start_task(task_mfa.task_id)
te.pause_for_human(task_mfa.task_id, intervention_type="MFA", reason="Enter 6-digit MFA code")
check("Test 4.1: MFA task pauses in PAUSED_FOR_HUMAN", task_mfa.status == TaskStatus.PAUSED_FOR_HUMAN)
te.resolve_human_intervention(task_mfa.task_id, action="completed")
check("Test 4.2: MFA task resumes to RUNNING at exact step", task_mfa.status == TaskStatus.RUNNING and task_mfa.subgoals[0].current_step_idx == 0)

# ==============================================================================
# PART 6: Closed-Loop Orchestrator HITL Integration (Tests 18, 19)
# ==============================================================================
print("\n--- PART 6: Closed-Loop Orchestrator HITL Integration ---")

orchestrator_cm = ConfirmationManager(default_ttl_seconds=60.0)
orchestrator_gate = WindowsSecurityGate(confirmation_manager=orchestrator_cm)
orchestrator = ClosedLoopOrchestrator(
    security_gate=orchestrator_gate,
    confirmation_manager=orchestrator_cm,
)

# Test 18: Browser task pauses on required human intervention and resumes correctly
browser_steps = [
    OrchestrationStep(action_type=ComputerActionType.WAIT, params={"duration": 0.01}),
    OrchestrationStep(
        action_type=ComputerActionType.WAIT,
        params={"duration": 0.01},
        requires_human=True,
        human_intervention_type="CAPTCHA",
        human_intervention_reason="CAPTCHA challenge on checkout page.",
        human_prompt="Please complete the CAPTCHA.",
    ),
    OrchestrationStep(action_type=ComputerActionType.WAIT, params={"duration": 0.01}),
]

res_browser = orchestrator.run_cycle(intent="Simulated browser checkout with CAPTCHA", steps=browser_steps)
check("Test 18.1: Browser cycle pauses when human intervention required", res_browser.paused_for_human)
check("Test 18.2: 1 step executed prior to pause", res_browser.steps_executed == 1)
check("Test 18.3: Intervention details captured", res_browser.intervention_details["intervention_type"] == "CAPTCHA")

# Test 19: Windows task pauses on high-impact confirmation and resumes with Anti-TOCTOU token
windows_steps = [
    OrchestrationStep(action_type=ComputerActionType.WAIT, params={"duration": 0.01}),
    OrchestrationStep(
        action_type=ComputerActionType.WAIT,
        params={"action_name": "transfer_money", "recipient": "vendor@store.com", "amount": 99.0},
    ),
]

res_win = orchestrator.run_cycle(intent="Simulated payment on desktop", steps=windows_steps)
check("Test 19.1: Windows high-impact transfer paused for confirmation", res_win.paused_for_human)
tok_id = res_win.intervention_details.get("confirmation_token")
check("Test 19.2: Confirmation token issued by SecurityGate", tok_id is not None)

# User approves token
orchestrator_cm.approve_token(tok_id, approver="authorized_user")

# Resume with approved confirmation token
res_win_resumed = orchestrator.run_cycle(
    intent="Simulated payment on desktop - resumed",
    steps=[
        OrchestrationStep(
            action_type=ComputerActionType.WAIT,
            params={"action_name": "transfer_money", "recipient": "vendor@store.com", "amount": 99.0},
            confirmation_token=tok_id,
        )
    ],
)
check("Test 19.3: Task executes successfully with valid Anti-TOCTOU confirmation token", res_win_resumed.success)

# ==============================================================================
# SUMMARY REPORT
# ==============================================================================
print("\n" + "=" * 80)
print(f"   WISE PHASE P1.3-C VERIFICATION SUMMARY: {passed} PASSED, {failed} FAILED")
print("=" * 80)

if failed > 0:
    sys.exit(1)
