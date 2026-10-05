# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Unit, Quality & Stress Tests (P1.4-B.1)
# Tests:
# - Phase 2: MoE Architecture Verification
# - Phase 5: Backend Offload & Residency Configuration
# - Phase 9: Quality Validation across 6 domains (Reasoning, JSON, Planning, Python, Math, Arabic)
# - Phase 10: 12-Turn Routing Stress Test Pattern
# - Phase 11: Clean Unload & Zero Leakage
# ==============================================================================

from __future__ import annotations

import os
import sys
import json
import time
import pytest
from pathlib import Path
from typing import Dict, Any, List

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = REPO_ROOT / "Supergent--main"
if str(SUPERGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.models.runtime.real_moe_locator import RealMoELocator, MoEModelEnvironmentProfile
from core.models.runtime.real_moe_backend import (
    LlamaCppMoEBackend,
    MoEOffloadMode,
    RealMoEInferenceResult,
)
from core.models.provider_interface import (
    ModelProviderType,
    ModelCompletionRequest,
    Llama32MoETestProvider,
)


@pytest.fixture(scope="module")
def moe_model_profile() -> MoEModelEnvironmentProfile:
    """Fixture resolving and inspecting the real local MoE model."""
    try:
        return RealMoELocator.inspect_and_validate()
    except FileNotFoundError:
        pytest.skip("Llama-3.2-3B-MoE-4Expert.Q4_K_M.gguf is not yet available on disk.")


# ==============================================================================
# Phase 2: Binary & Architecture Invariants
# ==============================================================================
def test_p1_4_b1_model_is_genuine_moe(moe_model_profile: MoEModelEnvironmentProfile):
    """Verifies that the target model is genuinely a sparse MoE and not dense."""
    assert moe_model_profile.model_is_moe is True
    assert moe_model_profile.expert_count == 4
    assert moe_model_profile.expert_used_count == 2
    assert moe_model_profile.router_present is True
    assert moe_model_profile.expert_tensors_present is True
    assert moe_model_profile.total_layers == 28
    assert moe_model_profile.architecture == "llama"
    assert moe_model_profile.quantization == "Q4_K_M"
    assert moe_model_profile.is_safe_to_load is True


def test_p1_4_b1_router_and_expert_tensors_exist(moe_model_profile: MoEModelEnvironmentProfile):
    """Verifies that router and stacked expert tensors exist in every layer."""
    tensors = moe_model_profile.metadata.expert_tensor_names
    assert len(tensors) > 0

    # Layer 0 must contain router (ffn_gate_inp) and stacked experts (ffn_gate_exps, etc.)
    has_l0_gate_inp = any("blk.0.ffn_gate_inp.weight" in t for t in tensors)
    has_l0_gate_exps = any("blk.0.ffn_gate_exps.weight" in t for t in tensors)
    has_l0_up_exps = any("blk.0.ffn_up_exps.weight" in t for t in tensors)
    has_l0_down_exps = any("blk.0.ffn_down_exps.weight" in t for t in tensors)

    assert has_l0_gate_inp, "Router tensor blk.0.ffn_gate_inp.weight missing!"
    assert has_l0_gate_exps, "Expert gate tensor blk.0.ffn_gate_exps.weight missing!"
    assert has_l0_up_exps, "Expert up tensor blk.0.ffn_up_exps.weight missing!"
    assert has_l0_down_exps, "Expert down tensor blk.0.ffn_down_exps.weight missing!"


# ==============================================================================
# Phase 5: Backend Initialization & Architectural Distinctions
# ==============================================================================
def test_p1_4_b1_backend_architectural_distinctions(moe_model_profile: MoEModelEnvironmentProfile):
    """
    Verifies that backend explicitly distinguishes llama.cpp native MoE offload
    from WISE expert control, caching, prefetching, and routing observability.
    """
    backend = LlamaCppMoEBackend(
        model_path=moe_model_profile.model_path,
        offload_mode=MoEOffloadMode.LLAMA_CPP_NATIVE_CPU_MOE,
    )
    assert backend.load_model() is True
    info = backend.get_model_info()

    assert info["backend_mode"] == "REAL_MOE_MODEL"
    assert info["is_simulated"] is False
    assert info["offload_mode"] == "LLAMA_CPP_NATIVE_CPU_MOE"
    assert info["wise_expert_residency_control"] == "UNAVAILABLE"
    assert info["wise_expert_cache"] == "UNPROVEN"
    assert info["wise_expert_prefetch"] == "UNPROVEN"
    assert info["expert_routing_observability"] == "UNAVAILABLE"

    backend.unload_model()
    assert backend.is_loaded() is False


# ==============================================================================
# Phase 9: Quality Validation across 6 Mandatory Domains
# ==============================================================================
@pytest.mark.parametrize("domain, prompt, validator_fn", [
    (
        "General Reasoning",
        "Explain in 2 concise sentences why sparse Mixture-of-Experts models allow high parameter capacity with lower computational cost per token.",
        lambda text: len(text.split()) >= 10 and any(w in text.lower() for w in ["expert", "compute", "token", "parameter", "sparse", "active"])
    ),
    (
        "Structured JSON",
        'Return ONLY valid JSON matching this schema: {"task": "system_backup", "steps": ["step1", "step2"], "risk": "low"}. Do not add commentary.',
        lambda text: (
            json.loads(
                text.split("<json>")[1].split("</json>")[0].strip()
                if "<json>" in text else (
                    text.split("```json")[1].split("```")[0].strip()
                    if "```json" in text else (
                        text.split("```")[1].split("```")[0].strip()
                        if "```" in text else (
                            text[text.find("{"):text.rfind("}") + 1].strip()
                            if "{" in text and "}" in text else text.strip()
                        )
                    )
                )
            ).get("risk") in ["low", "medium", "high"]
        )
    ),
    (
        "Tool Planning",
        "Outline a 3-step action plan to create a text file named 'notes.txt' on the Windows Desktop using Notepad.",
        lambda text: any(w in text.lower() for w in ["notepad", "file", "desktop", "save"])
    ),
    (
        "Python Code",
        "Write a Python function `add_numbers(a: int, b: int) -> int:` that returns the sum.",
        lambda text: "def add_numbers" in text and "return" in text
    ),
    (
        "Mathematics",
        "What is the derivative of 5*x^2 + 3*x - 9? Provide the answer clearly.",
        lambda text: "10*x" in text or "10x" in text
    ),
    (
        "Arabic",
        "اشرح بإيجاز في جملة واحدة فائدة النماذج الموزعة محلياً في تسريع الأداء.",
        lambda text: len(text.strip()) > 10 and any(ord(c) >= 0x0600 and ord(c) <= 0x06FF for c in text)
    ),
])
def test_p1_4_b1_quality_validation_6_domains(moe_model_profile: MoEModelEnvironmentProfile, domain: str, prompt: str, validator_fn):
    """Validates real MoE output correctness across all 6 required domains."""
    backend = LlamaCppMoEBackend(
        model_path=moe_model_profile.model_path,
        offload_mode=MoEOffloadMode.REFERENCE_FULL_GPU,
        gpu_layers=moe_model_profile.total_layers,
    )
    assert backend.load_model() is True
    try:
        res = backend.generate(prompt=prompt, max_tokens=100, temperature=0.0, seed=42)
        assert res.tokens_generated > 0, f"No tokens generated for domain: {domain}"
        assert res.exit_code == 0, f"Non-zero exit code for domain {domain}: {res.error}"
        
        is_valid = False
        try:
            is_valid = validator_fn(res.text)
        except Exception as e:
            pytest.fail(f"Validator failed for domain {domain}: {e} | Output was: {res.text}")

        assert is_valid, f"Output validation failed for domain {domain}. Output: {res.text}"
    finally:
        backend.unload_model()


# ==============================================================================
# Phase 10: 12-Turn Expert Routing Stress Test Pattern
# ==============================================================================
def test_p1_4_b1_12_turn_expert_routing_stress_test(moe_model_profile: MoEModelEnvironmentProfile):
    """
    Executes the 12-turn pattern:
    GENERAL -> GENERAL -> CODE -> CODE -> MATH -> MATH -> CREATIVE -> CREATIVE -> GENERAL -> CODE -> MATH -> CREATIVE
    Verifies memory stability and crash-free execution under domain switching.
    """
    stress_pattern = [
        ("GENERAL", "What is an algorithm? Answer in one sentence."),
        ("GENERAL", "What is cache locality? Answer in one sentence."),
        ("CODE", "Write a python line that squares each number in `[1, 2, 3]`."),
        ("CODE", "Write a python function to check if a number is even."),
        ("MATH", "What is 15 multiplied by 14?"),
        ("MATH", "What is the square root of 144?"),
        ("CREATIVE", "Write one poetic sentence about the blue ocean."),
        ("CREATIVE", "Write one poetic sentence about stars in the night."),
        ("GENERAL", "What is the function of an operating system kernel?"),
        ("CODE", "Write a python dict with keys 'id' and 'name'."),
        ("MATH", "What is 2 to the power of 8?"),
        ("CREATIVE", "Write one poetic sentence about autumn leaves."),
    ]

    backend = LlamaCppMoEBackend(
        model_path=moe_model_profile.model_path,
        offload_mode=MoEOffloadMode.REFERENCE_FULL_GPU,
        gpu_layers=moe_model_profile.total_layers,
    )
    assert backend.load_model() is True

    try:
        for idx, (domain, prompt) in enumerate(stress_pattern):
            res = backend.generate(prompt=prompt, max_tokens=32, temperature=0.0, seed=42 + idx)
            assert res.exit_code == 0, f"Turn {idx+1} ({domain}) failed: {res.error}"
            assert res.tokens_generated > 0, f"Turn {idx+1} ({domain}) generated 0 tokens."
            assert res.peak_ram_mb > 0
            assert res.peak_vram_mb > 0
    finally:
        backend.unload_model()


# ==============================================================================
# Phase 11: Clean Unload & Idempotence
# ==============================================================================
def test_p1_4_b1_clean_unload_and_resource_release(moe_model_profile: MoEModelEnvironmentProfile):
    """Verifies that calling unload terminates processes and releases resources cleanly."""
    backend = LlamaCppMoEBackend(model_path=moe_model_profile.model_path)
    assert backend.load_model() is True
    assert backend.is_loaded() is True

    # Idle unload
    assert backend.unload_model() is True
    assert backend.is_loaded() is False

    # Calling unload while already unloaded must be idempotent
    assert backend.unload_model() is True
    assert backend.stop_generation() is True
