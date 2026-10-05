"""
Windows High-Speed Screen Capture Engine (ScreenCaptureEngine).
Provides on-demand, low-latency screen, window, and regional captures via native Win32 GDI.
Zero continuous background capturing to strictly enforce zero-compute idle.
Level 2 of the WISE Hierarchical Perception Engine.
"""

from __future__ import annotations

import os
import io
import sys
import time
import base64
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass, field

from PIL import Image

LOG = logging.getLogger("wise.screen_capture")


@dataclass
class ScreenCaptureResult:
    success: bool
    width: int
    height: int
    image_bytes: bytes = field(repr=False, default=b"")
    timestamp: float = field(default_factory=time.time)
    error_reason: Optional[str] = None
    saved_path: Optional[str] = None

    @property
    def base64_image(self) -> str:
        if not self.image_bytes:
            return ""
        return base64.b64encode(self.image_bytes).decode("ascii")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "width": self.width,
            "height": self.height,
            "timestamp": self.timestamp,
            "error_reason": self.error_reason,
            "saved_path": self.saved_path,
            "byte_size": len(self.image_bytes),
        }


class ScreenCaptureEngine:
    """Captures desktop pixels on demand using low-overhead Win32 GDI interfaces."""

    def __init__(self, output_dir: Optional[Path] = None):
        self._lock = threading.RLock()
        self._is_win32 = sys.platform == "win32"
        
        if output_dir is None:
            base_dir = Path(__file__).resolve().parent.parent.parent
            self.output_dir = base_dir / "data" / "captures"
        else:
            self.output_dir = Path(output_dir).resolve()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._last_capture: Optional[ScreenCaptureResult] = None

    def capture_full_screen(self, save_to_disk: bool = False) -> ScreenCaptureResult:
        """Captures the primary monitor resolution on demand."""
        if not self._is_win32:
            return self._capture_unavailable("Screen capture requires an interactive Windows desktop")

        try:
            import ctypes
            user32 = ctypes.windll.user32
            width = user32.GetSystemMetrics(0)
            height = user32.GetSystemMetrics(1)
            return self.capture_region(0, 0, width, height, save_to_disk=save_to_disk)
        except Exception as e:
            LOG.error("Failed full screen capture: %s", e)
            return ScreenCaptureResult(success=False, width=0, height=0, error_reason=str(e))

    def capture_window(self, hwnd: int, save_to_disk: bool = False) -> ScreenCaptureResult:
        """Captures the bounding box of a specific window handle on demand."""
        if not self._is_win32 or hwnd == 0:
            return self._capture_unavailable("A valid interactive Windows window handle is required")
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            r = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                w = max(1, r.right - r.left)
                h = max(1, r.bottom - r.top)
                return self.capture_region(r.left, r.top, w, h, save_to_disk=save_to_disk)
            return self._capture_unavailable(f"Could not read geometry for target window {hwnd}")
        except Exception as e:
            LOG.error("Failed window capture for hwnd=%s: %s", hwnd, e)
            return self._capture_unavailable(f"Window capture failed: {e}")

    def capture_region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        save_to_disk: bool = False,
    ) -> ScreenCaptureResult:
        """Captures a specific bounding rectangle of the screen using Win32 GDI."""
        if not self._is_win32:
            return self._capture_unavailable("Screen capture requires an interactive Windows desktop")

        with self._lock:
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                gdi32 = ctypes.windll.gdi32

                if width <= 0 or height <= 0:
                    return ScreenCaptureResult(success=False, width=0, height=0, error_reason="Invalid dimensions")

                # Ensure attached to interactive desktop
                try:
                    h_def = user32.OpenDesktopW("Default", 0, False, 0x01FF)
                    if h_def:
                        user32.SetThreadDesktop(h_def)
                except Exception:
                    pass

                # Set 64-bit safe argtypes
                HANDLE = ctypes.c_void_p
                user32.GetDesktopWindow.restype = HANDLE
                user32.GetDC.argtypes = [HANDLE]
                user32.GetDC.restype = HANDLE
                user32.ReleaseDC.argtypes = [HANDLE, HANDLE]
                user32.GetWindowRect.argtypes = [HANDLE, ctypes.POINTER(wintypes.RECT)]
                gdi32.CreateCompatibleDC.argtypes = [HANDLE]
                gdi32.CreateCompatibleDC.restype = HANDLE
                gdi32.CreateCompatibleBitmap.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int]
                gdi32.CreateCompatibleBitmap.restype = HANDLE
                gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
                gdi32.SelectObject.restype = HANDLE
                gdi32.BitBlt.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, HANDLE, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
                gdi32.BitBlt.restype = wintypes.BOOL
                gdi32.GetDIBits.argtypes = [HANDLE, HANDLE, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
                gdi32.GetDIBits.restype = ctypes.c_int
                gdi32.DeleteObject.argtypes = [HANDLE]
                gdi32.DeleteDC.argtypes = [HANDLE]

                # Obtain DC for desktop window
                hdesktop = user32.GetDesktopWindow()
                hdc_screen = user32.GetDC(hdesktop)
                if not hdc_screen:
                    return self._capture_unavailable("Desktop device context is unavailable; no interactive session is attached")

                hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
                hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
                h_old = gdi32.SelectObject(hdc_mem, hbmp)

                # BitBlt copy from screen DC to memory DC
                SRCCOPY = 0x00CC0020
                bitblt_ok = gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, x, y, SRCCOPY)

                # Extract bitmap bytes using PIL Image or raw buffer
                img_bytes = b""
                saved_path_str = None

                if bitblt_ok:
                    # Read DIB bits into buffer
                    class BITMAPINFOHEADER(ctypes.Structure):
                        _fields_ = [
                            ("biSize", wintypes.DWORD),
                            ("biWidth", wintypes.LONG),
                            ("biHeight", wintypes.LONG),
                            ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", wintypes.LONG),
                            ("biYPelsPerMeter", wintypes.LONG),
                            ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD),
                        ]

                    bmi = BITMAPINFOHEADER()
                    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                    bmi.biWidth = width
                    bmi.biHeight = -height  # Top-down DIB
                    bmi.biPlanes = 1
                    bmi.biBitCount = 32
                    bmi.biCompression = 0  # BI_RGB

                    buf_size = width * height * 4
                    buf = ctypes.create_string_buffer(buf_size)

                    DIB_RGB_COLORS = 0
                    gdi32.GetDIBits(
                        hdc_mem,
                        hbmp,
                        0,
                        height,
                        buf,
                        ctypes.byref(bmi),
                        DIB_RGB_COLORS,
                    )

                    # Create PIL Image from raw BGRA buffer
                    image = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
                    
                    buf_out = io.BytesIO()
                    image.save(buf_out, format="PNG")
                    img_bytes = buf_out.getvalue()

                    if save_to_disk:
                        filename = f"capture_{int(time.time() * 1000)}.png"
                        p = self.output_dir / filename
                        image.save(p, format="PNG")
                        saved_path_str = str(p)

                # Cleanup GDI Objects to prevent resource leaks
                gdi32.SelectObject(hdc_mem, h_old)
                gdi32.DeleteObject(hbmp)
                gdi32.DeleteDC(hdc_mem)
                user32.ReleaseDC(hdesktop, hdc_screen)

                if not bitblt_ok or not img_bytes:
                    return self._capture_unavailable("Desktop copy failed or the session is not interactive")

                res = ScreenCaptureResult(
                    success=True,
                    width=width,
                    height=height,
                    image_bytes=img_bytes,
                    saved_path=saved_path_str,
                )
                self._last_capture = res
                return res

            except Exception as e:
                LOG.error("GDI region capture error: %s", e)
                return self._capture_unavailable(f"GDI capture failed: {e}")

    def capture_active_window(self, hwnd: int, save_to_disk: bool = False) -> ScreenCaptureResult:
        """Captures only the target window area."""
        if not self._is_win32 or hwnd == 0:
            return self._capture_unavailable("A valid interactive Windows window handle is required")

        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            r = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return ScreenCaptureResult(success=False, width=0, height=0, error_reason="GetWindowRect failed")

            w = max(10, r.right - r.left)
            h = max(10, r.bottom - r.top)
            return self.capture_region(r.left, r.top, w, h, save_to_disk=save_to_disk)
        except Exception as e:
            return self._capture_unavailable(f"Window capture failed: {e}")

    @staticmethod
    def _capture_unavailable(reason: str) -> ScreenCaptureResult:
        """Return an honest failure; perception must never consume fake pixels."""
        return ScreenCaptureResult(
            success=False,
            width=0,
            height=0,
            error_reason=reason,
        )

    def clear_buffers(self) -> None:
        """Evicts cached capture memory upon entering IDLE."""
        with self._lock:
            self._last_capture = None
            LOG.debug("ScreenCaptureEngine image buffers cleared.")


# Singleton
_GLOBAL_CAPTURE_ENGINE: Optional[ScreenCaptureEngine] = None
_SCE_LOCK = threading.Lock()


def get_screen_capture_engine() -> ScreenCaptureEngine:
    global _GLOBAL_CAPTURE_ENGINE
    if _GLOBAL_CAPTURE_ENGINE is None:
        with _SCE_LOCK:
            if _GLOBAL_CAPTURE_ENGINE is None:
                _GLOBAL_CAPTURE_ENGINE = ScreenCaptureEngine()
    return _GLOBAL_CAPTURE_ENGINE
