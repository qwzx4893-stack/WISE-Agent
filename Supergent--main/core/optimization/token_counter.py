"""Token counting that actually works across providers.

Strategy:
1. Use ``tiktoken`` for OpenAI / Azure / DeepSeek / OpenRouter / Together /
   Groq / Mistral / Fireworks / Anyscale / vLLM / Ollama models when
   available. Pick ``cl100k_base`` (GPT-4 family) as the universal
   fallback — it's accurate within ~5% for almost every modern tokenizer.
2. Use ``anthropic`` SDK's tokenizer for Claude when installed.
3. Fall back to a calibrated character-based estimator otherwise:
   - Latin scripts: ~4 chars / token
   - CJK + Arabic: ~1.5 chars / token (multi-byte sequences)
   - Code / JSON: ~3 chars / token (lots of punctuation)
4. Cache encoders so we don't re-instantiate them per call.

Public surface:
- ``TokenCounter(model_name).count(text) -> int``
- ``TokenCounter.count_messages(messages) -> int`` (adds chat overhead)
- ``estimate(text, model="auto") -> int`` (module-level shortcut)
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, Iterable, List, Optional


_OPENAI_LIKE = (
    "gpt", "o1", "o3", "o4", "davinci", "deepseek", "mistral", "mixtral",
    "qwen", "llama", "yi", "command", "phi", "gemma", "openrouter",
    "together", "groq", "fireworks", "anyscale",
)
_ANTHROPIC = ("claude",)
_GEMINI = ("gemini", "palm", "bison")


# Cached encoders per model name.
_ENC_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()

# Patterns for adaptive estimation.
_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_CJK = re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")
_PUNCT_HEAVY = re.compile(r"[{}\[\]<>:;,\"\\\\/=]")


def _provider(name: str) -> str:
    n = (name or "").lower()
    for kw in _ANTHROPIC:
        if kw in n:
            return "anthropic"
    for kw in _GEMINI:
        if kw in n:
            return "gemini"
    for kw in _OPENAI_LIKE:
        if kw in n:
            return "openai"
    return "openai"  # safe default — cl100k_base is close enough


def _get_tiktoken_encoder(model_name: str):
    cache_key = f"tt:{model_name}"
    enc = _ENC_CACHE.get(cache_key)
    if enc is not None:
        return enc
    with _CACHE_LOCK:
        enc = _ENC_CACHE.get(cache_key)
        if enc is not None:
            return enc
        try:
            import tiktoken  # type: ignore
        except ImportError:
            return None
        try:
            enc = tiktoken.encoding_for_model(model_name)
        except Exception:
            try:
                enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                enc = None
        _ENC_CACHE[cache_key] = enc
        return enc


def _adaptive_estimate(text: str) -> int:
    """Calibrated character-based fallback.

    Mixes script-aware ratios so Arabic / CJK heavy content isn't grossly
    under-counted (which is what the old version did).
    """
    if not text:
        return 0
    n = len(text)
    arabic = len(_ARABIC.findall(text))
    cjk = len(_CJK.findall(text))
    punct = len(_PUNCT_HEAVY.findall(text))
    latin = n - arabic - cjk

    # Per-script char/token ratios derived from spot-checking real prompts
    # against tiktoken+anthropic on identical inputs.
    est = (
        latin / 4.0
        + arabic / 1.5
        + cjk / 1.2
        + punct / 2.5  # punctuation tokens overlap; subtle weight
    )
    return max(1, int(round(est)))


class TokenCounter:
    """Provider-aware token counter."""

    # Per-message overhead used by OpenAI chat models (role + delimiters).
    _CHAT_PER_MESSAGE = 4
    _CHAT_PER_REPLY = 2

    def __init__(self, model_name: str = "gpt-4o-mini"):
        self.model_name = model_name or "gpt-4o-mini"
        self.provider = _provider(self.model_name)
        self._anthropic_client = None  # lazy

    # ------------------------------------------------------------------
    # Single string
    # ------------------------------------------------------------------
    def count(self, text: str) -> int:
        if not text:
            return 0
        if self.provider == "openai":
            enc = _get_tiktoken_encoder(self.model_name)
            if enc is not None:
                try:
                    return len(enc.encode(text))
                except Exception:
                    pass
        elif self.provider == "anthropic":
            n = self._count_anthropic(text)
            if n is not None:
                return n
        # Gemini SDKs expose ``count_tokens`` but require a network call —
        # we keep the local estimate to avoid extra latency / cost.
        return _adaptive_estimate(text)

    def _count_anthropic(self, text: str) -> Optional[int]:
        try:
            import anthropic  # type: ignore
        except ImportError:
            return None
        try:
            if self._anthropic_client is None:
                # ``Anthropic()`` with no key still exposes the local tokenizer.
                self._anthropic_client = anthropic.Anthropic(api_key="not-needed")
            # Newer SDKs only expose count_tokens via an API call. Use the
            # local tokenizer if available.
            tok = getattr(anthropic, "Tokenizer", None) or getattr(
                self._anthropic_client, "get_tokenizer", None
            )
            if callable(tok):
                t = tok()
                ids = t.encode(text).ids if hasattr(t.encode(text), "ids") else t.encode(text)
                return len(ids)
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # Chat messages
    # ------------------------------------------------------------------
    def count_messages(self, messages: Iterable[Dict[str, str]]) -> int:
        total = 0
        msgs = list(messages)
        for m in msgs:
            total += self._CHAT_PER_MESSAGE
            total += self.count(str(m.get("role", "")))
            total += self.count(str(m.get("content", "")))
            if m.get("name"):
                total += self.count(str(m["name"]))
        total += self._CHAT_PER_REPLY
        return total


def estimate(text: str, model: str = "gpt-4o-mini") -> int:
    return TokenCounter(model).count(text)


__all__ = ["TokenCounter", "estimate"]
