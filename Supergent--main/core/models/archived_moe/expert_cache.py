# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Expert Caches & Eviction Policies
# Architecture: Bounded VRAM Expert Cache (Hot) and Bounded RAM Expert Cache (Warm).
# Pluggable policies: LRU, LFU, FrequencyAware, PredictionAware, and Hybrid.
# Guarantees safety headroom for CUDA, KV cache, shared weights, and host OS.
# ==============================================================================

from __future__ import annotations

import time
import logging
import threading
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from .memory_tiers import MemoryTier, MemoryTierManager
from .expert_manager import ExpertManager, ExpertDescriptor, ExpertState

LOG = logging.getLogger("WISE.Models.Runtime.ExpertCache")


# ==============================================================================
# EVICTION POLICIES
# ==============================================================================

class BaseEvictionPolicy(ABC):
    """Abstract interface for expert cache eviction policies."""

    @abstractmethod
    def select_eviction_candidates(
        self,
        candidates: List[ExpertDescriptor],
        needed_bytes: int,
        protected_ids: Set[int],
    ) -> List[int]:
        """
        Selects expert IDs to evict until at least `needed_bytes` are freed.
        Experts in `protected_ids` (e.g. actively executing in the current token)
        must NEVER be selected.
        """
        pass


class LRUPolicy(BaseEvictionPolicy):
    """Least Recently Used: evicts candidates with oldest last_access_time."""

    def select_eviction_candidates(
        self,
        candidates: List[ExpertDescriptor],
        needed_bytes: int,
        protected_ids: Set[int],
    ) -> List[int]:
        evictable = [c for c in candidates if c.expert_id not in protected_ids and not c.is_pinned]
        evictable.sort(key=lambda d: d.last_access_time)

        selected: List[int] = []
        freed = 0
        for desc in evictable:
            selected.append(desc.expert_id)
            freed += desc.size_bytes
            if freed >= needed_bytes:
                break
        return selected


class LFUPolicy(BaseEvictionPolicy):
    """Least Frequently Used: evicts candidates with lowest access_count."""

    def select_eviction_candidates(
        self,
        candidates: List[ExpertDescriptor],
        needed_bytes: int,
        protected_ids: Set[int],
    ) -> List[int]:
        evictable = [c for c in candidates if c.expert_id not in protected_ids and not c.is_pinned]
        evictable.sort(key=lambda d: (d.access_count, d.last_access_time))

        selected: List[int] = []
        freed = 0
        for desc in evictable:
            selected.append(desc.expert_id)
            freed += desc.size_bytes
            if freed >= needed_bytes:
                break
        return selected


class FrequencyAwarePolicy(BaseEvictionPolicy):
    """
    Decay-weighted frequency: Score = access_count / (1.0 + idle_seconds * decay).
    Balances frequency against temporal recency.
    """

    def __init__(self, decay_rate: float = 0.5) -> None:
        self.decay_rate = decay_rate

    def select_eviction_candidates(
        self,
        candidates: List[ExpertDescriptor],
        needed_bytes: int,
        protected_ids: Set[int],
    ) -> List[int]:
        now = time.time()
        evictable = [c for c in candidates if c.expert_id not in protected_ids and not c.is_pinned]

        def compute_score(d: ExpertDescriptor) -> float:
            idle = max(0.0, now - d.last_access_time)
            return d.access_count / (1.0 + idle * self.decay_rate)

        evictable.sort(key=compute_score)

        selected: List[int] = []
        freed = 0
        for desc in evictable:
            selected.append(desc.expert_id)
            freed += desc.size_bytes
            if freed >= needed_bytes:
                break
        return selected


