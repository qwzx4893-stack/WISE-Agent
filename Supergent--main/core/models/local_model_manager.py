# ==============================================================================
# WISE Model & Provider Management Subsystem
# Architecture:
# - Manages local GGUF models (discovery, import, removal, active selection).
# - Discovers Dense vs MoE architectures and inspects GGUF capabilities.
# - Manages External API Providers (OpenAI, Anthropic, Gemini, Groq, Ollama, etc.)
#   using secure credential storage (KeyStore) without exposing raw secrets.
# - Allows runtime configuration of context sizes, GPU offload layers, and defaults.
# - Ensures WISE never breaks if the default LFM2.5 model is uninstalled.
# ==============================================================================

from __future__ import annotations

import os
import re
import json
import time
import shutil
import logging
import threading
import sys
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple

from core.paths import CONFIG_DIR, LOGS_DIR
from core.llm.providers import PROVIDERS, ProviderInfo, detect_provider
from core.llm.keystore import KeyStore

LOG = logging.getLogger("WISE.Models.Manager")

CONFIG_FILE = CONFIG_DIR / "model_registry.json"


@dataclass
class ModelCapabilities:
    text_generation: bool = True
    reasoning: bool = True
    tool_calling: bool = True
    structured_output: bool = True
    vision: bool = False
    context_length: int = 4096
    streaming: bool = True
    is_local: bool = True
    is_moe: bool = False
    is_dense: bool = True
    active_params: Optional[str] = None
    total_params: Optional[str] = None
    quantization: str = "Q4_K_M"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModelRecord:
    id: str
    name: str
    file_path: Optional[str] = None
    file_size_bytes: int = 0
    file_size_mb: float = 0.0
    architecture: str = "generic"
    provider_type: str = "LOCAL_GGUF"  # LOCAL_GGUF | CLOUD_API
    is_default: bool = False
    is_active: bool = False
    runtime_params: Dict[str, Any] = field(default_factory=lambda: {
        "n_gpu_layers": 33,
        "n_ctx": 4096,
        "temperature": 0.2,
        "threads": 8,
    })
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    status: str = "AVAILABLE"  # AVAILABLE | MISSING_FILE | ERROR | ACTIVE
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["capabilities"] = self.capabilities.to_dict()
        return d


