"""User-controlled resource settings for Lite/Pro modes.

The settings are persisted to ``$AGENT_OS_HOME/resources.json`` so they
survive restarts. They feed back into :class:`PlatformManager` so any
subprocess Agent OS spawns inherits the correct rlimits, and the
front-end can read/write them via the ``/admin/resources`` endpoints.

Only Pro Mode exposes the toggles for heavy capabilities (Docker, Wine,
heavy security tooling, GPU). Lite Mode silently ignores them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .paths import CONFIG_DIR

LOG = logging.getLogger("agent_os.resource_settings")

SETTINGS_FILE: Path = CONFIG_DIR / "resources.json"


# --- Defaults -----------------------------------------------------------------
LITE_DEFAULTS: Dict[str, Any] = {
    "cpu_seconds": 60,
    "memory_mb": 2048,
    "max_processes": 256,
    "parallel_tools": 1,
    "enable_docker": False,
    "enable_wine": False,
    "enable_heavy_security": False,
    "enable_gpu": False,
    "network_timeout_s": 30,
    "disk_mb": 4096,
    # Phase 8 Part 5 — advanced prompt compression (off by default
    # in Lite Mode because llmlingua pulls in torch/transformers).
    "compression_enabled": False,
    "compression_method": "auto",
    "compression_ratio": 0.5,
}

PRO_DEFAULTS: Dict[str, Any] = {
    "cpu_seconds": 0,        # unlimited
    "memory_mb": 0,          # unlimited
    "max_processes": 1024,
    "parallel_tools": 8,
    "enable_docker": True,
    "enable_wine": True,
    "enable_heavy_security": True,
    "enable_gpu": True,
    "network_timeout_s": 120,
    "disk_mb": 0,            # unlimited
    # Phase 8 Part 5 — advanced prompt compression. Even in Pro Mode
    # we leave it off until the user installs
    # ``requirements-optimization.txt`` explicitly.
    "compression_enabled": False,
    "compression_method": "auto",
    "compression_ratio": 0.5,
}

#: Toggles that only have meaning in Pro Mode. They are forced off when
#: the active mode is Lite, regardless of what is on disk.
PRO_ONLY_KEYS: frozenset = frozenset({
    "enable_docker", "enable_wine",
    "enable_heavy_security", "enable_gpu",
})


@dataclass
class ResourceSettings:
    """Strongly-typed view of the user's resource preferences."""

    cpu_seconds: int = 60
    memory_mb: int = 2048
    max_processes: int = 256
    parallel_tools: int = 1
    enable_docker: bool = False
    enable_wine: bool = False
    enable_heavy_security: bool = False
    enable_gpu: bool = False
    network_timeout_s: int = 30
    disk_mb: int = 4096
    # Advanced prompt compression (Phase 8 Part 5). ``method`` is one
    # of ``auto``, ``llmlingua``, ``llmlingua2``, ``longllmlingua``,
    # ``un-locc``, ``chonkify``, or ``none``. ``ratio`` is the target
    # fraction of the original token count (0.0–1.0).
    compression_enabled: bool = False
    compression_method: str = "auto"
    compression_ratio: float = 0.5
    # Bookkeeping — not part of the user-tunable surface.
    mode_at_save: str = "lite"
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --- Store --------------------------------------------------------------------
class ResourceSettingsStore:
    """Thread-safe JSON-backed settings store."""

    _instance: Optional["ResourceSettingsStore"] = None
    _lock = threading.RLock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "ResourceSettingsStore":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialised = False  # type: ignore[attr-defined]
            return cls._instance

    def __init__(self, *, path: Optional[Path] = None) -> None:
        if getattr(self, "_initialised", False) and path is None:
            return
        self._path = Path(path or SETTINGS_FILE)
        self._initialised = True

    # ------------------------------------------------------------------- API
    @property
    def path(self) -> Path:
        return self._path

    def load(self, *, mode: str) -> ResourceSettings:
        """Load settings, falling back to mode-appropriate defaults."""
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as exc:
                LOG.warning("resources.json corrupt; resetting: %s", exc)
                raw = {}
        else:
            raw = {}

        defaults = PRO_DEFAULTS if mode == "pro" else LITE_DEFAULTS
        merged: Dict[str, Any] = {**defaults, **(raw or {})}
        merged["mode_at_save"] = raw.get("mode_at_save", mode) if raw else mode
        merged["schema_version"] = 1

        # Force Pro-only keys to false in Lite Mode.
        if mode != "pro":
            for k in PRO_ONLY_KEYS:
                merged[k] = False

        return _coerce(merged)

    def save(self, settings: ResourceSettings) -> ResourceSettings:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return settings

    def reset(self, *, mode: str) -> ResourceSettings:
        """Wipe to mode-appropriate defaults."""
        defaults = PRO_DEFAULTS if mode == "pro" else LITE_DEFAULTS
        s = _coerce({**defaults, "mode_at_save": mode, "schema_version": 1})
        return self.save(s)