class PredictionAwarePolicy(BaseEvictionPolicy):
    """
    Shields predicted upcoming experts from eviction by deprioritizing them.
    Non-predicted experts are evicted first (LRU order).
    """

    def select_eviction_candidates(
        self,
        candidates: List[ExpertDescriptor],
        needed_bytes: int,
        protected_ids: Set[int],
    ) -> List[int]:
        evictable = [c for c in candidates if c.expert_id not in protected_ids and not c.is_pinned]
        # Sort key: predicted items sort later (1 if predicted, else 0), then by LRU
        evictable.sort(key=lambda d: (1 if d.is_predicted else 0, d.prediction_confidence, d.last_access_time))

        selected: List[int] = []
        freed = 0
        for desc in evictable:
            selected.append(desc.expert_id)
            freed += desc.size_bytes
            if freed >= needed_bytes:
                break
        return selected


class HybridPolicy(BaseEvictionPolicy):
    """
    Blended policy: Combines recency, frequency, and prediction protection.
    Computes an eviction priority score (lower score = evict first).
    """

    def __init__(self, recency_weight: float = 0.4, frequency_weight: float = 0.3, prediction_weight: float = 0.3) -> None:
        self.w_rec = recency_weight
        self.w_freq = frequency_weight
        self.w_pred = prediction_weight

    def select_eviction_candidates(
        self,
        candidates: List[ExpertDescriptor],
        needed_bytes: int,
        protected_ids: Set[int],
    ) -> List[int]:
        now = time.time()
        evictable = [c for c in candidates if c.expert_id not in protected_ids and not c.is_pinned]

        def hybrid_score(d: ExpertDescriptor) -> float:
            idle = max(0.0, now - d.last_access_time)
            rec_val = 1.0 / (1.0 + idle)
            freq_val = min(1.0, d.access_count / 100.0)
            pred_val = d.prediction_confidence if d.is_predicted else 0.0
            return (self.w_rec * rec_val) + (self.w_freq * freq_val) + (self.w_pred * pred_val)

        evictable.sort(key=hybrid_score)

        selected: List[int] = []
        freed = 0
        for desc in evictable:
            selected.append(desc.expert_id)
            freed += desc.size_bytes
            if freed >= needed_bytes:
                break
        return selected


# ==============================================================================
# VRAM EXPERT CACHE (HOT TIER)
# ==============================================================================

