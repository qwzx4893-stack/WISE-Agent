# ==============================================================================
# WISE Desktop Application Launcher
# Architecture: Standalone native desktop application that:
# 1. Starts the WISE FastAPI backend (uvicorn) in a background thread
# 2. Launches a native OS window via pywebview (with a platform webview backend)
# 3. Can explicitly refuse browser fallback for the product launcher
# 4. Provides native window lifecycle, taskbar icon, and clean shutdown coordination
#
# Run with:
#     python wise_desktop.py
#     python wise_desktop.py --port 8765 --host 127.0.0.1
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import signal
import logging
import argparse
import threading
import webbrowser
import socket
import json
from urllib.error import URLError
from urllib.request import urlopen
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# Keep model registry, credentials, sessions and generated artifacts beside
# this checkout unless an operator deliberately supplied another runtime root.
os.environ.setdefault("AGENT_OS_ROOT", str(_REPO_ROOT))
if getattr(sys, "frozen", False):
    os.environ.setdefault("WISE_RUNTIME_ROOT", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WISE"))

LOG = logging.getLogger("WISE.DesktopLauncher")
from core.paths import LOGS_DIR
_LOG_PATH = LOGS_DIR / "wise_desktop.log"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_log_handlers: list[logging.Handler] = [logging.FileHandler(_LOG_PATH, encoding="utf-8")]
if sys.stderr is not None:
    _log_handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=_log_handlers,
)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WINDOW_TITLE = "WISE — General Conversational Computer Agent"
WINDOW_WIDTH = 1440
WINDOW_HEIGHT = 900
MIN_WIDTH = 1024
MIN_HEIGHT = 640
# Building WISE's real tool/skill catalogue is deliberately done before the
# ASGI application accepts work.  On a full personal installation this takes
# about twenty seconds, so a short GUI-style timeout creates a false failure.
BACKEND_STARTUP_TIMEOUT = 60.0
NATIVE_TRANSPARENCY_ENV = "WISE_NATIVE_TRANSPARENCY"
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_TRANSIENTWINDOW = 3
_DWMWCP_ROUND = 2


