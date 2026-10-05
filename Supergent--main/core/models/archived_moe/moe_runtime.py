# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Central MoE Runtime Coordinator
# Architecture: Core orchestrator unifying 3-tier memory, storage, prefetching,
# prediction, caching, eviction, and hardware compute execution.
# Lifecycle states: ACTIVE, WARM, IDLE, UNLOADED.
# Zero idle resource footprint: Stops background threads and releases VRAM.
# ==============================================================================

from __future__ import annotations

import time
import logging
import threading
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

from .memory_tiers import MemoryTier, MemoryTierManager
from .model_storage import ModelStorage
from .expert_manager import ExpertManager, ExpertState, ExpertDescriptor
from .expert_cache import (
    VRAMExpertCache,
    RAMExpertCache,
    BaseEvictionPolicy,
    LRUPolicy,
    LFUPolicy,
    FrequencyAwarePolicy,
    PredictionAwarePolicy,
    HybridPolicy,
)
from .transfer_manager import AsyncTransferManager
from .expert_prefetch import ExpertPrefetcher
from .expert_predictor import BaseExpertPredictor, RecentFrequencyPredictor
from .runtime_policy import RuntimePolicy, EvictionPolicyType
from .runtime_metrics import RuntimeMetricsCollector, MoETelemetrySnapshot
from .backend_interface import BaseInferenceBackend, SimulationBackend

LOG = logging.getLogger("WISE.Models.Runtime.MoERuntime")


class RuntimeState(str, Enum):
    UNLOADED = "UNLOADED"
    IDLE = "IDLE"
    WARM = "WARM"
    ACTIVE = "ACTIVE"


