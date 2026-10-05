"""
WISE Model Router & Orchestrator Subsystem.
Implements canonical resource-aware workload routing, model capability registry,
bounded context management, and graceful provider failure recovery.

Workloads:
  - INTENT_CLASSIFICATION
  - SIMPLE_CONVERSATION
  - PLANNING
  - TOOL_SELECTION
  - RECOVERY_REASONING
  - SUMMARIZATION
  - CODING
  - RESEARCH_SYNTHESIS
  - VISUAL_GROUNDING
  - VERIFICATION_REASONING
  - LONG_CONTEXT_ANALYSIS
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import psutil

from core.contracts import (
    ModelWorkloadType,
    ModelProviderType,
    ModelRequest,
    ModelResponse,
    ModelRuntimeTelemetry,
)
from core.models.provider_interface import (
    BaseModelProvider,
    ModelCompletionRequest,
    ModelCompletionResponse,
    get_model_provider,
    UnavailableModelProvider,
    SimulatedTestProvider,
)

LOG = logging.getLogger("WISE.Models.Router")


@dataclass
class ModelCapability:
    """Canonical Model Capability definition in the registry."""
    model_name: str
    provider_type: ModelProviderType
    supports_text: bool = True
    supports_tools: bool = False
    supports_structured_json: bool = True
    supports_vision: bool = False
    supports_coding: bool = False
    supports_long_context: bool = False
    is_local: bool = False
    estimated_memory_gb: float = 0.0
    context_window: int = 4096
    latency_profile: str = "BALANCED"  # FAST | BALANCED | HIGH_REASONING
    availability_status: str = "READY"  # READY | ENVIRONMENT_BLOCKED | UNAVAILABLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_name": self.model_name,
            "provider_type": self.provider_type.value if hasattr(self.provider_type, "value") else str(self.provider_type),
            "supports_text": self.supports_text,
            "supports_tools": self.supports_tools,
            "supports_structured_json": self.supports_structured_json,
            "supports_vision": self.supports_vision,
            "supports_coding": self.supports_coding,
            "supports_long_context": self.supports_long_context,
            "is_local": self.is_local,
            "estimated_memory_gb": round(self.estimated_memory_gb, 2),
            "context_window": self.context_window,
            "latency_profile": self.latency_profile,
            "availability_status": self.availability_status,
        }


class ModelRouter:
    """
    Intelligent Resource-Aware Model Router.
    Routes cognitive requests based on workload class, real hardware metrics,
    and capability match, with anti-bloat bounded context and failover recovery.
    """

    def __init__(self) -> None:
        self._registry: Dict[str, ModelCapability] = {}
        self._init_capability_registry()

    def _init_capability_registry(self) -> None:
        """Populates the canonical capability registry."""
        # 1. Local LFM 2.5 8B Q4 GGUF
        self._registry["lfm2.5-8b-gguf"] = ModelCapability(
            model_name="LFM2.5-8B-A1B-Q4_K_M.gguf",
            provider_type=ModelProviderType.LFM2_5_COGNITIVE_CORE,
            supports_text=True,
            supports_tools=True,
            supports_structured_json=True,
            supports_vision=False,
            supports_coding=True,
            supports_long_context=False,
            is_local=True,
            estimated_memory_gb=5.8,
            context_window=4096,
            latency_profile="FAST",
            availability_status="READY",
        )

        # 2. Cloud OpenAI-Compatible (OpenAI, Groq, DeepSeek)
        self._registry["cloud-openai-compatible"] = ModelCapability(
            model_name="gpt-4o-or-compatible",
            provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
            supports_text=True,
            supports_tools=True,
            supports_structured_json=True,
            supports_vision=True,
            supports_coding=True,
            supports_long_context=True,
            is_local=False,
            estimated_memory_gb=0.0,
            context_window=128000,
            latency_profile="HIGH_REASONING",
            availability_status="READY",
        )

        # 3. Vision Provider (Local / Cloud VLM)
        self._registry["vision-vlm"] = ModelCapability(
            model_name="qwen-vl-or-compatible",
            provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
            supports_text=True,
            supports_tools=False,
            supports_structured_json=True,
            supports_vision=True,
            supports_coding=False,
            supports_long_context=False,
            is_local=False,
            estimated_memory_gb=0.0,
            context_window=8192,
            latency_profile="BALANCED",
            availability_status="READY",
        )

    def get_system_resource_status(self) -> Dict[str, Any]:
        """Inspects real host memory and pressure metrics."""
        vm = psutil.virtual_memory()
        total_gb = vm.total / (1024 ** 3)
        avail_gb = vm.available / (1024 ** 3)
        percent = vm.percent

        vram_avail = None
        try:
            import torch
            if torch.cuda.is_available():
                free_b, total_b = torch.cuda.mem_get_info()
                vram_avail = free_b / (1024 ** 3)
        except Exception:
            pass

        return {
            "total_ram_gb": total_gb,
            "available_ram_gb": avail_gb,
            "ram_percent": percent,
            "vram_available_gb": vram_avail,
            "can_safely_run_local_8b": avail_gb >= 5.8 and percent < 88.0,
        }

    def route_workload(
        self,
        workload: Union[ModelWorkloadType, str],
        context_tokens: int = 1000,
        require_vision: bool = False,
        offline_only: bool = False,
    ) -> Tuple[str, Optional[BaseModelProvider], str]:
        """
        Determines the optimal model provider for a cognitive workload.
        Returns: (model_key, provider_instance, reason)
        """
        w_type = workload.value if hasattr(workload, "value") else str(workload)
        res_status = self.get_system_resource_status()

        # Offline / Privacy constraint
        if offline_only:
            if res_status["can_safely_run_local_8b"]:
                try:
                    prov = get_model_provider(preferred_type=ModelProviderType.LFM2_5_COGNITIVE_CORE)
                    return "lfm2.5-8b-gguf", prov, "Selected local LFM2.5 for offline privacy with safe host RAM"
                except Exception:
                    pass
            return "unavailable", UnavailableModelProvider(
                reason=f"Offline requested but host available RAM ({res_status['available_ram_gb']:.2f}GB) is below 5.8GB safety threshold"
            ), "CONNECTED_BUT_ENVIRONMENT_BLOCKED"

        # Vision-required workloads
        if require_vision or w_type == ModelWorkloadType.VISUAL_GROUNDING.value:
            # Check for cloud vision endpoint
            try:
                prov = get_model_provider(preferred_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE)
                if prov and not isinstance(prov, UnavailableModelProvider):
                    return "vision-vlm", prov, "Selected vision-capable cloud endpoint for visual grounding"
            except Exception:
                pass
            return "unavailable", UnavailableModelProvider(
                reason="Visual grounding requested but no active vision model is configured or connected"
            ), "UNAVAILABLE"

        # Fast reactive workloads: INTENT_CLASSIFICATION, TOOL_SELECTION, SIMPLE_CONVERSATION
        if w_type in (
            ModelWorkloadType.INTENT_CLASSIFICATION.value,
            ModelWorkloadType.SIMPLE_CONVERSATION.value,
            ModelWorkloadType.TOOL_SELECTION.value,
        ):
            # Prefer fast local if safe, else cloud, else standard
            if res_status["can_safely_run_local_8b"]:
                prov = get_model_provider(preferred_type=ModelProviderType.LFM2_5_COGNITIVE_CORE)
                if prov and not isinstance(prov, UnavailableModelProvider):
                    return "lfm2.5-8b-gguf", prov, "Selected fast local LFM2.5 for reactive intent/conversation"
            prov = get_model_provider()
            return "default_provider", prov, "Selected default provider for reactive workload"

        # Heavy reasoning / planning / long-context: PLANNING, RECOVERY_REASONING, CODING, RESEARCH_SYNTHESIS
        if w_type in (
            ModelWorkloadType.PLANNING.value,
            ModelWorkloadType.RECOVERY_REASONING.value,
            ModelWorkloadType.CODING.value,
            ModelWorkloadType.RESEARCH_SYNTHESIS.value,
            ModelWorkloadType.LONG_CONTEXT_ANALYSIS.value,
        ):
            prov = get_model_provider(preferred_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE)
            if prov and not isinstance(prov, UnavailableModelProvider):
                return "cloud-openai-compatible", prov, f"Selected high-reasoning provider for workload '{w_type}'"
            prov = get_model_provider()
            return "default_provider", prov, f"Selected active provider for workload '{w_type}'"

        # Default fallback
        prov = get_model_provider()
        return "default_provider", prov, "Selected canonical active model provider"

    def format_bounded_context(
        self,
        goal: str,
        active_milestone: Optional[str] = None,
        active_subgoal: Optional[str] = None,
        recent_observations: Optional[List[str]] = None,
        recent_artifacts: Optional[List[str]] = None,
        max_chars: int = 4000,
    ) -> str:
        """
        Builds a compressed, bounded context representation to prevent context window sprawl.
        """
        parts = [f"### [Active Goal]: {goal}"]
        if active_milestone:
            parts.append(f"- **Current Milestone**: {active_milestone}")
        if active_subgoal:
            parts.append(f"- **Active Subgoal**: {active_subgoal}")
        if recent_artifacts:
            parts.append("- **Verified Artifacts**: " + ", ".join(recent_artifacts[-5:]))
        if recent_observations:
            parts.append("- **Recent Observations**:")
            for obs in recent_observations[-4:]:
                parts.append(f"  • {obs[:120]}")

        full_text = "\n".join(parts)
        if len(full_text) > max_chars:
            return full_text[:max_chars] + "\n...[context compressed]"
        return full_text

    def execute_with_failover(
        self,
        request: ModelCompletionRequest,
        workload: Union[ModelWorkloadType, str] = ModelWorkloadType.SIMPLE_CONVERSATION,
    ) -> ModelCompletionResponse:
        """
        Compatibility name retained; execute the selected provider exactly once.
        Provider failure never selects a secondary model behind the owner.
        """
        model_key, primary_prov, reason = self.route_workload(workload)
        if not primary_prov:
            return ModelCompletionResponse(
                text="",
                error=f"No provider available for workload {workload}: {reason}",
                is_simulated=False,
                provider_type=ModelProviderType.UNAVAILABLE,
            )

        # First attempt
        try:
            return primary_prov.generate(request)
        except Exception as e:
            LOG.warning("Selected provider %s failed (%s); no automatic failover", model_key, type(e).__name__)

        return ModelCompletionResponse(
            text="",
            error="The selected model failed to generate completion; no alternate provider was called",
            is_simulated=False,
            provider_type=ModelProviderType.UNAVAILABLE,
        )


_GLOBAL_MODEL_ROUTER: Optional[ModelRouter] = None


def get_model_router() -> ModelRouter:
    global _GLOBAL_MODEL_ROUTER
    if _GLOBAL_MODEL_ROUTER is None:
        _GLOBAL_MODEL_ROUTER = ModelRouter()
    return _GLOBAL_MODEL_ROUTER
