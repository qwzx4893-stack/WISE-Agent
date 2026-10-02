# ==============================================================================
# WISE Windows Security Gate (WindowsSecurityGate)
# Deterministic, out-of-model security governance engine.
# Classifies actions into 11 discrete policy tiers, enforces Anti-TOCTOU
# confirmation barriers on high-impact/financial/destructive actions,
# defends against Prompt Injection via provenance tainting, and maintains
# an immutable, secret-redacted audit log.
#
# Fundamental Invariant:
# MODEL OUTPUT != AUTHORIZATION
# Zero singletons for new components: Supports dependency injection.
# ==============================================================================

from __future__ import annotations

import os
import json
import time
import logging
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

from core.security.confirmation import ConfirmationManager, ActionConfirmationToken
from core.security.transient_vault import redact_sensitive_payload

LOG = logging.getLogger("wise.security_gate")


class ActionTier(str, Enum):
    # Tier 1: Passive Observation & Inspection
    READ = "READ"
    # Tier 2: Harmless / Low Impact Actions
    LOW_RISK = "LOW_RISK"
    LOW_RISK_ACTION = "LOW_RISK_ACTION"
    # Tier 3: Workspace File Updates
    FILE_MODIFICATION = "FILE_MODIFICATION"
    # Tier 4: App & Window Control
    APPLICATION_CONTROL = "APPLICATION_CONTROL"
    MODERATE = "MODERATE"
    # Tier 5: External Webhook / API Calls
    EXTERNAL_COMMUNICATION = "EXTERNAL_COMMUNICATION"
    # Tier 6: Profile & Account Settings
    ACCOUNT_ACTION = "ACCOUNT_ACTION"
    # Tier 7: Sensitive Credentials & Financial Data
    SENSITIVE_DATA = "SENSITIVE_DATA"
    # Tier 8: Authentication & Login
    AUTHENTICATION = "AUTHENTICATION"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    # Tier 9: Anti-Bot & CAPTCHA Barriers
    HUMAN_VERIFICATION = "HUMAN_VERIFICATION"
    # Tier 10: Financial Transactions & Payments
    FINANCIAL_ACTION = "FINANCIAL_ACTION"
    # Tier 11: Destructive System Operations
    DESTRUCTIVE = "DESTRUCTIVE"
    DESTRUCTIVE_ACTION = "DESTRUCTIVE_ACTION"


class HumanInterventionType(str, Enum):
    CAPTCHA = "CAPTCHA"
    MFA = "MFA"
    OTP = "OTP"
    LOGIN = "LOGIN"
    CREDENTIAL_ENTRY = "CREDENTIAL_ENTRY"
    PAYMENT_CONFIRMATION = "PAYMENT_CONFIRMATION"
    FINANCIAL_TRANSFER = "FINANCIAL_TRANSFER"
    DESTRUCTIVE_CONFIRMATION = "DESTRUCTIVE_CONFIRMATION"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    HIGH_IMPACT_CONFIRMATION = "HIGH_IMPACT_CONFIRMATION"
    POLICY_INTERVENTION = "POLICY_INTERVENTION"


@dataclass
class SecurityContext:
    caller: str = "agent"
    is_untrusted_content: bool = False
    confirmed: bool = False
    elevation_requested: bool = False
    session_id: str = "default"
    confirmation_token: Optional[str] = None
    taint_sources: List[str] = field(default_factory=list)
    project_write_scope: tuple = () # trusted runtime binding, never a tool argument


@dataclass
class SecurityEvaluation:
    allowed: bool
    tier: ActionTier
    reason: str
    requires_confirmation: bool = False
    requires_human_intervention: bool = False
    intervention_type: Optional[HumanInterventionType] = None
    quarantined: bool = False
    audit_id: str = ""
    confirmation_token: Optional[str] = None
    required_confirmation_details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "tier": self.tier.value,
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
            "requires_human_intervention": self.requires_human_intervention,
            "intervention_type": self.intervention_type.value if self.intervention_type else None,
            "quarantined": self.quarantined,
            "audit_id": self.audit_id,
            "confirmation_token": self.confirmation_token,
            "required_confirmation_details": self.required_confirmation_details,
        }


