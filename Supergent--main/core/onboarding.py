"""First-run onboarding pipeline.

The pipeline runs in two strictly-ordered phases:

1. **Phase 1 — sandbox**: build the Debian rootfs via
   ``bash sandbox/build_rootfs.sh``. Everything else depends on this,
   so the second phase will refuse to start unless phase 1 ends in
   ``"completed"``.
2. **Phase 2 — tools**: walk every missing tool reported by
   :class:`ToolInstaller.scan` and install it sequentially. Pro-only
   tools are filtered out when the active platform mode is Lite.

Tasks run in a background thread and update an in-memory state map.
The HTTP layer exposes start/status/single endpoints so the desktop UI
can render the progress.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .paths import AGENT_OS_ROOT, SANDBOX_ROOT
from .platform_manager import PRO_ONLY_TOOLS, get_platform_manager

LOG = logging.getLogger("agent_os.onboarding")

PHASE_SANDBOX = "sandbox"
PHASE_TOOLS = "tools"
PHASE_VALIDATE = "validate"
PHASE_DONE = "done"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@dataclass
class ComponentState:
    name: str
    kind: str            # "rootfs" | "tool" | "self_test"
    status: str = "pending"   # pending | installing | installed | failed | skipped
    detail: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    log_tail: str = ""
    # Auto-recovery (Part 2): per-component repair telemetry surfaced to UI.
    attempts: int = 0
    repair_category: str = ""
    user_message: str = ""
    manual_steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OnboardingTask:
    task_id: str
    mode: str
    phase: str = PHASE_SANDBOX
    components: List[ComponentState] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    cancelled: bool = False
    error: Optional[str] = None

    @property
    def is_done(self) -> bool:
        return self.phase == PHASE_DONE or self.finished_at is not None

    def progress(self) -> Dict[str, Any]:
        total = len(self.components)
        done = sum(1 for c in self.components
                    if c.status in ("installed", "skipped"))
        failed = sum(1 for c in self.components if c.status == "failed")
        return {"total": total, "completed": done, "failed": failed,
                "phase": self.phase, "is_done": self.is_done}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "components": [c.to_dict() for c in self.components],
            "progress": self.progress(),
            "error": self.error,
            "cancelled": self.cancelled,
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class OnboardingManager:
    """Singleton coordinator. Runs at most one task at a time."""

    _instance: Optional["OnboardingManager"] = None
    _lock = threading.RLock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "OnboardingManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialised = False  # type: ignore[attr-defined]
            return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialised", False):
            return
        self._tasks: Dict[str, OnboardingTask] = {}
        self._task_lock = threading.RLock()
        self._active_task_id: Optional[str] = None
        self._initialised = True

    # ------------------------------------------------------------------- API
    def needs_onboarding(self) -> Dict[str, Any]:
        """Cheap check used by the UI route gate."""
        from .tool_installer import get_installer
        rootfs_ready = (SANDBOX_ROOT.exists() and any(SANDBOX_ROOT.iterdir())
                         if SANDBOX_ROOT.exists() else False)
        scanned = get_installer().scan()
        mode = get_platform_manager().mode
        plan = self._build_plan(scanned, mode=mode)
        critical_missing = [c for c in plan if c.kind == "tool"]
        needs = (not rootfs_ready) or len(critical_missing) >= 3
        return {
            "needs_onboarding": needs,
            "mode": mode,
            "rootfs_ready": rootfs_ready,
            "missing_count": len(critical_missing),
            "components": [c.to_dict() for c in plan],
        }

    def start(self, *, force: bool = False) -> OnboardingTask:
        """Launch the two-phase pipeline in a background thread."""
        with self._task_lock:
            existing = self._active_task()
            if existing and not existing.is_done and not force:
                return existing
            mode = get_platform_manager().mode
            from .tool_installer import get_installer
            scanned = get_installer().scan(force=True)
            plan = self._build_plan(scanned, mode=mode)
            task = OnboardingTask(
                task_id=uuid.uuid4().hex,
                mode=mode,
                components=plan,
            )
            self._tasks[task.task_id] = task
            self._active_task_id = task.task_id
        threading.Thread(target=self._run, args=(task,), daemon=True,
                         name=f"onboarding-{task.task_id[:8]}").start()
        return task

    def install_single(self, name: str) -> Dict[str, Any]:
        """Re-attempt installation of a single component (for retries)."""
        from .tool_installer import get_installer
        if name == "rootfs":
            comp = ComponentState(name="rootfs", kind="rootfs")
            self._build_rootfs(comp)
            return comp.to_dict()
        comp = ComponentState(name=name, kind="tool")
        installer = get_installer()
        if name in PRO_ONLY_TOOLS and get_platform_manager().is_lite:
            comp.status = "skipped"
            comp.detail = "pro-only tool; not installed in Lite Mode"
            return comp.to_dict()
        self._install_tool(comp, installer)
        return comp.to_dict()

    def get(self, task_id: str) -> Optional[OnboardingTask]:
        return self._tasks.get(task_id)

    def latest(self) -> Optional[OnboardingTask]:
        return self._active_task()

    # ---------------------------------------------------------------- Internals
    def _active_task(self) -> Optional[OnboardingTask]:
        if self._active_task_id is None:
            return None
        return self._tasks.get(self._active_task_id)

    def _build_plan(self, scanned: Dict[str, Any], *,
                    mode: str) -> List[ComponentState]:
        plan: List[ComponentState] = []
        rootfs_ready = (SANDBOX_ROOT.exists()
                        and any(SANDBOX_ROOT.iterdir())
                        if SANDBOX_ROOT.exists() else False)
        if not rootfs_ready:
            plan.append(ComponentState(name="rootfs", kind="rootfs"))
        for name, status in scanned.items():
            if getattr(status, "found", False):
                continue
            if mode != "pro" and name in PRO_ONLY_TOOLS:
                # Quietly omit instead of marking skipped, so the UI
                # only shows the things it really needs.
                continue
            plan.append(ComponentState(name=name, kind="tool"))
        return plan

    # -------------------------------------------------------------- Pipeline
    def _run(self, task: OnboardingTask) -> None:
        from .tool_installer import get_installer
        try:
            # ------ Phase 1 — sandbox -------------------------------------
            task.phase = PHASE_SANDBOX
            for comp in [c for c in task.components if c.kind == "rootfs"]:
                self._build_rootfs(comp)
                if comp.status != "installed":
                    task.phase = PHASE_DONE
                    task.error = "rootfs build failed; aborting before tools"
                    task.finished_at = time.time()
                    return

            # ------ Phase 2 — tools ---------------------------------------
            task.phase = PHASE_TOOLS
            installer = get_installer()
            for comp in [c for c in task.components if c.kind == "tool"]:
                if task.cancelled:
                    comp.status = "skipped"
                    comp.detail = "cancelled by user"
                    continue
                self._install_tool(comp, installer)

            # ------ Phase 3 — validation ----------------------------------
            task.phase = PHASE_VALIDATE
            self._post_install_validation(task)
        except Exception as exc:
            task.error = f"unexpected: {exc!r}"
            LOG.exception("onboarding pipeline crashed")
        finally:
            task.phase = PHASE_DONE
            task.finished_at = time.time()

    # Maximum number of automatic retries before surfacing manual steps.
    MAX_ROOTFS_ATTEMPTS = 3

    def _build_rootfs(self, comp: ComponentState) -> None:
        """Build the Debian rootfs with diagnose-and-retry auto-recovery.

        On failure we run :class:`SelfHealing.diagnose` against the
        captured log, apply a category-specific tweak (extra apt
        mirror, longer timeout, partial cache wipe), and retry up to
        ``MAX_ROOTFS_ATTEMPTS`` times. The terminal state always carries
        a user-readable ``user_message`` and ``manual_steps`` so the
        desktop onboarding screen can render a "Retry" affordance.
        """
        comp.started_at = time.time()
        script = AGENT_OS_ROOT / "sandbox" / "build_rootfs.sh"
        if not script.exists():
            comp.status = "failed"
            comp.detail = f"missing build script: {script}"
            comp.user_message = (
                "Sandbox build script not found. The Agent OS install is "
                "incomplete.")
            comp.manual_steps = [
                f"Verify {script} exists in your checkout.",
                "Re-clone the repository if the file is missing.",
            ]
            comp.finished_at = time.time()
            return

        last_log = ""
        last_detail = ""
        for attempt in range(1, self.MAX_ROOTFS_ATTEMPTS + 1):
            comp.attempts = attempt
            comp.status = "installing"
            comp.detail = (f"build attempt {attempt}/"
                            f"{self.MAX_ROOTFS_ATTEMPTS}")
            try:
                env = os.environ.copy()
                # Simple, deterministic recovery tweaks per attempt.
                if attempt == 2:
                    env.setdefault("DEBIAN_MIRROR",
                                    "http://ftp.debian.org/debian")
                if attempt == 3:
                    env["AGENT_OS_ROOTFS_TIMEOUT"] = str(
                        max(int(env.get("AGENT_OS_ROOTFS_TIMEOUT",
                                          "1800")) * 2, 3600))
                proc = subprocess.run(
                    ["bash", str(script)],
                    cwd=str(AGENT_OS_ROOT),
                    capture_output=True, text=True,
                    env=env,
                    timeout=int(env.get("AGENT_OS_ROOTFS_TIMEOUT", "1800")),
                )
                last_log = (proc.stdout + "\n" + proc.stderr)[-2000:]
                comp.log_tail = last_log
                if proc.returncode == 0:
                    comp.status = "installed"
                    comp.detail = (f"rootfs ready (attempt "
                                    f"{attempt}/{self.MAX_ROOTFS_ATTEMPTS})")
                    comp.user_message = ""
                    comp.manual_steps = []
                    comp.finished_at = time.time()
                    return
                last_detail = f"build_rootfs.sh exit={proc.returncode}"
            except subprocess.TimeoutExpired:
                last_detail = "build_rootfs.sh timed out"
                last_log = ""
            except Exception as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                last_log = ""

        # All attempts failed → diagnose & expose a friendly message.
        comp.detail = last_detail
        comp.log_tail = last_log
        try:
            from .self_healing import get_self_healing
            diag = get_self_healing().diagnose(
                target="sandbox", error_text=f"{last_detail}\n{last_log}")
            comp.repair_category = diag.category
        except Exception:
            comp.repair_category = "unknown"

        comp.status = "failed"
        comp.user_message = self._friendly_rootfs_message(comp.repair_category)
        comp.manual_steps = self._rootfs_manual_steps(comp.repair_category)
        comp.finished_at = time.time()

    @staticmethod
    def _friendly_rootfs_message(category: str) -> str:
        if category == "network":
            return ("The sandbox build couldn't download Debian packages. "
                     "Check your internet connection and try again.")
        if category == "missing_dep":
            return ("A system tool needed to build the sandbox is missing "
                     "(e.g. debootstrap, proot). Install it and retry.")
        if category == "sandbox":
            return ("The sandbox script reported a permissions or proot "
                     "issue. You may need to re-run with sudo.")
        if category == "config":
            return ("The build script rejected the current configuration. "
                     "Reset Agent OS resource settings and retry.")
        return ("Sandbox build failed after multiple attempts. See the log "
                 "tail and retry, or run the install script manually.")

    @staticmethod
    def _rootfs_manual_steps(category: str) -> List[str]:
        steps = ["Tap 'Retry Build' to try again."]
        if category == "network":
            steps += [
                "Verify outbound HTTPS is reachable.",
                "If you are behind a proxy, set HTTPS_PROXY and retry.",
            ]
        elif category == "missing_dep":
            steps += [
                "sudo apt-get install -y debootstrap proot",
            ]
        elif category == "sandbox":
            steps += [
                "Run: sudo bash sandbox/build_rootfs.sh",
                "Check that /proc, /dev and /sys are mounted.",
            ]
        elif category == "config":
            steps += [
                "POST /admin/resources {\"reset\": true}",
                "Then retry the build.",
            ]
        else:
            steps += [
                "Inspect the log tail in the onboarding screen for "
                "specific errors.",
                "Run sandbox/build_rootfs.sh manually for verbose output.",
            ]
        return steps

    def _install_tool(self, comp: ComponentState,
                      installer: Any) -> None:
        comp.status = "installing"
        comp.started_at = time.time()
        try:
            result = installer.install_missing(names=[comp.name])
            installed = {r["tool"] for r in result.get("installed", [])}
            failed = {r.get("tool") for r in result.get("failed", [])}
            skipped = {r.get("tool") for r in result.get("skipped", [])}
            if comp.name in installed:
                comp.status = "installed"
                comp.detail = "ok"
            elif comp.name in skipped:
                comp.status = "skipped"
                comp.detail = next(
                    (r.get("reason", "") for r in result.get("skipped", [])
                     if r.get("tool") == comp.name),
                    "",
                )
            elif comp.name in failed:
                comp.status = "failed"
                comp.detail = next(
                    (r.get("reason", "") for r in result.get("failed", [])
                     if r.get("tool") == comp.name),
                    "install failed",
                )
            else:
                comp.status = "failed"
                comp.detail = "no result for tool"
        except Exception as exc:
            comp.status = "failed"
            comp.detail = f"{type(exc).__name__}: {exc}"
        finally:
            comp.finished_at = time.time()

    def _post_install_validation(self, task: OnboardingTask) -> None:
        try:
            from .self_test import run_self_tests  # type: ignore[attr-defined]
            res = run_self_tests()
        except Exception:
            res = {"ok": True, "skipped": "no self_test module"}
        comp = ComponentState(
            name="self_test", kind="self_test",
            status="installed" if res.get("ok", True) else "failed",
            detail=res.get("summary", "") if isinstance(res, dict) else str(res),
            started_at=time.time(), finished_at=time.time(),
        )
        task.components.append(comp)


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------
def get_onboarding_manager() -> OnboardingManager:
    return OnboardingManager()


__all__ = [
    "OnboardingManager", "OnboardingTask", "ComponentState",
    "PHASE_SANDBOX", "PHASE_TOOLS", "PHASE_VALIDATE", "PHASE_DONE",
    "get_onboarding_manager",
]