def _native_transparency_enabled() -> bool:
    """Return whether the experimental transparent Windows host is enabled.

    pywebview's WinForms backend implements a transparent WebView2 window by
    briefly showing and hiding the form and then showing it again from a
    navigation callback.  On some Windows/WebView2 combinations that lifecycle
    can dispose the only native form without emitting the normal ``closing``
    event.  WISE therefore uses the reliable opaque host by default while
    retaining an explicit opt-in switch for diagnostics and future runtimes.
    """
    return os.environ.get(NATIVE_TRANSPARENCY_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _keep_windows_renderer_opaque(native: object) -> None:
    """Set the actual WebView2 control on its WinForms UI thread.

    ``native.browser`` is pywebview's Python EdgeChrome wrapper, not the
    WebView2 control. Assigning a new attribute there silently changes nothing.
    """
    from System import Action  # type: ignore[import-not-found]
    from System.Drawing import Color  # type: ignore[import-not-found]

    control = getattr(native, "webview", None)
    if control is None:
        control = getattr(getattr(native, "browser", None), "webview", None)
    if control is None:
        raise RuntimeError("The native host did not expose its WebView2 control")

    def set_opaque_color() -> None:
        control.DefaultBackgroundColor = Color.FromArgb(255, 16, 16, 20)

    native.Invoke(Action(set_opaque_color))


def _apply_windows_backdrop(window: object) -> bool:
    """Apply the supported Windows 11 Desktop Acrylic host material.

    This deliberately does *not* set pywebview's ``transparent=True`` flag.
    Instead it asks DWM to draw the backdrop for the existing WinForms HWND,
    extends the composed frame only along the window edges, and keeps the
    WebView2 renderer opaque. Extending Acrylic through a transparent renderer
    can hide the entire application on some drivers. WISE's CSS surfaces retain
    their blur treatment without relying on that unstable composition path.

    The function is best-effort: Windows versions without the required DWM
    attribute retain the normal dark host and the CSS blur treatment.
    """
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        from ctypes import wintypes

        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None)
        if native is None or handle is None:
            LOG.warning("Native backdrop skipped because pywebview did not expose a WinForms handle.")
            return False

        hwnd = wintypes.HWND(int(handle.ToInt64()))
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        def _set_attribute(attribute: int, value: int) -> int:
            native_value = ctypes.c_int(value)
            return int(
                dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    attribute,
                    ctypes.byref(native_value),
                    ctypes.sizeof(native_value),
                )
            )

        # Title bar and corners should follow the app's dark, rounded visual
        # language regardless of whether the Acrylic material is available.
        _set_attribute(_DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
        _set_attribute(_DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND)
        backdrop_result = _set_attribute(
            _DWMWA_SYSTEMBACKDROP_TYPE,
            _DWMSBT_TRANSIENTWINDOW,
        )
        if backdrop_result != 0:
            LOG.info(
                "Windows Desktop Acrylic is unavailable (HRESULT=%s); using the dark CSS fallback.",
                backdrop_result,
            )
            return False

        class _Margins(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_int),
                ("right", ctypes.c_int),
                ("top", ctypes.c_int),
                ("bottom", ctypes.c_int),
            ]

        dwmapi.DwmExtendFrameIntoClientArea.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_Margins),
        ]
        dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
        # Extending Acrylic across the whole WinForms client combined with
        # a transparent WebView2 clear color can hide the entire web surface.
        # Keep the material at the window edge and the renderer opaque.
        margins = _Margins(7, 7, 7, 7)
        frame_result = int(dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)))
        if frame_result != 0:
            LOG.info(
                "Windows backdrop frame extension is unavailable (HRESULT=%s); using the dark CSS fallback.",
                frame_result,
            )
            return False

        # This changes only WebView2's clear color.  It does not activate the
        # pywebview transparent-window lifecycle that caused the window loss.
        try:
            _keep_windows_renderer_opaque(native)
        except Exception as exc:
            LOG.info("Windows backdrop kept opaque because WebView2 clear color could not be changed: %s", exc)
            return False

        LOG.info("Windows Desktop Acrylic edge enabled with an opaque content renderer.")
        return True
    except Exception as exc:
        LOG.info("Windows native backdrop is unavailable; using the dark CSS fallback: %s", exc)
        return False


