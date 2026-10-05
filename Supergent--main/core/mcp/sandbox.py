"""Apply Agent OS sandbox limits to MCP stdio subprocesses.

Re-uses the same ``RLIMIT_*`` envelope that the
:class:`agent_core.ProotSandbox` and the self-modify toolkit enforce:

- ``RLIMIT_CPU``    : 60 s (soft) / 65 s (hard)   — overridable.
- ``RLIMIT_AS``     : 2 GiB                        — overridable.
- ``RLIMIT_NPROC``  : 256                          — overridable.
- ``RLIMIT_CORE``   : 0 / 0  (no core dumps).
- ``setsid``        : new session for clean kill.
- ``proot``         : when available + a rootfs is configured, the
  command is wrapped in ``proot -r <rootfs> -w <cwd>``.

We don't ship our own subprocess launcher — instead we expose a
``preexec_fn`` and a ``wrap_command`` helper that ``MCPClient`` plugs
into ``subprocess.Popen``. That keeps the sandbox optional and easy to
disable per-server (``sandbox: false`` in the registry).
"""

from __future__ import annotations

import os
from typing import Callable, List, Optional


# --------------------------------------------------------------------------
def _read_limits() -> dict:
    return {
        "cpu_s": int(os.environ.get("AGENT_CPU_LIMIT_S", "60")),
        "mem_mb": int(os.environ.get("AGENT_MEM_LIMIT_MB", "2048")),
        "max_procs": int(os.environ.get("AGENT_MAX_PROCS", "256")),
    }


def make_preexec(*, cpu_s: Optional[int] = None,
                 mem_mb: Optional[int] = None,
                 max_procs: Optional[int] = None) -> Callable[[], None]:
    """Return a ``preexec_fn`` enforcing rlimits in the child."""
    defaults = _read_limits()
    cpu_s = defaults["cpu_s"] if cpu_s is None else int(cpu_s)
    mem_mb = defaults["mem_mb"] if mem_mb is None else int(mem_mb)
    max_procs = defaults["max_procs"] if max_procs is None else int(max_procs)

    def _preexec() -> None:
        # Detach into our own session so a runaway child can be killed
        # by killpg() without hitting the parent.
        try:
            os.setsid()
        except OSError:
            pass
        try:
            import resource as _r  # type: ignore
        except Exception:
            return

        # No core dumps under any condition.
        for limit, val in (
            (_r.RLIMIT_CORE, (0, 0)),
        ):
            try:
                _r.setrlimit(limit, val)
            except (ValueError, OSError):
                pass

        if cpu_s > 0:
            try:
                _r.setrlimit(_r.RLIMIT_CPU, (cpu_s, cpu_s + 5))
            except (ValueError, OSError):
                pass
        if mem_mb > 0:
            try:
                _r.setrlimit(_r.RLIMIT_AS, (mem_mb * 1024 * 1024,
                                             mem_mb * 1024 * 1024))
            except (ValueError, OSError):
                pass
        if max_procs > 0:
            try:
                _r.setrlimit(_r.RLIMIT_NPROC, (max_procs, max_procs))
            except (ValueError, OSError):
                pass

    return _preexec


# --------------------------------------------------------------------------
def wrap_command(command: str, args: List[str]) -> List[str]:
    """Wrap a command in ``proot`` when it's available *and* a rootfs is
    configured via ``AGENT_PROOT_ROOTFS``. Otherwise return unchanged.
    """
    rootfs = os.environ.get("AGENT_PROOT_ROOTFS", "").strip()
    proot_bin = os.environ.get("AGENT_PROOT_BIN", "/usr/bin/proot").strip()
    if not rootfs or not os.path.isdir(rootfs) or not os.path.exists(proot_bin):
        return [command, *args]
    return [proot_bin, "-r", rootfs, command, *args]


def is_sandbox_enforced() -> bool:
    """Whether this host can actually isolate a stdio MCP subprocess.

    The rlimit/preexec envelope is POSIX-only and does not provide a Windows
    filesystem sandbox.  Treating it as one would silently launch arbitrary
    third-party MCP code with the user's Windows account.  A configured PRoot
    rootfs is the minimum supported isolation mechanism for this transport.
    """
    if os.name == "nt":
        return False
    rootfs = os.environ.get("AGENT_PROOT_ROOTFS", "").strip()
    proot_bin = os.environ.get("AGENT_PROOT_BIN", "/usr/bin/proot").strip()
    return bool(rootfs and os.path.isdir(rootfs) and os.path.exists(proot_bin))


__all__ = ["make_preexec", "wrap_command", "is_sandbox_enforced"]
