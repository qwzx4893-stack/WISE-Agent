# ==============================================================================
# WISE Windows Window Manager
# Architecture: Direct Win32 Window Management, Guaranteed Foreground Activation,
# and Dialog / Modal Window Tracking.
# ==============================================================================

from __future__ import annotations

import re
import sys
import time
import ctypes
from ctypes import wintypes
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple

LOG = logging.getLogger("WISE.Windows.WindowManager")

# Win32 Constants
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7
SW_SHOWNA = 8
SW_RESTORE = 9

HWND_TOP = 0
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

WS_EX_TOPMOST = 0x00000008
WS_VISIBLE = 0x10000000

GA_ROOT = 2


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    process_id: int
    rect: Tuple[int, int, int, int]  # (left, top, width, height)
    is_visible: bool
    is_minimized: bool
    is_maximized: bool
    is_active: bool
    is_hung: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class_name": self.class_name,
            "process_id": self.process_id,
            "rect": {
                "x": self.rect[0],
                "y": self.rect[1],
                "width": self.rect[2],
                "height": self.rect[3],
            },
            "is_visible": self.is_visible,
            "is_minimized": self.is_minimized,
            "is_maximized": self.is_maximized,
            "is_active": self.is_active,
            "is_hung": self.is_hung,
        }


