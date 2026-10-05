"""
UniversalExecutor — single dispatcher that runs any tool headless.

Reads ``exec_strategy`` from a manifest (or auto-detects when missing)
and runs the tool through the appropriate driver:

==================  ==========================================
``cli``             ``proot -r rootfs <argv>`` (or host fallback)
``linux-gui``       ``xvfb-run -a <argv>`` inside rootfs
``windows-gui``     ``xvfb-run -a wine <exe>`` inside rootfs
``web-app``         Playwright (chromium headless)
``mac-gui``         not supported — explicitly excluded
==================  ==========================================

Every strategy returns a :class:`ExecutionResult` with stdout / stderr /
exit_code so callers don't have to special-case anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .observability import Tracer
from .platform_manager import PlatformManager, get_platform_manager, lite_preexec


@dataclass
class ExecutionResult:
    ok: bool
    strategy: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


SUPPORTED_STRATEGIES = (
    "cli", "linux-gui", "windows-gui", "web-app",
)


class UniversalExecutor:
    def __init__(self, *, rootfs: Optional[Path] = None,
                 platform_manager: Optional[PlatformManager] = None) -> None:
        self.platform = platform_manager or get_platform_manager()
        self.rootfs = Path(rootfs) if rootfs else (
            Path(self.platform.capabilities.rootfs)
            if self.platform.capabilities.rootfs else None
        )

    # ------------------------------------------------------------ Public API
    def execute(self, manifest: Dict[str, Any],
                *, args: Optional[List[str]] = None,
                stdin: Optional[str] = None,
                timeout: float = 120.0) -> ExecutionResult:
        strategy = (manifest.get("exec_strategy") or "").lower().strip()
        if not strategy:
            strategy = self._detect_strategy(manifest)
        Tracer.emit("universal_executor.start", tool=manifest.get("name"),
                    strategy=strategy, mode=self.platform.mode)

        # Mode gating — heavy strategies are blocked in Lite Mode.
        gate = self._gate_check(strategy, manifest)
        if gate is not None:
            return gate

        argv_extra = list(args or [])
        if strategy == "cli":
            return self._run_cli(manifest, argv_extra, stdin=stdin, timeout=timeout)
        if strategy == "linux-gui":
            return self._run_linux_gui(manifest, argv_extra, timeout=timeout)
        if strategy == "windows-gui":
            return self._run_windows_gui(manifest, argv_extra, timeout=timeout)
        if strategy == "web-app":
            return self._run_web(manifest, argv_extra, timeout=timeout)
        if strategy == "mac-gui":
            return ExecutionResult(False, "mac-gui", 2,
                                   stderr="macOS GUI execution is not supported.")
        return ExecutionResult(False, strategy or "unknown", 2,
                               stderr=f"unsupported strategy: {strategy!r}")

    # --------------------------------------------------------------- Gating
    def _gate_check(self, strategy: str,
                    manifest: Dict[str, Any]) -> Optional[ExecutionResult]:
        if self.platform.is_pro:
            return None
        if strategy in ("windows-gui", "linux-gui", "web-app"):
            tool = (manifest.get("name") or "").lower()
            if tool and tool in {"docker", "wine", "blender", "metasploit-framework",
                                  "burp-suite-testing"}:
                return ExecutionResult(
                    False, strategy, 1,
                    stderr=f"{tool} requires Pro Mode. Switch via "
                           "POST /admin/mode {\"mode\":\"pro\"}.",
                )
        return None

    # -------------------------------------------------------------- Strategies
    def _run_cli(self, manifest: Dict[str, Any], extra: List[str],
                 *, stdin: Optional[str], timeout: float) -> ExecutionResult:
        argv = self._build_cli_argv(manifest, extra)
        if not argv:
            return ExecutionResult(False, "cli", 2,
                                   stderr="empty cli_command in manifest")
        proot_cmd = self._wrap_proot(argv)
        return self._spawn(proot_cmd, "cli", stdin=stdin, timeout=timeout)

    def _run_linux_gui(self, manifest: Dict[str, Any], extra: List[str],
                       *, timeout: float) -> ExecutionResult:
        argv = self._build_cli_argv(manifest, extra)
        if not argv:
            return ExecutionResult(False, "linux-gui", 2,
                                   stderr="empty cli_command in manifest")
        xvfb_cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1280x720x24"]
        full = xvfb_cmd + argv
        wrapped = self._wrap_proot(full)
        return self._spawn(wrapped, "linux-gui", timeout=timeout)

    def _run_windows_gui(self, manifest: Dict[str, Any], extra: List[str],
                         *, timeout: float) -> ExecutionResult:
        exe = manifest.get("windows_exe") or manifest.get("entrypoint") or ""
        if not exe:
            return ExecutionResult(False, "windows-gui", 2,
                                   stderr="manifest.windows_exe missing")
        wine_argv = ["xvfb-run", "-a",
                     "--server-args=-screen 0 1280x720x24",
                     "wine", exe] + extra
        wrapped = self._wrap_proot(wine_argv)
        return self._spawn(wrapped, "windows-gui", timeout=timeout)

    def _run_web(self, manifest: Dict[str, Any], extra: List[str],
                 *, timeout: float) -> ExecutionResult:
        url = manifest.get("url") or (extra[0] if extra else "")
        if not url:
            return ExecutionResult(False, "web-app", 2,
                                   stderr="manifest.url missing for web-app strategy")
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception:
            return ExecutionResult(False, "web-app", 1,
                                   stderr="playwright not installed; "
                                          "run pip install playwright "
                                          "&& playwright install chromium")
        started = time.time()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, timeout=int(timeout * 1000))
                title = page.title()
                content = page.content()
                browser.close()
            return ExecutionResult(
                True, "web-app", 0,
                stdout=title,
                duration_ms=(time.time() - started) * 1000,
                extras={"title": title, "html_size": len(content)},
            )
        except Exception as exc:
            return ExecutionResult(False, "web-app", 1, stderr=str(exc),
                                   duration_ms=(time.time() - started) * 1000)

    # --------------------------------------------------------------- Helpers
    @staticmethod
    def _build_cli_argv(manifest: Dict[str, Any], extra: List[str]) -> List[str]:
        cmd = manifest.get("cli_command") or manifest.get("cli_binary") or ""
        if isinstance(cmd, list):
            argv = list(cmd)
        elif isinstance(cmd, str) and cmd.strip():
            # Naive split is fine — manifests use simple invocations.
            argv = cmd.strip().split()
        else:
            argv = []
        argv.extend(extra)
        return argv

    def _wrap_proot(self, argv: List[str]) -> List[str]:
        if self.rootfs and shutil.which("proot"):
            return ["proot", "-r", str(self.rootfs), *argv]
        return argv

    def _spawn(self, argv: List[str], strategy: str, *,
               stdin: Optional[str] = None,
               timeout: float = 120.0) -> ExecutionResult:
        Tracer.emit("universal_executor.spawn", argv=argv[:6], strategy=strategy)
        started = time.time()
        try:
            # ``echo`` and similar commands are cmd.exe built-ins on Windows,
            # not standalone executables. Preserve the manifest allow-list
            # model while making simple desktop CLI tools portable.
            if os.name == "nt" and argv and argv[0].lower() in {"echo", "cd", "dir", "type"}:
                argv = ["cmd.exe", "/d", "/s", "/c", " ".join(argv)]
            proc = subprocess.run(
                argv,
                input=stdin if stdin is not None else None,
                capture_output=True, text=True,
                timeout=timeout,
                preexec_fn=lite_preexec(self.platform),
                env=os.environ.copy(),
            )
            return ExecutionResult(
                ok=(proc.returncode == 0),
                strategy=strategy,
                exit_code=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                duration_ms=(time.time() - started) * 1000,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(False, strategy, 124,
                                   stdout=exc.stdout or "",
                                   stderr=(exc.stderr or "") + f"\n(timeout {timeout}s)",
                                   duration_ms=(time.time() - started) * 1000)
        except FileNotFoundError as exc:
            return ExecutionResult(False, strategy, 127,
                                   stderr=f"executable not found: {exc}",
                                   duration_ms=(time.time() - started) * 1000)
        except Exception as exc:
            return ExecutionResult(False, strategy, 1, stderr=str(exc),
                                   duration_ms=(time.time() - started) * 1000)

    @staticmethod
    def _detect_strategy(manifest: Dict[str, Any]) -> str:
        if manifest.get("windows_exe"):
            return "windows-gui"
        if manifest.get("url") and not manifest.get("cli_command"):
            return "web-app"
        if (manifest.get("type") or "").lower() == "gui":
            return "linux-gui"
        return "cli"


__all__ = ["UniversalExecutor", "ExecutionResult", "SUPPORTED_STRATEGIES"]
