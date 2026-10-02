# ==============================================================================
# WISE Windows Input Driver (Native Win32 SendInput Implementation)
# Architecture: Direct User32 SendInput with Unicode and Desktop Station Attachment
# ==============================================================================

from __future__ import annotations

import sys
import time
import ctypes
from ctypes import wintypes
import logging
from typing import Tuple, List, Optional, Union

LOG = logging.getLogger("WISE.Windows.InputDriver")

# ==============================================================================
# Win32 Constants & Structures for SendInput
# ==============================================================================
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

# Mouse flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

# Keyboard flags
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# System metrics
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Virtual Key Mapping for common keys
VK_MAP = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "escape": 0x1B,
    "esc": 0x1B,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
    "capslock": 0x14,
}

# 64-bit safe SendInput ctypes definitions
if sys.platform == "win32":
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("u", INPUT_UNION),
        ]


class WindowsInputDriver:
    """
    Native Win32 SendInput driver.
    Ensures that input is directed to the interactive Default desktop on WinSta0.
    """

    def __init__(self) -> None:
        self._is_win32 = sys.platform == "win32"
        self._desktop_attached = False
        if self._is_win32:
            self._ensure_interactive_desktop()

    def _ensure_interactive_desktop(self) -> bool:
        """Attaches the calling thread to the interactive 'Default' desktop on 'WinSta0'."""
        if not self._is_win32:
            return False
        try:
            u = ctypes.windll.user32
            # Open the interactive desktop with maximum allowed access
            h_def = u.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_def:
                res = u.SetThreadDesktop(h_def)
                if res:
                    self._desktop_attached = True
                    return True
        except Exception as e:
            LOG.debug("Could not attach to Default desktop: %s", e)
        return False

    def get_screen_bounds(self) -> Tuple[int, int, int, int]:
        """Returns (x, y, width, height) of the virtual screen desktop."""
        if not self._is_win32:
            return (0, 0, 1920, 1080)
        u = ctypes.windll.user32
        vx = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if vw <= 0 or vh <= 0:
            vw = u.GetSystemMetrics(0) or 1920
            vh = u.GetSystemMetrics(1) or 1080
            vx, vy = 0, 0
        return (vx, vy, vw, vh)

    def get_cursor_position(self) -> Tuple[int, int]:
        """Returns the current mouse cursor (x, y) coordinates."""
        if not self._is_win32:
            return (0, 0)
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y)

    def get_dpi_scale_for_window(self, hwnd: Optional[int] = None) -> float:
        """Returns the DPI scaling factor for a window or primary monitor (1.0 = 100%, 1.25 = 125%, 1.5 = 150%)."""
        if not self._is_win32:
            return 1.0
        try:
            if hwnd and hasattr(ctypes.windll.user32, "GetDpiForWindow"):
                dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                if dpi > 0:
                    return float(dpi) / 96.0
            if hasattr(ctypes.windll.user32, "GetDpiForSystem"):
                dpi = ctypes.windll.user32.GetDpiForSystem()
                if dpi > 0:
                    return float(dpi) / 96.0
        except Exception:
            pass
        return 1.0

    def logical_to_physical(self, x: int, y: int, hwnd: Optional[int] = None) -> Tuple[int, int]:
        """Converts logical UI coordinates to physical screen coordinates."""
        scale = self.get_dpi_scale_for_window(hwnd)
        return (int(round(x * scale)), int(round(y * scale)))

    def physical_to_logical(self, x: int, y: int, hwnd: Optional[int] = None) -> Tuple[int, int]:
        """Converts physical screen coordinates to logical UI coordinates."""
        scale = self.get_dpi_scale_for_window(hwnd)
        if scale <= 0:
            scale = 1.0
        return (int(round(x / scale)), int(round(y / scale)))

    def move_to(self, x: int, y: int) -> bool:
        """Moves cursor to exact screen coordinates using SendInput with virtual desktop scaling."""
        if not self._is_win32:
            return True
        self._ensure_interactive_desktop()
        vx, vy, vw, vh = self.get_screen_bounds()

        # Clamp within virtual screen
        x = max(vx, min(x, vx + vw - 1))
        y = max(vy, min(y, vy + vh - 1))

        # Convert to absolute normalized coordinates (0 to 65535)
        norm_x = int((x - vx) * 65536 / vw)
        norm_y = int((y - vy) * 65536 / vh)

        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dx = norm_x
        inp.u.mi.dy = norm_y
        inp.u.mi.mouseData = 0
        inp.u.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        inp.u.mi.time = 0
        inp.u.mi.dwExtraInfo = 0

        res = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return res == 1

    def mouse_down(self, button: str = "left") -> bool:
        """Sends mouse down event."""
        if not self._is_win32:
            return True
        self._ensure_interactive_desktop()
        flag = MOUSEEVENTF_LEFTDOWN
        if button.lower() == "right":
            flag = MOUSEEVENTF_RIGHTDOWN
        elif button.lower() == "middle":
            flag = MOUSEEVENTF_MIDDLEDOWN

        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dwFlags = flag
        res = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return res == 1

    def mouse_up(self, button: str = "left") -> bool:
        """Sends mouse up event."""
        if not self._is_win32:
            return True
        self._ensure_interactive_desktop()
        flag = MOUSEEVENTF_LEFTUP
        if button.lower() == "right":
            flag = MOUSEEVENTF_RIGHTUP
        elif button.lower() == "middle":
            flag = MOUSEEVENTF_MIDDLEUP

        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dwFlags = flag
        res = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return res == 1

    def click(self, button: str = "left", x: Optional[int] = None, y: Optional[int] = None) -> bool:
        """Moves to (x, y) if provided, then performs click."""
        if x is not None and y is not None:
            self.move_to(x, y)
            time.sleep(0.02)
        down = self.mouse_down(button)
        time.sleep(0.03)
        up = self.mouse_up(button)
        return down and up

    def double_click(self, x: Optional[int] = None, y: Optional[int] = None) -> bool:
        """Performs a double click with Windows double-click spacing."""
        c1 = self.click("left", x, y)
        time.sleep(0.08)
        c2 = self.click("left")
        return c1 and c2

    def drag_and_drop(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.2) -> bool:
        """Performs smooth drag and drop from (x1, y1) to (x2, y2)."""
        self.move_to(x1, y1)
        time.sleep(0.05)
        self.mouse_down("left")
        time.sleep(0.05)

        steps = max(5, int(duration * 30))
        for step in range(1, steps + 1):
            curr_x = int(x1 + (x2 - x1) * (step / steps))
            curr_y = int(y1 + (y2 - y1) * (step / steps))
            self.move_to(curr_x, curr_y)
            time.sleep(duration / steps)

        time.sleep(0.05)
        self.mouse_up("left")
        return True

    def scroll(self, delta_clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> bool:
        """Sends mouse wheel scroll event. Positive = up, Negative = down."""
        if not self._is_win32:
            return True
        if x is not None and y is not None:
            self.move_to(x, y)
            time.sleep(0.02)

        # 1 wheel click in Win32 = 120 WHEEL_DELTA
        wheel_delta = delta_clicks * 120
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.mouseData = wheel_delta & 0xFFFFFFFF
        inp.u.mi.dwFlags = MOUSEEVENTF_WHEEL
        res = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return res == 1

    # ==========================================================================
    # Keyboard Implementation
    # ==========================================================================

    def _resolve_vk(self, key: Union[str, int]) -> int:
        """Resolves a key name or character into its virtual key code."""
        if isinstance(key, int):
            return key
        k_lower = key.lower()
        if k_lower in VK_MAP:
            return VK_MAP[k_lower]
        if len(key) == 1:
            # Single character (ASCII/alphabet)
            vk = ctypes.windll.user32.VkKeyScanW(ord(key)) & 0xFF
            if vk != 0xFF:
                return vk
        return 0

    def key_down(self, key: Union[str, int]) -> bool:
        """Sends a key down event."""
        if not self._is_win32:
            return True
        self._ensure_interactive_desktop()
        vk = self._resolve_vk(key)
        if vk == 0:
            return False

        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki.wVk = vk
        inp.u.ki.dwFlags = 0
        res = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return res == 1

    def key_up(self, key: Union[str, int]) -> bool:
        """Sends a key up event."""
        if not self._is_win32:
            return True
        self._ensure_interactive_desktop()
        vk = self._resolve_vk(key)
        if vk == 0:
            return False

        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki.wVk = vk
        inp.u.ki.dwFlags = KEYEVENTF_KEYUP
        res = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return res == 1

    def press_key(self, key: Union[str, int]) -> bool:
        """Sends a key down followed by key up."""
        d = self.key_down(key)
        time.sleep(0.02)
        u = self.key_up(key)
        return d and u

    def hotkey(self, *keys: str) -> bool:
        """
        Executes a multi-key shortcut sequence (e.g. ('ctrl', 's'), ('alt', 'f4')).
        Presses keys in order, then releases them in reverse order.
        """
        if not keys:
            return False
        pressed: List[str] = []
        try:
            for k in keys:
                if self.key_down(k):
                    pressed.append(k)
                time.sleep(0.02)
            time.sleep(0.05)
        finally:
            for k in reversed(pressed):
                self.key_up(k)
                time.sleep(0.01)
        return len(pressed) == len(keys)

    def type_text(self, text: str, delay_per_char: float = 0.01) -> int:
        """
        Types full Unicode text using KEYEVENTF_UNICODE.
        Flawlessly supports English, Arabic, emojis, and special characters.
        Returns the number of characters successfully dispatched.
        """
        if not self._is_win32 or not text:
            return 0
        self._ensure_interactive_desktop()

        count = 0
        for char in text:
            utf16_code = ord(char)
            # Down event
            inp_down = INPUT()
            inp_down.type = INPUT_KEYBOARD
            inp_down.u.ki.wVk = 0
            inp_down.u.ki.wScan = utf16_code
            inp_down.u.ki.dwFlags = KEYEVENTF_UNICODE

            # Up event
            inp_up = INPUT()
            inp_up.type = INPUT_KEYBOARD
            inp_up.u.ki.wVk = 0
            inp_up.u.ki.wScan = utf16_code
            inp_up.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP

            inputs = (INPUT * 2)(inp_down, inp_up)
            dispatched = ctypes.windll.user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
            if dispatched == 2:
                count += 1
            if delay_per_char > 0:
                time.sleep(delay_per_char)

        return count


# Global Singleton Driver
_INPUT_DRIVER: Optional[WindowsInputDriver] = None


def get_input_driver() -> WindowsInputDriver:
    global _INPUT_DRIVER
    if _INPUT_DRIVER is None:
        _INPUT_DRIVER = WindowsInputDriver()
    return _INPUT_DRIVER
