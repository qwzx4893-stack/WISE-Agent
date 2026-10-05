#!/usr/bin/env python3
"""
WISE Desktop Application Launcher.
Launches the native WISE Desktop GUI using pywebview (with automatic fallback
to Brave App Mode or the default system browser).
"""

from __future__ import annotations

import os
import sys
import time
import socket
import logging
import subprocess
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [WISE-DESKTOP] %(message)s",
    datefmt="%H:%M:%S"
)
LOG = logging.getLogger("wise.desktop")

WORKSPACE_ROOT = Path(__file__).resolve().parent
SUPERGENT_ROOT = WORKSPACE_ROOT / "Supergent--main"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
if str(SUPERGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_ROOT))

PORT = int(os.environ.get("SUPERGENT_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/app/"


def is_server_listening(host: str = "127.0.0.1", port: int = PORT, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_backend_server() -> Optional[subprocess.Popen]:
    """Ensures that the FastAPI backend server is running."""
    if is_server_listening():
        LOG.info("WISE Backend API server is already online on port %d.", PORT)
        return None

    LOG.info("Starting WISE Backend API server on port %d...", PORT)
    cmd = [
        sys.executable, "-X", "utf8", "-m", "uvicorn",
        "api.server:app",
        "--host", "127.0.0.1",
        "--port", str(PORT),
        "--app-dir", str(SUPERGENT_ROOT)
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(SUPERGENT_ROOT),
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "AGENT_OS_ROOT": os.environ.get("AGENT_OS_ROOT", str(SUPERGENT_ROOT)),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    # Wait up to 10 seconds for port to open
    t0 = time.time()
    while time.time() - t0 < 10.0:
        if is_server_listening():
            LOG.info("WISE Backend API server is now ONLINE.")
            return proc
        time.sleep(0.5)

    LOG.warning("Backend server started but port probe timed out.")
    return proc


def launch_native_window(check_only: bool = False) -> bool:
    """Launches the desktop GUI via pywebview."""
    try:
        import webview
        LOG.info("Launching native desktop window via pywebview (WebView2)...")
        if check_only:
            LOG.info("Check-only mode verified pywebview import and window parameters.")
            return True

        window = webview.create_window(
            title="WISE — General Conversational Computer Agent",
            url=URL,
            width=1380,
            height=880,
            min_size=(1024, 700),
            background_color="#080b11",
            resizable=True,
            confirm_close=False,
        )
        webview.start(debug=False)
        return True
    except Exception as exc:
        LOG.warning("pywebview launch failed or not configured (%s); attempting App-Mode fallback...", exc)
        return False


def launch_app_mode_browser(check_only: bool = False) -> bool:
    """Fallback: launch Brave in standalone App Mode."""
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]

    exe_path = None
    for c in candidates:
        if os.path.exists(c):
            exe_path = c
            break

    if exe_path:
        LOG.info("Launching standalone App Mode using: %s", exe_path)
        if check_only:
            return True
        cmd = [exe_path, f"--app={URL}", "--window-size=1380,880"]
        subprocess.Popen(cmd)
        return True

    # Generic browser open
    import webbrowser
    LOG.info("Opening URL in default system browser...")
    if not check_only:
        webbrowser.open(URL)
    return True


def main():
    check_only = "--check-only" in sys.argv
    LOG.info("=" * 60)
    LOG.info("  WISE DESKTOP AGENT INITIALIZATION")
    LOG.info("=" * 60)

    server_proc = ensure_backend_server()

    try:
        success = launch_native_window(check_only=check_only)
        if not success:
            launch_app_mode_browser(check_only=check_only)
    finally:
        if check_only and server_proc:
            LOG.info("Terminating temporary check-only backend process...")
            server_proc.terminate()


if __name__ == "__main__":
    main()
