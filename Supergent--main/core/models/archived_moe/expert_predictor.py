# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Expert Predictor
# Architecture: Deterministic baseline predictor anticipating upcoming MoE experts.
# Builds transition matrices and temporal locality statistics to drive prefetching.
# Extensible base interface ready for future neural/learned predictors.
# ==============================================================================

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

LOG = logging.getLogger("WISE.Models.Runtime.ExpertPredictor")


@dataclass
class PredictionResult:
    predicted_expert_ids: List[int]
    confidences: Dict[int, float] = field(default_factory=dict)
    priorities: Dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "predicted_ids": self.predicted_expert_ids,
            "confidences": {str(k): round(v, 3) for k, v in self.confidences.items()},
            "priorities": {str(k): v for k, v in self.priorities.items()},
        }


class BaseExpertPredictor(ABC):
    """Abstract interface for all MoE expert prediction models."""

    @abstractmethod
    def predict(
        self,
        recent_expert_ids: List[int],
        top_k: int = 2,
        min_confidence: float = 0.25,
        context_metadata: Optional[Dict[str, Any]] = None,
    ) -> PredictionResult:
        """Predicts high-probability candidate experts for subsequent tokens."""
        pass

    @abstractmethod
    def record_step(self, active_expert_ids: List[int]) -> None:
        """Updates internal transition and locality history after each token."""
        pass

    @abstractmethod
    def reset(self) -> None:
        """Clears accumulated history."""
        pass


class RecentFrequencyPredictor(BaseExpertPredictor):
    """
    Deterministic baseline predictor:
    1. Tracks 1st-order expert-to-expert Markov transitions: P(B | A)
    2. Incorporates temporal locality and recent co-occurrence frequency.
    3. Guarantees 0.0 confidence when no predictive signal exists.
    """

    def __init__(self, history_capacity: int = 256) -> None:
        self._lock = threading.RLock()
        self.history_capacity = history_capacity
        # Transition counts: source_id -> {target_id: count}
        self._transitions: Dict[int, Dict[int, int]] = {}
        # Co-occurrence counts: expert_id -> count
        self._frequencies: Dict[int, int] = {}
        # Chronological history of active expert sets
        self._recent_history: List[List[int]] = []
        self._total_steps = 0

    def record_step(self, active_expert_ids: List[int]) -> None:
        """Records active experts for the token step and updates transition statistics."""
        if not active_expert_ids:
            return

        with self._lock:
            self._total_steps += 1
            # 1. Update frequency
            for exp_id in active_expert_ids:
                self._frequencies[exp_id] = self._frequencies.get(exp_id, 0) + 1

            # 2. Update transition matrix from previous step
            if self._recent_history:
                prev_experts = self._recent_history[-1]
                for prev_id in prev_experts:
                    if prev_id not in self._transitions:
                        self._transitions[prev_id] = {}
                    for curr_id in active_expert_ids:
                        self._transitions[prev_id][curr_id] = self._transitions[prev_id].get(curr_id, 0) + 1

            self._recent_history.append(list(active_expert_ids))
            if len(self._recent_history) > self.history_capacity:
                self._recent_history.pop(0)

    def predict(
        self,
        recent_expert_ids: List[int],
        top_k: int = 2,
        min_confidence: float = 0.25,
        context_metadata: Optional[Dict[str, Any]] = None,
    ) -> PredictionResult:
        """
        Calculates predicted expert IDs using empirical transition probability
        weighted by historical frequency.
        """
        with self._lock:
            if not recent_expert_ids or not self._transitions:
                return PredictionResult(predicted_expert_ids=[])

            # Candidate scores: candidate_id -> cumulative score
            candidate_scores: Dict[int, float] = {}

            for src_id in recent_expert_ids:
                if src_id in self._transitions:
                    targets = self._transitions[src_id]
                    total_out = sum(targets.values())
                    if total_out > 0:
                        for dst_id, count in targets.items():
                            prob = count / total_out
                            # Exclude if it's already in the immediate active set
                            if dst_id not in recent_expert_ids:
                                candidate_scores[dst_id] = candidate_scores.get(dst_id, 0.0) + prob

            # Normalize scores to [0.0, 1.0] confidence
            if not candidate_scores:
                return PredictionResult(predicted_expert_ids=[])

            # Sort by score descending
            sorted_candidates = sorted(candidate_scores.items(), key=lambda x: x[1], reverse=True)

            selected_ids: List[int] = []
            confidences: Dict[int, float] = {}
            priorities: Dict[int, int] = {}

            priority = 1
            for exp_id, raw_score in sorted_candidates:
                conf = min(1.0, raw_score / len(recent_expert_ids))
                if conf >= min_confidence:
                    selected_ids.append(exp_id)
                    confidences[exp_id] = conf
                    priorities[exp_id] = priority
                    priority += 1
                if len(selected_ids) >= top_k:
                    break

            return PredictionResult(
                predicted_expert_ids=selected_ids,
                confidences=confidences,
                priorities=priorities,
            )

    def reset(self) -> None:
        with self._lock:
            self._transitions.clear()
            self._frequencies.clear()
            self._recent_history.clear()
            self._total_steps = 0
            LOG.info("RecentFrequencyPredictor reset.")

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_steps_recorded": self._total_steps,
                "unique_sources_count": len(self._transitions),
                "tracked_frequencies_count": len(self._frequencies),
            }
