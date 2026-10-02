"""
Windows Session Daemon (WindowsSessionDaemon).
Ensures WISE is attached to the Interactive User Session (Session 1+, not Session 0),
manages Windows startup registration (Task Scheduler / Registry Run),
audits permissions and onboarding state (Limited vs Full Integrated Mode),
and monitors Session Lock/Unlock and System Sleep/Wake power notifications.
"""

from __future__ import annotations

import os
import sys
import time
import logging
import threading
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field, asdict

from core.windows.event_bus import get_event_bus, WindowsEvent
from core.state.state_machine import get_state_machine, WiseState

LOG = logging.getLogger("wise.session_daemon")

# Keep the legacy session-daemon API aligned with the owner-controlled
# Super Computer task.  It is deliberately an interactive *user* task, not a
# SYSTEM service and not an always-elevated process.
_AUTOSTART_TASK_NAME = "WISE Super Computer (User Session)"
_LEGACY_AUTOSTART_TASK_NAME = "WISE_Persistent_Layer"

# Win32 Constants
WTS_CONSOLE_CONNECT = 0x1
WTS_CONSOLE_DISCONNECT = 0x2
WTS_REMOTE_CONNECT = 0x3
WTS_REMOTE_DISCONNECT = 0x4
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOGOFF = 0x6
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8

# Power Broadcast Constants
PBT_APMQUERYSUSPEND = 0x0000
PBT_APMQUERYSTANDBY = 0x0001
PBT_APMQUERYSUSPENDFAILED = 0x0002
PBT_APMQUERYSTANDBYFAILED = 0x0003
PBT_APMSUSPEND = 0x0004
PBT_APMSTANDBY = 0x0005
PBT_APMRESUMECRITICAL = 0x0006
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMESTANDBY = 0x0008
PBT_APMBATTERYLOW = 0x0009
PBT_APMPOWERSTATUSCHANGE = 0x000A
PBT_APMOEMEVENT = 0x000B
PBT_APMRESUMEAUTOMATIC = 0x0012


class OnboardingMode(str, Enum):
    ONBOARDING_PENDING = "ONBOARDING_PENDING"
    LIMITED_MODE = "LIMITED_MODE"
    FULL_INTEGRATED_MODE = "FULL_INTEGRATED_MODE"


@dataclass
class PermissionAudit:
    session_id: int
    is_interactive_session: bool
    current_user: str
    is_admin: bool
    has_event_bus_hook: bool
    has_autostart_configured: bool
    autostart_method: Optional[str] = None
    onboarding_mode: OnboardingMode = OnboardingMode.LIMITED_MODE
    missing_permissions: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["onboarding_mode"] = self.onboarding_mode.value
        return d