# --- Coercion / validation ----------------------------------------------------
def _coerce(raw: Dict[str, Any]) -> ResourceSettings:
    def _as_int(key: str, default: int = 0) -> int:
        try:
            v = int(raw.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(0, v)

    def _as_bool(key: str) -> bool:
        v = raw.get(key, False)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    def _as_float(key: str, default: float = 0.0) -> float:
        try:
            v = float(raw.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(0.0, min(1.0, v))

    method = str(raw.get("compression_method", "auto")).strip().lower()
    if method not in {"auto", "llmlingua", "llmlingua2", "longllmlingua",
                       "un-locc", "chonkify", "none"}:
        method = "auto"

    return ResourceSettings(
        cpu_seconds=_as_int("cpu_seconds"),
        memory_mb=_as_int("memory_mb"),
        max_processes=_as_int("max_processes"),
        parallel_tools=max(1, _as_int("parallel_tools", default=1)),
        enable_docker=_as_bool("enable_docker"),
        enable_wine=_as_bool("enable_wine"),
        enable_heavy_security=_as_bool("enable_heavy_security"),
        enable_gpu=_as_bool("enable_gpu"),
        network_timeout_s=max(1, _as_int("network_timeout_s",
                                          default=30)),
        disk_mb=_as_int("disk_mb"),
        compression_enabled=_as_bool("compression_enabled"),
        compression_method=method,
        compression_ratio=_as_float("compression_ratio", default=0.5),
        mode_at_save=str(raw.get("mode_at_save", "lite")),
        schema_version=int(raw.get("schema_version", 1)),
    )


# --- Mode-switch warnings -----------------------------------------------------
def diff_for_mode_switch(current: ResourceSettings,
                         target_mode: str) -> Tuple[ResourceSettings, list[str]]:
    """Produce settings appropriate for ``target_mode`` and a list of
    human-readable warnings for any incompatible field that is being
    reset.
    """
    warnings: list[str] = []
    if target_mode not in ("lite", "pro"):
        return current, [f"unknown mode: {target_mode!r}"]

    payload = current.to_dict()
    if target_mode == "lite":
        for key in PRO_ONLY_KEYS:
            if payload.get(key):
                warnings.append(f"'{key}' will be disabled in Lite Mode")
                payload[key] = False
        defaults = LITE_DEFAULTS
        for key in ("cpu_seconds", "memory_mb", "max_processes",
                    "parallel_tools", "disk_mb", "network_timeout_s"):
            cur = payload.get(key, defaults[key])
            if cur == 0 or cur > defaults[key]:
                warnings.append(
                    f"'{key}' will be capped to {defaults[key]} in Lite Mode "
                    f"(was {cur})"
                )
                payload[key] = defaults[key]
    else:  # target == pro
        # Pro Mode just relaxes — nothing is destructive — but advertise
        # what changes for visibility.
        for key, val in PRO_DEFAULTS.items():
            if payload.get(key) != val and key not in PRO_ONLY_KEYS:
                payload[key] = max(payload.get(key, 0) or 0, val) if isinstance(val, int) and val > 0 else (
                    val if val == 0 else payload.get(key, val)
                )

    payload["mode_at_save"] = target_mode
    return _coerce(payload), warnings


# --- Convenience helpers ------------------------------------------------------
def get_store() -> ResourceSettingsStore:
    return ResourceSettingsStore()


def settings_to_limits(settings: ResourceSettings) -> Dict[str, int]:
    """Convert :class:`ResourceSettings` to the shape PlatformManager
    consumes for ``preexec_fn``-style rlimit application."""
    return {
        "cpu_seconds": settings.cpu_seconds,
        "memory_bytes": settings.memory_mb * 1024 * 1024,
        "max_processes": settings.max_processes,
        "open_files": 1024 if settings.max_processes else 0,
        "parallel_tool_calls": max(1, settings.parallel_tools),
    }


__all__ = [
    "ResourceSettings", "ResourceSettingsStore",
    "LITE_DEFAULTS", "PRO_DEFAULTS", "PRO_ONLY_KEYS",
    "diff_for_mode_switch", "get_store", "settings_to_limits",
    "SETTINGS_FILE",
]
