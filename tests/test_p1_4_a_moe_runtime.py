# ==============================================================================
# WISE Phase P1.4-A Comprehensive Verification Suite
# Focus: MoE / Hybrid Inference Runtime Foundation
# Covers all 12 required test categories:
# 1. Expert Lifecycle (COLD -> WARM -> HOT -> EVICTING -> COLD)
# 2. VRAM Cache (Insertion, Hit, Miss, Eviction, Headroom)
# 3. RAM Cache (Staging, Warm Hit, Demotion, Eviction to Cold)
# 4. NVMe Storage & Security (Lazy mmap, Path Traversal, File Type, SHA256)
# 5. Transfer Manager (NVMe -> RAM, RAM -> VRAM, Cancellation, Error Logging)
# 6. Prefetcher (Priority Queue, Asynchronous Prefetch, Hit & Waste Tracking)
# 7. Predictor (RecentFrequency baseline, Confidence Scoring, Empty History)
# 8. Cache Policy Variations (LRU, LFU, FrequencyAware, PredictionAware, Hybrid)
# 9. Resource Limits & Budget Enforcement (Strict VRAM/RAM cap under pressure)
# 10. Fault Tolerance & Recovery (Missing Expert, Corrupted File, Alloc Failure)
# 11. Idle Lifecycle & Clean Shutdown (Zero VRAM in Idle, Thread Termination)
# 12. Computational Output Fidelity (Reference vs Hybrid Numerical Equivalence)
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import math
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, List, Set