class WindowsSessionDaemon:
    """Manages attachment to the Windows interactive user session, startup, and power lifecycle."""

    def __init__(self):
        self._lock = threading.RLock()
        self._running = False
        self._session_id: int = 0
        self._is_interactive: bool = False
        self._current_user: str = os.environ.get("USERNAME", "Unknown")
        self._is_admin: bool = False
        self._is_locked: bool = False
        self._is_sleeping: bool = False
        self._onboarding_mode: OnboardingMode = OnboardingMode.LIMITED_MODE
        self._event_bus = get_event_bus()
        self._state_machine = get_state_machine()

        # Probe session and privileges
        self._probe_session()
        self._probe_elevation()
        self._audit_onboarding()

    def _probe_session(self) -> None:
        """Determines the current Windows session ID and whether it is interactive."""
        if sys.platform != "win32":
            self._session_id = 1
            self._is_interactive = True
            return

        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            
            # Try ProcessIdToSessionId
            pid = kernel32.GetCurrentProcessId()
            session_id = wintypes.DWORD()
            if kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
                self._session_id = session_id.value
            else:
                # Fallback to WTSGetActiveConsoleSessionId
                self._session_id = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()

            # Session 0 is isolated service session. Session 1+ is interactive user desktop
            self._is_interactive = (self._session_id > 0)
            LOG.info("Windows Session Probed: Session ID=%d (Interactive: %s)", self._session_id, self._is_interactive)
        except Exception as e:
            LOG.warning("Failed to query Windows Session ID: %s. Defaulting to interactive.", e)
            self._session_id = 1
            self._is_interactive = True

    def _probe_elevation(self) -> None:
        """Checks if current process possesses elevated Administrator rights."""
        if sys.platform != "win32":
            self._is_admin = (os.geteuid() == 0) if hasattr(os, "geteuid") else False
            return

        try:
            import ctypes
            self._is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception as e:
            LOG.debug("Elevation check error: %s", e)
            self._is_admin = False

    def _audit_onboarding(self) -> PermissionAudit:
        """Audits all Windows capabilities to determine if WISE is in Full or Limited mode."""
        missing = []
        if not self._is_interactive:
            missing.append("interactive_session_required (currently running in Session 0)")

        # Check startup registration
        autostart_configured, autostart_method = self.check_autostart_status()
        if not autostart_configured:
            missing.append("autostart_not_configured")

        # Determine mode
        if not self._is_interactive:
            mode = OnboardingMode.ONBOARDING_PENDING
        elif missing:
            mode = OnboardingMode.LIMITED_MODE
        else:
            mode = OnboardingMode.FULL_INTEGRATED_MODE

        self._onboarding_mode = mode

        return PermissionAudit(
            session_id=self._session_id,
            is_interactive_session=self._is_interactive,
            current_user=self._current_user,
            is_admin=self._is_admin,
            has_event_bus_hook=True,
            has_autostart_configured=autostart_configured,
            autostart_method=autostart_method,
            onboarding_mode=mode,
            missing_permissions=missing,
        )

    def get_audit(self) -> PermissionAudit:
        with self._lock:
            return self._audit_onboarding()

    # -------------------------------------------------------------------------
    # Windows Startup Registration (Interactive Session)
    # -------------------------------------------------------------------------
    def check_autostart_status(self) -> tuple[bool, Optional[str]]:
        """Checks if WISE is registered in Windows Startup (Task Scheduler or HKCU Run)."""
        if sys.platform != "win32":
            return True, "linux_systemd_or_profile"

        # 1. Check Task Scheduler
        try:
            res = subprocess.run(
                ["schtasks", "/query", "/tn", _AUTOSTART_TASK_NAME, "/fo", "csv"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and _AUTOSTART_TASK_NAME in res.stdout:
                return True, "task_scheduler"
        except Exception:
            pass

        # 2. Check HKCU Run key
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            ) as key:
                val, _ = winreg.QueryValueEx(key, "WISE")
                if val:
                    return True, "registry_hkcu_run"
        except Exception:
            pass

        return False, None

    def register_autostart(self, python_script_path: Optional[str] = None, use_scheduler: bool = True) -> Dict[str, Any]:
        """Registers WISE to launch upon Windows logon into the user's interactive desktop."""
        if sys.platform != "win32":
            return {"success": False, "reason": "Platform not Windows"}

        target_script = python_script_path or os.path.abspath(sys.argv[0])
        python_exe = sys.executable

        # Option A: Task Scheduler with an interactive user-session trigger.
        # This legacy helper intentionally never registers /RL HIGHEST.  UAC
        # is requested later for a specific maintenance operation, not granted
        # forever to a background agent.
        if use_scheduler:
            run_cmd = f'"{python_exe}" "{target_script}"'
            username = os.environ.get("USERNAME")
            cmd = [
                "schtasks", "/create", "/tn", _AUTOSTART_TASK_NAME,
                "/tr", run_cmd, "/sc", "onlogon",
            ]
            if username:
                cmd.extend(["/ru", username, "/it"])
            cmd.extend(["/rl", "limited", "/f"])
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
                if res.returncode == 0:
                    LOG.info("Registered WISE in Task Scheduler for interactive logon.")
                    self._audit_onboarding()
                    return {"success": True, "method": "task_scheduler", "command": run_cmd}
                else:
                    LOG.warning("Task scheduler registration failed: %s. Falling back to Registry.", res.stderr)
            except Exception as e:
                LOG.warning("Task scheduler registration error: %s", e)

        # Option B: HKCU Registry Run (Standard user permissions, interactive logon)
        try:
            import winreg
            run_cmd = f'"{python_exe}" "{target_script}"'
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, "WISE", 0, winreg.REG_SZ, run_cmd)
            LOG.info("Registered WISE in HKCU Run registry.")
            self._audit_onboarding()
            return {"success": True, "method": "registry_hkcu_run", "command": run_cmd}
        except Exception as e:
            LOG.error("Failed to write to HKCU Run: %s", e)
            return {"success": False, "reason": str(e)}

    def unregister_autostart(self) -> Dict[str, Any]:
        """Removes WISE from Windows autostart."""
        if sys.platform != "win32":
            return {"success": True}

        removed = []
        # Task Scheduler
        try:
            for task_name in (_AUTOSTART_TASK_NAME, _LEGACY_AUTOSTART_TASK_NAME):
                res = subprocess.run(
                    ["schtasks", "/delete", "/tn", task_name, "/f"],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
                if res.returncode == 0:
                    removed.append("task_scheduler")
        except Exception:
            pass

        # Registry
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, "WISE")
                removed.append("registry_hkcu_run")
        except Exception:
            pass

        self._audit_onboarding()
        return {"success": True, "removed": removed}

    # -------------------------------------------------------------------------
    # Session Lock/Unlock & Power Broadcast Event Dispatch
    # -------------------------------------------------------------------------
    def notify_session_event(self, event_code: int, session_id: int) -> None:
        """Dispatches session change notifications onto the EventBus and transitions state."""
        with self._lock:
            if event_code == WTS_SESSION_LOCK:
                self._is_locked = True
                LOG.info("Session LOCKED (Session %d)", session_id)
                self._event_bus.publish(WindowsEvent(
                    topic="session.lock",
                    data={"session_id": session_id, "locked": True},
                    source="session_daemon",
                ))
            elif event_code == WTS_SESSION_UNLOCK:
                self._is_locked = False
                LOG.info("Session UNLOCKED (Session %d)", session_id)
                self._event_bus.publish(WindowsEvent(
                    topic="session.unlock",
                    data={"session_id": session_id, "locked": False},
                    source="session_daemon",
                ))
            elif event_code == WTS_SESSION_LOGOFF:
                LOG.info("Session LOGOFF (Session %d)", session_id)
                self._event_bus.publish(WindowsEvent(
                    topic="session.logoff",
                    data={"session_id": session_id},
                    source="session_daemon",
                ))

    def notify_power_event(self, power_code: int) -> None:
        """Dispatches power change notifications (Sleep/Wake/AC/Battery) onto EventBus."""
        with self._lock:
            if power_code in (PBT_APMSUSPEND, PBT_APMSTANDBY):
                self._is_sleeping = True
                LOG.info("System entering SLEEP/STANDBY")
                self._event_bus.publish(WindowsEvent(
                    topic="power.sleep",
                    data={"sleeping": True, "power_code": power_code},
                    source="session_daemon",
                ))
            elif power_code in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESTANDBY):
                self._is_sleeping = False
                LOG.info("System RESUMING from sleep")
                self._event_bus.publish(WindowsEvent(
                    topic="power.wake",
                    data={"sleeping": False, "power_code": power_code},
                    source="session_daemon",
                ))
            elif power_code == PBT_APMPOWERSTATUSCHANGE:
                LOG.info("Power status changed (AC / Battery transition)")
                self._event_bus.publish(WindowsEvent(
                    topic="power.status_change",
                    data={"power_code": power_code},
                    source="session_daemon",
                ))

    def get_status(self) -> Dict[str, Any]:
        """Returns comprehensive status of the session daemon."""
        with self._lock:
            return {
                "session_id": self._session_id,
                "is_interactive": self._is_interactive,
                "current_user": self._current_user,
                "is_admin": self._is_admin,
                "is_locked": self._is_locked,
                "is_sleeping": self._is_sleeping,
                "onboarding_mode": self._onboarding_mode.value,
            }


# Singleton
_GLOBAL_SESSION_DAEMON: Optional[WindowsSessionDaemon] = None
_SD_LOCK = threading.Lock()


def get_session_daemon() -> WindowsSessionDaemon:
    global _GLOBAL_SESSION_DAEMON
    if _GLOBAL_SESSION_DAEMON is None:
        with _SD_LOCK:
            if _GLOBAL_SESSION_DAEMON is None:
                _GLOBAL_SESSION_DAEMON = WindowsSessionDaemon()
    return _GLOBAL_SESSION_DAEMON