# Comprehensive Default action tier classifications
DEFAULT_TIER_RULES: Dict[str, ActionTier] = {
    "browse_skills": ActionTier.READ,
    "browse_tools": ActionTier.READ,
    "browse_resources": ActionTier.READ,
    "security_scan": ActionTier.READ,
    # READ: Passive observation
    "get_state": ActionTier.READ,
    "observe": ActionTier.READ,
    "read_screen": ActionTier.READ,
    "read_file": ActionTier.READ,
    "list_directory": ActionTier.READ,
    "grep_file": ActionTier.READ,
    "search_knowledge": ActionTier.READ,
    "get_hardware_metrics": ActionTier.READ,
    "browser_scroll": ActionTier.READ,
    "browser_wait": ActionTier.READ,
    "browser_extract": ActionTier.READ,
    "browser_observe": ActionTier.READ,

    # LOW_RISK / LOW_RISK_ACTION: Normal low-friction desktop & browser interaction
    "move_mouse": ActionTier.LOW_RISK,
    "click": ActionTier.LOW_RISK,
    "double_click": ActionTier.LOW_RISK,
    "right_click": ActionTier.LOW_RISK,
    "scroll": ActionTier.LOW_RISK,
    "focus_window": ActionTier.LOW_RISK,
    "browser_navigate": ActionTier.LOW_RISK,
    "browser_click": ActionTier.LOW_RISK,
    "browser_type": ActionTier.LOW_RISK,
    "browser_clear": ActionTier.LOW_RISK,
    "browser_press_key": ActionTier.LOW_RISK,
    "browser_back": ActionTier.LOW_RISK,
    "browser_forward": ActionTier.LOW_RISK,
    "browser_reload": ActionTier.LOW_RISK,
    "browser_switch_tab": ActionTier.LOW_RISK,
    "browser_new_tab": ActionTier.LOW_RISK,
    "browser_close_tab": ActionTier.LOW_RISK,
    "browser_close": ActionTier.LOW_RISK,

    # FILE_MODIFICATION: Handled with FilesystemGovernor
    "write_workspace_file": ActionTier.LOW_RISK,
    "write_file": ActionTier.MODERATE,
    "move_file": ActionTier.MODERATE,

    # APPLICATION_CONTROL / MODERATE
    "open_app": ActionTier.MODERATE,
    "close_app": ActionTier.MODERATE,
    "type_text": ActionTier.MODERATE,
    "send_hotkey": ActionTier.MODERATE,
    "run_command": ActionTier.MODERATE,
    "run_shell": ActionTier.ADMINISTRATIVE,
    "execute_python": ActionTier.ADMINISTRATIVE,
    "execute_command": ActionTier.MODERATE,
    "execute_powershell_script": ActionTier.MODERATE,
    "run_script": ActionTier.MODERATE,
    "powershell": ActionTier.MODERATE,
    "cmd": ActionTier.MODERATE,

    # EXTERNAL_COMMUNICATION
    "post_webhook": ActionTier.EXTERNAL_COMMUNICATION,
    "send_email": ActionTier.EXTERNAL_COMMUNICATION,

    # ACCOUNT_ACTION
    "change_password": ActionTier.ACCOUNT_ACTION,
    "modify_account_settings": ActionTier.ACCOUNT_ACTION,
    "edit_profile": ActionTier.ACCOUNT_ACTION,

    # SENSITIVE_DATA / ADMINISTRATIVE: Sensitive fields
    "browser_type_sensitive": ActionTier.ADMINISTRATIVE,
    "browser_extract_sensitive": ActionTier.ADMINISTRATIVE,
    "read_credential": ActionTier.SENSITIVE_DATA,
    "input_sensitive_data": ActionTier.SENSITIVE_DATA,

    # AUTHENTICATION
    "login_submit": ActionTier.AUTHENTICATION,
    "enter_credentials": ActionTier.AUTHENTICATION,
    "submit_mfa": ActionTier.AUTHENTICATION,
    "enter_otp": ActionTier.AUTHENTICATION,

    # HUMAN_VERIFICATION: Must never be automated/bypassed
    "solve_captcha": ActionTier.HUMAN_VERIFICATION,
    "verify_human": ActionTier.HUMAN_VERIFICATION,
    "browser_captcha_solve": ActionTier.HUMAN_VERIFICATION,

    # FINANCIAL_ACTION: Mandatory Anti-TOCTOU confirmation token
    "transfer_money": ActionTier.FINANCIAL_ACTION,
    "execute_payment": ActionTier.FINANCIAL_ACTION,
    "confirm_purchase": ActionTier.FINANCIAL_ACTION,
    "checkout_submit": ActionTier.FINANCIAL_ACTION,
    "financial_wire": ActionTier.FINANCIAL_ACTION,

    # ADMINISTRATIVE
    "modify_registry": ActionTier.ADMINISTRATIVE,
    "manage_service": ActionTier.ADMINISTRATIVE,
    "change_system_setting": ActionTier.ADMINISTRATIVE,
    "run_elevated_command": ActionTier.ADMINISTRATIVE,
    "install_tool": ActionTier.ADMINISTRATIVE,

    # DESTRUCTIVE / DESTRUCTIVE_ACTION
    "delete_file": ActionTier.DESTRUCTIVE,
    "delete_directory": ActionTier.DESTRUCTIVE,
    "kill_process": ActionTier.DESTRUCTIVE,
    "format_drive": ActionTier.DESTRUCTIVE,
    "overwrite_system_file": ActionTier.DESTRUCTIVE,
}