class VRAMExpertCache:
    """
    GPU VRAM expert cache with strict budget and headroom enforcement.
    Stores hot expert payloads ready for compute.
    Evicts to RAM cache when budget is reached.
    """

    def __init__(
        self,
        tier_manager: MemoryTierManager,
        expert_manager: ExpertManager,
        policy: Optional[BaseEvictionPolicy] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.tier_manager = tier_manager
        self.expert_manager = expert_manager
        self.policy: BaseEvictionPolicy = policy or LRUPolicy()

        self._cache: Dict[int, Any] = {}  # expert_id -> expert_weight_tensor/buffer
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def set_policy(self, policy: BaseEvictionPolicy) -> None:
        with self._lock:
            self.policy = policy

    def contains(self, expert_id: int) -> bool:
        with self._lock:
            return expert_id in self._cache

    def get(self, expert_id: int, mark_access: bool = True) -> Optional[Any]:
        """Looks up an expert in VRAM cache. Records hit/miss metrics."""
        with self._lock:
            if expert_id in self._cache:
                self._hits += 1
                if mark_access:
                    self.expert_manager.mark_accessed(expert_id)
                return self._cache[expert_id]
            self._misses += 1
            return None

    def put(
        self,
        expert_id: int,
        expert_data: Any,
        protected_ids: Optional[Set[int]] = None,
    ) -> List[Tuple[int, Any]]:
        """
        Inserts an expert into VRAM.
        If capacity is insufficient, evicts cold candidates using the active policy.
        Returns a list of evicted (expert_id, expert_data) pairs for demotion to RAM.
        """
        with self._lock:
            desc = self.expert_manager.get_descriptor(expert_id)
            if not desc:
                raise KeyError(f"Expert ID {expert_id} is not registered in ExpertManager")

            size_bytes = desc.size_bytes
            tag = f"vram_exp_{expert_id}"
            protected = (protected_ids or set()) | {expert_id}

            evicted_items: List[Tuple[int, Any]] = []

            # Check if already present
            if expert_id in self._cache:
                self.expert_manager.mark_accessed(expert_id)
                return evicted_items

            # Free space if needed
            vram_cap = self.tier_manager.get_tier(MemoryTier.HOT_VRAM)
            while vram_cap.available_bytes < size_bytes:
                # Find eviction candidates among current cache residents
                current_in_vram = [
                    self.expert_manager.get_descriptor(eid)
                    for eid in self._cache.keys()
                    if self.expert_manager.get_descriptor(eid) is not None
                ]
                candidates = self.policy.select_eviction_candidates(
                    current_in_vram,
                    size_bytes - vram_cap.available_bytes,
                    protected,
                )
                if not candidates:
                    LOG.error(
                        "VRAM Expert Cache out of capacity: Cannot free %d bytes for expert %d (Protected: %s)",
                        size_bytes,
                        expert_id,
                        protected,
                    )
                    raise MemoryError(f"VRAM budget exceeded; no evictable experts for expert {expert_id}")

                for evict_id in candidates:
                    evicted_data = self._evict_internal(evict_id)
                    if evicted_data is not None:
                        evicted_items.append((evict_id, evicted_data))

            # Allocate in MemoryTierManager
            allocated = self.tier_manager.allocate(MemoryTier.HOT_VRAM, size_bytes, tag)
            if not allocated:
                raise MemoryError(f"MemoryTierManager rejected VRAM allocation for expert {expert_id}")

            self._cache[expert_id] = expert_data
            self.expert_manager.transition_state(
                expert_id,
                new_state=ExpertState.HOT,
                new_tier=MemoryTier.HOT_VRAM,
            )
            self.expert_manager.mark_accessed(expert_id)

            LOG.debug(
                "VRAM Cache inserted expert %d (%.2f MB). Cache count: %d, Evicted: %d",
                expert_id,
                desc.size_mb,
                len(self._cache),
                len(evicted_items),
            )
            return evicted_items

    def _evict_internal(self, expert_id: int) -> Optional[Any]:
        """Evicts a single expert from VRAM cache."""
        if expert_id not in self._cache:
            return None

        data = self._cache.pop(expert_id)
        tag = f"vram_exp_{expert_id}"
        self.tier_manager.release(tag)
        self.expert_manager.transition_state(
            expert_id,
            new_state=ExpertState.WARM,
            new_tier=MemoryTier.WARM_RAM,
        )
        self._evictions += 1
        LOG.debug("Evicted expert %d from VRAM Cache to WARM tier", expert_id)
        return data

    def clear(self) -> List[Tuple[int, Any]]:
        """Purges all entries from VRAM cache."""
        with self._lock:
            items = []
            for exp_id in list(self._cache.keys()):
                data = self._evict_internal(exp_id)
                if data is not None:
                    items.append((exp_id, data))
            self._cache.clear()
            return items

    @property
    def hit_rate(self) -> float:
        with self._lock:
            total = self._hits + self._misses
            return round((self._hits / total) * 100.0, 2) if total > 0 else 0.0

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_pct": self.hit_rate,
                "evictions": self._evictions,
                "resident_expert_ids": list(self._cache.keys()),
            }


# ==============================================================================
# RAM EXPERT CACHE (WARM TIER)
# ==============================================================================

