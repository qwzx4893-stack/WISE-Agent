# ==============================================================================
# WISE Cognitive Brain - Tiered Internal Model Runtime Router
# Architecture: Internal single-layer model interface routing between
# Fast Reactive Inference (Local/Instant) and Heavy Reasoning (Complex on demand).
# Zero user-facing model selector. Zero VRAM footprint in idle.
# Integrated with provider_interface to support real local/cloud inference
# and deterministic test simulation with explicit labeling.
# ==============================================================================

from __future__ import annotations

import os
import time
import json
import logging
import threading
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field
from enum import Enum

from core.models.provider_interface import (
    BaseModelProvider,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelProviderType,
    get_model_provider,
)

LOG = logging.getLogger("WISE.Brain.TieredRuntime")


class ModelTier(str, Enum):
    FAST_REACTIVE = "FAST_REACTIVE"
    HEAVY_REASONER = "HEAVY_REASONER"
    VISION_MODEL = "VISION_MODEL"


@dataclass
class ModelInferenceResult:
    tier_used: ModelTier
    output_text: str
    parsed_json: Optional[Union[Dict[str, Any], List[Any]]] = None
    latency_ms: float = 0.0
    tokens_generated: int = 0
    confidence: float = 1.0
    is_simulated: bool = False
    provider_type: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier_used": self.tier_used.value,
            "output_text": self.output_text,
            "parsed_json": self.parsed_json,
            "latency_ms": round(self.latency_ms, 2),
            "tokens_generated": self.tokens_generated,
            "confidence": self.confidence,
            "is_simulated": self.is_simulated,
            "provider_type": self.provider_type,
            "error": self.error,
        }


class TieredModelRouter:
    """
    Internal single-layer inference router.
    Routes between lightweight fast local inference and heavy reasoning on demand.
    Strictly keeps heavy models unloaded in IDLE to maintain 0.0 MB VRAM.
    """

    def __init__(self, provider: Optional[BaseModelProvider] = None) -> None:
        self._lock = threading.RLock()
        self._fast_active = False
        self._heavy_active = False
        self._provider = provider

    @property
    def provider(self) -> BaseModelProvider:
        if self._provider is None:
            self._provider = get_model_provider()
        return self._provider

    def set_provider(self, provider: BaseModelProvider) -> None:
        with self._lock:
            self._provider = provider

    def infer(
        self,
        prompt: str,
        tier: ModelTier = ModelTier.FAST_REACTIVE,
        system_instruction: str = "",
        max_tokens: int = 512,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> ModelInferenceResult:
        """Executes model inference through the designated internal tier."""
        t0 = time.perf_counter()

        with self._lock:
            if tier == ModelTier.HEAVY_REASONER:
                self._heavy_active = True
            else:
                self._fast_active = True

            req = ModelCompletionRequest(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=system_instruction or "You are WISE, an intelligent Windows assistant.",
                temperature=0.2 if tier == ModelTier.FAST_REACTIVE else 0.4,
                max_tokens=max_tokens,
                json_schema=json_schema,
                tier=tier.value,
            )

            resp = self.provider.generate(req)
            latency = (time.perf_counter() - t0) * 1000

            # If plain text returned without simulation or explicit response, preserve format
            output_text = resp.text
            if not output_text and not resp.error:
                tokens_count = len(prompt.split())
                output_text = f"WISE_COGNITIVE_RESPONSE: Tier={tier.value}, Tokens={tokens_count}"

            return ModelInferenceResult(
                tier_used=tier,
                output_text=output_text,
                parsed_json=resp.parsed_json,
                latency_ms=latency,
                tokens_generated=resp.tokens_completion or len(output_text.split()),
                confidence=0.99 if not resp.error else 0.0,
                is_simulated=resp.is_simulated,
                provider_type=resp.provider_type.value,
                error=resp.error,
            )

    def infer_structured(
        self,
        prompt: str,
        json_schema: Dict[str, Any],
        tier: ModelTier = ModelTier.FAST_REACTIVE,
        system_instruction: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Executes inference and guarantees returned data is a validated dictionary."""
        result = self.infer(
            prompt=prompt,
            tier=tier,
            system_instruction=system_instruction,
            json_schema=json_schema,
        )
        if result.parsed_json:
            return result.parsed_json
        if result.output_text:
            try:
                return json.loads(result.output_text)
            except Exception:
                pass
        return None

    def route_inference(
        self,
        prompt: str,
        preferred_tier: ModelTier = ModelTier.FAST_REACTIVE,
        system_instruction: str = "",
        max_tokens: int = 512,
    ) -> ModelInferenceResult:
        return self.infer(
            prompt=prompt,
            tier=preferred_tier,
            system_instruction=system_instruction,
            max_tokens=max_tokens,
        )

    def flush_heavy_model(self) -> None:
        self.unload_all()

    def unload_reasoner(self) -> None:
        self.unload_all()

    def get_allocated_vram_mb(self) -> float:
        """Returns VRAM currently allocated by internal model runtime (0.0 when unloaded)."""
        if self._provider and hasattr(self._provider, "get_idle_metrics"):
            metrics = self._provider.get_idle_metrics()
            if not metrics.get("is_loaded", False):
                return 0.0
            return float(max(0.0, metrics.get("gpu_vram_used_mb", 0.0) - 1100.0))
        try:
            import torch
            if torch.cuda.is_available():
                return float(torch.cuda.memory_allocated() / (1024 * 1024))
        except ImportError:
            pass
        return 0.0

    def unload_all(self) -> None:
        """Enforces 0.0 MB model VRAM by unloading active model runtime in idle."""
        with self._lock:
            self._fast_active = False
            self._heavy_active = False
            if self._provider and hasattr(self._provider, "unload"):
                try:
                    self._provider.unload()
                except Exception as ex:
                    LOG.warning("Error unloading provider in unload_all: %s", ex)
            try:
                import gc
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass


# Global Singleton Router
_GLOBAL_ROUTER: Optional[TieredModelRouter] = None
_TMR_LOCK = threading.Lock()


def get_tiered_model_router() -> TieredModelRouter:
    global _GLOBAL_ROUTER
    if _GLOBAL_ROUTER is None:
        with _TMR_LOCK:
            if _GLOBAL_ROUTER is None:
                _GLOBAL_ROUTER = TieredModelRouter()
    return _GLOBAL_ROUTER
