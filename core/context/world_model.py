"""
Computer World Model (ComputerWorldModel).
A live, structured object model representing the entire state of the host computer:
OS, User Session, Displays, Active Window, Applications, Processes, Files,
Devices, Network, Resources, Tasks, Browser State, and WISE State.
Updated event-driven via WindowsEventBus with zero-compute overhead.
"""

from __future__ import annotations

import os
import sys
import time
import platform
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

import psutil

from core.state.state_machine import get_state_machine, WiseState
from core.resource.resource_manager import get_resource_manager, HardwareMetrics
from core.windows.event_bus import get_event_bus, WindowsEvent

LOG = logging.getLogger("wise.world_model")


@dataclass
class ActiveWindowInfo:
    hwnd: int = 0
    title: str = ""
    process_name: str = ""
    pid: int = 0
    geometry: Dict[str, int] = field(default_factory=lambda: {"x": 0, "y": 0, "w": 0, "h": 0})
    updated_at: float = field(default_factory=time.time)


@dataclass
class OSInfo:
    system: str = "Windows"
    release: str = ""
    version: str = ""
    build: str = ""
    arch: str = ""
    uptime_seconds: float = 0.0


@dataclass
class UserSessionInfo:
    username: str = ""
    is_locked: bool = False
    is_admin: bool = False
    session_id: int = 1


@dataclass
class FilesInfo:
    desktop_dir: str = ""
    documents_dir: str = ""
    downloads_dir: str = ""
    latest_download: Optional[str] = None


@dataclass
class NetworkInfo:
    hostname: str = ""
    local_ip: str = ""
    internet_connected: bool = True
    active_interfaces: List[str] = field(default_factory=list)


@dataclass
class BrowserState:
    running_browsers: List[str] = field(default_factory=list)
    active_url: Optional[str] = None
    cdp_connected: bool = False


@dataclass
class ComputerWorldModel:
    """The central World State representation of the computer."""

    os_info: OSInfo = field(default_factory=OSInfo)
    user_session: UserSessionInfo = field(default_factory=UserSessionInfo)
    active_window: ActiveWindowInfo = field(default_factory=ActiveWindowInfo)
    open_windows: List[Dict[str, Any]] = field(default_factory=list)
    processes_summary: Dict[str, Any] = field(default_factory=dict)
    files_info: FilesInfo = field(default_factory=FilesInfo)
    network_info: NetworkInfo = field(default_factory=NetworkInfo)
    browser_state: BrowserState = field(default_factory=BrowserState)
    current_tasks: List[Dict[str, Any]] = field(default_factory=list)
    system_power_state: str = "ACTIVE"
    last_full_refresh: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        sm = get_state_machine()
        rm = get_resource_manager()
        metrics = rm.get_hardware_metrics()

        return {
            "os": asdict(self.os_info),
            "user_session": asdict(self.user_session),
            "active_window": asdict(self.active_window),
            "open_windows_count": len(self.open_windows),
            "open_windows": self.open_windows[:15],  # Top 15 visible
            "processes_summary": self.processes_summary,
            "files": asdict(self.files_info),
            "network": asdict(self.network_info),
            "browser": asdict(self.browser_state),
            "current_tasks": self.current_tasks,
            "wise_state": {
                "state": sm.current_state.value,
                "is_low_power": sm.current_profile.is_low_power,
                "time_in_state": round(sm.time_in_current_state, 1),
            },
            "system_power_state": self.system_power_state,
            "resources": metrics.to_dict(),
            "last_full_refresh": self.last_full_refresh,
        }

    def format_summary(self) -> str:
        """Generates a high-signal, compact markdown summary for LLM context injection."""
        sm = get_state_machine()
        rm = get_resource_manager()
        metrics = rm.get_hardware_metrics()

        win = self.active_window
        win_desc = f"'{win.title}' ({win.process_name}, PID {win.pid})" if win.title else "None"
        open_apps = ", ".join(w.get("process_name", "") for w in self.open_windows[:8] if w.get("process_name"))

        lines = [
            "### Computer World Model Summary:",
            f"- **Host**: {self.os_info.system} {self.os_info.release} (Build {self.os_info.build}) | User: `{self.user_session.username}`",
            f"- **Active Foreground Window**: {win_desc}",
            f"- **Visible Applications**: {open_apps or 'None'}",
            f"- **System Resources**: CPU: {metrics.cpu_percent}% | RAM: {metrics.ram_percent}% ({metrics.ram_used_mb}/{metrics.ram_total_mb}MB) | GPU: {metrics.gpu_name} (Util: {metrics.gpu_utilization_percent}%)",
            f"- **Power**: {'Plugged (AC)' if metrics.power_plugged else f'Battery ({metrics.battery_percent}%)'}",
            f"- **User Directories**: Downloads: `{self.files_info.downloads_dir}` | Documents: `{self.files_info.documents_dir}`",
            f"- **WISE Internal State**: `{sm.current_state.value}` (Low Power: {sm.current_profile.is_low_power})",
        ]
        if self.files_info.latest_download:
            lines.append(f"- **Latest Download**: `{self.files_info.latest_download}`")
        if self.current_tasks:
            lines.append(f"- **Active Tasks**: {len(self.current_tasks)} ongoing")

        return "\n".join(lines)


