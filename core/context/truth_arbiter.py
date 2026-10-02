"""
Epistemic Truth Arbiter (EpistemicTruthArbiter).
Determines factual reliability based on the target domain rather than a naive static hierarchy.
Every fact carries provenance, confidence, timestamp, freshness decay, and source domain.
Resolves conflicting claims through domain-authority weighting and explicit verification flags.
"""

from __future__ import annotations

import math
import time
import logging
import threading
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

LOG = logging.getLogger("wise.truth_arbiter")


class EpistemicDomain(str, Enum):
    USER_INTENT = "USER_INTENT"
    SYSTEM_STATE = "SYSTEM_STATE"
    TOOL_OUTCOME = "TOOL_OUTCOME"
    LOCAL_DATA = "LOCAL_DATA"
    EXTERNAL_KNOWLEDGE = "EXTERNAL_KNOWLEDGE"
    HISTORICAL_CONTEXT = "HISTORICAL_CONTEXT"
    HYPOTHESIS = "HYPOTHESIS"


class SourceType(str, Enum):
    DIRECT_USER_INPUT = "DIRECT_USER_INPUT"
    OS_HARDWARE_TELEMETRY = "OS_HARDWARE_TELEMETRY"
    VERIFIED_TOOL_OUTPUT = "VERIFIED_TOOL_OUTPUT"
    LOCAL_FILESYSTEM = "LOCAL_FILESYSTEM"
    INTERNET_SEARCH = "INTERNET_SEARCH"
    LONG_TERM_MEMORY = "LONG_TERM_MEMORY"
    MODEL_ASSUMPTION = "MODEL_ASSUMPTION"


# Domain Authority Weights: maps (Domain, SourceType) -> base authority score [0.0 - 1.0]
DOMAIN_AUTHORITY_MATRIX: Dict[EpistemicDomain, Dict[SourceType, float]] = {
    EpistemicDomain.USER_INTENT: {
        SourceType.DIRECT_USER_INPUT: 1.0,      # User is absolute ground truth for their own desires
        SourceType.LONG_TERM_MEMORY: 0.6,       # Historical preference
        SourceType.MODEL_ASSUMPTION: 0.2,
    },
    EpistemicDomain.SYSTEM_STATE: {
        SourceType.OS_HARDWARE_TELEMETRY: 1.0,  # Physical Win32/Hardware is absolute ground truth
        SourceType.VERIFIED_TOOL_OUTPUT: 0.9,
        SourceType.LOCAL_FILESYSTEM: 0.8,
        SourceType.DIRECT_USER_INPUT: 0.5,      # User might be mistaken about system internals
        SourceType.INTERNET_SEARCH: 0.3,
        SourceType.LONG_TERM_MEMORY: 0.4,
        SourceType.MODEL_ASSUMPTION: 0.1,
    },
    EpistemicDomain.TOOL_OUTCOME: {
        SourceType.VERIFIED_TOOL_OUTPUT: 1.0,   # Actual tool observation post-action is primary
        SourceType.OS_HARDWARE_TELEMETRY: 0.95,
        SourceType.MODEL_ASSUMPTION: 0.1,
    },
    EpistemicDomain.LOCAL_DATA: {
        SourceType.LOCAL_FILESYSTEM: 1.0,       # Exact content on disk
        SourceType.VERIFIED_TOOL_OUTPUT: 0.9,
        SourceType.LONG_TERM_MEMORY: 0.5,
        SourceType.MODEL_ASSUMPTION: 0.1,
    },
    EpistemicDomain.EXTERNAL_KNOWLEDGE: {
        SourceType.INTERNET_SEARCH: 0.9,
        SourceType.LONG_TERM_MEMORY: 0.7,
        SourceType.MODEL_ASSUMPTION: 0.3,
    },
    EpistemicDomain.HISTORICAL_CONTEXT: {
        SourceType.LONG_TERM_MEMORY: 0.9,
        SourceType.DIRECT_USER_INPUT: 0.8,
        SourceType.MODEL_ASSUMPTION: 0.2,
    },
    EpistemicDomain.HYPOTHESIS: {
        SourceType.MODEL_ASSUMPTION: 0.5,
    },
}


