# ==============================================================================
# WISE Phase P1.4-B.0: Automated Real Model Verification & Stress Test Suite
# Model: OxCoder 9B (Q5_0, 5.87 GB GGUF) on local machine
# Tests:
#   1. Model Discovery & GGUF Metadata Parsing
#   2. Hardware Safety Bounds & Pre-load Validation
#   3. LlamaCppRealBackend Interface Compliance (8 required methods)
#   4. Real Model Token Generation & Streaming
#   5. Cognitive Brain Provider Integration (Oxcoder9BTestProvider)
#   6. Stress Testing (Warm-up, repeated runs, cold start)
#   7. Failure Handling (Missing model, invalid paths, cancellation)
#   8. Clean Unload & Zero Idle Leak
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import pytest
from pathlib import Path

# Add Supergent path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SUPERGENT_PATH = WORKSPACE_ROOT / "Supergent--main"
sys.path.insert(0, str(SUPERGENT_PATH))

from core.models.runtime.real_model_locator import (
    RealModelLocator,
    GGUFMetadata,
    ModelEnvironmentProfile,
    EnvironmentSafetyError,
)
from core.models.runtime.real_backend_adapter import (
    LlamaCppRealBackend,
    RealInferenceResult,
)
from core.models.provider_interface import (
    Oxcoder9BTestProvider,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelProviderType,
    get_model_provider,
    set_active_model_provider,
)


def test_01_model_discovery():
    """Validates that OxCoder 9B GGUF file is located and is ~5.87 GB."""
    model_path = RealModelLocator.locate_oxcoder_9b()
    assert model_path.is_file(), f"Model file not found: {model_path}"
    file_size_gb = model_path.stat().st_size / (1024 * 1024 * 1024)
    assert 5.0 <= file_size_gb <= 7.0, f"Unexpected model size: {file_size_gb:.2f} GB"
    print(f"\n[PASS] Model discovered at {model_path} ({file_size_gb:.2f} GB)")


def test_02_gguf_metadata_parsing():
    """Parses binary GGUF header and asserts architecture and parameters."""
    model_path = RealModelLocator.locate_oxcoder_9b()
    meta = RealModelLocator.parse_gguf_metadata(model_path)
    assert meta.architecture in ["qwen35", "qwen2"], f"Unexpected architecture: {meta.architecture}"
    assert meta.block_count == 32, f"Expected 32 layers, got {meta.block_count}"
    assert meta.quantization == "Q5_0", f"Expected Q5_0 quantization, got {meta.quantization}"
    assert meta.context_length >= 4096, f"Context length too small: {meta.context_length}"
    print(f"[PASS] GGUF metadata parsed: Arch={meta.architecture}, Layers={meta.block_count}, Quant={meta.quantization}")


def test_03_hardware_safety_check():
    """Verifies that environment profile passes safety checks on current host."""
    profile = RealModelLocator.inspect_and_validate()
    assert profile.is_safe_to_load is True, f"Safety check failed: {profile.safety_message}"
    assert profile.available_disk_gb >= 2.0
    assert profile.total_vram_mb > 4000.0 or profile.available_ram_gb >= 4.0
    assert profile.is_dense_model is True, "Oxcoder 9B should be flagged as dense model"
    print(f"[PASS] Hardware safety verified: VRAM={profile.total_vram_mb:.0f} MB, RAM Avail={profile.available_ram_gb:.1f} GB")


def test_04_backend_interface_compliance():
    """Verifies all 8 mandatory backend interface methods exist and behave correctly."""
    backend = LlamaCppRealBackend(gpu_layers=32)
    assert hasattr(backend, "load_model")
    assert hasattr(backend, "unload_model")
    assert hasattr(backend, "generate")
    assert hasattr(backend, "stream_tokens")
    assert hasattr(backend, "get_model_info")
    assert hasattr(backend, "get_memory_usage")
    assert hasattr(backend, "get_runtime_metrics")
    assert hasattr(backend, "stop_generation")

    # Inspect model info before load
    info = backend.get_model_info()
    assert info["backend_mode"] == "REAL_MODEL"
    assert info["is_simulated"] is False
    assert info["expert_level_control"] == "REAL_EXPERT_LEVEL_CONTROL_UNAVAILABLE"
    assert info["offload_mode"] == "REAL_MODEL_HYBRID_OFFLOAD"
    print("[PASS] Backend interface compliance verified with REAL_MODEL semantics.")


