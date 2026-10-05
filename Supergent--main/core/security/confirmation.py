# ==============================================================================
# WISE Security Subsystem — Anti-TOCTOU Confirmation Architecture
# Enforces exact, single-use, cryptographically hashed authorization tokens
# bound to canonical parameters to prevent Time-of-Check to Time-of-Use tampering.
# Fully decoupled from model reasoning. Zero singletons (supports DI).
# ==============================================================================

from __future__ import annotations

import time
import uuid
import json
import hashlib
import logging
import threading
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

LOG = logging.getLogger("WISE.Security.Confirmation")


def _canonicalize_value(val: Any) -> Any:
    """Recursively converts structures to a deterministic, JSON-serializable form."""
    if isinstance(val, dict):
        return {str(k): _canonicalize_value(v) for k, v in sorted(val.items())}
    elif isinstance(val, (list, tuple, set)):
        return [_canonicalize_value(item) for item in val]
    elif isinstance(val, (int, float, bool, str)) or val is None:
        return val
    else:
        return str(val)


def compute_params_hash(action_name: str, params: Dict[str, Any]) -> str:
    """
    Computes a deterministic SHA-256 hash of normalized action parameters.
    Ensures that any variation in target, amount, path, URL, or recipient alters the hash.
    """
    normalized_params = _canonicalize_value(params)
    payload = {
        "action_name": str(action_name).strip().lower(),
        "params": normalized_params,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ActionConfirmationToken:
    """
    Represents an explicit authorization grant bound to exact action parameters.
    Enforces single-use consumption and expiration bounds.
    """
    token_id: str = field(default_factory=lambda: f"tok_{uuid.uuid4().hex[:12]}")
    action_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    params_hash: str = ""
    description: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    approved: bool = False
    rejected: bool = False
    used: bool = False
    approver: Optional[str] = None
    rejection_reason: Optional[str] = None
    consumed_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return self.approved and not self.rejected and not self.used and not self.is_expired

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token_id": self.token_id,
            "action_name": self.action_name,
            "params_hash": self.params_hash,
            "description": self.description,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "approved": self.approved,
            "rejected": self.rejected,
            "used": self.used,
            "approver": self.approver,
            "rejection_reason": self.rejection_reason,
            "consumed_at": self.consumed_at,
            "is_valid": self.is_valid,
        }


class ConfirmationManager:
    """
    Manages the lifecycle of out-of-model action confirmation requests.
    Guarantees Anti-TOCTOU parameter verification, single-use token consumption,
    and deterministic rejection on parameter tampering or expiration.
    Designed for dependency injection without forced global singletons.
    """

    def __init__(self, default_ttl_seconds: float = 60.0) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self._lock = threading.RLock()
        self._tokens: Dict[str, ActionConfirmationToken] = {}

    def create_confirmation_request(
        self,
        action_name: str,
        params: Dict[str, Any],
        description: str,
        ttl_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ActionConfirmationToken:
        """
        Generates a pending confirmation token bound to the exact canonical hash of params.
        """
        with self._lock:
            ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
            now = time.time()
            p_hash = compute_params_hash(action_name, params)

            token = ActionConfirmationToken(
                action_name=action_name,
                params=dict(params),
                params_hash=p_hash,
                description=description,
                created_at=now,
                expires_at=now + ttl,
                metadata=dict(metadata or {}),
            )
            self._tokens[token.token_id] = token
            LOG.info(
                "Created confirmation request %s for action '%s' (TTL: %.1fs). Hash: %s",
                token.token_id,
                action_name,
                ttl,
                p_hash[:12],
            )
            return token

    def get_token(self, token_id: str) -> Optional[ActionConfirmationToken]:
        with self._lock:
            return self._tokens.get(token_id)

    def approve_token(self, token_id: str, approver: str = "user") -> Tuple[bool, str]:
        """
        User or authorized human operator explicitly approves the pending token.
        """
        with self._lock:
            token = self._tokens.get(token_id)
            if not token:
                return False, "TOKEN_NOT_FOUND"
            if token.is_expired:
                return False, "TOKEN_EXPIRED"
            if token.used:
                return False, "TOKEN_ALREADY_USED"
            if token.rejected:
                return False, "TOKEN_PREVIOUSLY_REJECTED"

            token.approved = True
            token.approver = approver
            LOG.info("Confirmation token %s explicitly APPROVED by '%s'", token_id, approver)
            return True, "APPROVED"

    def reject_token(self, token_id: str, reason: str = "User rejected action") -> Tuple[bool, str]:
        """
        User explicitly rejects or cancels the pending action.
        """
        with self._lock:
            token = self._tokens.get(token_id)
            if not token:
                return False, "TOKEN_NOT_FOUND"

            token.rejected = True
            token.rejection_reason = reason
            LOG.info("Confirmation token %s REJECTED: %s", token_id, reason)
            return True, "REJECTED"

    def validate_and_consume_token(
        self,
        token_id: str,
        action_name: str,
        current_params: Dict[str, Any],
        *,
        consume: bool = True,
    ) -> Tuple[bool, str]:
        """
        Validates token validity and verifies that the current execution parameters
        match the exact parameters that were authorized (Anti-TOCTOU).
        Consumes the token on success (single-use).
        """
        with self._lock:
            token = self._tokens.get(token_id)
            if not token:
                LOG.warning("Validation failed: token '%s' not found.", token_id)
                return False, "TOKEN_NOT_FOUND"

            if token.used:
                LOG.error("TOCTOU / Replay violation: token '%s' was already consumed.", token_id)
                return False, "TOKEN_ALREADY_USED"

            if token.rejected:
                LOG.warning("Validation failed: token '%s' was rejected (%s).", token_id, token.rejection_reason)
                return False, f"TOKEN_REJECTED: {token.rejection_reason}"

            if not token.approved:
                LOG.warning("Validation failed: token '%s' has not been approved.", token_id)
                return False, "TOKEN_NOT_APPROVED"

            if token.is_expired:
                LOG.warning("Validation failed: token '%s' has expired.", token_id)
                return False, "TOKEN_EXPIRED"

            if token.action_name != action_name:
                LOG.error(
                    "Action mismatch: token was issued for '%s' but execution is for '%s'.",
                    token.action_name,
                    action_name,
                )
                return False, f"ACTION_MISMATCH: expected '{token.action_name}', got '{action_name}'"

            # Compute hash of current parameters
            current_hash = compute_params_hash(action_name, current_params)
            if current_hash != token.params_hash:
                LOG.error(
                    "TOCTOU Parameter Mismatch for token '%s'! Approved hash: %s, live execution hash: %s",
                    token_id,
                    token.params_hash[:12],
                    current_hash[:12],
                )
                return False, "TOCTOU_MISMATCH: Action parameters were modified after user approval."

            # Preview and execution share exact checks, but only execution
            # consumes under this same lock. Preview is not an authorization
            # receipt: the consuming boundary must recheck current parameters.
            if consume:
                token.used = True
                token.consumed_at = time.time()
                LOG.info("Confirmation token %s successfully VALIDATED and CONSUMED.", token_id)
                return True, "VALIDATED_AND_CONSUMED"
            return True, "VALIDATED_PREVIEW_ONLY"

    def purge_expired_tokens(self) -> int:
        """Removes expired tokens to prevent memory leaks."""
        with self._lock:
            now = time.time()
            expired_ids = [k for k, v in self._tokens.items() if now > v.expires_at + 300]  # keep 5m for audit
            for k in expired_ids:
                del self._tokens[k]
            return len(expired_ids)