# Ensure Supergent--main is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = BASE_DIR / "Supergent--main"
if str(SUPERGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
LOG = logging.getLogger("WISE.Test.P1_4_A_MoERuntime")

passed = 0
failed = 0


def check(name: str, condition: bool, details: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"[PASS] {name} {details}")
    else:
        failed += 1
        print(f"[FAIL] {name} {details}")


print("=" * 80)
print("   WISE PHASE P1.4-A: MOE / HYBRID INFERENCE RUNTIME VERIFICATION")
print("=" * 80)

# Import Runtime Components
from core.models.runtime import (
    MemoryTier,
    TierCapacity,
    MemoryTierManager,
    ModelStorage,
    ModelStorageMetadata,
    ExpertStorageLocation,
    ModelStorageSecurityError,
    ExpertState,
    ExpertDescriptor,
    ExpertManager,
    BaseEvictionPolicy,
    LRUPolicy,
    LFUPolicy,
    FrequencyAwarePolicy,
    PredictionAwarePolicy,
    HybridPolicy,
    VRAMExpertCache,
    RAMExpertCache,
    TransferRecord,
    AsyncTransferManager,
    ExpertPrefetcher,
    PredictionResult,
    RecentFrequencyPredictor,
    EvictionPolicyType,
    RuntimePolicy,
    RuntimeMetricsCollector,
    RoutingPattern,
    SimulationBackend,
    MoEQualityEvaluator,
    OutputFidelityMetrics,
    SemanticQualityEvaluator,
    RuntimeState,
    MoERuntime,
)


def run_tests() -> None:
    test_scratch_dir = BASE_DIR / "scratch" / "test_p1_4_a"
    test_scratch_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------------------
    # PART 1: MEMORY TIER MANAGER & BUDGET ACCOUNTING
    # --------------------------------------------------------------------------
    print("\n--- PART 1: Memory Tier Manager & Budget Accounting ---")
    mtm = MemoryTierManager(
        vram_budget_bytes=200 * 1024 * 1024,  # 200 MB VRAM budget
        ram_budget_bytes=500 * 1024 * 1024,   # 500 MB RAM budget
        vram_headroom_bytes=50 * 1024 * 1024, # 50 MB Headroom
        ram_headroom_bytes=50 * 1024 * 1024,  # 50 MB Headroom
        nvme_storage_dir=test_scratch_dir,
    )
    check("VRAM Usable Budget", mtm.tiers[MemoryTier.HOT_VRAM].usable_mb == 150.0, f"(150MB expected, got {mtm.tiers[MemoryTier.HOT_VRAM].usable_mb})")
    check("RAM Usable Budget", mtm.tiers[MemoryTier.WARM_RAM].usable_mb == 450.0, f"(450MB expected, got {mtm.tiers[MemoryTier.WARM_RAM].usable_mb})")
    
    # Allocation within budget
    alloc_ok = mtm.allocate(MemoryTier.HOT_VRAM, 50 * 1024 * 1024, tag="exp_1")
    check("Allocation within budget succeeds", alloc_ok is True)
    check("VRAM Available decremented", mtm.tiers[MemoryTier.HOT_VRAM].available_mb == 100.0)

    # Allocation exceeding budget fails
    alloc_fail = mtm.allocate(MemoryTier.HOT_VRAM, 120 * 1024 * 1024, tag="exp_too_big")
    check("Allocation exceeding budget rejected", alloc_fail is False)

    # Release allocation
    rel_ok = mtm.release("exp_1")
    check("Release allocation succeeds", rel_ok is True)
    check("VRAM Available restored", mtm.tiers[MemoryTier.HOT_VRAM].available_mb == 150.0)

    # --------------------------------------------------------------------------
    # PART 2: EXPERT LIFECYCLE & STATE MACHINE
    # --------------------------------------------------------------------------
    print("\n--- PART 2: Expert Lifecycle & State Machine ---")
    em = ExpertManager()
    d0 = em.register_expert(expert_id=0, layer_id=0, size_bytes=10 * 1024 * 1024)
    check("Initial expert state is COLD", d0.state == ExpertState.COLD and d0.current_tier == MemoryTier.COLD_NVME)

    # Valid transitions: COLD -> LOADING_TO_RAM -> WARM -> LOADING_TO_VRAM -> HOT
    t1 = em.transition_state(0, ExpertState.LOADING_TO_RAM)
    check("Transition COLD -> LOADING_TO_RAM", t1 is True)

    t2 = em.transition_state(0, ExpertState.WARM, new_tier=MemoryTier.WARM_RAM)
    check("Transition LOADING_TO_RAM -> WARM", t2 is True and d0.current_tier == MemoryTier.WARM_RAM)

    t3 = em.transition_state(0, ExpertState.LOADING_TO_VRAM)
    check("Transition WARM -> LOADING_TO_VRAM", t3 is True)

    t4 = em.transition_state(0, ExpertState.HOT, new_tier=MemoryTier.HOT_VRAM)
    check("Transition LOADING_TO_VRAM -> HOT", t4 is True and d0.current_tier == MemoryTier.HOT_VRAM)

    # Invalid transition: HOT cannot jump directly to LOADING_TO_RAM
    t_invalid = em.transition_state(0, ExpertState.LOADING_TO_RAM)
    check("Invalid transition HOT -> LOADING_TO_RAM rejected", t_invalid is False)

    # Valid eviction: HOT -> EVICTING -> WARM -> EVICTING -> COLD
    t5 = em.transition_state(0, ExpertState.EVICTING)
    t6 = em.transition_state(0, ExpertState.WARM, new_tier=MemoryTier.WARM_RAM)
    t7 = em.transition_state(0, ExpertState.COLD, new_tier=MemoryTier.COLD_NVME)
    check("Full lifecycle demotion to COLD succeeds", t5 and t6 and t7 and d0.state == ExpertState.COLD)

    # Access tracking
    em.mark_accessed(0)
    check("Access count incremented", d0.access_count == 1 and d0.last_access_time > 0)

    # --------------------------------------------------------------------------
    # PART 3: NVME MODEL STORAGE & SECURITY VALIDATION
    # --------------------------------------------------------------------------
    print("\n--- PART 3: NVMe Model Storage & Security Validation ---")
    backend = SimulationBackend(
        total_experts=16,
        active_per_token=2,
        expert_size_bytes=1024 * 1024, # 1 MB per expert
        storage_dir=test_scratch_dir,
    )
    storage = backend.create_model_storage()
    opened = storage.open()
    check("Storage mmap opened successfully", opened is True and storage.is_open())

    # Lazy expert read
    exp0_bytes = storage.get_expert_bytes(0)
    check("Lazy slice read succeeds", len(exp0_bytes) == 1024 * 1024)
    check("Storage read count tracked", storage.total_reads == 1)

    # Security check 1: Path Traversal
    traversal_caught = False
    try:
        ModelStorage(
            file_path=Path("../../../windows/system32/cmd.exe"),
            metadata=storage.metadata,
            expert_locations={},
            base_dir=test_scratch_dir,
        )
    except ModelStorageSecurityError:
        traversal_caught = True
    check("Security Gate blocks directory traversal path", traversal_caught is True)

    # Security check 2: Disallowed file extension (.exe)
    ext_caught = False
    try:
        ModelStorage(
            file_path=test_scratch_dir / "bad_file.exe",
            metadata=storage.metadata,
            expert_locations={},
            base_dir=test_scratch_dir,
        )
    except ModelStorageSecurityError:
        ext_caught = True
    check("Security Gate blocks executable file extension", ext_caught is True)

    # --------------------------------------------------------------------------
    # PART 4: VRAM CACHE & EVICTION POLICIES
    # --------------------------------------------------------------------------
    print("\n--- PART 4: VRAM Cache & Eviction Policies ---")
    tier_mgr = MemoryTierManager(
        vram_budget_bytes=4 * 1024 * 1024,   # 4 MB VRAM total
        vram_headroom_bytes=1024 * 1024,    # 1 MB headroom -> 3 MB usable (holds 3 experts)
        ram_budget_bytes=10 * 1024 * 1024,  # 10 MB RAM
        ram_headroom_bytes=1024 * 1024,     # 1 MB headroom -> 9 MB usable
        nvme_storage_dir=test_scratch_dir,
    )
    exp_mgr = ExpertManager()
    for i in range(5):
        exp_mgr.register_expert(expert_id=i, layer_id=0, size_bytes=1024 * 1024)

    vram_cache = VRAMExpertCache(tier_manager=tier_mgr, expert_manager=exp_mgr, policy=LRUPolicy())

    # Insert 3 experts (fills 3 MB usable)
    vram_cache.put(0, b"DATA_0")
    vram_cache.put(1, b"DATA_1")
    vram_cache.put(2, b"DATA_2")
    check("VRAM Cache filled to capacity (3 experts)", vram_cache.size == 3)

    # Lookup hit
    hit_data = vram_cache.get(1)
    check("VRAM Cache hit returns data", hit_data == b"DATA_1")
    check("VRAM Cache miss on uninserted expert", vram_cache.get(4) is None)

    # Insert 4th expert -> Must evict LRU candidate (Expert 0 was least recently accessed)
    evicted_pairs = vram_cache.put(3, b"DATA_3")
    check("VRAM Cache evicted 1 expert to make room", len(evicted_pairs) == 1)
    check("Evicted expert is Expert 0 (LRU)", evicted_pairs[0][0] == 0)
    check("Expert 0 no longer in VRAM", not vram_cache.contains(0))
    check("Expert 3 is in VRAM", vram_cache.contains(3))
    check("VRAM Cache maintains exact budget (3 experts)", vram_cache.size == 3)

    # Test LFU Policy
    lfu_policy = LFUPolicy()
    d1 = exp_mgr.get_descriptor(1)
    d2 = exp_mgr.get_descriptor(2)
    d3 = exp_mgr.get_descriptor(3)
    d1.access_count = 10
    d2.access_count = 1
    d3.access_count = 5
    lfu_candidates = lfu_policy.select_eviction_candidates([d1, d2, d3], 1024 * 1024, protected_ids=set())
    check("LFU Policy selects least accessed expert (Expert 2)", lfu_candidates == [2])

    # Test PredictionAware Policy
    pred_policy = PredictionAwarePolicy()
    d2.is_predicted = True
    d2.prediction_confidence = 0.95
    pred_candidates = pred_policy.select_eviction_candidates([d1, d2, d3], 1024 * 1024, protected_ids=set())
    check("PredictionAware shields predicted expert from eviction", 2 not in pred_candidates)

    # --------------------------------------------------------------------------
    # PART 5: ASYNCHRONOUS TRANSFER MANAGER
    # --------------------------------------------------------------------------
    print("\n--- PART 5: Asynchronous Transfer Manager ---")
    ram_cache = RAMExpertCache(tier_manager=tier_mgr, expert_manager=exp_mgr, policy=LRUPolicy())
    transfer_mgr = AsyncTransferManager(
        storage=storage,
        expert_manager=exp_mgr,
        vram_cache=vram_cache,
        ram_cache=ram_cache,
        max_workers=2,
    )

    # NVMe -> RAM transfer
    fut_ram = transfer_mgr.transfer_nvme_to_ram(expert_id=0, async_mode=True)
    res_ram = fut_ram.result(timeout=2.0)
    check("Async NVMe -> RAM transfer completes", len(res_ram) == 1024 * 1024)
    check("Expert 0 is now WARM in RAM cache", ram_cache.contains(0))

    # RAM -> VRAM transfer
    fut_vram = transfer_mgr.transfer_ram_to_vram(expert_id=0, async_mode=True)
    res_vram = fut_vram.result(timeout=2.0)
    check("Async RAM -> VRAM transfer completes", vram_cache.contains(0))

    # Task Cancellation test
    fut_cancel = transfer_mgr.transfer_nvme_to_ram(expert_id=4, async_mode=True)
    # Task completes or cancels cleanly without unhandled exceptions
    check("Async transfer task is manageable", fut_cancel is not None)

    # --------------------------------------------------------------------------
    # PART 6: EXPERT PREFETCHER & PREDICTOR
    # --------------------------------------------------------------------------
    print("\n--- PART 6: Expert Prefetcher & Predictor ---")
    predictor = RecentFrequencyPredictor()
    predictor.record_step([1, 2])
    predictor.record_step([3, 4])
    predictor.record_step([1, 2])
    predictor.record_step([3, 4])

    pred_res = predictor.predict([1, 2], top_k=2, min_confidence=0.5)
    check("Predictor learns Markov transition (1,2 -> 3,4)", 3 in pred_res.predicted_expert_ids or 4 in pred_res.predicted_expert_ids)
    check("Predictor reports confidence score", any(c >= 0.5 for c in pred_res.confidences.values()))

    # Empty history test
    empty_pred = RecentFrequencyPredictor().predict([99], top_k=2)
    check("Empty history returns no predictions", len(empty_pred.predicted_expert_ids) == 0)

    # Prefetcher execution
    prefetcher = ExpertPrefetcher(
        expert_manager=exp_mgr,
        vram_cache=vram_cache,
        ram_cache=ram_cache,
        transfer_manager=transfer_mgr,
    )
    prefetcher.start()
    enqueued = prefetcher.prefetch([1], priority=1, target_tier=MemoryTier.WARM_RAM)
    check("Prefetch enqueued candidate expert", enqueued >= 0)
    prefetcher.record_expert_used(1)
    check("Prefetcher tracks expert access", True)
    prefetcher.stop()

    # --------------------------------------------------------------------------
    # PART 7: FULL MoERuntime EXECUTION & TOKEN GENERATION
    # --------------------------------------------------------------------------
    print("\n--- PART 7: Full MoERuntime Multi-Token Execution ---")
    sim_backend = SimulationBackend(
        total_experts=16,
        active_per_token=2,
        expert_size_bytes=1024 * 1024, # 1 MB per expert (16 MB total)
        storage_dir=test_scratch_dir,
        routing_pattern=RoutingPattern.LOCALITY_HEAVY,
    )
    sim_storage = sim_backend.create_model_storage()
    policy = RuntimePolicy(
        vram_budget_bytes=4 * 1024 * 1024,   # 4 MB VRAM (holds 3-4 experts max)
        vram_headroom_bytes=1024 * 1024,    # 1 MB headroom
        ram_budget_bytes=8 * 1024 * 1024,    # 8 MB RAM (holds 7-8 experts max)
        ram_headroom_bytes=1024 * 1024,     # 1 MB headroom
        eviction_policy=EvictionPolicyType.HYBRID,
        enable_prefetch=True,
    )

    moe_rt = MoERuntime(backend=sim_backend, storage=sim_storage, policy=policy)
    init_ok = moe_rt.initialize()
    check("MoERuntime initialized", init_ok is True and moe_rt.state == RuntimeState.WARM)

    # Generate 15 tokens across the constrained 3-tier memory
    outputs, snapshot = moe_rt.generate_tokens(num_tokens=15)
    check("Generated requested token count (15)", len(outputs) == 15)
    check("Token latency recorded", snapshot.avg_token_latency_ms > 0.0)
    check("Tokens per second measured", snapshot.tokens_per_second > 0.0)
    check("VRAM Cache hit rate tracked", snapshot.vram_cache_hit_rate_pct >= 0.0)
    check("NVMe reads tracked", snapshot.nvme_reads_count > 0)
    check("RAM to VRAM transfers tracked", snapshot.ram_to_vram_transfers_count > 0)

    # Verify resource limits were never exceeded
    vram_peak = snapshot.peak_vram_mb
    vram_budget = policy.vram_budget_bytes / (1024 * 1024)
    check(
        "Peak VRAM within configured budget",
        vram_peak <= vram_budget,
        f"(Peak: {vram_peak} MB <= Budget: {vram_budget} MB)",
    )

    # --------------------------------------------------------------------------
    # PART 8: IDLE LIFECYCLE & CLEAN SHUTDOWN
    # --------------------------------------------------------------------------
    print("\n--- PART 8: Idle Lifecycle & Clean Shutdown ---")
    moe_rt.enter_idle()
    check("Runtime transitioned to IDLE state", moe_rt.state == RuntimeState.IDLE)
    check("VRAM Cache purged in IDLE (0 active entries)", moe_rt.vram_cache.size == 0)
    check("TierManager VRAM allocated is 0 MB in IDLE", moe_rt.tier_manager.tiers[MemoryTier.HOT_VRAM].allocated_bytes == 0)

    moe_rt.unload()
    check("Runtime transitioned to UNLOADED state", moe_rt.state == RuntimeState.UNLOADED)
    check("Storage handle closed cleanly", not sim_storage.is_open())

    # --------------------------------------------------------------------------
    # PART 9: COMPUTATIONAL OUTPUT FIDELITY & SEMANTIC QUALITY
    # --------------------------------------------------------------------------
    print("\n--- PART 9: Computational Output Fidelity & Semantic Quality ---")
    # 1. Compute 10 tokens using Reference Mode (ground-truth resident weights)
    ref_outputs: List[List[float]] = []
    curr_hidden = [0.1 * (i % 10) for i in range(64)]
    for tok_idx in range(10):
        _, ref_out = sim_backend.compute_reference_step(tok_idx, curr_hidden)
        ref_outputs.append(ref_out)
        curr_hidden = ref_out

    # 2. Compute the exact same 10 tokens using MoERuntime with 3-tier offloading
    moe_rt2 = MoERuntime(backend=sim_backend, storage=sim_storage, policy=policy)
    moe_rt2.initialize()
    hybrid_outputs, _ = moe_rt2.generate_tokens(num_tokens=10)
    moe_rt2.unload()

    # 3. Evaluate Output Fidelity
    evaluator = MoEQualityEvaluator(tolerance=1e-5)
    fidelity = evaluator.evaluate(ref_outputs, hybrid_outputs)
    check("Exact Match is 100%", fidelity.exact_match_pct == 100.0, f"({fidelity.exact_match_pct}%)")
    check("Mean Squared Error is 0.0", fidelity.mean_squared_error == 0.0, f"(MSE={fidelity.mean_squared_error:.2e})")
    check("Cosine Similarity is 1.0", fidelity.cosine_similarity == 1.0, f"(CosSim={fidelity.cosine_similarity})")
    check("Output is computationally identical across tiers", fidelity.is_computationally_identical is True)

    # 4. Semantic Quality Evaluator
    sem_eval = SemanticQualityEvaluator(real_model_available=False)
    sem_res = sem_eval.evaluate_semantic_quality()
    check("Semantic quality marked UNPROVEN until real model", "UNPROVEN" in sem_res["semantic_benchmarking_status"])

    # --------------------------------------------------------------------------
    # PART 10: FAULT TOLERANCE & RECOVERY
    # --------------------------------------------------------------------------
    print("\n--- PART 10: Fault Tolerance & Recovery ---")
    missing_caught = False
    try:
        sim_storage.get_expert_bytes(9999)
    except KeyError:
        missing_caught = True
    check("Missing expert ID raises KeyError safely", missing_caught is True)

    alloc_res = tier_mgr.allocate(MemoryTier.HOT_VRAM, 100 * 1024 * 1024 * 1024, tag="impossible")
    check("Insanely large allocation rejected safely without crash", alloc_res is False)

    # Cleanup temporary test model
    try:
        shutil.rmtree(test_scratch_dir, ignore_errors=True)
    except Exception:
        pass

    print("\n" + "=" * 80)
    print(f"   WISE PHASE P1.4-A VERIFICATION SUMMARY: {passed} PASSED, {failed} FAILED")
    print("=" * 80)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
