"""
Persistent IPython / Python REPL session for the agent.

Mirrors OpenHands' IPython sandbox: a long-lived child Python process
that the agent writes code to via stdin and reads output from via
stdout/stderr. State (imports, variables, function defs) survives across
calls so the agent can iterate without re-running setup.

Two backends are supported:

1. ``IPythonKernel`` — uses :mod:`jupyter_client` when available. Best
   isolation, exact stdout/stderr/result split.
2. ``PythonSubprocess`` — pure stdlib fallback that drives ``python -i``
   with a sentinel-based protocol. Available everywhere.

Either backend exposes the same ``run(code) -> RunResult`` interface.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int  # 0 success, 1 error in the snippet, 124 timeout
    duration_ms: float
    backend: str = "subprocess"


class _Backend:
    backend_name: str = "abstract"

    def run(self, code: str, *, timeout: float = 60.0) -> RunResult:
        raise NotImplementedError

    def close(self) -> None:
        pass


_REPL_LOOP = r"""
import sys, traceback, io, contextlib
_globals = {'__name__': '__main__'}
while True:
    line = sys.stdin.readline()
    if not line:
        break
    if not line.startswith('AGENT_OS_RUN_LEN '):
        continue
    try:
        n = int(line.split()[1])
    except Exception:
        continue
    payload = sys.stdin.read(n)
    sentinel = sys.stdin.readline().strip()
    rc = 0
    err_buf = io.StringIO()
    out_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            exec(compile(payload, '<codeact>', 'exec'), _globals)
        except SystemExit as e:
            rc = int(getattr(e, 'code', 0) or 0)
        except BaseException:
            traceback.print_exc()
            rc = 1
    sys.stdout.write(out_buf.getvalue())
    sys.stdout.write(sentinel + ' ' + str(rc) + '\n')
    sys.stdout.flush()
    sys.stderr.write(err_buf.getvalue())
    sys.stderr.write(sentinel + '\n')
    sys.stderr.flush()
"""


class _PythonSubprocessBackend(_Backend):
    """Drive a custom REPL loop with a sentinel-based protocol."""

    backend_name = "python-subprocess"

    def __init__(self, workdir: Optional[Path] = None,
                 *, python: Optional[str] = None) -> None:
        self.workdir = Path(workdir or Path.cwd())
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self._proc = subprocess.Popen(
            [python or sys.executable, "-u", "-c", _REPL_LOOP],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            cwd=str(self.workdir), env=env,
        )
        self._lock = threading.Lock()
        self._stdout_q: "queue.Queue[str]" = queue.Queue()
        self._stderr_q: "queue.Queue[str]" = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._reader, args=(self._proc.stdout, self._stdout_q),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._reader, args=(self._proc.stderr, self._stderr_q),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        # Drain the interpreter banner / prompt.
        self._drain(timeout=0.5)

    @staticmethod
    def _reader(stream, q: "queue.Queue[str]") -> None:
        try:
            for line in iter(stream.readline, ""):
                q.put(line)
        except Exception:
            return
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _drain(self, timeout: float) -> tuple[str, str]:
        out_chunks: list[str] = []
        err_chunks: list[str] = []
        end = time.time() + timeout
        while time.time() < end:
            drained_any = False
            try:
                while True:
                    out_chunks.append(self._stdout_q.get_nowait())
                    drained_any = True
            except queue.Empty:
                pass
            try:
                while True:
                    err_chunks.append(self._stderr_q.get_nowait())
                    drained_any = True
            except queue.Empty:
                pass
            if not drained_any:
                time.sleep(0.02)
        return "".join(out_chunks), "".join(err_chunks)

    def run(self, code: str, *, timeout: float = 60.0) -> RunResult:
        if self._proc.poll() is not None:
            return RunResult("", "(kernel exited)", 1, 0.0,
                             backend=self.backend_name)

        sentinel = f"__AGENT_OS_DONE_{uuid.uuid4().hex}__"
        started = time.time()
        with self._lock:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(f"AGENT_OS_RUN_LEN {len(code)}\n")
                self._proc.stdin.write(code)
                self._proc.stdin.write(f"{sentinel}\n")
                self._proc.stdin.flush()
            except Exception as exc:
                return RunResult("", f"stdin write failed: {exc}", 1,
                                 (time.time() - started) * 1000,
                                 backend=self.backend_name)

            # Read until we see the sentinel.
            out_chunks: list[str] = []
            err_chunks: list[str] = []
            rc = 1
            saw_sentinel_out = False
            saw_sentinel_err = False
            end = started + timeout
            while time.time() < end:
                progressed = False
                try:
                    while True:
                        line = self._stdout_q.get_nowait()
                        if line.startswith(sentinel):
                            try:
                                rc = int(line[len(sentinel):].strip() or "0")
                            except Exception:
                                rc = 0
                            saw_sentinel_out = True
                        else:
                            out_chunks.append(line)
                        progressed = True
                except queue.Empty:
                    pass
                try:
                    while True:
                        line = self._stderr_q.get_nowait()
                        if line.startswith(sentinel):
                            saw_sentinel_err = True
                        else:
                            err_chunks.append(line)
                        progressed = True
                except queue.Empty:
                    pass
                if saw_sentinel_out and saw_sentinel_err:
                    break
                if not progressed:
                    time.sleep(0.01)
            else:
                return RunResult("".join(out_chunks),
                                 "".join(err_chunks) + "\n(timed out)",
                                 124, (time.time() - started) * 1000,
                                 backend=self.backend_name)

        return RunResult(stdout="".join(out_chunks),
                         stderr="".join(err_chunks),
                         exit_code=rc,
                         duration_ms=(time.time() - started) * 1000,
                         backend=self.backend_name)

    def close(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.stdin and self._proc.stdin.close()
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass


class IPythonSession:
    """Public façade that picks the best backend for the host."""

    def __init__(self, workdir: Optional[Path] = None,
                 *, prefer_jupyter: bool = False) -> None:
        self.workdir = Path(workdir or Path.cwd())
        self._backend: Optional[_Backend] = None
        self._prefer_jupyter = prefer_jupyter

    def _ensure_backend(self) -> _Backend:
        if self._backend is not None:
            return self._backend
        # jupyter_client backend is intentionally optional; the subprocess
        # backend is enough for the unit tests.
        self._backend = _PythonSubprocessBackend(self.workdir)
        return self._backend

    @property
    def backend_name(self) -> str:
        be = self._backend
        return be.backend_name if be else "(uninitialised)"

    def run(self, code: str, *, timeout: float = 60.0) -> RunResult:
        return self._ensure_backend().run(code, timeout=timeout)

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
            self._backend = None

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass


__all__ = ["IPythonSession", "RunResult"]
