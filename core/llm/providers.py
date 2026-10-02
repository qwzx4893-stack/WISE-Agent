"""Provider catalogue + auto-detection.

Each :class:`ProviderInfo` knows:
- a stable ``id`` (``openai``, ``anthropic``, ``gemini``, ``openrouter``…)
- the wire ``transport`` it speaks (``openai_compat`` | ``anthropic`` |
  ``gemini``) — this is what determines the request format.
- a default ``base_url`` and ``default_model`` for one-line setup.
- a ``key_prefix`` regex used by :func:`detect_provider` to recognise a
  bare API key.

Detection is best-effort and conservative: when ambiguous, we return
``unknown`` so the caller can fall back to ``openai_compat`` against
whatever ``base_url`` the user supplied. That covers any
self-hosted-vLLM / Ollama / LM Studio / litellm-proxy without us having
to enumerate them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    name: str
    transport: str  # "openai_compat" | "anthropic" | "gemini"
    base_url: Optional[str] = None
    default_model: Optional[str] = None
    key_prefix: Optional[str] = None  # regex
    api_key_env: Optional[str] = None
    notes: str = ""


# The catalogue is intentionally exhaustive on the OpenAI-compatible
# side because that's where 90% of the ecosystem sits. We only need
# dedicated transports for providers whose request format genuinely
# differs (Anthropic and Google).
PROVIDERS: List[ProviderInfo] = [
    # --- OpenAI native + Azure ---
    ProviderInfo("openai", "OpenAI", "openai_compat",
                 base_url="https://api.openai.com/v1",
                 default_model="gpt-4o-mini",
                 key_prefix=r"^sk-(?!ant-|or-)",
                 api_key_env="OPENAI_API_KEY"),
    ProviderInfo("azure", "Azure OpenAI", "openai_compat",
                base_url=None,  # user must supply
                 default_model="gpt-4o",
                 api_key_env="AZURE_OPENAI_API_KEY",
                 notes="set base_url=https://<resource>.openai.azure.com/openai/v1 (or a deployment endpoint)"),

    # --- OpenAI-compatible third parties ---
    ProviderInfo("openrouter", "OpenRouter", "openai_compat",
                 base_url="https://openrouter.ai/api/v1",
                 default_model="openrouter/auto",
                 key_prefix=r"^sk-or-",
                 api_key_env="OPENROUTER_API_KEY"),
    ProviderInfo("opencode_zen", "OpenCode Zen", "openai_compat",
                 base_url="https://api.opencode.ai/v1",
                 default_model="opencode-zen",
                 key_prefix=r"^oc-zen-",
                 api_key_env="OPENCODE_ZEN_API_KEY",
                 notes="OpenCode Zen advanced reasoning engine"),
    ProviderInfo("opencode_go", "OpenCode Go", "openai_compat",
                 base_url="https://api.opencode.ai/v1",
                 default_model="opencode-go",
                 key_prefix=r"^oc-go-",
                 api_key_env="OPENCODE_GO_API_KEY",
                 notes="OpenCode Go ultra-fast programming engine"),
    ProviderInfo("groq", "Groq", "openai_compat",
                base_url="https://api.groq.com/openai/v1",
                 default_model="openai/gpt-oss-20b",
                 key_prefix=r"^gsk_",
                 api_key_env="GROQ_API_KEY"),
    ProviderInfo("together", "Together.ai", "openai_compat",
                 base_url="https://api.together.xyz/v1",
                 default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
                 api_key_env="TOGETHER_API_KEY"),
    ProviderInfo("deepseek", "DeepSeek", "openai_compat",
                 base_url="https://api.deepseek.com/v1",
                 default_model="deepseek-chat",
                 api_key_env="DEEPSEEK_API_KEY"),
    ProviderInfo("mistral", "Mistral", "openai_compat",
                 base_url="https://api.mistral.ai/v1",
                 default_model="mistral-small-latest",
                 api_key_env="MISTRAL_API_KEY"),
    ProviderInfo("fireworks", "Fireworks", "openai_compat",
                 base_url="https://api.fireworks.ai/inference/v1",
                 default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
                 key_prefix=r"^fw_",
                 api_key_env="FIREWORKS_API_KEY"),
    ProviderInfo("anyscale", "Anyscale", "openai_compat",
                 base_url="https://api.endpoints.anyscale.com/v1",
                 default_model="meta-llama/Meta-Llama-3-70B-Instruct",
                 api_key_env="ANYSCALE_API_KEY"),
    ProviderInfo("perplexity", "Perplexity", "openai_compat",
                 base_url="https://api.perplexity.ai",
                 default_model="llama-3.1-sonar-large-128k-online",
                 key_prefix=r"^pplx-",
                 api_key_env="PERPLEXITY_API_KEY"),
    ProviderInfo("xai", "xAI Grok", "openai_compat",
                 base_url="https://api.x.ai/v1",
                 default_model="grok-2",
                 key_prefix=r"^xai-",
                 api_key_env="XAI_API_KEY"),
    ProviderInfo("cohere", "Cohere", "openai_compat",
                 base_url="https://api.cohere.ai/compatibility/v1",
                 default_model="command-r-plus",
                 api_key_env="COHERE_API_KEY"),

    # --- Local / self-hosted (no key required) ---
    ProviderInfo("ollama", "Ollama (local)", "openai_compat",
                 base_url="http://localhost:11434/v1",
                 default_model="llama3.1",
                 notes="ollama exposes /v1 at port 11434 by default"),
    ProviderInfo("lm-studio", "LM Studio (local)", "openai_compat",
                 base_url="http://localhost:1234/v1",
                 default_model="loaded-model"),
    ProviderInfo("vllm", "vLLM (local)", "openai_compat",
                 base_url="http://localhost:8000/v1",
                 default_model="local-model"),
    ProviderInfo("litellm-proxy", "LiteLLM proxy", "openai_compat",
                 base_url="http://localhost:4000",
                 default_model="any"),

    # --- Anthropic native ---
    ProviderInfo("anthropic", "Anthropic Claude", "anthropic",
                base_url="https://api.anthropic.com/v1",
                 default_model="claude-sonnet-4-6",
                 key_prefix=r"^sk-ant-",
                 api_key_env="ANTHROPIC_API_KEY"),

    # --- Google Gemini native ---
    ProviderInfo("gemini", "Google Gemini", "gemini",
                 base_url="https://generativelanguage.googleapis.com/v1beta",
                 default_model="gemini-2.0-flash",
                 key_prefix=r"^AIza",
                 api_key_env="GEMINI_API_KEY"),

    # Catch-all for anything we don't recognise; user MUST supply base_url + model.
    ProviderInfo("custom", "Custom OpenAI-compatible", "openai_compat",
                 base_url=None, default_model=None,
                 notes="any /v1/chat/completions endpoint"),
]


_PROVIDERS_BY_ID: Dict[str, ProviderInfo] = {p.id: p for p in PROVIDERS}


def get_provider(provider_id: str) -> Optional[ProviderInfo]:
    return _PROVIDERS_BY_ID.get(provider_id)


def detect_provider(api_key: str = "",
                    base_url: Optional[str] = None) -> ProviderInfo:
    """Best-effort provider detection.

    Order of precedence:
    1. ``base_url`` host match (most reliable)
    2. ``api_key`` prefix match
    3. fall back to ``custom`` (OpenAI-compatible)
    """
    if base_url:
        bl = base_url.lower()
        for p in PROVIDERS:
            if p.base_url and p.base_url.lower() in bl:
                return p
        # Heuristics on host fragments
        host_map = {
            "openai.com": "openai",
            "openai.azure.com": "azure",
            "openrouter.ai": "openrouter",
            "opencode.ai": "opencode_zen",
            "groq.com": "groq",
            "together.xyz": "together",
            "deepseek.com": "deepseek",
            "mistral.ai": "mistral",
            "fireworks.ai": "fireworks",
            "anyscale.com": "anyscale",
            "perplexity.ai": "perplexity",
            "x.ai": "xai",
            "cohere.ai": "cohere",
            "anthropic.com": "anthropic",
            "googleapis.com": "gemini",
            "ollama": "ollama",
            "localhost:11434": "ollama",
            "localhost:1234": "lm-studio",
            "localhost:8000": "vllm",
            "localhost:4000": "litellm-proxy",
        }
        for needle, pid in host_map.items():
            if needle in bl:
                return _PROVIDERS_BY_ID[pid]

    if api_key:
        for p in PROVIDERS:
            if p.key_prefix and re.search(p.key_prefix, api_key):
                return p

    return _PROVIDERS_BY_ID["custom"]


def list_provider_ids() -> List[str]:
    return [p.id for p in PROVIDERS]


__all__ = [
    "ProviderInfo",
    "PROVIDERS",
    "detect_provider",
    "get_provider",
    "list_provider_ids",
]
