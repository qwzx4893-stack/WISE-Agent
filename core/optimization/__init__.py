"""Token-saving layer.

Public surface:

- ``TokenCounter(model_name)``        — provider-aware token counting
- ``ContextCompressor(model_name)``   — multi-stage chat compressor
- ``PlanCache()``                      — sqlite answer cache + metrics
- ``PromptOptimizer(model_name)``      — section-aware prompt squeezer
- ``TokenOptimizer(model_name)``       — lazy umbrella that gives you
  all four sub-tools while sharing a single ``model_name``.
- ``optimization_stats()``             — global metrics for /admin endpoints
"""

from __future__ import annotations

from typing import Any, Dict

from .advanced_compression import (
    CompressionOutcome,
    available_backends,
    compress_prompt,
    compression_stats,
    is_any_available,
    record_stats,
)
from .context_compressor import (
    CompressionReport,
    CompressionResult,
    ContextCompressor,
)
from .plan_cache import InMemoryCache, PlanCache, RequestCoalescer
from .prompt_optimizer import PromptOptimizer
from .token_counter import TokenCounter, estimate

# A singleton plan cache shared across modes / API endpoints, so /admin
# can report meaningful stats.
_GLOBAL_CACHE: PlanCache | None = None
_MODEL_CALL_COALESCER = RequestCoalescer()


def get_plan_cache() -> PlanCache:
    global _GLOBAL_CACHE
    if _GLOBAL_CACHE is None:
        _GLOBAL_CACHE = PlanCache()
    return _GLOBAL_CACHE


def optimization_stats() -> Dict[str, Any]:
    return {
        "plan_cache": get_plan_cache().stats(),
        "compression": compression_stats(),
        "request_coalescing": _MODEL_CALL_COALESCER.stats(),
    }


def coalesce_model_call(key: str, call):
    """Run one identical in-flight model invocation and share its result."""
    return _MODEL_CALL_COALESCER.run(key, call)


class TokenOptimizer:
    """Convenience wrapper that lazily creates each sub-tool."""

    def __init__(self, model_name: str = "gpt-4o-mini",
                 max_context_tokens: int = 8000,
                 max_prompt_tokens: int = 2000):
        self.model_name = model_name
        self.max_context_tokens = max_context_tokens
        self.max_prompt_tokens = max_prompt_tokens
        self._counter: TokenCounter | None = None
        self._compressor: ContextCompressor | None = None
        self._cache: PlanCache | None = None
        self._prompt_opt: PromptOptimizer | None = None

    @property
    def counter(self) -> TokenCounter:
        if self._counter is None:
            self._counter = TokenCounter(self.model_name)
        return self._counter

    @property
    def compressor(self) -> ContextCompressor:
        if self._compressor is None:
            self._compressor = ContextCompressor(
                self.model_name, self.max_context_tokens
            )
        return self._compressor

    @property
    def cache(self) -> PlanCache:
        if self._cache is None:
            self._cache = get_plan_cache()
        return self._cache

    @property
    def prompt_opt(self) -> PromptOptimizer:
        if self._prompt_opt is None:
            self._prompt_opt = PromptOptimizer(
                self.model_name, self.max_prompt_tokens
            )
        return self._prompt_opt

    # ------------------------------------------------------------------
    # Advanced compression (Phase 8 Part 5)
    # ------------------------------------------------------------------
    def compress_prompt(self, text: str, *, method: str = "auto",
                        ratio: float = 0.5) -> CompressionOutcome:
        """Run :func:`advanced_compression.compress_prompt` with
        ``PromptOptimizer`` as the deterministic local fallback."""

        def _pytho_fallback(t: str, r: float) -> str:
            opt = self.prompt_opt
            # ``PromptOptimizer`` speaks in token budgets rather than
            # ratios, so derive a target budget from the caller's
            # ratio while respecting the optimizer's configured cap.
            tokens_now = self.counter.count(t)
            target = max(1, int(tokens_now * r))
            target = min(target, self.max_prompt_tokens)
            return opt.optimize(t, max_tokens=target)

        outcome = compress_prompt(
            text, method=method, ratio=ratio,
            model_name=self.model_name,
            fallback=_pytho_fallback,
        )
        record_stats(outcome)
        return outcome


__all__ = [
    "TokenCounter",
    "ContextCompressor",
    "CompressionReport",
    "CompressionResult",
    "CompressionOutcome",
    "PlanCache",
    "InMemoryCache",
    "RequestCoalescer",
    "PromptOptimizer",
    "TokenOptimizer",
    "available_backends",
    "compress_prompt",
    "compression_stats",
    "is_any_available",
    "record_stats",
    "get_plan_cache",
    "optimization_stats",
    "coalesce_model_call",
    "estimate",
]
