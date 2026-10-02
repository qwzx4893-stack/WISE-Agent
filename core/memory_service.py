"""
Canonical Unified Memory Service for WISE.

Provides persistent episodic, working, user preference, project, and task memory retrieval
for the canonical ConversationalCore and TaskEngine architectures.
Implements bounded, relevance-based retrieval using token overlap and composite scoring:
    Score = 0.5 * Relevance + 0.3 * Recency + 0.2 * Confidence
Persisted to MEMORY_DIR/episodic_memory.json with sensitive token redaction and contradiction reconciliation.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.contracts import MemoryItem, MemoryQuery, MemoryResult, MemoryTier
from core.paths import MEMORY_DIR

LOG = logging.getLogger("WISE.Memory.Service")

# Sensitive credential and private data redaction patterns
_SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)\b(?:bearer\s+)[a-zA-Z0-9_\-\.]{16,}\b"), "[REDACTED_BEARER_TOKEN]"),
    (re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)(?:password|passwd|secret)\s*[:=]\s*\S+"), "[REDACTED_SECRET]"),
    (re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"), "[REDACTED_CARD_NUMBER]"),
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
]


def redact_sensitive_memory(text: str) -> str:
    """Sanitizes sensitive credentials, tokens, and secrets from memory content before persistence."""
    if not isinstance(text, str):
        return text
    sanitized = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


class MemoryService:
    """
    Canonical Memory Service implementing persistent tiered memory:
    WORKING_SESSION, USER_PREFERENCE, PROJECT, TASK, and EPISODIC.
    Thread-safe persistence, contradiction reconciliation, and bounded composite retrieval.
    """

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self.storage_path = storage_path or (MEMORY_DIR / "episodic_memory.json")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._items: Dict[str, MemoryItem] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                for raw in data.get("items", []):
                    # Tier resolution
                    raw_tier = raw.get("tier", "EPISODIC")
                    try:
                        tier = MemoryTier(raw_tier)
                    except Exception:
                        tier = MemoryTier.EPISODIC

                    item = MemoryItem(
                        key=raw["key"],
                        content=redact_sensitive_memory(raw["content"]),
                        metadata=raw.get("metadata", {}),
                        timestamp=raw.get("timestamp", time.time()),
                        relevance_score=raw.get("relevance_score", 1.0),
                        tier=tier,
                        superseded_by=raw.get("superseded_by"),
                        access_count=raw.get("access_count", 0),
                    )
                    self._items[item.key] = item
        except Exception as e:
            LOG.warning("Failed to load memory from %s (%s); starting fresh", self.storage_path, e)

    def _save(self) -> None:
        try:
            with self._lock:
                payload = {
                    "version": 2,
                    "updated_at": time.time(),
                    "items": [
                        {
                            "key": it.key,
                            "content": it.content,
                            "metadata": it.metadata,
                            "timestamp": it.timestamp,
                            "relevance_score": it.relevance_score,
                            "tier": it.tier.value if hasattr(it.tier, "value") else str(it.tier),
                            "superseded_by": it.superseded_by,
                            "access_count": it.access_count,
                        }
                        for it in self._items.values()
                    ],
                }
            tmp_path = self.storage_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.storage_path)
        except Exception as e:
            LOG.error("Failed to persist memory to %s: %s", self.storage_path, e)

    def store(
        self,
        key: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        relevance_score: float = 1.0,
        tier: MemoryTier = MemoryTier.EPISODIC,
        entity_key: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> MemoryItem:
        """
        Stores a memory item with sensitive token redaction, tier classification,
        and contradiction reconciliation.
        """
        sanitized_content = redact_sensitive_memory(content)
        meta = dict(metadata or {})
        if session_id:
            meta["session_id"] = session_id
        eff_entity = entity_key or meta.get("entity_key")
        if eff_entity:
            meta["entity_key"] = eff_entity

        with self._lock:
            # Contradiction Reconciliation: if entity_key is provided, mark older active entries as superseded
            if eff_entity:
                for old_key, old_item in self._items.items():
                    if old_key != key and old_item.metadata.get("entity_key") == eff_entity and old_item.superseded_by is None:
                        old_item.superseded_by = key
                        LOG.info("Contradiction reconciled: older memory '%s' superseded by '%s' for entity '%s'", old_key, key, eff_entity)

            item = MemoryItem(
                key=key,
                content=sanitized_content,
                metadata=meta,
                timestamp=time.time(),
                relevance_score=relevance_score,
                tier=tier,
                superseded_by=None,
                access_count=0,
            )
            self._items[key] = item
            self._save()
            return item

    def get(self, key: str) -> Optional[MemoryItem]:
        with self._lock:
            item = self._items.get(key)
            if item:
                item.access_count += 1
            return item

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._items:
                del self._items[key]
                self._save()
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._save()

    def list_all(self, tier: Optional[MemoryTier] = None, include_superseded: bool = False) -> List[MemoryItem]:
        with self._lock:
            res = []
            for it in self._items.values():
                if tier is not None and it.tier != tier:
                    continue
                if not include_superseded and it.superseded_by is not None:
                    continue
                res.append(it)
            return res

    def query(
        self,
        query_text: str,
        limit: int = 3,
        min_score: float = 0.05,
        tier: Optional[MemoryTier] = None,
        include_superseded: bool = False,
        session_id: Optional[str] = None,
    ) -> MemoryResult:
        """
        Retrieves relevant memory items bounded by composite score and limit.
        Scoring formula:
            Score = 0.5 * Relevance + 0.3 * Recency + 0.2 * Confidence
        """
        t0 = time.perf_counter()
        tokens = set(re.findall(r"\w+", query_text.lower()))
        if not tokens:
            return MemoryResult(items=[], query_text=query_text, duration_ms=0.0)

        scored: List[tuple[float, MemoryItem]] = []
        now = time.time()

        with self._lock:
            items_snapshot = list(self._items.values())

        for item in items_snapshot:
            if tier is not None and item.tier != tier:
                continue
            if not include_superseded and item.superseded_by is not None:
                continue
            # Scoped working session memory: Working session items are restricted to matching session_id
            if session_id and item.tier == MemoryTier.WORKING_SESSION:
                item_sess = item.metadata.get("session_id")
                if item_sess and item_sess != session_id:
                    continue

            item_text = f"{item.key} {item.content} {' '.join(str(v) for v in item.metadata.values())}".lower()
            item_tokens = set(re.findall(r"\w+", item_text))
            if not item_tokens:
                continue

            intersection = tokens.intersection(item_tokens)
            if not intersection:
                continue

            # 1. Relevance Score (Token overlap / Jaccard-like)
            rel_score = len(intersection) / math.sqrt(len(tokens) * len(item_tokens))

            # 2. Recency Score (Exponential half-life decay over 7 days)
            age_days = max(0.0, (now - item.timestamp) / 86400.0)
            recency_score = math.exp(-age_days / 7.0)

            # 3. Confidence Score
            confidence_score = float(item.metadata.get("confidence", 1.0)) if isinstance(item.metadata, dict) else 1.0

            # Composite Formula: 0.5 * Relevance + 0.3 * Recency + 0.2 * Confidence
            composite_score = (0.5 * rel_score) + (0.3 * recency_score) + (0.2 * confidence_score)

            if composite_score >= min_score:
                item.relevance_score = round(composite_score, 3)
                scored.append((composite_score, item))

        # Sort descending by composite score, then timestamp
        scored.sort(key=lambda pair: (pair[0], pair[1].timestamp), reverse=True)
        top_items = []
        for _, it in scored[:limit]:
            it.access_count += 1
            top_items.append(it)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return MemoryResult(items=top_items, query_text=query_text, duration_ms=elapsed_ms)


_GLOBAL_MEMORY_SERVICE: Optional[MemoryService] = None
_MEM_LOCK = threading.Lock()


def get_memory_service() -> MemoryService:
    global _GLOBAL_MEMORY_SERVICE
    if _GLOBAL_MEMORY_SERVICE is None:
        with _MEM_LOCK:
            if _GLOBAL_MEMORY_SERVICE is None:
                _GLOBAL_MEMORY_SERVICE = MemoryService()
    return _GLOBAL_MEMORY_SERVICE