class ComputerWorldModelManager:
    """Manages ComputerWorldModel lifecycle, event updates, and queries."""

    def __init__(self):
        self.model = ComputerWorldModel()
        self._lock = threading.RLock()
        self._init_static_os_info()
        self._init_user_dirs()
        self._wire_event_bus()
        self.refresh_active_window()

    def _init_static_os_info(self) -> None:
        """Loads static OS attributes once."""
        try:
            self.model.os_info = OSInfo(
                system=platform.system(),
                release=platform.release(),
                version=platform.version(),
                build=platform.win32_ver()[1] if sys.platform == "win32" else "",
                arch=platform.machine(),
                uptime_seconds=time.time() - psutil.boot_time(),
            )
            # User session
            username = os.environ.get("USERNAME", os.environ.get("USER", "Unknown"))
            is_admin = False
            if sys.platform == "win32":
                import ctypes
                try:
                    is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
                except Exception:
                    pass

            self.model.user_session = UserSessionInfo(
                username=username,
                is_admin=is_admin,
            )
        except Exception as e:
            LOG.error("Failed to initialize static OS info: %s", e)

    def _init_user_dirs(self) -> None:
        """Resolves standard user directories (Desktop, Documents, Downloads)."""
        home = Path.home()
        desktop = home / "Desktop"
        docs = home / "Documents"
        downloads = home / "Downloads"

        latest_dl = None
        if downloads.exists():
            try:
                files = sorted(downloads.glob("*"), key=os.path.getmtime, reverse=True)
                if files:
                    latest_dl = files[0].name
            except Exception:
                pass

        self.model.files_info = FilesInfo(
            desktop_dir=str(desktop) if desktop.exists() else str(home),
            documents_dir=str(docs) if docs.exists() else str(home),
            downloads_dir=str(downloads) if downloads.exists() else str(home),
            latest_download=latest_dl,
        )

    def _wire_event_bus(self) -> None:
        """Connects to native event bus to update world model without polling."""
        bus = get_event_bus()
        bus.subscribe("window.foreground_changed", self._on_foreground_changed)
        bus.subscribe("window.created", self._on_window_event)
        bus.subscribe("window.destroyed", self._on_window_event)
        bus.subscribe("task.*", self._on_task_event)
        bus.subscribe("session.*", self._on_session_event)
        bus.subscribe("power.*", self._on_power_event)

    def _on_session_event(self, event: WindowsEvent) -> None:
        with self._lock:
            if event.topic == "session.lock":
                self.model.user_session.is_locked = True
            elif event.topic == "session.unlock":
                self.model.user_session.is_locked = False
            LOG.debug("WorldModel updated session locked state: %s", self.model.user_session.is_locked)

    def _on_power_event(self, event: WindowsEvent) -> None:
        with self._lock:
            if event.topic == "power.sleep":
                self.model.system_power_state = "SLEEP"
            elif event.topic == "power.wake":
                self.model.system_power_state = "ACTIVE"
            LOG.debug("WorldModel updated power state: %s", self.model.system_power_state)

    def _on_foreground_changed(self, event: WindowsEvent) -> None:
        """Event handler: updates active foreground window instantly upon Win32 hook."""
        with self._lock:
            data = event.data
            hwnd = data.get("hwnd", 0)
            rect = {"x": 0, "y": 0, "w": 0, "h": 0}

            if sys.platform == "win32" and hwnd:
                import ctypes
                from ctypes import wintypes
                r = wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    rect = {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top}

            self.model.active_window = ActiveWindowInfo(
                hwnd=hwnd,
                title=data.get("title", ""),
                process_name=data.get("process_name", ""),
                pid=data.get("pid", 0),
                geometry=rect,
                updated_at=time.time(),
            )
            LOG.debug("WorldModel updated active window: %s", self.model.active_window.title)

    def _on_window_event(self, event: WindowsEvent) -> None:
        """Updates list of open windows when windows are created or destroyed."""
        # Refresh open windows lazily on changes
        self.refresh_open_windows()

    def _on_task_event(self, event: WindowsEvent) -> None:
        with self._lock:
            # Handle task start/stop
            topic = event.topic
            data = event.data
            if topic == "task.started":
                self.model.current_tasks.append(data)
            elif topic == "task.completed" or topic == "task.failed":
                task_id = data.get("task_id")
                self.model.current_tasks = [t for t in self.model.current_tasks if t.get("task_id") != task_id]

    def refresh_active_window(self) -> ActiveWindowInfo:
        """Synchronously queries active foreground window via Win32."""
        if sys.platform != "win32":
            return self.model.active_window

        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return self.model.active_window

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pname = ""
        if pid.value > 0:
            try:
                pname = psutil.Process(pid.value).name()
            except Exception:
                pname = "unknown"

        rect = {"x": 0, "y": 0, "w": 0, "h": 0}
        r = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            rect = {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top}

        with self._lock:
            self.model.active_window = ActiveWindowInfo(
                hwnd=hwnd,
                title=title,
                process_name=pname,
                pid=pid.value,
                geometry=rect,
                updated_at=time.time(),
            )
            return self.model.active_window

    def refresh_open_windows(self) -> List[Dict[str, Any]]:
        """Enumerates visible top-level windows using EnumWindows."""
        if sys.platform != "win32":
            return []

        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        windows = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_windows_callback(hwnd, lParam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value
                    if title.strip():
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        pname = ""
                        if pid.value > 0:
                            try:
                                pname = psutil.Process(pid.value).name()
                            except Exception:
                                pass
                        windows.append({
                            "hwnd": hwnd,
                            "title": title,
                            "process_name": pname,
                            "pid": pid.value,
                        })
            return True

        cb = WNDENUMPROC(enum_windows_callback)
        user32.EnumWindows(cb, 0)

        # Resilient fallback: if running in a session where EnumWindows returns 0
        # (e.g., subshell or background runner), enumerate active UI processes
        if not windows:
            known_gui_apps = {
                "chrome.exe", "msedge.exe", "code.exe", "explorer.exe",
                "notepad.exe", "slack.exe", "discord.exe", "terminal.exe",
                "powershell.exe", "devenv.exe", "spotify.exe"
            }
            try:
                for proc in psutil.process_iter(["pid", "name"]):
                    name = (proc.info.get("name") or "").lower()
                    if name in known_gui_apps or name.endswith(".exe"):
                        if name in known_gui_apps:
                            windows.append({
                                "hwnd": 0,
                                "title": name.replace(".exe", "").capitalize(),
                                "process_name": proc.info.get("name"),
                                "pid": proc.info.get("pid", 0),
                            })
            except Exception as e:
                LOG.debug("Process fallback error: %s", e)

        with self._lock:
            self.model.open_windows = windows
            return windows

    def get_snapshot(self) -> Dict[str, Any]:
        """Returns instantaneous snapshot of the Computer World Model."""
        with self._lock:
            return self.model.to_dict()

    def get_prompt_context(self) -> str:
        """Returns token-efficient context formatted for LLM system/user prompts."""
        with self._lock:
            return self.model.format_summary()


# Singleton instance
_GLOBAL_WORLD_MODEL_MGR: Optional[ComputerWorldModelManager] = None
_WMM_LOCK = threading.Lock()


def get_world_model_manager() -> ComputerWorldModelManager:
    global _GLOBAL_WORLD_MODEL_MGR
    if _GLOBAL_WORLD_MODEL_MGR is None:
        with _WMM_LOCK:
            if _GLOBAL_WORLD_MODEL_MGR is None:
                _GLOBAL_WORLD_MODEL_MGR = ComputerWorldModelManager()
    return _GLOBAL_WORLD_MODEL_MGR
