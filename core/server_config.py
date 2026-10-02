"""Server-side configuration loader for ``api/server.py``.

The desktop UI and external orchestrators talk to Agent OS over HTTP.
This module is the single source of truth for *server-side* knobs:
bind host/port, CORS origins, request size cap, and per-route rate
limits.

Configuration sources, in increasing precedence:

1. ``core/server_config.DEFAULTS``           — code defaults.
2. ``config/server.json`` on disk (if any)   — operator-supplied.
3. Environment variables                     — last word, easy to set
                                                from systemd / docker.

The loader is intentionally tiny so it can be imported during early
startup before the FastAPI app exists. It returns a plain ``dict`` so
tests can copy/mutate without monkey-patching dataclasses.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import CONFIG_DIR, resolve_config_file


SERVER_CONFIG_PATH: Path = CONFIG_DIR / "server.json"


DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 5000,
    "cors_origins": [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:8765",
        "http://127.0.0.1:8765",
    ],
    "max_request_bytes": 1024 * 1024,
    "max_execute_bytes": 65 * 1024,
    # Approved WISE voice.  Isolated runtimes keep the main server lightweight.
    "tts_engine": "indextts",
    "tts_reference_audio": "assets/voice/wise_indextts_reference_canonical.wav",
    "tts_clone_language": "ar",
    "tts_indextts_gpt_checkpoint": "assets/voice/models/wise-indextts-2.5-gpt.pth",
    "stt_engine": "whisper_cpu",
    "stt_model": "base",
    "stt_language": "auto",
    # IndexTTS is demand-loaded.  Forty-five seconds preserves a natural
    # conversational follow-up while releasing several GB of GPU memory soon
    # after the user is done speaking.
    "tts_idle_unload_seconds": 45,
    # ``balanced`` is the production default: it keeps the approved voice but
    # avoids the three-beam decoding and large transient allocations that are
    # impractical beside a local LLM on an 8 GB laptop GPU.
    "tts_quality_profile": "balanced",
    "rate_limit": {
        # 30 chat requests per minute (sustained); burst of 30.
        "chat": {"capacity": 30, "refill_per_sec": 0.5},
        # 10 sandbox commands per minute; burst of 10.
        "execute": {"capacity": 10, "refill_per_sec": 10.0 / 60.0},
    },
}


def _coerce(default_value: Any, raw: str) -> Any:
    """Coerce an env-string to the same type as ``default_value``."""
    if isinstance(default_value, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default_value, int) and not isinstance(default_value, bool):
        try:
            return int(raw.strip())
        except ValueError:
            return default_value
    if isinstance(default_value, float):
        try:
            return float(raw.strip())
        except ValueError:
            return default_value
    if isinstance(default_value, list):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return raw


def _read_disk() -> Dict[str, Any]:
    # A detached runtime tree starts from the checked-in operator defaults,
    # then may override them with its own writable server.json.
    source = resolve_config_file("server.json")
    if not source.exists():
        return {}
    try:
        return json.loads(source.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _apply_env(cfg: Dict[str, Any]) -> Dict[str, Any]:
    # Top-level scalars.
    for env_key, cfg_key in (
        ("AGENT_API_HOST", "host"),
        ("AGENT_API_PORT", "port"),
        ("AGENT_MAX_REQUEST_BYTES", "max_request_bytes"),
        ("AGENT_MAX_EXECUTE_BYTES", "max_execute_bytes"),
    ):
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        cfg[cfg_key] = _coerce(DEFAULTS[cfg_key], raw)

    raw_cors = os.environ.get("AGENT_CORS_ORIGINS")
    if raw_cors is not None:
        cfg["cors_origins"] = [s.strip() for s in raw_cors.split(",")
                               if s.strip()] or list(DEFAULTS["cors_origins"])

    # Rate-limit overrides (per-route capacity/refill).
    rl_block = cfg.setdefault("rate_limit", {})
    for route in ("chat", "execute"):
        per_route = rl_block.setdefault(
            route, copy.deepcopy(DEFAULTS["rate_limit"][route]))
        cap_env = os.environ.get(f"AGENT_RATE_LIMIT_{route.upper()}_CAPACITY")
        if cap_env is not None:
            try:
                per_route["capacity"] = max(1, int(cap_env))
            except ValueError:
                pass
        rps_env = os.environ.get(
            f"AGENT_RATE_LIMIT_{route.upper()}_REFILL_PER_SEC")
        if rps_env is not None:
            try:
                per_route["refill_per_sec"] = max(0.001, float(rps_env))
            except ValueError:
                pass
    return cfg


def load_server_config() -> Dict[str, Any]:
    """Return a fully-merged config dict.

    Always returns a deep copy so callers may mutate freely without
    affecting the on-disk file or shared state.
    """
    cfg = copy.deepcopy(DEFAULTS)
    disk = _read_disk()
    _deep_merge(cfg, disk)
    _apply_env(cfg)
    return cfg


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    """Recursive dict merge that overwrites scalars and lists."""
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


def cors_origins() -> List[str]:
    """Convenience accessor used by the FastAPI bootstrap."""
    cfg = load_server_config()
    origins = cfg.get("cors_origins") or list(DEFAULTS["cors_origins"])
    return list(origins)


def save_server_config(cfg: Dict[str, Any]) -> None:
    """Persist the configuration dict to ``config/server.json``.

    Creates the config directory if it doesn't exist.
    Only saves keys that differ from ``DEFAULTS`` to keep the file minimal.
    """
    try:
        SERVER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=SERVER_CONFIG_PATH.parent,
                                             prefix="server-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(cfg, handle, indent=2, ensure_ascii=False)
                handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, SERVER_CONFIG_PATH)
        finally:
            if temporary is not None and temporary.exists(): temporary.unlink()
    except Exception as exc:
        import logging
        logging.getLogger("WISE.ServerConfig").warning(
            "Failed to save server config: %s", exc,
        )
        raise


__all__ = [
    "DEFAULTS",
    "SERVER_CONFIG_PATH",
    "load_server_config",
    "save_server_config",
    "cors_origins",
]