CRITICAL_SYSTEM_DIRECTORIES = [
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
    r"c:\boot",
]

CRITICAL_SYSTEM_PROCESSES = [
    "csrss.exe", "lsass.exe", "services.exe", "smss.exe", "wininit.exe",
    "winlogon.exe", "explorer.exe", "antigravity.exe", "svchost.exe",
]


class WindowsSecurityGate:
    """
    Enforces deterministic security boundaries outside the LLM.
    Governs action authorization, prompt injection taint isolation,
    and anti-TOCTOU confirmation validation.
    """

    def __init__(
        self,
        audit_log_path: Optional[Path] = None,
        confirmation_manager: Optional[ConfirmationManager] = None,
    ) -> None:
        if audit_log_path is None:
            base_dir = Path(__file__).resolve().parent.parent.parent
            self.audit_log_path = base_dir / "logs" / "security_audit.jsonl"
        else:
            self.audit_log_path = Path(audit_log_path)

        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._action_rules: Dict[str, ActionTier] = dict(DEFAULT_TIER_RULES)
        # Injectable confirmation manager for Anti-TOCTOU validation
        self.confirmation_manager = confirmation_manager or ConfirmationManager()

    def register_action_tier(self, action_name: str, tier: ActionTier) -> None:
        with self._lock:
            self._action_rules[action_name] = tier

    def get_action_tier(self, action_name: str) -> ActionTier:
        with self._lock:
            return self._action_rules.get(action_name, ActionTier.MODERATE)

    def evaluate_action(
        self,
        action_name: str,
        params: Dict[str, Any],
        context: Optional[SecurityContext] = None,
    ) -> SecurityEvaluation:
        """Alias for evaluate() ensuring backward and canonical compatibility."""
        return self.evaluate(action_name, params, context)

    def evaluate(
        self,
        action_name: str,
        params: Dict[str, Any],
        context: Optional[SecurityContext] = None,
    ) -> SecurityEvaluation:
        """
        Evaluates whether an action is allowed, requires confirmation,
        requires human intervention, or is blocked/quarantined.
        """
        ctx = context or SecurityContext()
        tier = self.get_action_tier(action_name)
        audit_id = f"SEC-{int(time.time() * 1000)}-{os.urandom(3).hex()}"

        if action_name == "open_app":
            target = str(params.get("app") or params.get("app_name") or "")
            if any(c in target for c in ('&', '|', ';', '<', '>', '\n', '\r', '\x00', '`')):
                evaluation = SecurityEvaluation(
                    allowed=False, tier=tier, reason="Application target must not contain shell syntax.",
                    audit_id=audit_id,
                )
                self._write_audit_log(action_name, params, ctx, evaluation)
                return evaluation

        # Command security inspection
        if (
            action_name in ("run_command", "run_shell", "execute_python", "execute_command", "execute_powershell_script", "run_script", "powershell", "cmd")
            and ("command" in params or "script" in params)
        ):
            cmd = str(params.get("command") or params.get("script") or "").lower()
            import re
            destructive_cmd_patterns = [
                r"\brm\s+-rf\b", r"\bmkfs\b", r"\bformat\b",
                r"\bdd\s+if=", r"\bdel\s+/[fqs/]*\b", r"\brmdir\s+/[s/]*\b",
                r">\s*/dev/sda", r":\(\)\s*\{",
                r"remove-item.*-(?:recurse|force)",
            ]
            for pat in destructive_cmd_patterns:
                if re.search(pat, cmd):
                    tier = ActionTier.DESTRUCTIVE
                    break
            for sys_dir in CRITICAL_SYSTEM_DIRECTORIES:
                if sys_dir.lower() in cmd:
                    tier = ActionTier.ADMINISTRATIVE
                    break

        # A force-close can terminate a process and discard unsaved work even
        # when it was requested through a window-control API rather than an
        # explicit ``kill_process`` tool.
        if action_name in ("close_window", "close_app") and bool(params.get("force")):
            tier = ActionTier.DESTRUCTIVE

        # 1. Filesystem Governor Integration & Canonical Directory Protection
        if "path" in params or action_name in ("read_file", "write_file", "delete_file", "move_file", "list_directory"):
            try:
                from core.windows.filesystem_governor import get_filesystem_governor, FileOperation
                gov = get_filesystem_governor()
                op_map = {
                    "read_file": FileOperation.READ,
                    "list_directory": FileOperation.LIST,
                    "write_file": FileOperation.WRITE,
                    "delete_file": FileOperation.DELETE,
                    "move_file": FileOperation.MOVE,
                }
                file_op = op_map.get(action_name, FileOperation.READ)
                primary_path = params.get("path") or params.get("source_path") or params.get("target_path", "")
                # ``ComputerControl.move_file`` and older tool manifests use
                # ``destination``.  Omitting it here checked only the source,
                # allowing a move into a protected credential/system path.
                secondary_path = (
                    params.get("destination_path")
                    or params.get("destination")
                    or params.get("secondary_path")
                )

                if primary_path:
                    fs_decision = gov.evaluate_operation(
                        op=file_op,
                        path=primary_path,
                        secondary_path=secondary_path,
                        is_confirmed=ctx.confirmed,
                    )
                    base_tier = self.get_action_tier(action_name)
                    tier_order = {
                        ActionTier.READ: 1,
                        ActionTier.LOW_RISK: 2,
                        ActionTier.LOW_RISK_ACTION: 2,
                        ActionTier.MODERATE: 3,
                        ActionTier.FILE_MODIFICATION: 3,
                        ActionTier.APPLICATION_CONTROL: 3,
                        ActionTier.EXTERNAL_COMMUNICATION: 4,
                        ActionTier.ACCOUNT_ACTION: 4,
                        ActionTier.SENSITIVE_DATA: 4,
                        ActionTier.AUTHENTICATION: 4,
                        ActionTier.ADMINISTRATIVE: 4,
                        ActionTier.HUMAN_VERIFICATION: 5,
                        ActionTier.FINANCIAL_ACTION: 5,
                        ActionTier.DESTRUCTIVE: 5,
                        ActionTier.DESTRUCTIVE_ACTION: 5,
                    }
                    if tier_order.get(fs_decision.tier, 0) > tier_order.get(base_tier, 0):
                        tier = fs_decision.tier
                    else:
                        tier = base_tier

                    if not fs_decision.allowed and (not ctx.is_untrusted_content or ctx.project_write_scope):
                        evaluation = SecurityEvaluation(
                            allowed=False,
                            tier=tier,
                            reason=fs_decision.reason,
                            requires_confirmation=fs_decision.requires_confirmation,
                            audit_id=audit_id,
                        )
                        self._write_audit_log(action_name, params, ctx, evaluation)
                        return evaluation
            except Exception as e:
                LOG.error("FilesystemGovernor integration error: %s", e)

        # 2. Prompt Injection Firewall
        # Content marked as untrusted (from web search, emails, documents) can NEVER trigger
        # Administrative, Destructive, Financial, Account, or Authentication actions, nor arbitrary shell execution.
        privileged_tiers = (
            ActionTier.ADMINISTRATIVE,
            ActionTier.DESTRUCTIVE,
            ActionTier.DESTRUCTIVE_ACTION,
            ActionTier.FINANCIAL_ACTION,
            ActionTier.ACCOUNT_ACTION,
            ActionTier.AUTHENTICATION,
        )
        blocked_untrusted_actions = (
            "format_drive", "delete_file", "kill_process", "terminate_process",
            "run_command", "run_shell", "execute_python", "open_app", "execute_command", "shell_exec", "powershell", "cmd",
            "write_registry", "set_environment_variable", "install_service",
            "execute_powershell_script", "run_script", "write_file",
        )
        from core.security.project_scope import scoped_workspace_write
        scoped_write = action_name == "write_file" and tier not in privileged_tiers and scoped_workspace_write(params,ctx.project_write_scope)
        if ctx.is_untrusted_content and (tier in privileged_tiers or action_name in blocked_untrusted_actions) and not scoped_write:
            evaluation = SecurityEvaluation(
                allowed=False,
                tier=tier,
                reason="BLOCKED by Prompt Injection Firewall: Untrusted content source cannot execute system commands or escalated actions.",
                requires_confirmation=False,
                quarantined=True,
                audit_id=audit_id,
            )
            self._write_audit_log(action_name, params, ctx, evaluation)
            LOG.warning("Security Alert: Prompt Injection blocked for action '%s'", action_name)
            return evaluation

        # 2b. Critical Process Protection Guard
        if action_name in ("kill_process", "terminate_process", "close_app", "close_window"):
            target_proc = str(
                params.get("process_name") or params.get("name") or params.get("image_name")
                or params.get("process") or params.get("app_name") or ""
            ).lower().strip()
            if target_proc and any(p == target_proc or target_proc.endswith(f"\\{p}") for p in CRITICAL_SYSTEM_PROCESSES):
                evaluation = SecurityEvaluation(
                    allowed=False,
                    tier=ActionTier.DESTRUCTIVE,
                    reason=f"Protected system process '{target_proc}' cannot be terminated.",
                    requires_confirmation=False,
                    audit_id=audit_id,
                )
                self._write_audit_log(action_name, params, ctx, evaluation)
                LOG.warning("Security Alert: Blocked attempt to terminate protected system process: %s", target_proc)
                return evaluation

        # 3. Human Verification Barriers (CAPTCHA / Anti-Bot)
        # WISE strictly refuses to automate or bypass CAPTCHA.
        if tier == ActionTier.HUMAN_VERIFICATION or action_name in ("solve_captcha", "verify_human", "browser_captcha_solve"):
            evaluation = SecurityEvaluation(
                allowed=False,
                tier=tier,
                reason="CAPTCHA / human verification challenge detected. WISE must pause and require human intervention.",
                requires_confirmation=False,
                requires_human_intervention=True,
                intervention_type=HumanInterventionType.CAPTCHA,
                audit_id=audit_id,
            )
            self._write_audit_log(action_name, params, ctx, evaluation)
            return evaluation

        # 4. Financial Actions (High Impact — Anti-TOCTOU Confirmation Required)
        if tier == ActionTier.FINANCIAL_ACTION:
            if ctx.confirmation_token:
                valid, msg = self.confirmation_manager.validate_and_consume_token(
                    token_id=ctx.confirmation_token,
                    action_name=action_name,
                    current_params=params,
                )
                if valid:
                    evaluation = SecurityEvaluation(
                        allowed=True,
                        tier=tier,
                        reason=f"Financial action '{action_name}' authorized via valid single-use confirmation token.",
                        audit_id=audit_id,
                        confirmation_token=ctx.confirmation_token,
                    )
                else:
                    evaluation = SecurityEvaluation(
                        allowed=False,
                        tier=tier,
                        reason=f"Financial action '{action_name}' blocked: {msg}",
                        requires_confirmation=True,
                        audit_id=audit_id,
                    )
            else:
                # Issue new confirmation token request
                tok = self.confirmation_manager.create_confirmation_request(
                    action_name=action_name,
                    params=params,
                    description=f"Financial action: '{action_name}' with parameters {params}",
                )
                evaluation = SecurityEvaluation(
                    allowed=False,
                    tier=tier,
                    reason=f"Financial action '{action_name}' requires explicit user confirmation.",
                    requires_confirmation=True,
                    requires_human_intervention=True,
                    intervention_type=HumanInterventionType.FINANCIAL_TRANSFER,
                    confirmation_token=tok.token_id,
                    required_confirmation_details=tok.to_dict(),
                    audit_id=audit_id,
                )
            self._write_audit_log(action_name, params, ctx, evaluation)
            return evaluation

        # 5. Destructive Actions (High Impact — Anti-TOCTOU Confirmation Required)
        if tier in (ActionTier.DESTRUCTIVE, ActionTier.DESTRUCTIVE_ACTION):
            if ctx.confirmation_token:
                valid, msg = self.confirmation_manager.validate_and_consume_token(
                    token_id=ctx.confirmation_token,
                    action_name=action_name,
                    current_params=params,
                )
                if valid:
                    evaluation = SecurityEvaluation(
                        allowed=True,
                        tier=tier,
                        reason="Destructive action explicitly confirmed by user.",
                        audit_id=audit_id,
                        confirmation_token=ctx.confirmation_token,
                    )
                else:
                    evaluation = SecurityEvaluation(
                        allowed=False,
                        tier=tier,
                        reason=f"Destructive action '{action_name}' blocked: {msg}",
                        requires_confirmation=True,
                        audit_id=audit_id,
                    )
            elif ctx.confirmed:
                # Backward-compatible direct confirmed flag
                evaluation = SecurityEvaluation(
                    allowed=True,
                    tier=tier,
                    reason="Destructive action explicitly confirmed by user.",
                    audit_id=audit_id,
                )
            else:
                tok = self.confirmation_manager.create_confirmation_request(
                    action_name=action_name,
                    params=params,
                    description=f"Destructive operation: '{action_name}' with parameters {params}",
                )
                evaluation = SecurityEvaluation(
                    allowed=False,
                    tier=tier,
                    reason=f"Destructive action '{action_name}' blocked without explicit interactive confirmation.",
                    requires_confirmation=True,
                    requires_human_intervention=True,
                    intervention_type=HumanInterventionType.DESTRUCTIVE_CONFIRMATION,
                    confirmation_token=tok.token_id,
                    required_confirmation_details=tok.to_dict(),
                    audit_id=audit_id,
                )
            self._write_audit_log(action_name, params, ctx, evaluation)
            return evaluation

        # 6. Evaluation for Other Tiers
        if tier in (ActionTier.READ, ActionTier.LOW_RISK, ActionTier.LOW_RISK_ACTION):
            evaluation = SecurityEvaluation(allowed=True, tier=tier, reason="Low risk action allowed.", audit_id=audit_id)

        elif tier in (ActionTier.MODERATE, ActionTier.FILE_MODIFICATION, ActionTier.APPLICATION_CONTROL, ActionTier.EXTERNAL_COMMUNICATION):
            evaluation = SecurityEvaluation(allowed=True, tier=tier, reason=f"{tier.value} action allowed with logging.", audit_id=audit_id)

        elif tier == ActionTier.ACCOUNT_ACTION:
            if ctx.confirmed:
                evaluation = SecurityEvaluation(allowed=True, tier=tier, reason="Account action confirmed.", audit_id=audit_id)
            else:
                evaluation = SecurityEvaluation(
                    allowed=False,
                    tier=tier,
                    reason=f"Account action '{action_name}' requires confirmation.",
                    requires_confirmation=True,
                    requires_human_intervention=True,
                    intervention_type=HumanInterventionType.POLICY_INTERVENTION,
                    audit_id=audit_id,
                )

        elif tier in (ActionTier.ADMINISTRATIVE, ActionTier.SENSITIVE_DATA):
            if ctx.confirmed or ctx.elevation_requested:
                evaluation = SecurityEvaluation(allowed=True, tier=tier, reason="Administrative action confirmed.", audit_id=audit_id)
            else:
                evaluation = SecurityEvaluation(
                    allowed=False,
                    tier=tier,
                    reason=f"Administrative action '{action_name}' requires confirmation (confirmed=True).",
                    requires_confirmation=True,
                    requires_human_intervention=True,
                    intervention_type=HumanInterventionType.CREDENTIAL_ENTRY,
                    audit_id=audit_id,
                )

        elif tier == ActionTier.AUTHENTICATION:
            if ctx.confirmed:
                evaluation = SecurityEvaluation(allowed=True, tier=tier, reason="Authentication action confirmed.", audit_id=audit_id)
            else:
                evaluation = SecurityEvaluation(
                    allowed=False,
                    tier=tier,
                    reason=f"Authentication action '{action_name}' requires human intervention.",
                    requires_confirmation=True,
                    requires_human_intervention=True,
                    intervention_type=HumanInterventionType.LOGIN,
                    audit_id=audit_id,
                )

        else:
            evaluation = SecurityEvaluation(allowed=False, tier=tier, reason="Unknown action tier.", audit_id=audit_id)

        self._write_audit_log(action_name, params, ctx, evaluation)
        return evaluation

    def _write_audit_log(
        self,
        action_name: str,
        params: Dict[str, Any],
        ctx: SecurityContext,
        evaluation: SecurityEvaluation,
    ) -> None:
        """Appends immutable audit record to jsonl log file with recursive secret redaction."""
        # Sanitize sensitive params (passwords, tokens, OTPs, credit cards)
        clean_params = redact_sensitive_payload(params)

        record = {
            "audit_id": evaluation.audit_id,
            "timestamp": time.time(),
            "iso_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "action": action_name,
            "tier": evaluation.tier.value,
            "allowed": evaluation.allowed,
            "reason": evaluation.reason,
            "requires_confirmation": evaluation.requires_confirmation,
            "requires_human_intervention": evaluation.requires_human_intervention,
            "intervention_type": evaluation.intervention_type.value if evaluation.intervention_type else None,
            "quarantined": evaluation.quarantined,
            "caller": ctx.caller,
            "untrusted": ctx.is_untrusted_content,
            "confirmed": ctx.confirmed,
            "confirmation_token": evaluation.confirmation_token or ctx.confirmation_token,
            "params": clean_params,
        }

        try:
            with self._lock:
                with open(self.audit_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            LOG.error("Failed to append security audit log: %s", e)


# Singleton instance maintained strictly for backward compatibility with existing components
_GLOBAL_SECURITY_GATE: Optional[WindowsSecurityGate] = None
_SEC_LOCK = threading.Lock()


def get_security_gate() -> WindowsSecurityGate:
    """
    Returns global WindowsSecurityGate singleton for backward compatibility.
    New components should prefer explicit dependency injection of WindowsSecurityGate.
    """
    global _GLOBAL_SECURITY_GATE
    if _GLOBAL_SECURITY_GATE is None:
        with _SEC_LOCK:
            if _GLOBAL_SECURITY_GATE is None:
                _GLOBAL_SECURITY_GATE = WindowsSecurityGate()
    return _GLOBAL_SECURITY_GATE