class LocalModelManager:
    """
    Central Manager for Local GGUF models and External Model Providers in WISE.
    Saves state in config/model_registry.json and keeps credentials in KeyStore.
    """

    DEFAULT_SCAN_DIRS = [
        Path("scratch/models"),
        Path("models"),
        Path("scratch"),
    ]

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config_path = config_path or CONFIG_FILE
        self.keystore = KeyStore()
        self._models: Dict[str, ModelRecord] = {}
        self._active_provider_id: str = "local"
        self._active_model_id: str = ""
        self._lock = threading.RLock()
        self._load_or_discover()

    # --------------------------------------------------------------------------
    # Discovery & Persistence
    # --------------------------------------------------------------------------
    def _load_or_discover(self) -> None:
        with self._lock:
            # 1. Load saved config if exists
            if self.config_path.exists():
                try:
                    data = json.loads(self.config_path.read_text(encoding="utf-8"))
                    self._active_provider_id = data.get("active_provider_id", "local")
                    self._active_model_id = data.get("active_model_id", "")
                    for mid, mdata in data.get("models", {}).items():
                        # Older releases persisted a fabricated "available"
                        # model when no runtime existed.  Never load that
                        # record into a production registry.
                        if mdata.get("provider_type") == "SIMULATED":
                            continue
                        caps_data = mdata.pop("capabilities", {})
                        caps = ModelCapabilities(**caps_data) if isinstance(caps_data, dict) else ModelCapabilities()
                        rec = ModelRecord(**mdata, capabilities=caps)
                        # Verify file presence for local models
                        if rec.file_path:
                            p = Path(rec.file_path)
                            rec.status = "AVAILABLE" if p.exists() else "MISSING_FILE"
                        self._models[mid] = rec
                except Exception as e:
                    LOG.error("Failed loading model registry from %s: %s", self.config_path, e)

            # 2. Auto-discover models in standard paths if empty
            self._scan_standard_directories()

            # 3. Ensure an active model is set
            self._reconcile_active_model()
            self._save()

    def _save(self) -> None:
        with self._lock:
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "active_provider_id": self._active_provider_id,
                    "active_model_id": self._active_model_id,
                    "updated_at": time.time(),
                    "models": {mid: m.to_dict() for mid, m in self._models.items()},
                }
                self.config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except Exception as e:
                LOG.error("Failed saving model registry: %s", e)

    def _scan_standard_directories(self) -> None:
        """Discovers local GGUF models on disk and extracts architecture metadata."""
        workspace_root = Path(__file__).resolve().parent.parent.parent
        for rel_dir in self.DEFAULT_SCAN_DIRS:
            target_dir = (workspace_root / rel_dir).resolve()
            if not target_dir.exists():
                continue
            for gguf_path in target_dir.glob("*.gguf"):
                self._register_file(gguf_path, set_as_default=False)

    def _register_file(self, path: Path, set_as_default: bool = False) -> ModelRecord:
        mid = path.stem.lower()
        if mid in self._models and self._models[mid].status == "AVAILABLE":
            return self._models[mid]

        stat = path.stat()
        file_size_bytes = stat.st_size
        file_size_mb = round(file_size_bytes / (1024 * 1024), 2)

        # Inspect architecture heuristically
        lower_name = path.name.lower()
        is_moe = any(k in lower_name for k in ["moe", "a1b", "mixtral", "deepseek", "32x", "8x"])
        arch = "lfm2moe" if "lfm" in lower_name else ("moe" if is_moe else "llama")

        caps = ModelCapabilities(
            text_generation=True,
            reasoning=True,
            tool_calling=True,
            structured_output=True,
            vision="vision" in lower_name or "vl" in lower_name,
            context_length=8192 if "128k" in lower_name or "32k" in lower_name else 4096,
            is_local=True,
            is_moe=is_moe,
            is_dense=not is_moe,
            active_params="~1B" if "a1b" in lower_name else None,
            total_params="8B" if "8b" in lower_name else ("3B" if "3b" in lower_name else None),
            quantization="Q4_K_M" if "q4_k_m" in lower_name else "Q4",
        )

        record = ModelRecord(
            id=mid,
            name=path.stem,
            file_path=str(path.resolve()),
            file_size_bytes=file_size_bytes,
            file_size_mb=file_size_mb,
            architecture=arch,
            provider_type="LOCAL_GGUF",
            is_default=set_as_default,
            capabilities=caps,
            status="AVAILABLE",
        )
        self._models[mid] = record
        LOG.info("Registered local model: %s (%s, %.1f MB, MoE=%s)", record.name, arch, file_size_mb, is_moe)
        return record

    def _reconcile_active_model(self) -> None:
        """Ensures at least one available model is active and default."""
        available = [m for m in self._models.values() if m.status == "AVAILABLE"]
        if not available:
            # No installed local model is an honest state.  Cloud providers
            # are resolved by the canonical provider layer.  A legacy registry
            # can still say "local" even though it has no local model; migrate
            # only when there is exactly one already-configured external choice.
            # This is a state repair, never runtime failover.
            self._active_model_id = ""
            for model in self._models.values():
                model.is_active = False
            if self._active_provider_id == "local":
                configured = [
                    entry for entry in self.keystore.list(reveal=False)
                    if entry.get("enabled", True) and entry.get("provider")
                ]
                providers = {str(entry["provider"]) for entry in configured}
                if len(providers) == 1:
                    self._active_provider_id = providers.pop()
            return

        # Check if current active is still valid
        if not self._active_model_id or self._active_model_id not in self._models or self._models[self._active_model_id].status != "AVAILABLE":
            # Pick default, or first LFM2.5, or first available
            default_candidates = [m for m in available if m.is_default]
            lfm_candidates = [m for m in available if "lfm" in m.id]
            chosen = default_candidates[0] if default_candidates else (lfm_candidates[0] if lfm_candidates else available[0])
            self._active_model_id = chosen.id

        # Update flags
        for mid, m in self._models.items():
            m.is_active = (mid == self._active_model_id)

    # --------------------------------------------------------------------------
    # Public Local Model Operations
    # --------------------------------------------------------------------------
    def list_local_models(self) -> List[Dict[str, Any]]:
        with self._lock:
            # Refresh file status
            for m in self._models.values():
                if m.file_path:
                    m.status = "AVAILABLE" if Path(m.file_path).exists() else "MISSING_FILE"
            self._reconcile_active_model()
            return [m.to_dict() for m in self._models.values()]

    def import_local_model(
        self,
        file_path: str,
        name: Optional[str] = None,
        set_as_default: bool = False,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Imports an external GGUF file into WISE."""
        with self._lock:
            p = Path(file_path).resolve()
            if not p.exists():
                return False, f"File does not exist: {file_path}", None
            if not p.suffix.lower() == ".gguf":
                return False, "File must be a .gguf binary model", None

            rec = self._register_file(p, set_as_default=set_as_default)
            if name:
                rec.name = name
            if set_as_default:
                for m in self._models.values():
                    m.is_default = (m.id == rec.id)
                self._active_model_id = rec.id

            self._reconcile_active_model()
            self._save()
            return True, f"Successfully imported {rec.name}", rec.to_dict()

    def remove_local_model(self, model_id: str, delete_file: bool = False) -> Tuple[bool, str]:
        """
        Removes a local model from the registry.
        If the active model is removed, WISE automatically falls back to another available model.
        """
        with self._lock:
            if model_id not in self._models:
                return False, f"Model ID '{model_id}' not found"

            rec = self._models.pop(model_id)
            if delete_file and rec.file_path:
                try:
                    p = Path(rec.file_path)
                    if p.exists():
                        p.unlink()
                        LOG.info("Deleted physical model file: %s", rec.file_path)
                except Exception as ex:
                    LOG.warning("Could not delete file %s: %s", rec.file_path, ex)

            # Reconcile so an alternative model immediately becomes active
            self._reconcile_active_model()
            self._save()
            return True, f"Model '{rec.name}' removed. Active model is now '{self._active_model_id}'."

    def set_active_local_model(self, model_id: str) -> Tuple[bool, str]:
        """Switches the active local model used for inference."""
        with self._lock:
            if model_id not in self._models:
                return False, f"Model ID '{model_id}' not registered."
            m = self._models[model_id]
            if m.status != "AVAILABLE":
                return False, f"Model '{m.name}' is not currently available (status: {m.status})"

            self._active_model_id = model_id
            self._active_provider_id = "local"
            for mid, mod in self._models.items():
                mod.is_active = (mid == model_id)
            self._save()
            voice_module = sys.modules.get("core.voice.runtime")
            voice_runtime = getattr(voice_module, "_VOICE_RUNTIME", None)
            if voice_runtime is not None:
                release = getattr(voice_runtime.tts.primary_provider, "release_gpu_for_local_model", None)
                if release is not None:
                    release()
            self._reset_canonical_provider()
            LOG.info("Active model switched to '%s' (%s)", m.name, model_id)
            return True, f"Active model successfully switched to '{m.name}'"

    def configure_runtime_params(self, model_id: str, params: Dict[str, Any]) -> Tuple[bool, str]:
        """Updates runtime tuning parameters (n_gpu_layers, n_ctx, temperature)."""
        with self._lock:
            if model_id not in self._models:
                return False, f"Model ID '{model_id}' not found."
            m = self._models[model_id]
            m.runtime_params.update(params)
            self._save()
            if m.is_active and self._active_provider_id == "local":
                # The next request must rebuild the local runtime with the new
                # values instead of leaving a cached provider alive.
                self._reset_canonical_provider()
            return True, f"Runtime parameters updated for '{m.name}'"

    def get_active_model_info(self) -> Dict[str, Any]:
        with self._lock:
            self._reconcile_active_model()
            rec = self._models.get(self._active_model_id)
            if rec:
                return rec.to_dict()
            return {"id": "none", "name": "None", "status": "NO_MODEL_LOADED"}

    # --------------------------------------------------------------------------
    # External Provider Operations (OpenAI, Anthropic, Gemini, Groq, Ollama...)
    # --------------------------------------------------------------------------
    def list_external_providers(self) -> List[Dict[str, Any]]:
        """Returns the list of 18+ supported providers with configured status."""
        out = []
        for p in PROVIDERS:
            configured = self.keystore.get_for_provider(p.id, reveal=False)
            has_key = bool(
                configured and configured.get("api_key")
            ) or bool(p.api_key_env and os.environ.get(p.api_key_env))
            out.append({
                "id": p.id,
                "name": p.name,
                "transport": p.transport,
                # A saved endpoint override is not a secret and must be
                # returned so the provider settings form can be edited later.
                "base_url": (configured or {}).get("base_url") or p.base_url,
                "default_model": p.default_model,
                "configured_model": (configured or {}).get("model"),
                "is_configured": bool(configured) or bool(p.api_key_env and os.environ.get(p.api_key_env)),
                "has_api_key": has_key,
                "is_active": (self._active_provider_id == p.id),
                "notes": p.notes,
            })
        return out

    def configure_external_provider(
        self,
        provider_id: str,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        set_as_active: bool = False,
    ) -> Tuple[bool, str]:
        """Securely saves credentials in KeyStore and configures an external provider."""
        with self._lock:
            known = next((p for p in PROVIDERS if p.id == provider_id), None)
            if not known and provider_id != "custom":
                return False, f"Unknown provider ID '{provider_id}'"

            existing = self.keystore.get_for_provider(provider_id, reveal=True)
            resolved_key = (api_key or (existing or {}).get("api_key") or "").strip()
            resolved_model = model or (existing or {}).get("model") or (known.default_model if known else None)
            resolved_base_url = base_url or (existing or {}).get("base_url") or (known.base_url if known else None)

            # Remote providers need an actual credential.  Local endpoints
            # (Ollama/LM Studio/vLLM) may deliberately run without one.
            is_local_endpoint = bool(resolved_base_url and (
                "localhost" in resolved_base_url or "127.0.0.1" in resolved_base_url
            ))
            if not resolved_key and not is_local_endpoint:
                return False, f"Provider '{provider_id}' requires an API key."

            # A stable provider-scoped name lets the v2 UI update a provider
            # without creating an unusable pile of anonymous credentials.
            self.keystore.add(
                name=f"provider:{provider_id}",
                api_key=resolved_key,
                provider=provider_id,
                model=resolved_model,
                base_url=resolved_base_url,
                enabled=True,
                priority=100 if set_as_active else int((existing or {}).get("priority", 0) or 0),
            )
            LOG.info("Stored configuration for external provider '%s'", provider_id)

            if set_as_active:
                self._active_provider_id = provider_id
                self._save()

            # ConversationalCore caches the canonical provider.  Reset it so
            # a user can add/select a cloud model and use it in the next turn.
            try:
                from core.models.provider_interface import reset_model_provider
                reset_model_provider()
                from core.brain.conversational_core import reset_conversational_core_provider
                reset_conversational_core_provider()
            except Exception as exc:  # pragma: no cover - defensive only
                LOG.warning("Could not reset active model provider: %s", exc)

            return True, f"Provider '{provider_id}' successfully configured."

    def disconnect_external_provider(self, provider_id: str) -> Tuple[bool, str]:
        """Remove a provider-scoped credential and reconcile the active choice."""
        with self._lock:
            removed = self.keystore.remove(f"provider:{provider_id}")
            if not removed:
                return False, f"Provider '{provider_id}' is not configured."

            if self._active_provider_id == provider_id:
                available_local = [m for m in self._models.values() if m.status == "AVAILABLE"]
                if available_local:
                    self._active_provider_id = "local"
                    if not self._active_model_id:
                        self._active_model_id = available_local[0].id
                else:
                    remaining = [
                        entry for entry in self.keystore.list(reveal=False)
                        if entry.get("enabled", True) and entry.get("provider")
                    ]
                    remaining.sort(key=lambda entry: int(entry.get("priority", 0) or 0), reverse=True)
                    self._active_provider_id = str(remaining[0]["provider"]) if remaining else "local"
                self._save()

            self._reset_canonical_provider()
            LOG.info("Disconnected external provider '%s'", provider_id)
            return True, f"Provider '{provider_id}' disconnected."

    @staticmethod
    def _reset_canonical_provider() -> None:
        """Invalidate provider caches after a model setting changes."""
        try:
            from core.models.provider_interface import reset_model_provider
            reset_model_provider()
            from core.brain.conversational_core import reset_conversational_core_provider
            reset_conversational_core_provider()
        except Exception as exc:  # pragma: no cover - defensive only
            LOG.warning("Could not reset active model provider: %s", exc)

    def fetch_live_provider_models(
        self,
        provider_id: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Tuple[bool, List[str], str]:
        """Queries the external provider's API live using the provided or stored API key,
        returning the real, complete list of available models.
        """
        import json
        import requests

        with self._lock:
            p = next((item for item in PROVIDERS if item.id == provider_id), None)
            stored = self.keystore.get_for_provider(provider_id, reveal=True)
            target_key = api_key or (stored or {}).get("api_key") or (os.environ.get(p.api_key_env) if p and p.api_key_env else None)
            if provider_id == "gemini":
                target_key = target_key or os.environ.get("GOOGLE_API_KEY")
            # Saved overrides must win over the catalogue default.  The old
            # code ignored them, making custom Azure/proxy endpoints appear
            # broken even though their configuration was stored correctly.
            target_url = base_url or (stored or {}).get("base_url") or (p.base_url if p else None)

        if not target_url and p and p.transport == "openai_compat":
            target_url = "https://api.openai.com/v1"

        models: List[str] = []
        headers = {
            "User-Agent": "WISE-Supergent/2.5"
        }

        try:
            if provider_id == "anthropic" or (p and p.transport == "anthropic"):
                if not target_key:
                    return False, [], "Missing API Key for Anthropic"
                resp = requests.get(
                    f"{(target_url or 'https://api.anthropic.com/v1').rstrip('/')}/models?limit=1000",
                    headers={
                        "x-api-key": target_key,
                        "anthropic-version": "2023-06-01",
                        "User-Agent": "WISE-Supergent/2.5"
                    },
                    timeout=12
                )
                if resp.status_code == 401:
                    return False, [], "Invalid API Key for Anthropic (HTTP 401)"
                resp.raise_for_status()
                data = resp.json()
                models = [m["id"] for m in data.get("data", []) if "id" in m]

            elif provider_id == "gemini" or (p and p.transport == "gemini"):
                if not target_key:
                    return False, [], "Missing API Key for Google Gemini"
                # Keep API credentials out of URLs/logs; Gemini supports the
                # x-goog-api-key header for every REST request.
                url = f"{(target_url or 'https://generativelanguage.googleapis.com/v1beta').rstrip('/')}/models?pageSize=1000"
                resp = requests.get(url, headers={**headers, "x-goog-api-key": target_key}, timeout=12)
                if resp.status_code == 400 or resp.status_code == 401 or resp.status_code == 403:
                    try:
                        err = resp.json().get("error", {}).get("message", "Invalid Gemini API Key")
                        return False, [], f"Gemini API Error: {err}"
                    except Exception:
                        return False, [], f"Gemini API Key rejected (HTTP {resp.status_code})"
                resp.raise_for_status()
                data = resp.json()
                for m in data.get("models", []):
                    name = m.get("name", "").replace("models/", "")
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods or "gemini" in name or "gemma" in name:
                        models.append(name)

            elif provider_id == "ollama":
                # Ollama's native list endpoint lives at /api/tags, not below
                # the OpenAI compatibility /v1 prefix.
                local_root = (target_url or "http://localhost:11434").rstrip("/")
                if local_root.endswith("/v1"):
                    local_root = local_root[:-3]
                local_url = local_root + "/api/tags"
                try:
                    resp = requests.get(local_url, headers=headers, timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        models = [m["name"] for m in data.get("models", []) if "name" in m]
                except Exception:
                    # Fallback to /v1/models if /api/tags fails
                    v1_url = local_root + "/v1/models"
                    resp = requests.get(v1_url, headers=headers, timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        models = [m.get("id") or m.get("name") for m in data.get("data", []) if m.get("id") or m.get("name")]

            else:
                # Default OpenAI-compatible endpoint
                if not target_url:
                    return False, [], "No API Base URL configured for provider"
                endpoint = f"{target_url.rstrip('/')}/models"
                if target_key:
                    headers["Authorization"] = f"Bearer {target_key}"
                resp = requests.get(endpoint, headers=headers, timeout=12)
                if resp.status_code == 401:
                    return False, [], f"Invalid API Key for {provider_id} (HTTP 401)"
                elif resp.status_code == 403:
                    return False, [], f"Forbidden: API Key lacks models permission for {provider_id} (HTTP 403)"
                resp.raise_for_status()
                data = resp.json()
                items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                models = [m.get("id") or m.get("name") for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))]

            # Filter unique non-empty models
            unique_models = []
            for m in models:
                if m and str(m) not in unique_models:
                    unique_models.append(str(m))

            if not unique_models:
                return False, [], f"No models returned by {provider_id} API."

            return True, unique_models, f"Successfully fetched {len(unique_models)} live models."

        except requests.exceptions.HTTPError as he:
            err_msg = f"HTTP Error {he.response.status_code}: {he.response.reason}"
            try:
                err_json = he.response.json()
                if "error" in err_json:
                    err_msg = str(err_json["error"].get("message") or err_json["error"])
            except Exception:
                pass
            return False, [], err_msg
        except requests.exceptions.Timeout:
            return False, [], f"Connection timed out connecting to {provider_id} API (12s)"
        except Exception as ex:
            return False, [], f"Connection error: {str(ex)}"


# Global singleton
_LOCAL_MODEL_MANAGER: Optional[LocalModelManager] = None
_LMM_LOCK = threading.Lock()


def get_local_model_manager() -> LocalModelManager:
    global _LOCAL_MODEL_MANAGER
    if _LOCAL_MODEL_MANAGER is None:
        with _LMM_LOCK:
            if _LOCAL_MODEL_MANAGER is None:
                _LOCAL_MODEL_MANAGER = LocalModelManager()
    return _LOCAL_MODEL_MANAGER
