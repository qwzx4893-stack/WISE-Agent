# ==============================================================================
# WISE Phase P1.4-A MoE Runtime Speed & Quality Benchmark Runner
# Architecture: Comprehensive benchmark comparing:
# 1. 6 Runtime Configurations (Naive, RAM Cache, VRAM Cache, VRAM+RAM, Prefetch, Prediction+Prefetch)
# 2. 5 Cache Eviction Policies (LRU, LFU, FrequencyAware, PredictionAware, Hybrid)
# 3. 3 Routing Dynamics (Locality-Heavy, Clustered, Random)
# 4. Prefetch Latency-Hiding Effectiveness
# 5. Computational Output Fidelity (Ground-Truth Reference vs Hybrid Execution)
# 6. Real Hardware Probing (CPU, RAM, GPU, VRAM, NVMe)
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import json
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = BASE_DIR / "Supergent--main"
if str(SUPERGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_DIR))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from core.models.runtime import (
    MemoryTier,
    MemoryTierManager,
    ModelStorage,
    ExpertManager,
    VRAMExpertCache,
    RAMExpertCache,
    LRUPolicy,
    LFUPolicy,
    FrequencyAwarePolicy,
    PredictionAwarePolicy,
    HybridPolicy,
    AsyncTransferManager,
    ExpertPrefetcher,
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


def run_single_benchmark(
    name: str,
    backend: SimulationBackend,
    num_tokens: int,
    policy: RuntimePolicy,
    is_naive_mode: bool = False,
    reference_outputs: Optional[List[List[float]]] = None,
) -> Dict[str, Any]:
    """Executes a benchmark configuration and returns detailed metrics."""
    storage = backend.create_model_storage()
    runtime = MoERuntime(backend=backend, storage=storage, policy=policy)
    runtime.initialize()

    if is_naive_mode:
        # In Naive mode: clear caches before each token to force 100% NVMe reading
        orig_route = backend.route
        def _naive_route(tok_idx: int, ctx: Optional[Dict[str, Any]] = None) -> List[int]:
            runtime.vram_cache.clear()
            runtime.ram_cache.clear()
            return orig_route(tok_idx, ctx)
        backend.route = _naive_route

    outputs, snapshot = runtime.generate_tokens(num_tokens=num_tokens)
    runtime.unload()

    # Compute fidelity against reference if available
    fidelity_res = None
    if reference_outputs is not None:
        evaluator = MoEQualityEvaluator(tolerance=1e-5)
        fidelity_res = evaluator.evaluate(reference_outputs, outputs)

    return {
        "config_name": name,
        "tokens": snapshot.total_tokens,
        "ttft_ms": snapshot.time_to_first_token_ms,
        "avg_token_latency_ms": snapshot.avg_token_latency_ms,
        "p50_ms": snapshot.p50_latency_ms,
        "p95_ms": snapshot.p95_latency_ms,
        "p99_ms": snapshot.p99_latency_ms,
        "tokens_per_sec": snapshot.tokens_per_second,
        "vram_hit_rate_pct": snapshot.vram_cache_hit_rate_pct,
        "ram_hit_rate_pct": snapshot.ram_cache_hit_rate_pct,
        "nvme_reads": snapshot.nvme_reads_count,
        "nvme_bytes_mb": snapshot.nvme_bytes_read_mb,
        "ram_to_vram_transfers": snapshot.ram_to_vram_transfers_count,
        "total_transferred_mb": snapshot.total_bytes_transferred_mb,
        "prefetch_hit_rate_pct": snapshot.prefetch_hit_rate_pct,
        "prefetch_waste_pct": snapshot.prefetch_waste_pct,
        "peak_vram_mb": snapshot.peak_vram_mb,
        "peak_ram_mb": snapshot.peak_ram_mb,
        "fidelity": fidelity_res.to_dict() if fidelity_res else None,
    }


def main() -> None:
    scratch_dir = BASE_DIR / "scratch" / "moe_benchmark"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 85)
    print("   WISE MOE RUNTIME — SPEED, QUALITY & MEMORY HIERARCHY BENCHMARK")
    print("=" * 85)

    num_tokens = 40
    total_experts = 32
    active_per_token = 2
    expert_size = 4 * 1024 * 1024  # 4 MB per expert (32 * 4MB = 128 MB total model size)

    print(f"Benchmark Parameters: Tokens={num_tokens}, Total Experts={total_experts}, "
          f"Active/Token={active_per_token}, Expert Size={expert_size // (1024*1024)} MB (Total Model: 128 MB)\n")

    backend = SimulationBackend(
        total_experts=total_experts,
        active_per_token=active_per_token,
        expert_size_bytes=expert_size,
        routing_pattern=RoutingPattern.LOCALITY_HEAVY,
        storage_dir=scratch_dir,
    )

    # 1. Generate Ground-Truth Reference Outputs (all resident)
    print("[1/5] Computing Ground-Truth Reference Output (all weights resident in RAM)...")
    ref_outputs: List[List[float]] = []
    curr_hidden = [0.1 * (i % 10) for i in range(64)]
    t0_ref = time.perf_counter()
    for tok_idx in range(num_tokens):
        _, ref_out = backend.compute_reference_step(tok_idx, curr_hidden)
        ref_outputs.append(ref_out)
        curr_hidden = ref_out
    ref_time_ms = (time.perf_counter() - t0_ref) * 1000.0
    ref_tps = num_tokens / (ref_time_ms / 1000.0)
    print(f"      Reference Mode: Duration={ref_time_ms:.1f}ms, TPS={ref_tps:.1f}\n")

    # --------------------------------------------------------------------------
    # 2. RUN THE 6 PRIMARY CONFIGURATIONS
    # --------------------------------------------------------------------------
    print("[2/5] Benchmarking 6 Runtime Configurations...")

    # A. Naive: Re-read NVMe on every token (cache cleared per token step)
    pol_naive = RuntimePolicy(
        vram_budget_bytes=16 * 1024 * 1024,
        vram_headroom_bytes=2 * 1024 * 1024,
        ram_budget_bytes=16 * 1024 * 1024,
        ram_headroom_bytes=2 * 1024 * 1024,
        enable_prefetch=False,
    )
    res_a = run_single_benchmark("A. Naive NVMe", backend, num_tokens, pol_naive, is_naive_mode=True, reference_outputs=ref_outputs)

    # B. RAM Cache Only: NVMe -> RAM -> VRAM (RAM budget large, VRAM tight)
    pol_ram = RuntimePolicy(
        vram_budget_bytes=16 * 1024 * 1024,
        vram_headroom_bytes=2 * 1024 * 1024,
        ram_budget_bytes=64 * 1024 * 1024,
        ram_headroom_bytes=4 * 1024 * 1024,
        enable_prefetch=False,
    )
    res_b = run_single_benchmark("B. RAM Cache Only", backend, num_tokens, pol_ram, reference_outputs=ref_outputs)

    # C. VRAM Cache Only: Tight RAM budget, active VRAM cache
    pol_vram = RuntimePolicy(
        vram_budget_bytes=16 * 1024 * 1024,
        vram_headroom_bytes=2 * 1024 * 1024,
        ram_budget_bytes=16 * 1024 * 1024,
        ram_headroom_bytes=2 * 1024 * 1024,
        enable_prefetch=False,
    )
    res_c = run_single_benchmark("C. VRAM Cache Only", backend, num_tokens, pol_vram, reference_outputs=ref_outputs)

    # D. VRAM + RAM Cache
    pol_vram_ram = RuntimePolicy(
        vram_budget_bytes=16 * 1024 * 1024,
        vram_headroom_bytes=2 * 1024 * 1024,
        ram_budget_bytes=48 * 1024 * 1024,
        ram_headroom_bytes=4 * 1024 * 1024,
        enable_prefetch=False,
    )
    res_d = run_single_benchmark("D. VRAM + RAM Cache", backend, num_tokens, pol_vram_ram, reference_outputs=ref_outputs)

    # E. VRAM + RAM + Prefetch
    pol_prefetch = RuntimePolicy(
        vram_budget_bytes=16 * 1024 * 1024,
        vram_headroom_bytes=2 * 1024 * 1024,
        ram_budget_bytes=48 * 1024 * 1024,
        ram_headroom_bytes=4 * 1024 * 1024,
        enable_prefetch=True,
        prefetch_depth=2,
        prefetch_confidence_threshold=0.20,
    )
    res_e = run_single_benchmark("E. VRAM + RAM + Prefetch", backend, num_tokens, pol_prefetch, reference_outputs=ref_outputs)

    # F. VRAM + RAM + Prediction + Prefetch
    pol_pred = RuntimePolicy(
        vram_budget_bytes=16 * 1024 * 1024,
        vram_headroom_bytes=2 * 1024 * 1024,
        ram_budget_bytes=48 * 1024 * 1024,
        ram_headroom_bytes=4 * 1024 * 1024,
        eviction_policy=EvictionPolicyType.PREDICTION_AWARE,
        enable_prefetch=True,
        prefetch_depth=2,
        prefetch_confidence_threshold=0.25,
    )
    res_f = run_single_benchmark("F. VRAM+RAM+Pred+Prefetch", backend, num_tokens, pol_pred, reference_outputs=ref_outputs)

    configs_results = [res_a, res_b, res_c, res_d, res_e, res_f]

    # Print Configuration Summary Table
    print("\n" + "=" * 105)
    print(f"{'Configuration':<26} | {'TPS':<7} | {'Avg Lat':<8} | {'p50':<6} | {'p95':<6} | {'VRAM Hit%':<9} | {'RAM Hit%':<9} | {'NVMe Reads':<10} | {'Fidelity':<8}")
    print("-" * 105)
    for r in configs_results:
        f_stat = r["fidelity"]["output_fidelity_score_pct"] if r["fidelity"] else 0.0
        print(f"{r['config_name']:<26} | {r['tokens_per_sec']:<7.1f} | {r['avg_token_latency_ms']:<8.2f} | {r['p50_ms']:<6.1f} | {r['p95_ms']:<6.1f} | "
              f"{r['vram_hit_rate_pct']:<9.1f} | {r['ram_hit_rate_pct']:<9.1f} | {r['nvme_reads']:<10} | {f_stat:<7.1f}%")
    print("=" * 105)

    # --------------------------------------------------------------------------
    # 3. PREFETCH EFFECTIVENESS EVALUATION
    # --------------------------------------------------------------------------
    print("\n[3/5] Evaluating Prefetch Latency-Hiding Effectiveness...")
    lat_no_pref = res_d["avg_token_latency_ms"]
    lat_pref = res_f["avg_token_latency_ms"]
    reduction_pct = round(((lat_no_pref - lat_pref) / lat_no_pref) * 100.0, 2) if lat_no_pref > 0 else 0.0
    hidden_pct = res_f["prefetch_hit_rate_pct"]

    print(f"      Latency Without Prefetch:  {lat_no_pref:.2f} ms")
    print(f"      Latency With Prefetch:     {lat_pref:.2f} ms")
    print(f"      Latency Reduction:         {reduction_pct:+.2f}%")
    print(f"      Prefetch Hit Rate:         {res_f['prefetch_hit_rate_pct']:.1f}%")
    print(f"      Prefetch Waste Rate:       {res_f['prefetch_waste_pct']:.1f}%")
    print(f"      Transfers Hidden Behind Compute: {hidden_pct:.1f}%\n")

    # --------------------------------------------------------------------------
    # 4. CACHE POLICY BENCHMARK ACROSS ROUTING DYNAMICS
    # --------------------------------------------------------------------------
    print("[4/5] Benchmarking Eviction Policies Across Routing Dynamics...")
    policies = [
        ("LRU", EvictionPolicyType.LRU),
        ("LFU", EvictionPolicyType.LFU),
        ("FrequencyAware", EvictionPolicyType.FREQUENCY_AWARE),
        ("PredictionAware", EvictionPolicyType.PREDICTION_AWARE),
        ("Hybrid", EvictionPolicyType.HYBRID),
    ]
    dynamics = [
        ("Locality-Heavy", RoutingPattern.LOCALITY_HEAVY),
        ("Clustered", RoutingPattern.CLUSTERED),
        ("Random", RoutingPattern.RANDOM),
    ]

    policy_results = []
    for dyn_name, dyn_pat in dynamics:
        dyn_backend = SimulationBackend(
            total_experts=total_experts,
            active_per_token=active_per_token,
            expert_size_bytes=expert_size,
            routing_pattern=dyn_pat,
            storage_dir=scratch_dir,
        )
        print(f"  Dynamic: {dyn_name:<15}")
        for pol_name, pol_type in policies:
            p = RuntimePolicy(
                vram_budget_bytes=16 * 1024 * 1024,
                vram_headroom_bytes=2 * 1024 * 1024,
                ram_budget_bytes=48 * 1024 * 1024,
                ram_headroom_bytes=4 * 1024 * 1024,
                eviction_policy=pol_type,
                enable_prefetch=True,
            )
            res = run_single_benchmark(f"{pol_name} [{dyn_name}]", dyn_backend, num_tokens=25, policy=p)
            policy_results.append((dyn_name, pol_name, res["vram_hit_rate_pct"], res["tokens_per_sec"], res["avg_token_latency_ms"]))
            print(f"    - {pol_name:<16}: VRAM Hit={res['vram_hit_rate_pct']:<5.1f}%, TPS={res['tokens_per_sec']:<5.1f}, AvgLat={res['avg_token_latency_ms']:<5.2f}ms")

    # --------------------------------------------------------------------------
    # 5. REAL HOST HARDWARE TELEMETRY PROBE
    # --------------------------------------------------------------------------
    print("\n[5/5] Real Host Hardware Telemetry Probe...")
    import psutil
    vmem = psutil.virtual_memory()
    ram_total_gb = round(vmem.total / (1024**3), 2)
    ram_avail_gb = round(vmem.available / (1024**3), 2)

    gpu_name = "None/CPU-Only"
    vram_total_mb = 0.0
    try:
        import subprocess
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("\n")[0].split(",")
            gpu_name = parts[0].strip()
            vram_total_mb = float(parts[1].replace("MiB", "").strip())
    except Exception:
        pass

    print(f"      CPU Cores:        {psutil.cpu_count(logical=True)} logical ({psutil.cpu_count(logical=False)} physical)")
    print(f"      Host RAM:         Total={ram_total_gb} GB, Available={ram_avail_gb} GB")
    print(f"      Host GPU:         {gpu_name} (VRAM: {vram_total_mb:.1f} MB)")
    print(f"      NVMe Storage:     Volume free={round(shutil.disk_usage(str(scratch_dir)).free / (1024**3), 1)} GB")

    # Save complete benchmark report to disk
    report_data = {
        "configurations": configs_results,
        "prefetch_effectiveness": {
            "latency_no_prefetch_ms": lat_no_pref,
            "latency_with_prefetch_ms": lat_pref,
            "latency_reduction_pct": reduction_pct,
            "prefetch_hit_rate_pct": res_f["prefetch_hit_rate_pct"],
            "prefetch_waste_pct": res_f["prefetch_waste_pct"],
        },
        "policy_comparison": [
            {"dynamic": d, "policy": p, "vram_hit_pct": vh, "tps": tps, "latency_ms": lat}
            for d, p, vh, tps, lat in policy_results
        ],
        "hardware": {
            "cpu_cores": psutil.cpu_count(logical=True),
            "ram_total_gb": ram_total_gb,
            "ram_avail_gb": ram_avail_gb,
            "gpu_name": gpu_name,
            "vram_total_mb": vram_total_mb,
        },
    }

    report_path = scratch_dir / "benchmark_report.json"
    with open(report_path, "w") as f:
        json.dump(report_data, f, indent=2)
    print(f"\nMachine-readable benchmark report saved to: {report_path}")

    # Cleanup scratch binary file
    try:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    except Exception:
        pass

    print("\n" + "=" * 85)
    print("   WISE PHASE P1.4-A BENCHMARK COMPLETED SUCCESSFULLY")
    print("=" * 85)


if __name__ == "__main__":
    main()
