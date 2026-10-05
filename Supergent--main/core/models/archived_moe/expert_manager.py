# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Expert Manager
# Architecture: Tracks expert lifecycle, residency, frequency, recency, and latencies.
# State Machine: COLD -> LOADING_TO_RAM -> WARM -> LOADING_TO_VRAM -> HOT -> EVICTING -> ERROR
# ==============================================================================

from __future__ import annotations

import time
import logging
import threading
from enum import Enum
from typing import Dict, Any, List, Optional, Set
from dataclasses import dataclass, field, asdict

from .memory_tiers import MemoryTier

LOG = logging.getLogger("WISE.Models.Runtime.ExpertManager")


class ExpertState(str, Enum):
    COLD = "COLD"                     # On NVMe only
    LOADING_TO_RAM = "LOADING_TO_RAM" # In transit NVMe -> RAM
    WARM = "WARM"                     # Resident in host RAM
    LOADING_TO_VRAM = "LOADING_TO_VRAM" # In transit RAM -> VRAM
    HOT = "HOT"                       # Resident in GPU VRAM (active compute)
    EVICTING = "EVICTING"             # Being demoted VRAM -> RAM or RAM -> COLD
    ERROR = "ERROR"                   # Transfer or allocation failed


# Valid state transitions
VALID_STATE_TRANSITIONS: Dict[ExpertState, Set[ExpertState]] = {
    ExpertState.COLD: {ExpertState.COLD, ExpertState.LOADING_TO_RAM, ExpertState.WARM, ExpertState.LOADING_TO_VRAM, ExpertState.HOT, ExpertState.ERROR},
    ExpertState.LOADING_TO_RAM: {ExpertState.LOADING_TO_RAM, ExpertState.WARM, ExpertState.COLD, ExpertState.ERROR},
    ExpertState.WARM: {ExpertState.WARM, ExpertState.LOADING_TO_RAM, ExpertState.LOADING_TO_VRAM, ExpertState.HOT, ExpertState.EVICTING, ExpertState.COLD, ExpertState.ERROR},
    ExpertState.LOADING_TO_VRAM: {ExpertState.LOADING_TO_VRAM, ExpertState.HOT, ExpertState.WARM, ExpertState.COLD, ExpertState.ERROR},
    ExpertState.HOT: {ExpertState.HOT, ExpertState.EVICTING, ExpertState.WARM, ExpertState.COLD, ExpertState.ERROR},
    ExpertState.EVICTING: {ExpertState.EVICTING, ExpertState.WARM, ExpertState.COLD, ExpertState.ERROR},
    ExpertState.ERROR: {ExpertState.ERROR, ExpertState.COLD, ExpertState.WARM, ExpertState.HOT},
}


@dataclass
class ExpertDescriptor:
    expert_id: int
    layer_id: int
    size_bytes: int
    current_tier: MemoryTier = MemoryTier.COLD_NVME
    state: ExpertState = ExpertState.COLD
    access_count: int = 0
    last_access_time: float = 0.0
    first_loaded_time: float = 0.0
    load_latency_ms: float = 0.0
    transfer_latency_ms: float = 0.0
    is_pinned: bool = False
    is_predicted: bool = False
    prediction_confidence: float = 0.0
    error_message: Optional[str] = None

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def idle_time_seconds(self) -> float:
        if self.last_access_time <= 0:
            return 0.0
        return max(0.0, time.time() - self.last_access_time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "layer_id": self.layer_id,
            "size_mb": self.size_mb,
            "current_tier": self.current_tier.value,
            "state": self.state.value,
            "access_count": self.access_count,
            "last_access_time": round(self.last_access_time, 3),
            "idle_time_seconds": round(self.idle_time_seconds, 2),
            "load_latency_ms": round(self.load_latency_ms, 2),
            "transfer_latency_ms": round(self.transfer_latency_ms, 2),
            "is_pinned": self.is_pinned,
            "is_predicted": self.is_predicted,
            "prediction_confidence": round(self.prediction_confidence, 3),
            "error_message": self.error_message,
        }