def _find_free_port(preferred: int = DEFAULT_PORT) -> int:
    """Return *preferred* if available, otherwise pick an ephemeral port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", preferred))
            return preferred
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _wise_backend_is_running(host: str, port: int) -> bool:
    """Check whether the requested loopback port already hosts WISE.

    The background login task must not make a second, invisible copy on an
    arbitrary port: callers and the desktop app intentionally use the stable
    loopback port.  A running WISE instance is therefore sufficient work.
    """
    try:
        with urlopen(f"http://{host}:{port}/health", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        import hashlib
        from core.paths import RUNTIME_ROOT
        runtime_id = hashlib.sha256(str(RUNTIME_ROOT).casefold().encode()).hexdigest()[:16]
        return (isinstance(payload, dict) and payload.get("service") == "wise"
                and payload.get("runtime_id") == runtime_id
                and payload.get("status") in {"ok", "degraded"})
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def _port_is_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True
    except OSError:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Backend server management
# ──────────────────────────────────────────────────────────────────────────────
_server_thread: Optional[threading.Thread] = None
_server_shutdown_event = threading.Event()
_shutdown_requested = threading.Event()
_BACKGROUND_MAX_RESTARTS = 3
_native_mutex_handle: Optional[int] = None
_uvicorn_server = None
_system_session_guard = False


def _release_native_window_mutex() -> None:
    global _native_mutex_handle
    if _native_mutex_handle is not None and sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.CloseHandle(_native_mutex_handle)
        _native_mutex_handle = None


def _background_mode_enabled() -> bool:
    from core.system_integration import get_system_integration_settings
    settings = get_system_integration_settings()
    return bool(settings["enabled"] and settings["background_mode"])


def _stop_owned_backend(timeout: float = 10.0) -> None:
    """Drain only the server started by this launcher, never an attached server."""
    _shutdown_requested.set()
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True
    _server_shutdown_event.set()
    thread = _server_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=timeout)
        if thread.is_alive():
            LOG.error("Backend did not drain within %.1fs; shutdown remains incomplete.", timeout)


def _acquire_native_window_mutex() -> bool:
    """Allow only one native WISE window in the current Windows session."""
    global _native_mutex_handle
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    # Independently configured runtimes must not restore or close each other's
    # windows. Production still has exactly one window per runtime/session.
    import hashlib
    from core.paths import RUNTIME_ROOT
    runtime_id = hashlib.sha256(str(RUNTIME_ROOT).casefold().encode()).hexdigest()[:16]
    handle = kernel32.CreateMutexW(None, False, f"Local\\WISEDesktopNativeWindow-{runtime_id}")
    if not handle:
        LOG.error("Could not create the WISE desktop instance mutex.")
        return False
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        if _reveal_existing_native_window():
            LOG.info("A native WISE window is already open; restored it instead of creating a duplicate.")
        else:
            LOG.warning("A native WISE instance exists, but no owned window could be verified; duplicate launch skipped.")
        return False
    _native_mutex_handle = handle
    return True


def _native_window_property_name() -> str:
    """Internal runtime ownership tag, not a security boundary against malware."""
    import hashlib
    from core.paths import RUNTIME_ROOT
    runtime_id = hashlib.sha256(str(RUNTIME_ROOT).casefold().encode()).hexdigest()[:16]
    return f"WISE.NativeRuntime.{runtime_id}"


def _native_window_user32() -> object:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
    user32.SetPropW.restype = wintypes.BOOL
    user32.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.GetPropW.restype = wintypes.HANDLE
    user32.RemovePropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.RemovePropW.restype = wintypes.HANDLE
    return user32


def _native_window_runtime_pid(hwnd: int, user32: object, property_name: str) -> int:
    """Return the current PID only when the HWND has this runtime's PID tag."""
    import ctypes
    from ctypes import wintypes
    if hwnd <= 0 or not user32.IsWindow(hwnd):
        return 0
    pid = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return 0
    return int(pid.value) if pid.value and int(user32.GetPropW(hwnd, property_name) or 0) == pid.value else 0