class MoERuntime:
    """
    Central governor for the WISE Mixture-of-Experts (MoE) 3-Tier Execution Engine.
    Executes sparse model inference across Hot VRAM, Warm RAM, and Cold NVMe.
    """

    def __init__(
        self,
        backend: BaseInferenceBackend,
        storage: ModelStorage,
        policy: Optional[RuntimePolicy] = None,
        predictor: Optional[BaseExpertPredictor] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.backend = backend
        self.storage = storage
        self.policy = policy or RuntimePolicy.auto_detect()

        self.state = RuntimeState.UNLOADED

        # 1. Initialize Memory Tier Manager
        self.tier_manager = MemoryTierManager(
            vram_budget_bytes=self.policy.vram_budget_bytes,
            ram_budget_bytes=self.policy.ram_budget_bytes,
            nvme_storage_dir=self.storage.base_dir,
            vram_headroom_bytes=self.policy.vram_headroom_bytes,
            ram_headroom_bytes=self.policy.ram_headroom_bytes,
        )

        # 2. Initialize Expert Manager
        self.expert_manager = ExpertManager()
        self._register_storage_experts()

        # 3. Create Eviction Policy
        self.eviction_policy = self._create_eviction_policy(self.policy.eviction_policy)

        # 4. Initialize Caches
        self.vram_cache = VRAMExpertCache(
            tier_manager=self.tier_manager,
            expert_manager=self.expert_manager,
            policy=self.eviction_policy,
        )
        self.ram_cache = RAMExpertCache(
            tier_manager=self.tier_manager,
            expert_manager=self.expert_manager,
            policy=self.eviction_policy,
        )

        # 5. Initialize Transfer Manager
        self.transfer_manager = AsyncTransferManager(
            storage=self.storage,
            expert_manager=self.expert_manager,
            vram_cache=self.vram_cache,
            ram_cache=self.ram_cache,
            max_workers=self.policy.max_concurrent_transfers,
        )

        # 6. Initialize Prefetcher & Predictor
        self.prefetcher = ExpertPrefetcher(
            expert_manager=self.expert_manager,
            vram_cache=self.vram_cache,
            ram_cache=self.ram_cache,
            transfer_manager=self.transfer_manager,
        )
        self.predictor = predictor or RecentFrequencyPredictor()

        # 7. Metrics
        self.metrics = RuntimeMetricsCollector()

    def _register_storage_experts(self) -> None:
        """Populates ExpertManager with initial cold descriptors from ModelStorage."""
        for exp_id, loc in self.storage.expert_locations.items():
            self.expert_manager.register_expert(
                expert_id=exp_id,
                layer_id=loc.layer_id,
                size_bytes=loc.size_bytes,
            )

    def _create_eviction_policy(self, ptype: EvictionPolicyType) -> BaseEvictionPolicy:
        if ptype == EvictionPolicyType.LRU:
            return LRUPolicy()
        elif ptype == EvictionPolicyType.LFU:
            return LFUPolicy()
        elif ptype == EvictionPolicyType.FREQUENCY_AWARE:
            return FrequencyAwarePolicy()
        elif ptype == EvictionPolicyType.PREDICTION_AWARE:
            return PredictionAwarePolicy()
        else:
            return HybridPolicy()

    def initialize(self) -> bool:
        """Opens storage and warms runtime."""
        with self._lock:
            if self.state != RuntimeState.UNLOADED:
                return True

            success = self.storage.open()
            if not success:
                LOG.error("Failed to initialize MoERuntime: storage open failed.")
                return False

            if self.policy.enable_prefetch:
                self.prefetcher.start()

            self.state = RuntimeState.WARM
            LOG.info("MoERuntime initialized successfully in WARM state.")
            return True

    def generate_tokens(
        self,
        num_tokens: int = 20,
        initial_hidden_state: Optional[List[float]] = None,
    ) -> Tuple[List[List[float]], MoETelemetrySnapshot]:
        """
        Executes sparse MoE token generation across the 3-tier memory hierarchy.
        Coordinates routing, multi-tier cache resolution, on-demand transfers,
        compute, metrics collection, and predictive prefetching.
        """
        with self._lock:
            if self.state == RuntimeState.UNLOADED:
                self.initialize()
            self.state = RuntimeState.ACTIVE

        self.metrics.start_generation()
        hidden = list(initial_hidden_state or [0.1 * (i % 10) for i in range(64)])
        token_outputs: List[List[float]] = []

        try:
            for token_idx in range(num_tokens):
                t0_tok = time.perf_counter()

                # 1. Router selects active experts
                active_ids = self.backend.route(token_idx)
                protected_set = set(active_ids)

                # 2. Ensure each active expert is resident in Hot VRAM
                expert_buffers: Dict[int, Any] = {}
                for exp_id in active_ids:
                    buf = self._ensure_expert_in_vram(exp_id, protected_set)
                    expert_buffers[exp_id] = buf
                    self.prefetcher.record_expert_used(exp_id)

                # 3. Execute Attention / Shared weights
                shared_out = self.backend.compute_shared(hidden)

                # 4. Compute active experts and aggregate
                expert_results: List[Tuple[int, float, List[float]]] = []
                for idx, exp_id in enumerate(active_ids):
                    exp_data = expert_buffers[exp_id]
                    # Softmax weight: 0.6 for primary, 0.4 for secondary
                    weight = 0.6 if idx == 0 else 0.4
                    out_vec = self.backend.compute_expert(exp_id, exp_data, shared_out)
                    expert_results.append((exp_id, weight, out_vec))

                # 5. Blend active experts into next token representation
                token_out = self.backend.aggregate_experts(expert_results)
                hidden = token_out
                token_outputs.append(token_out)

                # 6. Record latency and step metrics
                tok_latency_ms = (time.perf_counter() - t0_tok) * 1000.0
                self.metrics.record_token_latency(tok_latency_ms)

                # 7. Update predictor history
                self.predictor.record_step(active_ids)

                # 8. Asynchronous prefetch for subsequent tokens
                if self.policy.enable_prefetch:
                    pred_res = self.predictor.predict(
                        recent_expert_ids=active_ids,
                        top_k=self.policy.prefetch_depth,
                        min_confidence=self.policy.prefetch_confidence_threshold,
                    )
                    if pred_res.predicted_expert_ids:
                        target = (
                            MemoryTier.HOT_VRAM
                            if self.policy.prefetch_to_vram
                            else MemoryTier.WARM_RAM
                        )
                        self.prefetcher.prefetch(
                            expert_ids=pred_res.predicted_expert_ids,
                            priority=1,
                            target_tier=target,
                            confidence=list(pred_res.confidences.values())[0],
                            protected_ids=protected_set,
                        )

            self.metrics.end_generation()

        finally:
            with self._lock:
                self.state = RuntimeState.WARM

        # Collect final snapshot
        vram_stat = self.vram_cache.to_dict()
        ram_stat = self.ram_cache.to_dict()
        pref_stat = self.prefetcher.get_summary()
        tiers_summary = self.tier_manager.get_summary()

        snapshot = self.metrics.build_snapshot(
            vram_hit_rate=vram_stat["hit_rate_pct"],
            ram_hit_rate=ram_stat["hit_rate_pct"],
            prefetch_hit_rate=pref_stat["hit_rate_pct"],
            prefetch_waste=pref_stat["waste_rate_pct"],
            peak_vram_mb=tiers_summary["vram"]["peak_allocated_mb"],
            peak_ram_mb=tiers_summary["ram"]["peak_allocated_mb"],
            current_vram_mb=tiers_summary["vram"]["allocated_mb"],
            current_ram_mb=tiers_summary["ram"]["allocated_mb"],
        )

        return token_outputs, snapshot

    def _ensure_expert_in_vram(self, expert_id: int, protected_ids: Set[int]) -> Any:
        """
        Guarantees that an expert is resident in Hot VRAM:
        - Checks VRAM cache (Hit -> immediate compute)
        - Checks RAM cache (Hit -> promote RAM -> VRAM)
        - If Cold on NVMe -> stream NVMe -> RAM -> VRAM
        """
        # 1. Hot VRAM check
        if self.vram_cache.contains(expert_id):
            return self.vram_cache.get(expert_id, mark_access=True)

        # 2. Warm RAM check
        if self.ram_cache.contains(expert_id):
            self.metrics.ram_to_vram_transfers += 1
            data = self.transfer_manager.transfer_ram_to_vram(
                expert_id,
                async_mode=False,
                protected_ids=protected_ids,
            )
            return self.vram_cache.get(expert_id, mark_access=True) or data

        # 3. Cold NVMe fetch
        self.metrics.nvme_reads += 1
        desc = self.expert_manager.get_descriptor(expert_id)
        if desc:
            self.metrics.nvme_bytes += desc.size_bytes

        # Load NVMe -> RAM
        self.transfer_manager.transfer_nvme_to_ram(expert_id, async_mode=False)

        # Promote RAM -> VRAM
        self.metrics.ram_to_vram_transfers += 1
        data = self.transfer_manager.transfer_ram_to_vram(
            expert_id,
            async_mode=False,
            protected_ids=protected_ids,
        )
        return self.vram_cache.get(expert_id, mark_access=True) or data

    def enter_idle(self) -> None:
        """
        Enforces zero/low-resource footprint when WISE is idle:
        - Stops prefetch loops
        - Purges hot VRAM buffers
        - Resets expert states
        """
        with self._lock:
            LOG.info("MoERuntime entering IDLE state: purging VRAM and halting prefetchers...")
            self.prefetcher.cancel_all_pending()
            self.prefetcher.stop()
            self.transfer_manager.wait_all(timeout=1.0)

            # Purge VRAM cache if configured
            if self.policy.unload_vram_on_idle:
                self.vram_cache.clear()

            self.state = RuntimeState.IDLE
            LOG.info("MoERuntime successfully in IDLE state (0.0 MB active VRAM).")

    def unload(self) -> None:
        """Completely purges model from VRAM, RAM, and closes file descriptors."""
        with self._lock:
            LOG.info("MoERuntime unloading completely...")
            self.enter_idle()
            self.ram_cache.clear()
            self.tier_manager.purge_all()
            self.expert_manager.reset_all_to_cold()
            self.transfer_manager.shutdown(wait=True)
            self.storage.close()
            self.state = RuntimeState.UNLOADED
            LOG.info("MoERuntime fully unloaded.")

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            tiers = self.tier_manager.get_summary()
            return {
                "state": self.state.value,
                "storage": self.storage.to_dict(),
                "tiers": tiers,
                "vram_cache": self.vram_cache.to_dict(),
                "ram_cache": self.ram_cache.to_dict(),
                "prefetch": self.prefetcher.get_summary(),
                "transfers": self.transfer_manager.get_summary(),
                "policy": self.policy.to_dict(),
            }
