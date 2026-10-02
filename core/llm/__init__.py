"""Universal LLM adapter layer.

Public surface:

- :class:`UniversalLLM` — single-model adapter: takes an arbitrary key
  and (optional) base URL/model and exposes the same ``__call__(messages)
  -> str`` interface as the rest of Agent OS.
- :class:`LLMRouter` — multi-provider router with fallback / ensemble /
  vote modes. Backwards-compatible drop-in for ``TeamModel``.
- :func:`detect_provider(api_key, base_url)` — auto-detect provider from
  the shape of the key or the base URL.
- :class:`KeyStore` — persistent (XOR-obfuscated) credential storage in
  ``MEMORY_DIR/keys.json``.
"""

from .providers import (
    PROVIDERS,
    ProviderInfo,
    detect_provider,
)
from .keystore import KeyStore
from .universal import UniversalLLM, LLMRouter, build_router_from_keystore

__all__ = [
    "PROVIDERS",
    "ProviderInfo",
    "detect_provider",
    "KeyStore",
    "UniversalLLM",
    "LLMRouter",
    "build_router_from_keystore",
]
