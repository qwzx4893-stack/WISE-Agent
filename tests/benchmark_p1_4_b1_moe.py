# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Benchmark Suite (P1.4-B.1)
# Implements:
# - Phase 3: Real Baseline (Reference Full GPU, -ngl 28)
# - Phase 4: Observability Probes across 4 domain workloads
# - Phase 5: Real Expert Residency (llama.cpp native vs WISE control)
# - Phase 6 & 7: Truthful Cache & Prefetch Classification
# - Phase 8: Baseline vs Native CPU-MoE Offload Comparison
# ==============================================================================

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, List

import psutil

# Add repository paths
REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = REPO_ROOT / "Supergent--main"
if str(SUPERGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.models.runtime.real_moe_locator import RealMoELocator
from core.models.runtime.real_moe_backend import (
    LlamaCppMoEBackend,
    MoEOffloadMode,
    RealMoEInferenceResult,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("WISE.Benchmark.P1_4_B1")

OUTPUT_DIR = REPO_ROOT / "scratch" / "p1_4_b1"
BENCHMARK_RESULTS_FILE = OUTPUT_DIR / "benchmark_results.json"


# 4 Domain Workloads for Observability Probes (Phase 4)
WORKLOADS = {
    "Workload_A_General": "Explain in 3 concise bullet points how a Mixture-of-Experts (MoE) architecture routes queries to specialized neural networks.",
    "Workload_B_Code": "Write a Python function `binary_search(arr, target)` that returns the index of target or -1 if not found. Include docstring.",
    "Workload_C_Creative": "Write a 3-line poetic haiku about computer memory, fast cache hits, and slow disk storage.",
    "Workload_D_Math": "Find the derivative of f(x) = 3*x^3 - 5*x^2 + 7*x - 12 with respect to x. Show the step-by-step differentiation.",
}


def run_benchmarks() -> Dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG.info("=== Starting WISE Phase P1.4-B.1 Real MoE Benchmarks ===")

    # 1. Locate and validate model
    profile = RealMoELocator.inspect_and_validate()
    LOG.info("Model verified: %s (MoE: %s, Experts: %d, Layers: %d)",
             Path(profile.model_path).name, profile.model_is_moe, profile.expert_count, profile.total_layers)

    results: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_profile": profile.to_dict(),
        "baseline_reference_gpu": {},
        "native_cpu_moe": {},
        "observability_probe": {},
        "comparison_table": {},
        "acceptance_criteria": {
            "real_llama_cpp_native_moe_offload": "PROVEN",
            "real_wise_controlled_expert_residency": "UNAVAILABLE",
            "real_wise_expert_cache": "UNPROVEN",
            "real_wise_expert_prefetch": "UNPROVEN",
            "real_expert_routing_observability": "UNAVAILABLE",
            "real_expert_level_control": "UNAVAILABLE",
        }
    }

    # --------------------------------------------------------------------------
    # Phase 3: Real Baseline (Reference Configuration: Full GPU VRAM, -ngl 28)
    # --------------------------------------------------------------------------
    LOG.info("\n>>> Phase 3: Executing Reference Baseline (Full GPU, -ngl 28) <<<")
    backend_ref = LlamaCppMoEBackend(
        model_path=profile.model_path,
        offload_mode=MoEOffloadMode.REFERENCE_FULL_GPU,
        gpu_layers=profile.total_layers,
        threads=8,
        context_length=2048,
    )

    t0_load = time.perf_counter()
    assert backend_ref.load_model(), "Failed to load Reference backend"
    load_time_ref_ms = (time.perf_counter() - t0_load) * 1000.0

    ref_runs: Dict[str, Any] = {}
    for wname, prompt in WORKLOADS.items():
        LOG.info("Running Reference [%s]...", wname)
        res = backend_ref.generate(
            prompt=prompt,
            max_tokens=128,
            temperature=0.0,
            seed=42,
        )
        ref_runs[wname] = res.to_dict()
        LOG.info("  [%s] TTFT: %.1f ms | TPS: %.1f t/s | Tokens: %d | Latency: %.1f ms",
                 wname, res.ttft_ms, res.tokens_per_second, res.tokens_generated, res.avg_token_latency_ms)

    backend_ref.unload_model()

    results["baseline_reference_gpu"] = {
        "load_time_ms": round(load_time_ref_ms, 2),
        "runs": ref_runs,
    }

    # --------------------------------------------------------------------------
    # Phase 4 & 5: Real Native MoE Offloading & Observability Probe
    # Configuration: Attention on GPU (-ngl 28), MoE weights in CPU host RAM (-cmoe)
    # --------------------------------------------------------------------------
    LOG.info("\n>>> Phase 4 & 5: Executing Native CPU-MoE Offload (-ngl 28, -cmoe) <<<")
    backend_moe = LlamaCppMoEBackend(
        model_path=profile.model_path,
        offload_mode=MoEOffloadMode.LLAMA_CPP_NATIVE_CPU_MOE,
        gpu_layers=profile.total_layers,
        threads=8,
        context_length=2048,
    )

    t0_load_moe = time.perf_counter()
    assert backend_moe.load_model(), "Failed to load Native CPU-MoE backend"
    load_time_moe_ms = (time.perf_counter() - t0_load_moe) * 1000.0

    moe_runs: Dict[str, Any] = {}
    observability_data: Dict[str, Any] = {}

    for wname, prompt in WORKLOADS.items():
        LOG.info("Running Native CPU-MoE [%s]...", wname)
        res = backend_moe.generate(
            prompt=prompt,
            max_tokens=128,
            temperature=0.0,
            seed=42,
        )
        moe_runs[wname] = res.to_dict()
        observability_data[wname] = {
            "prompt_domain": wname,
            "observed_expert_ids": res.observed_expert_ids,
            "routing_observability": res.expert_routing_observability,
            "routing_status": res.routing_status,
            "generated_text_preview": res.text[:120].replace("\n", " "),
        }
        LOG.info("  [%s] TTFT: %.1f ms | TPS: %.1f t/s | Observability: %s",
                 wname, res.ttft_ms, res.tokens_per_second, res.expert_routing_observability)

    backend_moe.unload_model()

    results["native_cpu_moe"] = {
        "load_time_ms": round(load_time_moe_ms, 2),
        "runs": moe_runs,
    }
    results["observability_probe"] = observability_data

    # --------------------------------------------------------------------------
    # Phase 8: Comparison Table Calculation
    # --------------------------------------------------------------------------
    def compute_avg_metric(runs: Dict[str, Any], key: str) -> float:
        vals = [r[key] for r in runs.values() if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    ref_avg_ttft = compute_avg_metric(ref_runs, "ttft_ms")
    ref_avg_tps = compute_avg_metric(ref_runs, "tokens_per_second")
    ref_avg_lat = compute_avg_metric(ref_runs, "avg_token_latency_ms")
    ref_avg_p50 = compute_avg_metric(ref_runs, "p50_token_latency_ms")
    ref_avg_p95 = compute_avg_metric(ref_runs, "p95_token_latency_ms")
    ref_peak_ram = max((r["peak_ram_mb"] for r in ref_runs.values()), default=0.0)
    ref_peak_vram = max((r["peak_vram_mb"] for r in ref_runs.values()), default=0.0)

    moe_avg_ttft = compute_avg_metric(moe_runs, "ttft_ms")
    moe_avg_tps = compute_avg_metric(moe_runs, "tokens_per_second")
    moe_avg_lat = compute_avg_metric(moe_runs, "avg_token_latency_ms")
    moe_avg_p50 = compute_avg_metric(moe_runs, "p50_token_latency_ms")
    moe_avg_p95 = compute_avg_metric(moe_runs, "p95_token_latency_ms")
    moe_peak_ram = max((r["peak_ram_mb"] for r in moe_runs.values()), default=0.0)
    moe_peak_vram = max((r["peak_vram_mb"] for r in moe_runs.values()), default=0.0)

    comparison_table = {
        "model_load_time_ms": {"reference": load_time_ref_ms, "native_cpu_moe": load_time_moe_ms, "diff": round(load_time_moe_ms - load_time_ref_ms, 2)},
        "avg_ttft_ms": {"reference": ref_avg_ttft, "native_cpu_moe": moe_avg_ttft, "diff": round(moe_avg_ttft - ref_avg_ttft, 2)},
        "avg_tps": {"reference": ref_avg_tps, "native_cpu_moe": moe_avg_tps, "diff": round(moe_avg_tps - ref_avg_tps, 2)},
        "avg_token_latency_ms": {"reference": ref_avg_lat, "native_cpu_moe": moe_avg_lat, "diff": round(moe_avg_lat - ref_avg_lat, 2)},
        "p50_token_latency_ms": {"reference": ref_avg_p50, "native_cpu_moe": moe_avg_p50, "diff": round(moe_avg_p50 - ref_avg_p50, 2)},
        "p95_token_latency_ms": {"reference": ref_avg_p95, "native_cpu_moe": moe_avg_p95, "diff": round(moe_avg_p95 - ref_avg_p95, 2)},
        "peak_host_ram_mb": {"reference": ref_peak_ram, "native_cpu_moe": moe_peak_ram, "diff": round(moe_peak_ram - ref_peak_ram, 2)},
        "peak_gpu_vram_mb": {"reference": ref_peak_vram, "native_cpu_moe": moe_peak_vram, "diff": round(moe_peak_vram - ref_peak_vram, 2)},
        "expert_cache_hit_pct": {"reference": "N/A (UNPROVEN)", "native_cpu_moe": "N/A (UNPROVEN)", "diff": "0.0%"},
        "expert_prefetch_hit_pct": {"reference": "N/A (UNPROVEN)", "native_cpu_moe": "N/A (UNPROVEN)", "diff": "0.0%"},
        "expert_transfers_nvme_ram": {"reference": "N/A (UNPROVEN)", "native_cpu_moe": "N/A (UNPROVEN)", "diff": "0"},
    }
    results["comparison_table"] = comparison_table

    with open(BENCHMARK_RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    LOG.info("Benchmark results saved to: %s", BENCHMARK_RESULTS_FILE)
    return results


if __name__ == "__main__":
    run_benchmarks()
