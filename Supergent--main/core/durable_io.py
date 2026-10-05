"""Small OS-backed writer lock for durable local-state transactions.

All participants must hold the same lock across read -> modify -> atomic write.
This coordinates cooperating processes; it is not a sandbox or protection
against a malicious process. The lock file contains no user data and must not
be removed while other instances may be active (inode-lock race on Unix).
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict


_PATH_LOCKS: Dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_LOCK_DEPTH = threading.local()


@contextmanager
def storage_writer_lock(directory: Path, *, lock_name: str = ".wise-writer.lock", timeout: float = 5.0):
    """Reentrant per thread, exclusive across processes, bounded on contention."""
    if not lock_name or Path(lock_name).name != lock_name or "/" in lock_name or "\\" in lock_name:
        raise ValueError("Lock name must be a local filename")
    directory = Path(directory)
    deadline = time.monotonic() + max(0.0, timeout)
    key = str((directory / lock_name).resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.setdefault(key, threading.RLock())
    # The in-process wait is bounded too; a blocked writer cannot freeze a
    # preference/session API indefinitely while another worker performs I/O.
    if not lock.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError("Durable state writer is busy; retry the update")
    try:
        depths = getattr(_LOCK_DEPTH, "paths", {})
        if depths.get(key):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / lock_name).open("a+b")
        acquired = False
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Durable state writer is busy; retry the update")
                    time.sleep(min(.025, max(0.0, deadline - time.monotonic())))
            depths[key] = 1
            _LOCK_DEPTH.paths = depths
            yield
        finally:
            depths.pop(key, None)
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
    finally:
        lock.release()