class WISEWindowManager:
    """
    Manages top-level and dialog windows on the interactive Windows desktop.
    Provides robust focus/foreground activation overcoming Windows focus-stealing locks.
    """

    def __init__(self) -> None:
        self._is_win32 = sys.platform == "win32"
        if self._is_win32:
            self._ensure_interactive_desktop()

    def _ensure_interactive_desktop(self) -> bool:
        """Attaches calling thread to interactive Default desktop."""
        if not self._is_win32:
            return False
        try:
            u = ctypes.windll.user32
            h_def = u.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_def:
                return bool(u.SetThreadDesktop(h_def))
        except Exception:
            pass
        return False

    def get_foreground_window(self) -> Optional[WindowInfo]:
        """Returns WindowInfo for the currently active foreground window."""
        if not self._is_win32:
            return None
        self._ensure_interactive_desktop()
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        return self.get_window_info(hwnd)

    def get_window_info(self, hwnd: int) -> Optional[WindowInfo]:
        """Inspects all state attributes for a given window handle."""
        if not self._is_win32 or hwnd == 0:
            return None
        self._ensure_interactive_desktop()
        u = ctypes.windll.user32

        if not u.IsWindow(hwnd):
            return None

        # Title
        length = u.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(length)
        u.GetWindowTextW(hwnd, buf, length)
        title = buf.value

        # Class
        cls_buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls_buf, 256)
        class_name = cls_buf.value

        # Process ID
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        # Rect
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        rect = (r.left, r.top, max(0, r.right - r.left), max(0, r.bottom - r.top))

        # States
        is_vis = bool(u.IsWindowVisible(hwnd))
        is_min = bool(u.IsIconic(hwnd))
        is_max = bool(u.IsZoomed(hwnd))
        active_hwnd = u.GetForegroundWindow()
        is_act = (hwnd == active_hwnd)

        # Hung check
        is_hung = False
        try:
            is_hung = bool(u.IsHungAppWindow(hwnd))
        except Exception:
            pass

        return WindowInfo(
            hwnd=hwnd,
            title=title,
            class_name=class_name,
            process_id=pid.value,
            rect=rect,
            is_visible=is_vis,
            is_minimized=is_min,
            is_maximized=is_max,
            is_active=is_act,
            is_hung=is_hung,
        )

    def enumerate_windows(self, visible_only: bool = True) -> List[WindowInfo]:
        """Enumerates top-level windows on the interactive desktop."""
        if not self._is_win32:
            return []
        self._ensure_interactive_desktop()
        u = ctypes.windll.user32

        windows: List[WindowInfo] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_cb(h: int, l: int) -> bool:
            if visible_only and not u.IsWindowVisible(h):
                return True
            # Filter zero-size or off-screen shell windows if visible_only
            info = self.get_window_info(h)
            if info:
                if visible_only and (info.rect[2] < 10 or info.rect[3] < 10):
                    return True
                if not info.title:
                    info.title = f"[{info.class_name}]"
                windows.append(info)
            return True

        proc = WNDENUMPROC(enum_cb)
        u.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows(proc, 0)
        return windows

    def find_windows(self, query: str, visible_only: bool = True) -> List[WindowInfo]:
        """Finds windows matching title or class name (case-insensitive or regex)."""
        all_wins = self.enumerate_windows(visible_only=visible_only)
        q_lower = query.lower()
        matches: List[WindowInfo] = []
        for w in all_wins:
            if q_lower in w.title.lower() or q_lower in w.class_name.lower():
                matches.append(w)
                continue
            try:
                if re.search(query, w.title, re.IGNORECASE):
                    matches.append(w)
            except Exception:
                pass
        return matches

    def find_window(self, query: str, visible_only: bool = True) -> Optional[WindowInfo]:
        """Finds the first window matching query."""
        results = self.find_windows(query, visible_only=visible_only)
        return results[0] if results else None

    def bring_to_front(self, hwnd: int) -> bool:
        """
        Reliably brings a window to the foreground using the AttachThreadInput trick.
        Overcomes Windows restriction on SetForegroundWindow from background processes.
        """
        if not self._is_win32 or hwnd == 0:
            return False
        self._ensure_interactive_desktop()
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32

        try:
            # If minimized, restore first
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.05)

            fore_hwnd = u.GetForegroundWindow()
            if fore_hwnd == hwnd:
                return True

            curr_tid = k.GetCurrentThreadId()
            fore_tid = u.GetWindowThreadProcessId(fore_hwnd, None) if fore_hwnd else 0
            target_tid = u.GetWindowThreadProcessId(hwnd, None)

            # Attach thread inputs to allow SetForegroundWindow
            attached_fore = False
            attached_target = False
            if fore_tid and fore_tid != curr_tid:
                attached_fore = bool(u.AttachThreadInput(curr_tid, fore_tid, True))
            if target_tid and target_tid != curr_tid:
                attached_target = bool(u.AttachThreadInput(curr_tid, target_tid, True))

            try:
                u.ShowWindow(hwnd, SW_SHOW)
                u.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
                u.SetFocus(hwnd)
            finally:
                if attached_fore:
                    u.AttachThreadInput(curr_tid, fore_tid, False)
                if attached_target:
                    u.AttachThreadInput(curr_tid, target_tid, False)

            time.sleep(0.05)
            return u.GetForegroundWindow() == hwnd
        except Exception as e:
            LOG.error("Failed to bring window %s to front: %s", hwnd, e)
            return False

    def close_window(self, hwnd: int, force: bool = False) -> bool:
        """Closes target window gracefully via WM_CLOSE or forces termination."""
        if not self._is_win32 or hwnd == 0:
            return False
        self._ensure_interactive_desktop()
        u = ctypes.windll.user32
        WM_CLOSE = 0x0010

        if not force:
            posted = bool(u.PostMessageW(hwnd, WM_CLOSE, 0, 0))
            if not posted:
                return False
            # GUI applications may need a few message-loop turns to process
            # WM_CLOSE.  The action succeeds once Windows accepts the message;
            # the closed-loop verifier separately checks the requested state.
            # Conflating the two gave a false action failure for applications
            # that close asynchronously or hand shutdown to a child process.
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if not u.IsWindow(hwnd):
                    return True
                time.sleep(0.05)
            return True
        else:
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                import os
                import signal
                try:
                    os.kill(pid.value, signal.SIGTERM)
                    return True
                except Exception:
                    pass
        return False

    def enumerate_child_dialogs(self, parent_hwnd: int) -> List[WindowInfo]:
        """Enumerates child dialogs or popups belonging to parent window."""
        if not self._is_win32 or parent_hwnd == 0:
            return []
        self._ensure_interactive_desktop()
        dialogs: List[WindowInfo] = []
        u = ctypes.windll.user32

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(h: int, l: int) -> bool:
            owner = u.GetWindow(h, 4)  # GW_OWNER
            if owner == parent_hwnd and u.IsWindowVisible(h):
                info = self.get_window_info(h)
                if info:
                    dialogs.append(info)
            return True

        proc = WNDENUMPROC(cb)
        u.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows(proc, 0)
        return dialogs


# Global Singleton Manager
_WINDOW_MGR: Optional[WISEWindowManager] = None


def get_window_manager() -> WISEWindowManager:
    global _WINDOW_MGR
    if _WINDOW_MGR is None:
        _WINDOW_MGR = WISEWindowManager()
    return _WINDOW_MGR
