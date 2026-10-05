"""
PlatformManager — desktop runtime capability and resource manager.

Detects desktop host capabilities (OS, CPU, RAM, GPU, Docker, WSL) and
loads either *Lite Mode* (constrained desktop, sequential, capped) or *Pro
Mode* (parallel, unconstrained) based on the persisted user preference or an
automatic heuristic.

Resource limits in Lite Mode are enforced when subprocesses are spawned
through :class:`UniversalExecutor` and :func:`run_in_lite_mode` — they
call :func:`apply_lite_resource_limits` in the child process before
``exec``.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .observability import Tracer
from .paths import AGENT_OS_ROOT, CONFIG_DIR

#: Path of the persisted mode preference. Created on first save.
MODE_FILE: Path = CONFIG_DIR / "platform_mode.json"

#: Hard tools that are *only* surfaced in Pro Mode. Names match curated map keys.
PRO_ONLY_TOOLS: frozenset = frozenset({
    "docker", "docker-compose", "wine", "winetricks", "xvfb", "xvfb-run",
    "playwright", "playwright-browsers",
    "kubectl", "kubernetes", "k9s", "helm",
    "terraform", "terragrunt",
    "blender",
    "metasploit-framework", "burp-suite-testing",
    "qemu-system", "virtualbox",
})

#: Soft caps used by Lite Mode on constrained desktop hosts.
LITE_LIMITS: Dict[str, int] = {
    "cpu_seconds": 60,
    "memory_bytes": 2 * 1024 * 1024 * 1024,  # 2 GiB
    "max_processes": 256,
    "open_files": 1024,
    "parallel_tool_calls": 1,
}

#: Pro Mode caps. ``0`` means "no limit applied by Agent OS"; the OS still
#: enforces its own limits.
PRO_LIMITS: Dict[str, int] = {
    "cpu_seconds": 0,
    "memory_bytes": 0,
    "max_processes": 0,
    "open_files": 0,
    "parallel_tool_calls": 8,
}


@dataclass
class PlatformCapabilities:
    os: str
    os_release: str
    arch: str
    cpu_cores: int
    ram_bytes: int
    gpu: List[str] = field(default_factory=list)
    docker: bool = False
    wsl: bool = False
    has_xvfb: bool = False
    has_wine: bool = False
    has_playwright: bool = False
    rootfs: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PlatformManager:
    """Singleton that owns mode + capability detection."""

    _instance: Optional["PlatformManager"] = None
    _lock = threading.RLock()

    def __new__(cls) -> "PlatformManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        with self._lock:
            if getattr(self, "_initialised", False):
                return
            self.capabilities = self._detect_capabilities()
            self.mode = self._load_mode_preference() or self._auto_select_mode()
            self._initialised = True
            Tracer.emit("platform.boot",
                        os=self.capabilities.os, mode=self.mode,
                        cores=self.capabilities.cpu_cores,
                        ram_gb=round(self.capabilities.ram_bytes / 1e9, 1))

    # ------------------------------------------------------------------ API
    @property
    def is_lite(self) -> bool:
        return self.mode == "lite"

    @property
    def is_pro(self) -> bool:
        return self.mode == "pro"

    @property
    def limits(self) -> Dict[str, int]:
        # Prefer user-tuned values from ResourceSettingsStore when
        # available — they always reflect the active mode's defaults
        # plus any user overrides.
        try:
            from .resource_settings import get_store, settings_to_limits
            settings = get_store().load(mode=self.mode)
            return settings_to_limits(settings)
        except Exception:
            return dict(LITE_LIMITS if self.is_lite else PRO_LIMITS)

    def set_mode(self, mode: str, *, persist: bool = True) -> str:
        if mode not in ("lite", "pro"):
            raise ValueError(f"unknown mode: {mode!r}")
        old = self.mode
        self.mode = mode
        if persist:
            self._save_mode_preference(mode)
        Tracer.emit("platform.mode_change", old=old, new=mode)
        return mode

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Pro-only tools are hidden in Lite Mode."""
        if not tool_name:
            return True
        if self.is_pro:
            return True
        return tool_name.lower() not in PRO_ONLY_TOOLS

    def filter_tools(self, tools: Dict[str, Any]) -> Dict[str, Any]:
        if self.is_pro:
            return tools
        return {n: t for n, t in tools.items() if self.is_tool_allowed(n)}

    def info(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "limits": self.limits,
            "capabilities": self.capabilities.to_dict(),
            "pro_only_tools": sorted(PRO_ONLY_TOOLS),
            "agent_os_root": str(AGENT_OS_ROOT),
        }

    # ------------------------------------------------------------- Detection
    @staticmethod
    def _detect_capabilities() -> PlatformCapabilities:
        sysname = platform.system().lower()
        rel = platform.release()
        arch = platform.machine() or "unknown"

        cores = os.cpu_count() or 1
        ram = _detect_ram_bytes()
        gpus = _detect_gpus()
        is_docker = bool(shutil.which("docker"))
        is_wsl = "microsoft" in (rel or "").lower() or os.path.exists("/proc/version") and "microsoft" in _safe_read("/proc/version").lower()
        has_xvfb = bool(shutil.which("xvfb-run") or shutil.which("Xvfb"))
        has_wine = bool(shutil.which("wine"))
        has_playwright = _python_module_available("playwright")

        rootfs_path: Optional[str] = None
        for candidate in (AGENT_OS_ROOT / "sandbox" / "rootfs",
                          Path("/home/ubuntu/test_rootfs")):
            if candidate.exists() and (candidate / ".agent-os-stamp").exists():
                rootfs_path = str(candidate)
                break

        return PlatformCapabilities(
            os=sysname, os_release=rel, arch=arch,
            cpu_cores=cores, ram_bytes=ram, gpu=gpus,
            docker=is_docker, wsl=is_wsl,
            has_xvfb=has_xvfb, has_wine=has_wine,
            has_playwright=has_playwright,
            rootfs=rootfs_path,
        )

    def _auto_select_mode(self) -> str:
        cap = self.capabilities
        # Constrained desktops: <2 cores or <4 GiB RAM → lite.
        if cap.cpu_cores < 2 or cap.ram_bytes < 4 * 1024 * 1024 * 1024:
            return "lite"
        return "pro"

    # ------------------------------------------------------------- Persistence
    def _load_mode_preference(self) -> Optional[str]:
        env = (os.environ.get("AGENT_OS_MODE") or "").strip().lower()
        if env in ("lite", "pro"):
            return env
        try:
            data = json.loads(MODE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
        mode = (data or {}).get("mode")
        return mode if mode in ("lite", "pro") else None

    @staticmethod
    def _save_mode_preference(mode: str) -> None:
        MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODE_FILE.write_text(
            json.dumps({"mode": mode, "saved_at": time.time()},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Resource limits — applied in child processes before ``exec``.
# ---------------------------------------------------------------------------
def apply_lite_resource_limits() -> None:
    """Apply Lite-mode rlimits to the *current* process.

    Designed to be passed to :func:`subprocess.Popen` via ``preexec_fn``.
    On platforms that don't support ``resource.setrlimit`` (Windows) this
    is a no-op.
    """
    try:
        import resource  # type: ignore
    except ImportError:
        return

    try:
        resource.setrlimit(resource.RLIMIT_CPU,
                           (LITE_LIMITS["cpu_seconds"], LITE_LIMITS["cpu_seconds"] * 2))
    except Exception:
        pass
    try:
        mem = LITE_LIMITS["memory_bytes"]
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC,
                           (LITE_LIMITS["max_processes"],
                            LITE_LIMITS["max_processes"]))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE,
                           (LITE_LIMITS["open_files"], LITE_LIMITS["open_files"]))
    except Exception:
        pass


def lite_preexec(manager: Optional[PlatformManager] = None):
    """Return a ``preexec_fn`` for ``subprocess.Popen`` based on the active mode."""
    mgr = manager or PlatformManager()
    if mgr.is_lite:
        return apply_lite_resource_limits
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _detect_ram_bytes() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb * 1024
    except Exception:
        pass
    return 4 * 1024 * 1024 * 1024  # safe default


def _detect_gpus() -> List[str]:
    out: List[str] = []
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            res = subprocess.run(
                [nvidia, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=2,
            )
            for line in (res.stdout or "").splitlines():
                line = line.strip()
                if line:
                    out.append(line)
        except Exception:
            pass
    if not out and shutil.which("rocm-smi"):
        out.append("amd-rocm")
    return out


def _python_module_available(name: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _safe_read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


_DEFAULT: Optional[PlatformManager] = None


def get_platform_manager() -> PlatformManager:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = PlatformManager()
    return _DEFAULT


__all__ = [
    "PlatformManager",
    "PlatformCapabilities",
    "apply_lite_resource_limits",
    "lite_preexec",
    "get_platform_manager",
    "PRO_ONLY_TOOLS",
    "LITE_LIMITS",
    "PRO_LIMITS",
]
