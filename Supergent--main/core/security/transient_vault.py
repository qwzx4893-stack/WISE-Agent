# ==============================================================================
# WISE Security Subsystem — Transient Sensitive Data Store & Secret Redaction
# Provides ephemeral in-memory lifecycle for transient authentication credentials
# and deterministic recursive redaction filters for logs, state items, and checkpoints.
#
# Memory Guarantees (Objective Note):
# In standard CPython, immutable string objects cannot be guaranteed to undergo
# low-level C zero-fill upon deallocation due to object interning and garbage collection
# behaviors. Therefore, this module explicitly guarantees:
# 1. Ephemeral, strictly bounded lifetime via immediate reference eviction.
# 2. Complete exclusion from disk persistence, JSON serialization, and audit logging.
# 3. Deterministic scrubbing across recursive dicts, lists, and strings.
# Zero singletons: TransientSensitiveStore is instantiable and injectable.
# ==============================================================================

from __future__ import annotations

import re
import time
import logging
import threading
from typing import Dict, Any, Optional, List, Union

LOG = logging.getLogger("WISE.Security.TransientVault")

# Sensitive keyword patterns indicating sensitive dictionary keys
_SENSITIVE_KEY_SUBSTRINGS = (
    "password", "passwd", "passphrase", "secret", "token", "api_key", "apikey",
    "auth_token", "bearer", "cookie", "session_id", "cvv", "cvc", "pin",
    "otp", "mfa", "totp", "two_factor", "verification_code",
    "كلمة مرور", "كلمة السر", "رمز الأمان", "رمز التحقق",
)

# Regex patterns for scrubbing sensitive values in strings/text
_SENSITIVE_VALUE_REGEXES = [
    (re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}\b"), "[REDACTED:API_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED:API_KEY]"),
    # Credit Card Numbers (13-16 digits with dashes or spaces)
    (re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"), "[REDACTED:CARD_NUMBER]"),
    # Social Security Numbers (SSN: 3-2-4 digits)
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
    # Explicit OTP / Verification code patterns in text (e.g. "code: 123456", "OTP is 654321")
    (re.compile(r"(?i)\b(?:otp|code|verification code|pin|رمز التحقق)\s*[:=]?\s*(\b\d{4,8}\b)"), "[REDACTED:OTP]"),
    # Standard Bearer / API token prefixes
    (re.compile(r"(?i)\b(?:bearer|token|apikey|api_key)\s*[:=]?\s*([a-zA-Z0-9_\-\.]{16,})\b"), "[REDACTED:TOKEN]"),
    # Private credentials in URLs (e.g. https://user:pass@host)
    (re.compile(r"(https?://)([^:]+):([^@]+)@"), r"\1[REDACTED_USER]:[REDACTED_PASS]@"),
]


def is_sensitive_key(key: str) -> bool:
    """Checks if a dictionary key name suggests sensitive authentication or payment data."""
    lowered = str(key).lower()
    return any(sub in lowered for sub in _SENSITIVE_KEY_SUBSTRINGS)


def sanitize_text(text: str) -> str:
    """Scans and redacts known sensitive token patterns from a string."""
    if not isinstance(text, str) or not text:
        return text
    sanitized = text
    for pattern, replacement in _SENSITIVE_VALUE_REGEXES:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def redact_sensitive_payload(payload: Any, depth: int = 0) -> Any:
    """
    Recursively inspects and redacts sensitive credentials, passwords, tokens,
    and OTP codes from dictionaries, lists, tuples, and strings.
    Safe for JSON serialization and audit logging.
    """
    if depth > 20:
        return "[TRUNCATED_DEPTH]"

    if isinstance(payload, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in payload.items():
            if is_sensitive_key(str(k)):
                cleaned[k] = "[REDACTED:SENSITIVE_FIELD]"
            else:
                cleaned[k] = redact_sensitive_payload(v, depth + 1)
        return cleaned

    elif isinstance(payload, list):
        return [redact_sensitive_payload(item, depth + 1) for item in payload]

    elif isinstance(payload, tuple):
        return tuple(redact_sensitive_payload(item, depth + 1) for item in payload)

    elif isinstance(payload, str):
        return sanitize_text(payload)

    else:
        return payload


class TransientSensitiveStore:
    """
    Ephemeral in-memory key-value store for transient authentication secrets (e.g. OTPs).
    Enforces automatic TTL expiration, explicit eviction, and complete isolation from disk.
    """

    def __init__(self, default_ttl_seconds: float = 120.0) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self._lock = threading.RLock()
        self._store: Dict[str, Dict[str, Any]] = {}

    def store_transient_secret(
        self,
        key: str,
        secret_value: str,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Stores a secret with bounded lifetime in volatile memory."""
        with self._lock:
            ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
            self._store[key] = {
                "value": secret_value,
                "expires_at": time.time() + ttl,
            }
            LOG.info("Stored transient secret for key '%s' (TTL: %.1fs)", key, ttl)

    def retrieve_transient_secret(self, key: str, consume: bool = False) -> Optional[str]:
        """
        Retrieves the secret if unexpired. If consume=True, evicts it immediately.
        """
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None

            if time.time() > entry["expires_at"]:
                self._evict_internal(key)
                LOG.warning("Transient secret for key '%s' has expired and was evicted.", key)
                return None

            val = entry["value"]
            if consume:
                self._evict_internal(key)
                LOG.info("Transient secret for key '%s' retrieved and immediately consumed.", key)
            return val

    def evict(self, key: str) -> bool:
        """Explicitly deletes the secret from volatile memory."""
        with self._lock:
            return self._evict_internal(key)

    def _evict_internal(self, key: str) -> bool:
        if key in self._store:
            # Overwrite reference before deleting
            self._store[key]["value"] = ""
            del self._store[key]
            return True
        return False

    def clear(self) -> None:
        """Clears all stored secrets."""
        with self._lock:
            for k in list(self._store.keys()):
                self._evict_internal(k)
            self._store.clear()

    def purge_expired(self) -> int:
        """Purges expired secrets from memory."""
        with self._lock:
            now = time.time()
            expired = [k for k, v in self._store.items() if now > v["expires_at"]]
            for k in expired:
                self._evict_internal(k)
            return len(expired)