class ExpertManager:
    """
    Governor for all MoE expert states, residency tracking, and access metrics.
    Enforces atomic state transitions according to the lifecycle state machine.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._experts: Dict[int, ExpertDescriptor] = {}
        self._total_accesses = 0

    def register_expert(self, expert_id: int, layer_id: int, size_bytes: int) -> ExpertDescriptor:
        """Registers a newly discovered expert into the Cold pool."""
        with self._lock:
            desc = ExpertDescriptor(
                expert_id=expert_id,
                layer_id=layer_id,
                size_bytes=size_bytes,
                current_tier=MemoryTier.COLD_NVME,
                state=ExpertState.COLD,
            )
            self._experts[expert_id] = desc
            return desc

    def get_descriptor(self, expert_id: int) -> Optional[ExpertDescriptor]:
        with self._lock:
            return self._experts.get(expert_id)

    def transition_state(
        self,
        expert_id: int,
        new_state: ExpertState,
        new_tier: Optional[MemoryTier] = None,
        error_msg: Optional[str] = None,
    ) -> bool:
        """
        Executes a validated state transition for an expert.
        Returns True on success, False if the transition is invalid.
        """
        with self._lock:
            desc = self._experts.get(expert_id)
            if not desc:
                LOG.error("Cannot transition unregistered expert ID %d", expert_id)
                return False

            old_state = desc.state
            allowed = VALID_STATE_TRANSITIONS.get(old_state, set())

            if new_state not in allowed:
                LOG.warning(
                    "Invalid state transition for Expert %d: %s -> %s (Allowed: %s)",
                    expert_id,
                    old_state.value,
                    new_state.value,
                    [s.value for s in allowed],
                )
                return False

            desc.state = new_state
            if new_tier is not None:
                desc.current_tier = new_tier

            if new_state == ExpertState.ERROR:
                desc.error_message = error_msg
            elif old_state == ExpertState.ERROR and new_state != ExpertState.ERROR:
                desc.error_message = None

            if new_state in (ExpertState.WARM, ExpertState.HOT) and desc.first_loaded_time <= 0:
                desc.first_loaded_time = time.time()

            LOG.debug(
                "Expert %d transitioned %s -> %s (Tier: %s)",
                expert_id,
                old_state.value,
                new_state.value,
                desc.current_tier.value,
            )
            return True

    def mark_accessed(self, expert_id: int) -> None:
        """Records token-level access recency and frequency."""
        with self._lock:
            desc = self._experts.get(expert_id)
            if desc:
                desc.access_count += 1
                desc.last_access_time = time.time()
                self._total_accesses += 1

    def record_latencies(
        self,
        expert_id: int,
        load_latency_ms: Optional[float] = None,
        transfer_latency_ms: Optional[float] = None,
    ) -> None:
        with self._lock:
            desc = self._experts.get(expert_id)
            if desc:
                if load_latency_ms is not None:
                    desc.load_latency_ms = load_latency_ms
                if transfer_latency_ms is not None:
                    desc.transfer_latency_ms = transfer_latency_ms

    def set_predicted(self, expert_id: int, is_predicted: bool, confidence: float = 0.0) -> None:
        with self._lock:
            desc = self._experts.get(expert_id)
            if desc:
                desc.is_predicted = is_predicted
                desc.prediction_confidence = confidence

    def get_hot_experts(self) -> List[ExpertDescriptor]:
        with self._lock:
            return [d for d in self._experts.values() if d.state == ExpertState.HOT]

    def get_warm_experts(self) -> List[ExpertDescriptor]:
        with self._lock:
            return [d for d in self._experts.values() if d.state == ExpertState.WARM]

    def get_cold_experts(self) -> List[ExpertDescriptor]:
        with self._lock:
            return [d for d in self._experts.values() if d.state == ExpertState.COLD]

    def get_all_descriptors(self) -> List[ExpertDescriptor]:
        with self._lock:
            return list(self._experts.values())

    def reset_all_to_cold(self) -> None:
        """Resets all experts to COLD on runtime unload/idle."""
        with self._lock:
            for desc in self._experts.values():
                desc.state = ExpertState.COLD
                desc.current_tier = MemoryTier.COLD_NVME
                desc.is_predicted = False
                desc.prediction_confidence = 0.0
                desc.error_message = None
            LOG.info("All %d experts reset to COLD tier.", len(self._experts))

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            hot_count = sum(1 for d in self._experts.values() if d.state == ExpertState.HOT)
            warm_count = sum(1 for d in self._experts.values() if d.state == ExpertState.WARM)
            cold_count = sum(1 for d in self._experts.values() if d.state == ExpertState.COLD)
            error_count = sum(1 for d in self._experts.values() if d.state == ExpertState.ERROR)
            return {
                "total_registered": len(self._experts),
                "hot_vram_count": hot_count,
                "warm_ram_count": warm_count,
                "cold_nvme_count": cold_count,
                "error_count": error_count,
                "total_accesses": self._total_accesses,
            }