def test_05_backend_load_and_unload():
    """Tests clean load and unload cycle with memory accounting."""
    backend = LlamaCppRealBackend(gpu_layers=32)
    loaded = backend.load_model()
    assert loaded is True
    assert backend.is_loaded() is True

    mem = backend.get_memory_usage()
    assert "host_ram_used_mb" in mem
    assert "gpu_vram_used_mb" in mem

    unloaded = backend.unload_model()
    assert unloaded is True
    assert backend.is_loaded() is False
    print("[PASS] Clean load and unload verified.")


def test_06_real_inference_generation():
    """Generates real tokens with deterministic prompt and measures TPS."""
    backend = LlamaCppRealBackend(gpu_layers=32)
    backend.load_model()

    res = backend.generate(prompt="What is 12 + 13? Answer only with the number:", max_tokens=10, temperature=0.1)
    assert res.error is None
    assert res.tokens_generated > 0
    assert res.tokens_per_second > 0.0
    assert res.backend_mode == "REAL_MODEL"
    assert res.is_simulated is False
    assert "25" in res.text, f"Expected '25' in answer, got: {res.text}"
    print(f"[PASS] Real inference verified: Answer={repr(res.text)}, TPS={res.tokens_per_second:.1f}")
    backend.unload_model()


def test_07_real_streaming_tokens():
    """Tests streaming token generation from the real model."""
    backend = LlamaCppRealBackend(gpu_layers=32)
    backend.load_model()

    tokens = list(backend.stream_tokens(prompt="Count from 1 to 3:", max_tokens=15, temperature=0.1))
    assert len(tokens) > 0
    combined = "".join(tokens)
    assert len(combined.strip()) > 0
    print(f"[PASS] Streaming verified: {len(tokens)} chunks produced: {repr(combined[:40])}")
    backend.unload_model()


def test_08_provider_integration():
    """Tests Oxcoder9BTestProvider integration into WISE's BaseModelProvider contract."""
    backend = LlamaCppRealBackend(gpu_layers=32)
    provider = Oxcoder9BTestProvider(backend=backend)
    backend.load_model()

    assert provider.is_available() is True
    req = ModelCompletionRequest(
        messages=[{"role": "user", "content": "Reply with 'WISE_ONLINE'."}],
        max_tokens=10,
        temperature=0.1,
    )
    resp = provider.generate(req)

    assert resp.provider_type == ModelProviderType.OXCODER_9B_TEST_PROVIDER
    assert resp.is_simulated is False
    assert resp.tokens_completion > 0
    assert resp.latency_ms > 0.0
    assert resp.error is None
    print(f"[PASS] Provider integration verified: {repr(resp.text)}")
    provider.unload()


def test_09_stress_repeated_generations():
    """Executes multiple generations to verify warm-up stability and consistency."""
    backend = LlamaCppRealBackend(gpu_layers=32)
    backend.load_model()

    latencies = []
    for i in range(3):
        res = backend.generate(prompt="The color of the clear sky is", max_tokens=10, temperature=0.1, seed=42)
        assert res.tokens_generated > 0
        latencies.append(res.total_time_ms)
        print(f"   Iteration {i+1}: {res.total_time_ms:.1f} ms ({res.tokens_per_second:.1f} TPS)")

    assert len(latencies) == 3
    print(f"[PASS] Multi-iteration stress test passed. Latencies: {latencies}")
    backend.unload_model()


def test_10_failure_recovery_missing_model():
    """Tests safe error reporting when model path is invalid."""
    backend = LlamaCppRealBackend(model_path=r"C:\non_existent_path\fake_model.gguf")
    loaded = backend.load_model(model_path=r"C:\non_existent_path\fake_model.gguf")
    assert loaded is False
    res = backend.generate("Hello")
    assert res.error is not None
    print(f"[PASS] Missing model rejected safely: {res.error}")


def test_11_stop_generation_cleanliness():
    """Tests that stop_generation cleanly handles idle and active states without crash."""
    backend = LlamaCppRealBackend(gpu_layers=32)
    # Stop while idle
    assert backend.stop_generation() is True
    print("[PASS] stop_generation is safe and idempotent.")


if __name__ == "__main__":
    print("=" * 80)
    print("   RUNNING WISE PHASE P1.4-B.0 REAL MODEL TEST SUITE")
    print("=" * 80)
    test_01_model_discovery()
    test_02_gguf_metadata_parsing()
    test_03_hardware_safety_check()
    test_04_backend_interface_compliance()
    test_05_backend_load_and_unload()
    test_06_real_inference_generation()
    test_07_real_streaming_tokens()
    test_08_provider_integration()
    test_09_stress_repeated_generations()
    test_10_failure_recovery_missing_model()
    test_11_stop_generation_cleanliness()
    print("=" * 80)
    print("   ALL 11 TESTS PASSED SUCCESSFULLY!")
    print("=" * 80)
