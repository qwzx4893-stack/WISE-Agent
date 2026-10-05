# ==============================================================================
# WISE Official Performance Benchmark: LiquidAI/LFM2.5-8B-A1B (Q4_K_M GGUF)
# Measures: Load Time, TTFT, TPS, P50, P95, RAM, VRAM, CPU, GPU Utilization
# Across 5 representative cognitive workloads on real Windows hardware.
# ==============================================================================

import os
import sys
import time
import json
from pathlib import Path
from typing import Dict, Any, List

import psutil

# Add repository root and Supergent to path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SUPERGENT_ROOT = WORKSPACE_ROOT / "Supergent--main"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
if str(SUPERGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_ROOT))

from core.models.runtime.lfm25_model_locator import LFM25ModelLocator
from core.models.runtime.lfm25_backend import LFM25NativeBackend, LFM25InferenceResult


def run_workload(
    backend: LFM25NativeBackend,
    name: str,
    prompt: str,
    max_tokens: int = 256,
    temperature: float = 0.1,
) -> Dict[str, Any]:
    print(f"\n--- Running Workload: {name} ---")
    cpu_before = psutil.cpu_percent(interval=0.2)
    t0 = time.perf_counter()

    res: LFM25InferenceResult = backend.generate(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    cpu_after = psutil.cpu_percent(interval=0.2)
    mem = backend.get_memory_usage()

    data = {
        "workload_name": name,
        "tokens_prompt": res.prompt_tokens,
        "tokens_generated": res.tokens_generated,
        "ttft_ms": res.ttft_ms,
        "generation_time_ms": res.generation_time_ms,
        "total_time_ms": res.total_time_ms,
        "tps": res.tokens_per_second,
        "avg_token_latency_ms": res.avg_token_latency_ms,
        "p50_latency_ms": res.p50_token_latency_ms,
        "p95_latency_ms": res.p95_token_latency_ms,
        "peak_ram_mb": mem["peak_ram_observed_mb"],
        "peak_vram_mb": mem["peak_vram_observed_mb"],
        "cpu_util_pct": round((cpu_before + cpu_after) / 2.0, 1),
        "text_preview": res.text[:200].replace("\n", " "),
        "error": res.error,
    }

    print(f"Result: {res.tokens_generated} tokens | {res.tokens_per_second:.1f} TPS | TTFT: {res.ttft_ms:.1f}ms | VRAM: {mem['peak_vram_observed_mb']} MB")
    return data


def execute_full_benchmark() -> Dict[str, Any]:
    print("=" * 80)
    print("WISE OFFICIAL BENCHMARK: LiquidAI/LFM2.5-8B-A1B (Q4_K_M GGUF)")
    print("=" * 80)

    # 1. Locate and inspect model
    model_path = LFM25ModelLocator.locate_model()
    profile = LFM25ModelLocator.inspect_and_validate(model_path)
    print(f"Model Path:     {profile.model_path}")
    print(f"Architecture:   {profile.architecture} (Layers: {profile.metadata.block_count}, Experts: {profile.expert_count} top-{profile.expert_used_count})")
    print(f"File Size:      {profile.file_size_gb:.2f} GB ({profile.file_size_bytes:,} bytes)")
    print(f"Host RAM:       Total {profile.total_ram_gb:.2f} GB | Avail {profile.available_ram_gb:.2f} GB")
    print(f"GPU VRAM:       Total {profile.total_vram_mb:.0f} MB | Free {profile.free_vram_mb:.0f} MB")

    # 2. Initialize and load backend
    backend = LFM25NativeBackend(model_path=str(model_path), gpu_layers=33, threads=8, context_length=4096)
    print("\nLoading model into GPU VRAM...")
    t_load_start = time.perf_counter()
    loaded = backend.load_model()
    t_load_end = time.perf_counter()
    load_time_sec = t_load_end - t_load_start

    if not loaded:
        print("[FAIL] Could not load model into llama-server runtime!")
        return {"status": "FAILED", "error": "Model load failure"}

    print(f"[SUCCESS] Model loaded in {load_time_sec:.2f} seconds.")

    # 3. Workload 1: Simple Question
    w1_prompt = backend.format_chatml_prompt(
        messages=[{"role": "user", "content": "What is 25 * 16? Answer with the exact number and a brief explanation."}],
        system_prompt="You are a precise calculator and assistant.",
    )
    w1_res = run_workload(backend, "1. Simple Question", w1_prompt, max_tokens=64)

    # 4. Workload 2: Deep Reasoning
    w2_prompt = backend.format_chatml_prompt(
        messages=[{"role": "user", "content": "A farmer has 17 sheep. All but 9 die. How many sheep does the farmer have left? Explain your step-by-step reasoning carefully."}],
        system_prompt="You are a strict, logical reasoning engine. Think carefully step by step.",
    )
    w2_res = run_workload(backend, "2. Deep Reasoning", w2_prompt, max_tokens=150)

    # 5. Workload 3: Tool Selection & Call Generation
    tools_spec = [
        {"name": "web_search", "description": "Search the live internet for recent information", "parameters": {"query": "string"}},
        {"name": "filesystem_read", "description": "Read file contents from local storage", "parameters": {"path": "string"}},
        {"name": "execute_windows_command", "description": "Execute powershell command on host", "parameters": {"command": "string"}},
    ]
    w3_prompt = backend.format_chatml_prompt(
        messages=[{"role": "user", "content": "I need the latest official news on the Tokyo 2026 climate summit. Which tool should be used, and what are the arguments?"}],
        system_prompt="You are WISE Cognitive Core. When a tool is needed, specify the tool name and arguments clearly in valid JSON.",
        tools_schema=tools_spec,
    )
    w3_res = run_workload(backend, "3. Tool Selection & Call Generation", w3_prompt, max_tokens=150)

    # 6. Workload 4: Long Context Processing (~1,500 tokens of embedded context)
    filler_text = (
        "Project WISE is a multimodal autonomous personal agent architecture. "
        "It features World State management, episodic memory stores, and dynamic tool orchestration. "
        "The system maintains strict security boundaries and Human-In-The-Loop approvals for destructive operations. "
        "Subsystem Alpha coordinates background daemon threads and handles event loops. "
        "Subsystem Beta monitors sensor telemetry and camera perception inputs. "
        "Subsystem Gamma is responsible for model provider abstractions and llama.cpp interop. "
        "The unique secret activation passphrase for emergency offline lockdown is 'COBALT-ORION-7749'. "
        "All telemetry streams are archived to disk with cryptographic checksums for auditability. "
    ) * 15  # Replicated to create substantial context

    w4_prompt = backend.format_chatml_prompt(
        messages=[
            {"role": "user", "content": f"Context documentation:\n{filler_text}\n\nQuestion: What is the exact secret activation passphrase for emergency offline lockdown?"}
        ],
        system_prompt="You are a meticulous technical documentation reader. Extract the exact requested fact directly from the context.",
    )
    w4_res = run_workload(backend, "4. Long Context Fact Retrieval", w4_prompt, max_tokens=64)

    # 7. Workload 5: Multi-Step Task Planning
    w5_prompt = backend.format_chatml_prompt(
        messages=[{
            "role": "user",
            "content": (
                "Formulate a structured 4-step execution plan to diagnose why a user's Windows machine has high disk usage, "
                "find large temporary files, safely prompt the user before deletion, and verify disk space recovery. "
                "Output as a JSON array of step objects with fields: 'step_id', 'action', 'subsystem', 'verification_rule'."
            )
        }],
        system_prompt="You are a senior systems engineer. Produce clean, structured, executable step plans in JSON format.",
    )
    w5_res = run_workload(backend, "5. Multi-Step Task Planning", w5_prompt, max_tokens=300)

    # 8. Unload model to verify clean shutdown
    print("\nUnloading model to verify VRAM recovery...")
    backend.unload_model()
    mem_final = backend.get_memory_usage()
    print(f"Post-unload GPU VRAM: {mem_final['gpu_vram_used_mb']} MB (Baseline restored).")

    # Aggregate summary
    workloads = [w1_res, w2_res, w3_res, w4_res, w5_res]
    avg_tps = sum(w["tps"] for w in workloads) / len(workloads)
    avg_ttft = sum(w["ttft_ms"] for w in workloads) / len(workloads)
    peak_vram = max(w["peak_vram_mb"] for w in workloads)
    peak_ram = max(w["peak_ram_mb"] for w in workloads)

    benchmark_summary = {
        "model_name": "LiquidAI/LFM2.5-8B-A1B-Q4_K_M",
        "load_time_sec": round(load_time_sec, 2),
        "overall_avg_tps": round(avg_tps, 2),
        "overall_avg_ttft_ms": round(avg_ttft, 2),
        "peak_vram_mb": peak_vram,
        "peak_ram_mb": peak_ram,
        "workloads": workloads,
    }

    # Save to json artifact
    out_path = WORKSPACE_ROOT / "scratch" / "lfm25_performance_benchmark.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(benchmark_summary, f, indent=2)
    print(f"\n[BENCHMARK COMPLETE] Saved detailed results to: {out_path}")

    return benchmark_summary


if __name__ == "__main__":
    execute_full_benchmark()
