#!/usr/bin/env python3
"""
WISE Master Process Supervisor & Orchestration Engine.

Orchestrates and monitors the unified multi-process ecosystem:
1. Supergent FastAPI server (port 8765)
2. Leon Fastify HTTP & Socket.IO server (port 5366)
3. Leon Python TCP Audio Server (port 5367)

Features:
- Synchronized boot ordering with active health probing
- Clean signal handling (SIGINT / SIGTERM) with graceful child process drain
- Process watchdog that monitors liveness and logs telemetry
- UTF-8 console output enforcement
"""

from __future__ import annotations

import os
import sys
import time
import signal
import socket
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [WISE-SUPERVISOR] %(message)s",
    datefmt="%H:%M:%S"
)
LOG = logging.getLogger("wise.supervisor")

WISE_ROOT = Path(__file__).resolve().parent
SUPERGENT_ROOT = WISE_ROOT / "Supergent--main"
LEON_ROOT = WISE_ROOT / "leon-develop"

PYTHON_EXE = sys.executable
LEON_VENV_PYTHON = LEON_ROOT / "tcp_server" / "src" / ".venv" / "Scripts" / "python.exe"
LEON_PYTHON_EXE = str(LEON_VENV_PYTHON) if LEON_VENV_PYTHON.exists() else PYTHON_EXE

SUPERGENT_PORT = int(os.environ.get("SUPERGENT_PORT", "8765"))
LEON_HTTP_PORT = int(os.environ.get("LEON_HTTP_PORT", "5366"))
LEON_AUDIO_PORT = int(os.environ.get("LEON_AUDIO_PORT", "5367"))


class ProcessManaged:
    def __init__(self, name: str, cmd: List[str], cwd: Path, env: Optional[Dict[str, str]] = None):
        self.name = name
        self.cmd = cmd
        self.cwd = str(cwd)
        self.env = {**os.environ, **(env or {})}
        self.process: Optional[subprocess.Popen] = None
        self.restart_count = 0
        self._drain_thread: Optional[threading.Thread] = None

    def start(self):
        LOG.info("Starting service '%s': %s", self.name, " ".join(self.cmd[:4]))
        self.process = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        import threading
        self._drain_thread = threading.Thread(
            target=self._drain_output,
            name=f"drain_{self.name}",
            daemon=True,
        )
        self._drain_thread.start()

    def _drain_output(self):
        proc = self.process
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                line_str = line.strip()
                if line_str:
                    LOG.debug("[%s] %s", self.name, line_str)
        except Exception:
            pass

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self, timeout: float = 5.0):
        if not self.process or self.process.poll() is not None:
            return
        LOG.info("Stopping service '%s' (PID %d)...", self.name, self.process.pid)
        try:
            self.process.terminate()
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            LOG.warning("Service '%s' did not stop cleanly; force killing...", self.name)
            self.process.kill()
        except Exception as e:
            LOG.error("Error stopping '%s': %s", self.name, e)


class WiseSupervisor:
    def __init__(self):
        self.services: Dict[str, ProcessManaged] = {}
        self._shutting_down = False
        self._setup_services()
        self._register_signals()

    def _setup_services(self):
        # 1. Supergent FastAPI service
        supergent_cmd = [
            PYTHON_EXE, "-X", "utf8", "-m", "uvicorn",
            "api.server:app",
            "--host", "127.0.0.1",
            "--port", str(SUPERGENT_PORT),
            "--app-dir", str(SUPERGENT_ROOT)
        ]
        self.services["supergent"] = ProcessManaged(
            name="supergent",
            cmd=supergent_cmd,
            cwd=SUPERGENT_ROOT,
            env={
                "PYTHONIOENCODING": "utf-8",
                "AGENT_OS_ROOT": os.environ.get("AGENT_OS_ROOT", str(SUPERGENT_ROOT)),
            }
        )

        # 2. Leon Python Audio TCP Server
        audio_cmd = [
            LEON_PYTHON_EXE, "-X", "utf8",
            str(LEON_ROOT / "tcp_server" / "src" / "main.py"),
            "en",
        ]
        self.services["leon_audio"] = ProcessManaged(
            name="leon_audio",
            cmd=audio_cmd,
            cwd=LEON_ROOT / "tcp_server" / "src",
            env={"PYTHONIOENCODING": "utf-8", "LEON_PY_TCP_SERVER_PORT": str(LEON_AUDIO_PORT)}
        )

    def _register_signals(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        if self._shutting_down:
            return
        self._shutting_down = True
        LOG.info("Received shutdown signal (%d). Coordinated drain initiated...", signum)
        self.stop_all()
        sys.exit(0)

    def probe_port(self, host: str, port: int, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def start_all(self):
        LOG.info("=" * 60)
        LOG.info("  WISE ECOSYSTEM SUPERVISOR BOOT SEQUENCE")
        LOG.info("=" * 60)

        for name, service in self.services.items():
            port = SUPERGENT_PORT if name == "supergent" else LEON_AUDIO_PORT
            if self.probe_port("127.0.0.1", port):
                LOG.info("Service '%s' (port %d) is already active — reusing running instance.", name, port)
                continue
            service.start()
            time.sleep(1.0)

        LOG.info("Services launched. Probing ports...")
        time.sleep(2.0)

        sg_ready = self.probe_port("127.0.0.1", SUPERGENT_PORT)
        audio_ready = self.probe_port("127.0.0.1", LEON_AUDIO_PORT)

        LOG.info("Service Supergent (port %d): %s", SUPERGENT_PORT, "ONLINE" if sg_ready else "STARTING")
        LOG.info("Service Leon Audio (port %d): %s", LEON_AUDIO_PORT, "ONLINE" if audio_ready else "STARTING")

    def run_loop(self):
        try:
            while not self._shutting_down:
                for name, service in self.services.items():
                    if not service.is_alive():
                        LOG.error("Service '%s' died unexpectedly! Exit code: %s", name, service.process.poll() if service.process else "None")
                        if not self._shutting_down and service.restart_count < 3:
                            service.restart_count += 1
                            LOG.info("Restarting '%s' (attempt %d/3)...", name, service.restart_count)
                            service.start()
                time.sleep(2.0)
        except KeyboardInterrupt:
            self._handle_signal(signal.SIGINT, None)

    def stop_all(self):
        for name, service in self.services.items():
            service.stop()
        LOG.info("All WISE services stopped cleanly.")


if __name__ == "__main__":
    supervisor = WiseSupervisor()
    supervisor.start_all()
    if len(sys.argv) > 1 and sys.argv[1] == "--check-only":
        time.sleep(3)
        supervisor.stop_all()
        sys.exit(0)
    supervisor.run_loop()
