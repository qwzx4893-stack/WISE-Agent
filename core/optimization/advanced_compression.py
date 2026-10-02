"""Advanced prompt compression (Phase 8 Part 5).

A thin, production-grade facade over state-of-the-art LLM-prompt
compression libraries. Every backend is detected **lazily** — missing
dependencies never break import, they just disappear from
:func:`available_backends` and :func:`compress_prompt` degrades to the
next best option.

Backends recognised:

* ``llmlingua``      — Microsoft's coarse-to-fine prompt compression
                       using a small LM. MIT.
                       https://github.com/microsoft/LLMLingua
* ``llmlingua2``     — task-agnostic BERT-based token classification
                       (ships inside the ``llmlingua`` package as
                       ``llmlingua.PromptCompressor`` with the
                       ``llmlingua2`` flag). MIT.
* ``longllmlingua``  — long-context variant that addresses position
                       bias (same ``llmlingua`` package, different
                       ``model_name``). MIT.
* ``un-locc``        — Optical Context Compression; renders text to
                       an image so a VLM can absorb more tokens. MIT.
                       https://github.com/lagom-labs/un-locc
* ``chonkify``       — Compression for RAG / agents with reported
                       ~4× better performance than LLMLingua. MIT,
                       GitHub-only (``pip install
                       git+https://github.com/thomheinrich/chonkify``).

Research frameworks that are *not* installable Python packages —
TokenSqueeze, RECOMP, CompLLM, LongCodeZip, ACON, Stingy Context —
are documented in ``docs/token_optimization_research.md`` but are not
wired into this module.

On every public call we open a :func:`Tracer.span` ``token.compression``
so ``/admin/observability/traces`` shows how much each call saved.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.observability import Tracer
from .token_counter import TokenCounter

LOG = logging.getLogger("agent_os.optimization.advanced")

# The order in which ``auto`` tries backends: roughly
# "best quality → cheapest → last-ditch". ``un-locc`` requires an
# external VLM call so it is last before ``none``.
_AUTO_ORDER: Tuple[str, ...] = (
    "chonkify",
    "longllmlingua",
    "llmlingua2",
    "llmlingua",
    "un-locc",
)

_ALL_METHODS: Tuple[str, ...] = _AUTO_ORDER + ("none", "auto")


# ----------------------------------------------------------------------
# Lazy imports
# ----------------------------------------------------------------------
def _try_import(module: str) -> Optional[Any]:
    try:
        return importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("optional compression backend %s unavailable: %s",
                  module, exc)
        return None


_LLMLINGUA = None  # PromptCompressor class, lazy
_UNLOCC = None
_CHONKIFY = None
_LOAD_LOCK = threading.Lock()


def _load_llmlingua():
    global _LLMLINGUA
    if _LLMLINGUA is not None:
        return _LLMLINGUA
    with _LOAD_LOCK:
        if _LLMLINGUA is None:
            mod = _try_import("llmlingua")
            if mod is not None and hasattr(mod, "PromptCompressor"):
                _LLMLINGUA = mod.PromptCompressor
    return _LLMLINGUA


def _load_unlocc():
    global _UNLOCC
    if _UNLOCC is not None:
        return _UNLOCC
    with _LOAD_LOCK:
        if _UNLOCC is None:
            _UNLOCC = _try_import("un_locc") or _try_import("unlocc")
    return _UNLOCC


def _load_chonkify():
    global _CHONKIFY
    if _CHONKIFY is not None:
        return _CHONKIFY
    with _LOAD_LOCK:
        if _CHONKIFY is None:
            _CHONKIFY = _try_import("chonkify")
    return _CHONKIFY


# ----------------------------------------------------------------------
# Public dataclass
# ----------------------------------------------------------------------
@dataclass
class CompressionOutcome:
    text: str
    method: str
    before_tokens: int
    after_tokens: int
    ratio: float  # after / before
    duration_ms: float
    fallback: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "ratio": round(self.ratio, 4),
            "duration_ms": round(self.duration_ms, 2),
            "fallback": self.fallback,
            "error": self.error,
            "saved_tokens": self.saved_tokens,
            "metadata": self.metadata,
        }


# ----------------------------------------------------------------------
# Backend registry
# ----------------------------------------------------------------------
def available_backends() -> Dict[str, bool]:
    """Which compression backends are installed *right now*."""
    return {
        "llmlingua": _load_llmlingua() is not None,
        "llmlingua2": _load_llmlingua() is not None,
        "longllmlingua": _load_llmlingua() is not None,
        "un-locc": _load_unlocc() is not None,
        "chonkify": _load_chonkify() is not None,
    }


def is_any_available() -> bool:
    return any(available_backends().values())


# ----------------------------------------------------------------------
# Shared PromptCompressor cache
# ----------------------------------------------------------------------
_COMPRESSOR_CACHE: Dict[Tuple[str, str], Any] = {}


def _get_llmlingua_compressor(variant: str):
    """``variant`` ∈ {``llmlingua``, ``llmlingua2``, ``longllmlingua``}."""
    cls = _load_llmlingua()
    if cls is None:
        return None
    # Model choices follow the PromptCompressor README.
    if variant == "llmlingua2":
        model = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"
        kwargs = {"model_name": model, "use_llmlingua2": True}
    elif variant == "longllmlingua":
        kwargs = {"model_name": "NousResearch/Llama-2-7b-hf"}
    else:
        kwargs = {"model_name": "NousResearch/Llama-2-7b-hf"}
    key = (variant, kwargs.get("model_name", ""))
    cached = _COMPRESSOR_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        inst = cls(**kwargs)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("llmlingua[%s] failed to initialise: %s",
                    variant, exc)
        return None
    _COMPRESSOR_CACHE[key] = inst
    return inst


# ----------------------------------------------------------------------
# Backend adapters
# ----------------------------------------------------------------------
def _run_llmlingua(text: str, ratio: float, variant: str
                   ) -> Tuple[str, Dict[str, Any]]:
    comp = _get_llmlingua_compressor(variant)
    if comp is None:
        raise RuntimeError(f"{variant} unavailable")
    # ``compress_prompt`` signature is stable across 0.2.x.
    # ``rate`` = target fraction (kept), so rate = ratio.
    result = comp.compress_prompt(text, rate=float(ratio))
    if isinstance(result, dict):
        out = result.get("compressed_prompt") or result.get("prompt")
        meta = {k: v for k, v in result.items()
                if k not in ("compressed_prompt", "prompt")}
    else:
        out, meta = str(result), {}
    if not out:
        raise RuntimeError(f"{variant} returned empty output")
    return out, meta


def _run_unlocc(text: str, ratio: float
                ) -> Tuple[str, Dict[str, Any]]:
    mod = _load_unlocc()
    if mod is None:
        raise RuntimeError("un-locc unavailable")
    # un-locc renders text to an image meant for a VLM. When we're not
    # about to send that image to a VLM, the usable textual fallback
    # is whatever helper the package ships. Guard every call path.
    fn = (getattr(mod, "compress_text", None)
          or getattr(mod, "compress", None))
    if fn is None:
        raise RuntimeError("un-locc has no compress_text helper")
    out = fn(text, rate=float(ratio)) if _accepts_kw(fn, "rate") else fn(text)
    if isinstance(out, (list, tuple)):
        out = out[0] if out else ""
    if not isinstance(out, str) or not out:
        raise RuntimeError("un-locc returned empty output")
    return out, {"mode": "optical"}


def _run_chonkify(text: str, ratio: float
                  ) -> Tuple[str, Dict[str, Any]]:
    mod = _load_chonkify()
    if mod is None:
        raise RuntimeError("chonkify unavailable")
    fn = (getattr(mod, "compress", None)
          or getattr(mod, "chonkify", None))
    if fn is None:
        raise RuntimeError("chonkify has no compress helper")
    out = fn(text, ratio=float(ratio)) if _accepts_kw(fn, "ratio") else fn(text)
    if not isinstance(out, str) or not out:
        raise RuntimeError("chonkify returned empty output")
    return out, {}


def _accepts_kw(fn: Callable[..., Any], name: str) -> bool:
    try:
        import inspect
        return name in inspect.signature(fn).parameters
    except Exception:  # noqa: BLE001
        return False


_BACKENDS: Dict[str, Callable[[str, float],
                               Tuple[str, Dict[str, Any]]]] = {
    "llmlingua": lambda t, r: _run_llmlingua(t, r, "llmlingua"),
    "llmlingua2": lambda t, r: _run_llmlingua(t, r, "llmlingua2"),
    "longllmlingua": lambda t, r: _run_llmlingua(t, r, "longllmlingua"),
    "un-locc": _run_unlocc,
    "chonkify": _run_chonkify,
}


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def compress_prompt(
    text: str,
    *,
    method: str = "auto",
    ratio: float = 0.5,
    model_name: str = "gpt-4o-mini",
    fallback: Optional[Callable[[str, float], str]] = None,
) -> CompressionOutcome:
    """Compress ``text`` using the selected backend.

    Parameters
    ----------
    text:
        Raw prompt / context to compress.
    method:
        One of ``auto`` (default; tries backends in ``_AUTO_ORDER``),
        ``none`` (disabled; passthrough), or a specific backend id.
    ratio:
        Target fraction of the original length (``0.0`` … ``1.0``).
        ``0.5`` keeps roughly half the tokens.
    model_name:
        Used only to measure token counts for reporting.
    fallback:
        Callable ``(text, ratio) -> str`` invoked when the chosen
        backend is unavailable or raises. Default is a no-op
        passthrough so callers always receive a valid string.
    """
    counter = TokenCounter(model_name)
    before = counter.count(text or "")
    method_l = (method or "auto").strip().lower()
    if method_l not in _ALL_METHODS:
        method_l = "auto"
    ratio = max(0.05, min(1.0, float(ratio)))

    with Tracer.span("token.compression",
                     method=method_l, ratio=ratio,
                     before_tokens=before):
        t0 = time.time()
        if not text or method_l == "none":
            return _passthrough(text, before, counter, t0,
                                 method="none")

        candidates: List[str] = (
            list(_AUTO_ORDER) if method_l == "auto" else [method_l]
        )
        last_error: Optional[str] = None
        for cand in candidates:
            backend = _BACKENDS.get(cand)
            if backend is None:
                continue
            if not available_backends().get(cand, False):
                continue
            try:
                out, meta = backend(text, ratio)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{cand}: {exc}"
                LOG.info("compression backend %s failed: %s", cand, exc)
                continue
            after = counter.count(out)
            dur = (time.time() - t0) * 1000.0
            Tracer.emit("token.compression.result",
                        method=cand, before_tokens=before,
                        after_tokens=after,
                        saved=max(0, before - after))
            return CompressionOutcome(
                text=out, method=cand,
                before_tokens=before, after_tokens=after,
                ratio=(after / before) if before else 1.0,
                duration_ms=dur, metadata=meta,
            )

        # Nothing worked → fallback path.
        used_fallback = fallback(text, ratio) if fallback else text
        after = counter.count(used_fallback)
        dur = (time.time() - t0) * 1000.0
        return CompressionOutcome(
            text=used_fallback, method="none",
            before_tokens=before, after_tokens=after,
            ratio=(after / before) if before else 1.0,
            duration_ms=dur, fallback=True, error=last_error,
        )


def _passthrough(text: str, before: int,
                  counter: TokenCounter, t0: float,
                  *, method: str) -> CompressionOutcome:
    after = counter.count(text or "")
    dur = (time.time() - t0) * 1000.0
    return CompressionOutcome(
        text=text or "", method=method,
        before_tokens=before, after_tokens=after,
        ratio=1.0 if before == 0 else (after / before),
        duration_ms=dur,
    )


# ----------------------------------------------------------------------
# Aggregate stats for /admin/tools/stats
# ----------------------------------------------------------------------
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {
    "calls": 0,
    "saved_tokens": 0,
    "by_method": {},
}


def record_stats(outcome: CompressionOutcome) -> None:
    with _STATS_LOCK:
        _STATS["calls"] += 1
        _STATS["saved_tokens"] += outcome.saved_tokens
        m = outcome.method
        bucket = _STATS["by_method"].setdefault(
            m, {"calls": 0, "saved_tokens": 0, "total_ms": 0.0}
        )
        bucket["calls"] += 1
        bucket["saved_tokens"] += outcome.saved_tokens
        bucket["total_ms"] += outcome.duration_ms


def compression_stats() -> Dict[str, Any]:
    with _STATS_LOCK:
        # Deep copy so callers can mutate safely.
        return {
            "calls": _STATS["calls"],
            "saved_tokens": _STATS["saved_tokens"],
            "by_method": {k: dict(v)
                          for k, v in _STATS["by_method"].items()},
            "available": available_backends(),
        }


__all__ = [
    "CompressionOutcome",
    "available_backends",
    "is_any_available",
    "compress_prompt",
    "record_stats",
    "compression_stats",
]