@dataclass
class Fact:
    """An epistemic fact with full provenance and credibility tracking."""
    key: str
    value: Any
    domain: EpistemicDomain
    source_type: SourceType
    provenance: str
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)
    halflife_seconds: float = 300.0  # 5 minutes default freshness halflife

    @property
    def freshness(self) -> float:
        """Calculates exponential decay of freshness: 2^(-age / halflife)."""
        age = max(0.0, time.time() - self.timestamp)
        if self.halflife_seconds <= 0:
            return 1.0
        return math.pow(2.0, -age / self.halflife_seconds)

    @property
    def composite_score(self) -> float:
        """Computes domain authority * confidence * freshness."""
        matrix = DOMAIN_AUTHORITY_MATRIX.get(self.domain, {})
        base_authority = matrix.get(self.source_type, 0.2)
        return round(base_authority * self.confidence * self.freshness, 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "domain": self.domain.value,
            "source_type": self.source_type.value,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "freshness": round(self.freshness, 3),
            "composite_score": self.composite_score,
            "timestamp": self.timestamp,
        }


class EpistemicTruthArbiter:
    """Manages knowledge arbitration and resolves conflicting facts across sources."""

    def __init__(self):
        self._facts: Dict[str, Fact] = {}
        self._lock = threading.RLock()

    def record_fact(
        self,
        key: str,
        value: Any,
        domain: EpistemicDomain,
        source_type: SourceType,
        provenance: str,
        confidence: float = 1.0,
        halflife_seconds: float = 300.0,
    ) -> Fact:
        """Records or updates a fact, arbitrating automatically if an existing fact conflicts."""
        new_fact = Fact(
            key=key,
            value=value,
            domain=domain,
            source_type=source_type,
            provenance=provenance,
            confidence=max(0.0, min(1.0, confidence)),
            timestamp=time.time(),
            halflife_seconds=halflife_seconds,
        )

        with self._lock:
            existing = self._facts.get(key)
            if existing is None:
                self._facts[key] = new_fact
                return new_fact

            # Arbitrate conflict
            winner, reason = self.resolve_conflict(existing, new_fact)
            self._facts[key] = winner
            LOG.info("Truth arbitration for '%s': %s", key, reason)
            return winner

    def get_fact(self, key: str) -> Optional[Fact]:
        with self._lock:
            return self._facts.get(key)

    def resolve_conflict(self, fact_a: Fact, fact_b: Fact) -> Tuple[Fact, str]:
        """Arbitrates between two conflicting claims for the same key.
        Returns the winning Fact and an explanatory rationale.
        """
        score_a = fact_a.composite_score
        score_b = fact_b.composite_score

        diff = abs(score_a - score_b)

        if diff < 0.1 and fact_a.value != fact_b.value:
            # Ambiguity: scores are too close and values differ -> verification required
            # Prefer the newer fact temporarily, but lower confidence to trigger verification
            winner = fact_b if fact_b.timestamp >= fact_a.timestamp else fact_a
            winner.confidence = max(0.4, winner.confidence * 0.7)
            rationale = (
                f"Close dispute between {fact_a.source_type.value} (score={score_a}) "
                f"and {fact_b.source_type.value} (score={score_b}). Flagged for verification."
            )
            return winner, rationale

        if score_b > score_a:
            winner = fact_b
            rationale = (
                f"{fact_b.source_type.value} overrode {fact_a.source_type.value} "
                f"(score {score_b} > {score_a}) in domain {fact_b.domain.value}."
            )
        else:
            winner = fact_a
            rationale = (
                f"{fact_a.source_type.value} retained over {fact_b.source_type.value} "
                f"(score {score_a} >= {score_b}) in domain {fact_a.domain.value}."
            )

        return winner, rationale

    def get_all_verified_facts(self, min_composite_score: float = 0.4) -> List[Fact]:
        """Returns all current facts passing minimum credibility threshold."""
        with self._lock:
            return [
                f for f in self._facts.values()
                if f.composite_score >= min_composite_score
            ]


# Singleton instance
_GLOBAL_TRUTH_ARBITER: Optional[EpistemicTruthArbiter] = None
_TA_LOCK = threading.Lock()


def get_truth_arbiter() -> EpistemicTruthArbiter:
    global _GLOBAL_TRUTH_ARBITER
    if _GLOBAL_TRUTH_ARBITER is None:
        with _TA_LOCK:
            if _GLOBAL_TRUTH_ARBITER is None:
                _GLOBAL_TRUTH_ARBITER = EpistemicTruthArbiter()
    return _GLOBAL_TRUTH_ARBITER