class RAMExpertCache:
    """
    Host RAM expert cache acting as warm staging.
    Prevents reading from NVMe if weights are already warm.
    Evicts back to COLD tier when RAM budget is reached.
    """

    def __init__(
        self,
        tier_manager: MemoryTierManager,
        expert_manager: ExpertManager,
        policy: Optional[BaseEvictionPolicy] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.tier_manager = tier_manager
        self.expert_manager = expert_manager
        self.policy: BaseEvictionPolicy = policy or LRUPolicy()

        self._cache: Dict[int, Any] = {}  # expert_id -> expert_buffer
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def contains(self, expert_id: int) -> bool:
        with self._lock:
            return expert_id in self._cache

    def get(self, expert_id: int, mark_access: bool = True) -> Optional[Any]:
        with self._lock:
            if expert_id in self._cache:
                self._hits += 1
                if mark_access:
                    self.expert_manager.mark_accessed(expert_id)
                return self._cache[expert_id]
            self._misses += 1
            return None

    def put(
        self,
        expert_id: int,
        expert_data: Any,
        protected_ids: Optional[Set[int]] = None,
    ) -> List[int]:
        """
        Inserts an expert into RAM cache. Evicts oldest warm experts if needed.
        Returns list of expert IDs evicted back to COLD NVMe.
        """
        with self._lock:
            desc = self.expert_manager.get_descriptor(expert_id)
            if not desc:
                raise KeyError(f"Expert ID {expert_id} is not registered in ExpertManager")

            size_bytes = desc.size_bytes
            tag = f"ram_exp_{expert_id}"
            protected = (protected_ids or set()) | {expert_id}
            evicted_ids: List[int] = []

            if expert_id in self._cache:
                return evicted_ids

            ram_cap = self.tier_manager.get_tier(MemoryTier.WARM_RAM)
            while ram_cap.available_bytes < size_bytes:
                current_in_ram = [
                    self.expert_manager.get_descriptor(eid)
                    for eid in self._cache.keys()
                    if self.expert_manager.get_descriptor(eid) is not None
                ]
                candidates = self.policy.select_eviction_candidates(
                    current_in_ram,
                    size_bytes - ram_cap.available_bytes,
                    protected,
                )
                if not candidates:
                    LOG.error("RAM Expert Cache out of capacity for expert %d", expert_id)
                    raise MemoryError(f"RAM budget exceeded; no evictable experts for expert {expert_id}")

                for evict_id in candidates:
                    self._evict_internal(evict_id)
                    evicted_ids.append(evict_id)

            allocated = self.tier_manager.allocate(MemoryTier.WARM_RAM, size_bytes, tag)
            if not allocated:
                raise MemoryError(f"MemoryTierManager rejected RAM allocation for expert {expert_id}")

            self._cache[expert_id] = expert_data
            self.expert_manager.transition_state(
                expert_id,
                new_state=ExpertState.WARM,
                new_tier=MemoryTier.WARM_RAM,
            )
            return evicted_ids

    def remove(self, expert_id: int) -> Optional[Any]:
        """Removes an expert from RAM cache (e.g. when promoted to VRAM)."""
        with self._lock:
            if expert_id in self._cache:
                data = self._cache.pop(expert_id)
                tag = f"ram_exp_{expert_id}"
                self.tier_manager.release(tag)
                return data
            return None

    def _evict_internal(self, expert_id: int) -> None:
        """Demotes an expert from RAM cache to COLD NVMe."""
        if expert_id in self._cache:
            self._cache.pop(expert_id)
            tag = f"ram_exp_{expert_id}"
            self.tier_manager.release(tag)
            self.expert_manager.transition_state(
                expert_id,
                new_state=ExpertState.COLD,
                new_tier=MemoryTier.COLD_NVME,
            )
            self._evictions += 1
            LOG.debug("Evicted expert %d from RAM Cache to COLD tier", expert_id)

    def clear(self) -> None:
        with self._lock:
            for exp_id in list(self._cache.keys()):
                self._evict_internal(exp_id)
            self._cache.clear()

    @property
    def hit_rate(self) -> float:
        with self._lock:
            total = self._hits + self._misses
            return round((self._hits / total) * 100.0, 2) if total > 0 else 0.0

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_pct": self.hit_rate,
                "evictions": self._evictions,
                "resident_expert_ids": list(self._cache.keys()),
            }