def _register_native_window_owner(window: object) -> int:
    """Register only the actual pywebview HWND belonging to this process."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None)
        hwnd = int(handle.ToInt64()) if handle is not None else 0
        user32 = _native_window_user32()
        if hwnd <= 0 or not user32.IsWindow(hwnd):
            return 0
        pid = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value != os.getpid():
            return 0
        property_name = _native_window_property_name()
        if int(user32.GetPropW(hwnd, property_name) or 0) not in {0, pid.value}:
            return 0
        if not user32.SetPropW(hwnd, property_name, pid.value):
            return 0
        return hwnd if _native_window_runtime_pid(hwnd, user32, property_name) == os.getpid() else 0
    except Exception as exc:
        LOG.warning("Could not register the native window's runtime ownership: %s", exc)
        return 0


def _remove_native_window_owner(hwnd: int) -> bool:
    """Remove only our tag from our still-live HWND; destroyed tags disappear."""
    if sys.platform != "win32" or hwnd <= 0:
        return False
    try:
        user32 = _native_window_user32()
        property_name = _native_window_property_name()
        if _native_window_runtime_pid(hwnd, user32, property_name) != os.getpid():
            return False
        return int(user32.RemovePropW(hwnd, property_name) or 0) == os.getpid()
    except Exception as exc:
        LOG.warning("Could not remove the native window's runtime ownership: %s", exc)
        return False


def _reveal_existing_native_window() -> bool:
    """Restore and foreground the existing native window when launched again."""
    if sys.platform != "win32":
        return False
    try:
        import win32con
        import win32gui
        user32 = _native_window_user32()
        property_name = _native_window_property_name()

        matches: list[tuple[int, int]] = []

        def _match(hwnd: int, _extra: object) -> None:
            if win32gui.GetWindowText(hwnd) == WINDOW_TITLE:
                pid = _native_window_runtime_pid(hwnd, user32, property_name)
                if pid:
                    matches.append((hwnd, pid))

        win32gui.EnumWindows(_match, None)
        if not matches:
            return False
        hwnd, pid = matches[0]
        if _native_window_runtime_pid(hwnd, user32, property_name) != pid:
            return False
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if _native_window_runtime_pid(hwnd, user32, property_name) != pid:
            return False
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        if _native_window_runtime_pid(hwnd, user32, property_name) != pid:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            return False
        return True
    except Exception as exc:
        LOG.warning("Could not restore the existing WISE window: %s", exc)
        return False


def _start_backend(host: str, port: int) -> None:
    """Start uvicorn in a background daemon thread."""
    global _server_thread
    _server_shutdown_event.clear()

    def _run_uvicorn() -> None:
        global _uvicorn_server
        try:
            import uvicorn
            config = uvicorn.Config(
                "api.server:app",
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                # ``pythonw.exe`` has no usable stdout/stderr streams.  The
                # default Uvicorn dictConfig resolves its ``default``
                # formatter against those streams and can abort before the
                # ASGI application starts.  WISE already owns a file-backed
                # launcher logger above, so do not install Uvicorn's console
                # logging configuration for the native desktop process.
                log_config=None,
            )
            _uvicorn_server = uvicorn.Server(config)
            _uvicorn_server.run()
        except Exception as exc:
            LOG.error("Uvicorn server failed: %s", exc)
        finally:
            _server_shutdown_event.set()

    _server_thread = threading.Thread(
        target=_run_uvicorn,
        name="WISE_Backend_Server",
        daemon=True,
    )
    _server_thread.start()
    LOG.info("Backend server starting on http://%s:%d", host, port)


def _wait_for_backend(host: str, port: int, timeout: float = BACKEND_STARTUP_TIMEOUT) -> bool:
    """Block until WISE's health endpoint, not merely its TCP port, is ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _wise_backend_is_running(host, port):
            LOG.info("Backend is live on http://%s:%d", host, port)
            return True
        time.sleep(0.25)
    LOG.error("Backend did not come online within %.1fs", timeout)
    return False


def _keep_background_backend_alive(host: str, port: int) -> None:
    """Boundedly restart a crashed resident backend in the user session.

    This is intentionally a process-local recovery mechanism, not a service
    manager.  It only runs for the opt-in background mode, uses exponential
    backoff, and stops after a small number of consecutive crashes so a broken
    install never burns CPU or hides a persistent failure.
    """
    restarts = 0
    while not _shutdown_requested.is_set():
        if not _server_shutdown_event.wait(timeout=2.0):
            if _system_session_guard:
                try:
                    if not _background_mode_enabled():
                        LOG.info("Owner disabled resident mode; draining the background backend.")
                        _stop_owned_backend()
                        return
                except Exception:
                    LOG.exception("Cannot verify resident consent; stopping conservatively.")
                    _stop_owned_backend()
                    return
            continue
        if _shutdown_requested.is_set():
            return
        thread = _server_thread
        if thread and thread.is_alive():
            # A spurious event cannot cause a duplicate server.
            _server_shutdown_event.clear()
            continue
        if restarts >= _BACKGROUND_MAX_RESTARTS:
            LOG.critical("WISE background backend stopped after %d failed recovery attempts.", restarts)
            return
        restarts += 1
        delay = min(8.0, float(2 ** (restarts - 1)))
        LOG.warning("WISE backend stopped unexpectedly; restarting in %.0fs (attempt %d/%d).",
                    delay, restarts, _BACKGROUND_MAX_RESTARTS)
        if _shutdown_requested.wait(delay):
            return
        _server_shutdown_event.clear()
        _start_backend(host, port)
        if not _wait_for_backend(host, port, timeout=BACKEND_STARTUP_TIMEOUT):
            LOG.error("WISE recovery attempt %d did not become ready.", restarts)


