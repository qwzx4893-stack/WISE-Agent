"""Bounded idempotency registry for externally-triggered agent turns.

Network retries and double-clicks must not execute a desktop task twice. A
caller may send ``Idempotency-Key`` with a chat/control request; identical
retries receive the original completed response, while a concurrent duplicate
is explicitly reported as in progress. Entries are deliberately in-memory:
they cache response delivery, never become a second durable task store.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class IdempotencyDecision:
    state: str  # EXECUTE | REPLAY | IN_PROGRESS | CONFLICT
    response: Optional[Dict[str, Any]] = None


class IdempotencyStore:
    def __init__(self, *, ttl_seconds: Optional[int] = None, max_entries: int = 512) -> None:
        self.ttl_seconds = max(30, int(ttl_seconds or os.environ.get("WISE_IDEMPOTENCY_TTL_SECONDS", "900")))
        self.max_entries = max(16, int(max_entries))
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def fingerprint(payload: Dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def begin(self, scope: str, key: str, payload: Dict[str, Any]) -> IdempotencyDecision:
        if not key:
            return IdempotencyDecision("EXECUTE")
        token = f"{scope}:{key.strip()}"
        digest = self.fingerprint(payload)
        with self._lock:
            self._prune_locked()
            previous = self._entries.get(token)
            if previous is None:
                self._entries[token] = {"digest": digest, "state": "IN_PROGRESS", "updated_at": time.time(), "response": None}
                return IdempotencyDecision("EXECUTE")
            if previous["digest"] != digest:
                return IdempotencyDecision("CONFLICT")
            previous["updated_at"] = time.time()
            if previous["state"] == "COMPLETED":
                return IdempotencyDecision("REPLAY", copy.deepcopy(previous["response"]))
            return IdempotencyDecision("IN_PROGRESS")

    def complete(self, scope: str, key: str, response: Dict[str, Any]) -> None:
        if not key:
            return
        token = f"{scope}:{key.strip()}"
        with self._lock:
            entry = self._entries.get(token)
            if entry is not None:
                entry.update({"state": "COMPLETED", "response": copy.deepcopy(response), "updated_at": time.time()})

    def fail(self, scope: str, key: str) -> None:
        if not key:
            return
        with self._lock:
            self._entries.pop(f"{scope}:{key.strip()}", None)

    def _prune_locked(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for token in [name for name, entry in self._entries.items() if entry["updated_at"] < cutoff]:
            self._entries.pop(token, None)
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            oldest = sorted(self._entries, key=lambda name: self._entries[name]["updated_at"])
            for token in oldest[:overflow]:
                self._entries.pop(token, None)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            self._prune_locked()
            return {
                "entries": len(self._entries),
                "in_progress": sum(entry["state"] == "IN_PROGRESS" for entry in self._entries.values()),
                "ttl_seconds": self.ttl_seconds,
            }

    def inspect(self, scope: str, key: str) -> Dict[str, Any]:
        """Read request status without replaying or starting any execution."""
        with self._lock:
            self._prune_locked()
            entry = self._entries.get(f"{scope}:{key.strip()}")
            if entry is None:
                return {"state": "UNKNOWN", "response": None}
            return {"state": entry["state"], "response": copy.deepcopy(entry["response"])}


_STORE: Optional[IdempotencyStore] = None
_STORE_LOCK = threading.Lock()


def get_idempotency_store() -> IdempotencyStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = IdempotencyStore()
    return _STORE


__all__ = ["IdempotencyDecision", "IdempotencyStore", "get_idempotency_store"]