# ──────────────────────────────────────────────────────────────────────────────
# Desktop window management
# ──────────────────────────────────────────────────────────────────────────────
class DesktopAPI:
    def open_external_url(self, url: str) -> bool:
        """Human authorization opens in Brave, outside the model context."""
        from urllib.parse import urlsplit
        import shutil
        import subprocess
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only HTTPS authorization URLs are supported")
        candidates = [shutil.which("brave.exe")]
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(key)
            if base:
                candidates.append(str(Path(base) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"))
        browser = next((p for p in candidates if p and Path(p).is_file()), None)
        if not browser:
            raise RuntimeError("Brave is unavailable; open the authorization link manually")
        subprocess.Popen([browser, url])
        return True


def _launch_pywebview(url: str) -> bool:
    """Try to launch a native OS window via pywebview.  Returns True on success."""
    try:
        import webview  # type: ignore[import-untyped]

        transparent_host = _native_transparency_enabled()
        LOG.info(
            "Launching native window via pywebview (transparent_host=%s) → %s",
            transparent_host,
            url,
        )

        window = webview.create_window(
            title=WINDOW_TITLE,
            url=url,
            js_api=DesktopAPI(),
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            resizable=True,
            text_select=True,
            # Reliability is a product requirement.  The pywebview WinForms
            # transparent-window path uses a show/hide/navigation workaround
            # that can destroy the form on some WebView2 builds.  Keep it as an
            # explicit diagnostic opt-in until that native backend is stable.
            transparent=transparent_host,
            background_color="#101014",
        )

        # ``create_window`` owns the first show lifecycle. Calling ``show``
        # and ``restore`` from the startup callback races the WinForms/
        # WebView2 navigation path on some Windows installations, leaving the
        # event loop with no surviving window. Keep the native lifecycle in
        # pywebview and log it so an unexpected close is diagnosable.
        registered_hwnd = 0

        def _on_native_window_shown() -> None:
            nonlocal registered_hwnd
            registered_hwnd = _register_native_window_owner(window)
            if sys.platform == "win32" and not registered_hwnd:
                LOG.warning("Native window ownership registration failed; duplicate-launch restoration is disabled.")
            LOG.info("WISE native window shown.")
            if not transparent_host:
                _apply_windows_backdrop(window)

        window.events.shown += _on_native_window_shown
        window.events.closing += lambda: LOG.warning("WISE native window is closing.")
        def _on_native_window_closed() -> None:
            _remove_native_window_owner(registered_hwnd)
            LOG.warning("WISE native window closed.")

        window.events.closed += _on_native_window_closed

        # Blocking — runs the native platform webview event loop.
        try:
            webview.start(
                debug=os.environ.get("WISE_DEBUG", "").lower() in ("1", "true"),
            )
        finally:
            _remove_native_window_owner(registered_hwnd)
        return True
    except ImportError:
        LOG.error("pywebview is not installed; the native WISE window is unavailable.")
        return False
    except Exception as exc:
        LOG.error("pywebview failed (%s).", exc)
        return False


def _launch_browser_app_mode(url: str) -> bool:
    """Open the WISE UI in the user's Brave browser app mode when available."""
    import shutil

    brave_paths = [
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]

    browser_exe: Optional[str] = None
    for candidate in brave_paths:
        if Path(candidate).is_file():
            browser_exe = candidate
            break

    if browser_exe is None:
        browser_exe = shutil.which("brave") or shutil.which("brave-browser")

    if browser_exe:
        LOG.info("Opening WISE in app-mode: %s --app=%s", browser_exe, url)
        import subprocess
        try:
            subprocess.Popen(
                [browser_exe, f"--app={url}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:
            LOG.warning("App-mode launch failed: %s", exc)

    # Ultimate fallback — use the OS default browser only when Brave is not installed.
    LOG.info("Opening WISE UI in default browser: %s", url)
    webbrowser.open(url)
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────────────────────────────────────
def _shutdown_handler(signum: int, frame: object) -> None:
    LOG.info("Received signal %d — shutting down WISE Desktop.", signum)
    _stop_owned_backend()
    _release_native_window_mutex()
    sys.exit(0)


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry-point
# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    global _system_session_guard
    parser = argparse.ArgumentParser(
        description="WISE Desktop — General Conversational Computer Agent",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="API port (default: 8765)")
    parser.add_argument("--no-native", action="store_true", help="Skip pywebview, use browser")
    parser.add_argument("--native-only", action="store_true", help="Require a native pywebview window; never open a browser")
    parser.add_argument(
        "--background", action="store_true",
        help="Run the loopback backend without opening a browser or window",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug mode for webview DevTools")
    parser.add_argument("--system-session", action="store_true", help="Require current owner opt-in for login startup")
    args = parser.parse_args()
    _system_session_guard = args.system_session
    _shutdown_requested.clear()
    if args.system_session:
        from core.system_integration import get_system_integration_settings
        settings = get_system_integration_settings()
        if not args.background or not (settings["enabled"] and settings["start_at_login"] and settings["background_mode"]):
            LOG.info("Login launch skipped: resident startup is not enabled by the owner.")
            return

    if args.debug:
        os.environ["WISE_DEBUG"] = "1"

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    if not args.background and not _acquire_native_window_mutex():
        return

    existing_backend = False
    if args.background:
        # Login-started WISE is a singleton for this user session.  Do not
        # silently move it to an unknown random port when a desktop copy is
        # already open or another program owns the port.
        if _wise_backend_is_running(args.host, args.port):
            LOG.info("A WISE backend is already running on port %d; background startup is complete.", args.port)
            return
        if not _port_is_available(args.host, args.port):
            LOG.error("Port %d is occupied by another application; WISE background startup was not started.", args.port)
            sys.exit(1)
        port = args.port
    else:
        existing_backend = _wise_backend_is_running(args.host, args.port)
        port = args.port if existing_backend else _find_free_port(args.port)
    app_url = f"http://{args.host}:{port}/app/index.html"

    # 1. Start backend
    if not existing_backend:
        _start_backend(args.host, port)

    # 2. Wait for backend readiness
    if not existing_backend and not _wait_for_backend(args.host, port, timeout=BACKEND_STARTUP_TIMEOUT):
        if args.background:
            # The resident mode is explicitly supervised.  Treat an initial
            # failure like any other failed start so transient boot-time
            # conditions (for example a delayed network stack or dependency
            # load) receive the same bounded recovery policy.
            LOG.error("WISE background backend did not become ready; entering bounded recovery.")
            try:
                _keep_background_backend_alive(args.host, port)
            except KeyboardInterrupt:
                LOG.info("Keyboard interrupt received during startup recovery.")
            return
        LOG.error("WISE backend failed to start. Aborting desktop launch.")
        sys.exit(1)

    LOG.info("WISE Desktop URL: %s", app_url)

    if args.background:
        # Used only by the opt-in, per-user login task. The heavy model stays
        # demand-loaded; this resident process merely keeps local control and
        # the loopback API available while Windows is awake.
        LOG.info("WISE is running in low-power background mode. Press Ctrl+C to stop.")
        try:
            _keep_background_backend_alive(args.host, port)
        except KeyboardInterrupt:
            LOG.info("Keyboard interrupt received — shutting down.")
        LOG.info("WISE Desktop shutdown complete.")
        return

    # 3. Launch desktop window
    launched_native = False
    if not args.no_native:
        launched_native = _launch_pywebview(app_url)

    if not launched_native and args.native_only:
        LOG.error("WISE was asked to use a native window only, so browser fallback is disabled.")
        if not existing_backend:
            _stop_owned_backend()
        _release_native_window_mutex()
        sys.exit(2)

    if not launched_native:
        _launch_browser_app_mode(app_url)
        # Keep the main thread alive so the backend doesn't exit
        LOG.info("WISE is running. Press Ctrl+C to stop.")
        try:
            _server_shutdown_event.wait()
        except KeyboardInterrupt:
            LOG.info("Keyboard interrupt received — shutting down.")

    _release_native_window_mutex()
    if launched_native and not existing_backend and _background_mode_enabled():
        _system_session_guard = True
        LOG.info("Window closed; continuing the owner-enabled resident backend.")
        try:
            _keep_background_backend_alive(args.host, port)
        finally:
            _stop_owned_backend()
    elif not existing_backend:
        _stop_owned_backend()
    LOG.info("WISE Desktop shutdown complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("WISE Desktop terminated because of an unhandled launcher error.")
        raise
