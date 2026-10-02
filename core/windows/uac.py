"""Owner-visible, monitored Windows UAC maintenance operations.

WISE deliberately does *not* expose an ``elevate(command)`` primitive. Every
operation has a fixed executable and fixed arguments. Windows still displays
its protected UAC consent UI, and the owner must approve it.

The broker uses ``ShellExecuteExW`` with ``SEE_MASK_NOCLOSEPROCESS``. This
retains a process handle after consent so WISE can report the real running,
completed, or failed state without automating or bypassing the UAC prompt.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional

from core.security import SecurityContext, get_security_gate


LOG = logging.getLogger("wise.uac")
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NOASYNC = 0x00000100
_SW_SHOWNORMAL = 1
_ERROR_CANCELLED = 1223
_INFINITE = 0xFFFFFFFF
_WAIT_FAILED = 0xFFFFFFFF
_STILL_ACTIVE = 259


@dataclass(frozen=True)
class UacOperation:
    """A non-parameterised, code-reviewed elevated maintenance operation."""

    id: str
    title: str
    description: str
    executable: str
    arguments: str


@dataclass(frozen=True)
class UacLaunch:
    """The process identity returned by a successful ShellExecuteExW call."""

    process_handle: int
    process_id: int


@dataclass
class UacRun:
    """Bounded, non-secret lifecycle record for one elevated operation."""

    request_id: str
    operation_id: str
    title: str
    state: str
    requested_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    process_id: Optional[int] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# No user-controlled command, path, or argument is interpolated into this
# catalogue. Adding an operation is therefore a code-reviewed change.
_OPERATIONS: Dict[str, UacOperation] = {
    "system_file_check": UacOperation(
        id="system_file_check",
        title="System File Checker",
        description="Scans and repairs protected Windows system files (sfc /scannow).",
        executable="sfc.exe",
        arguments="/scannow",
    ),
    "windows_image_repair": UacOperation(
        id="windows_image_repair",
        title="Windows component-store repair",
        description="Repairs the Windows component store using DISM. It may take a while and use Windows Update.",
        executable="DISM.exe",
        arguments="/Online /Cleanup-Image /RestoreHealth",
    ),
    "flush_dns": UacOperation(
        id="flush_dns",
        title="Flush DNS cache",
        description="Clears the local Windows DNS resolver cache to resolve stale DNS records.",
        executable="ipconfig.exe",
        arguments="/flushdns",
    ),
}


class UacBroker:
    """Launch and monitor owner-reviewed Windows UAC work."""

    def __init__(self, *, history_limit: int = 50) -> None:
        self._lock = threading.RLock()
        self._history: Deque[UacRun] = deque(maxlen=max(10, min(int(history_limit), 200)))
        self._active: Dict[str, UacRun] = {}
        self._active_by_operation: Dict[str, str] = {}
        self._intents: Dict[str, tuple[str, float]] = {}

    def supported(self) -> bool:
        return sys.platform == "win32"

    def operations(self) -> List[Dict[str, str]]:
        return [
            {"id": item.id, "title": item.title, "description": item.description}
            for item in _OPERATIONS.values()
        ]

    def runs(self, *, limit: int = 20) -> Dict[str, Any]:
        """Return a thread-safe snapshot of active and recent UAC runs."""
        bounded = max(1, min(int(limit), self._history.maxlen or 50))
        with self._lock:
            active = [item.to_dict() for item in self._active.values()]
            recent = [item.to_dict() for item in list(self._history)[-bounded:]][::-1]
        active.sort(key=lambda item: item["requested_at"], reverse=True)
        return {
            "supported": self.supported(),
            "active_count": len(active),
            "active": active,
            "recent": recent,
        }

    def issue_intent(self, operation_id: str, *, ttl_seconds: int = 90) -> Dict[str, Any]:
        """Issue a short-lived, single-use token bound to one operation.

        A hostile web origin can cause a browser to send some loopback requests,
        but it cannot read this same-origin JSON response through WISE's CORS
        boundary. Requiring the token on the second request therefore makes a
        bare ``confirmed=true`` body insufficient for administrative work.
        """
        if operation_id not in _OPERATIONS:
            return {"ok": False, "error": "Unknown UAC operation."}
        if not self.supported():
            return {"ok": False, "error": "UAC elevation is available only on Windows."}
        ttl = max(15, min(int(ttl_seconds), 180))
        expires_at = time.time() + ttl
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with self._lock:
            now = time.time()
            self._intents = {
                key: value for key, value in self._intents.items()
                if value[1] > now
            }
            if len(self._intents) >= 128:
                oldest = min(self._intents, key=lambda key: self._intents[key][1])
                self._intents.pop(oldest, None)
            self._intents[token] = (operation_id, expires_at)
        return {
            "ok": True,
            "operation": operation_id,
            "intent_token": token,
            "expires_at": expires_at,
        }

    def _consume_intent(self, operation_id: str, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            record = self._intents.pop(token, None)
        return bool(record and record[0] == operation_id and record[1] > time.time())

    def _record_activity(self, run: UacRun) -> None:
        try:
            from core.dashboard import ActivityLog

            status = {
                "completed": "success",
                "failed": "failed",
                "cancelled": "warning",
                "running": "info",
                "awaiting_approval": "info",
            }.get(run.state, "info")
            ActivityLog().record(
                "system",
                actor="uac_broker",
                summary=f"UAC {run.operation_id}: {run.state}",
                status=status,
                payload={
                    "request_id": run.request_id,
                    "operation_id": run.operation_id,
                    "process_id": run.process_id,
                    "exit_code": run.exit_code,
                },
            )
        except Exception:
            LOG.debug("Could not append UAC activity event.", exc_info=True)

    def _launch_runas(self, operation: UacOperation) -> UacLaunch:
        """Ask Windows to elevate a fixed operation and retain its process handle."""
        from ctypes import wintypes

        class _ShellExecuteInfoW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", ctypes.c_ulong),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HANDLE),
                ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfoW)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL

        info = _ShellExecuteInfoW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
        info.lpVerb = "runas"
        info.lpFile = operation.executable
        info.lpParameters = operation.arguments
        info.nShow = _SW_SHOWNORMAL  # Elevated maintenance is never hidden.

        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            code = int(ctypes.get_last_error())
            message = (
                "The owner cancelled the Windows UAC request."
                if code == _ERROR_CANCELLED
                else "Windows could not start the elevated operation."
            )
            raise OSError(code, message)
        handle = int(info.hProcess or 0)
        if not handle:
            raise OSError(0, "Windows did not return an elevated process handle.")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel32.GetProcessId.restype = wintypes.DWORD
        process_id = int(kernel32.GetProcessId(wintypes.HANDLE(handle)))
        return UacLaunch(process_handle=handle, process_id=process_id)

    def _reserve_run(self, operation: UacOperation) -> tuple[Optional[UacRun], Optional[Dict[str, Any]]]:
        with self._lock:
            existing_id = self._active_by_operation.get(operation.id)
            if existing_id and existing_id in self._active:
                existing = self._active[existing_id]
                return None, {
                    "ok": False,
                    "error": "This UAC maintenance operation is already active.",
                    "duplicate": True,
                    "run": existing.to_dict(),
                }
            run = UacRun(
                request_id=uuid.uuid4().hex,
                operation_id=operation.id,
                title=operation.title,
                state="awaiting_approval",
                requested_at=time.time(),
            )
            self._active[run.request_id] = run
            self._active_by_operation[operation.id] = run.request_id
            return run, None

    def _finish_run(
        self,
        request_id: str,
        *,
        state: str,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
    ) -> Optional[UacRun]:
        with self._lock:
            run = self._active.pop(request_id, None)
            if run is None:
                return None
            self._active_by_operation.pop(run.operation_id, None)
            run.state = state
            run.completed_at = time.time()
            run.exit_code = exit_code
            run.error = error
            self._history.append(run)
        self._record_activity(run)
        return run

    def _monitor_process(self, request_id: str, process_handle: int) -> None:
        from ctypes import wintypes

        exit_code: Optional[int] = None
        error: Optional[str] = None
        state = "failed"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            wait_result = int(kernel32.WaitForSingleObject(wintypes.HANDLE(process_handle), _INFINITE))
            if wait_result == _WAIT_FAILED:
                code = int(ctypes.get_last_error())
                error = f"Windows could not monitor the elevated process (error {code})."
            else:
                code_value = wintypes.DWORD(_STILL_ACTIVE)
                kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                kernel32.GetExitCodeProcess.restype = wintypes.BOOL
                if not kernel32.GetExitCodeProcess(wintypes.HANDLE(process_handle), ctypes.byref(code_value)):
                    code = int(ctypes.get_last_error())
                    error = f"Windows could not read the elevated process result (error {code})."
                else:
                    exit_code = int(code_value.value)
                    state = "completed" if exit_code == 0 else "failed"
                    if exit_code != 0:
                        error = f"The elevated operation exited with code {exit_code}."
        except Exception as exc:  # noqa: BLE001 - monitoring must fail closed
            error = f"Elevated process monitoring failed: {exc}"
        finally:
            try:
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.CloseHandle(wintypes.HANDLE(process_handle))
            except Exception:
                LOG.debug("Could not close elevated process handle.", exc_info=True)
        self._finish_run(request_id, state=state, exit_code=exit_code, error=error)

    def _start_monitor(self, request_id: str, process_handle: int) -> None:
        threading.Thread(
            target=self._monitor_process,
            args=(request_id, process_handle),
            name=f"WISE_UAC_{request_id[:8]}",
            daemon=True,
        ).start()

    def request(
        self,
        operation_id: str,
        *,
        confirmed: bool,
        intent_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        operation: Optional[UacOperation] = _OPERATIONS.get(operation_id)
        if operation is None:
            return {"ok": False, "error": "Unknown UAC operation."}
        if not self.supported():
            return {"ok": False, "error": "UAC elevation is available only on Windows."}

        from core.system_integration import get_system_integration_settings

        if not get_system_integration_settings()["enabled"]:
            return {"ok": False, "error": "Enable Super Computer mode before requesting UAC maintenance."}

        evaluation = get_security_gate().evaluate_action(
            "run_elevated_command",
            {"operation": operation.id, "title": operation.title},
            SecurityContext(caller="uac_broker", confirmed=confirmed),
        )
        if not evaluation.allowed:
            return {
                "ok": False,
                "error": evaluation.reason,
                "requires_confirmation": evaluation.requires_confirmation,
                "security_evaluation": evaluation.to_dict(),
            }

        if not self._consume_intent(operation.id, intent_token):
            return {
                "ok": False,
                "error": "A fresh, single-use WISE UAC intent is required.",
                "requires_intent": True,
            }

        run, duplicate = self._reserve_run(operation)
        if duplicate is not None:
            return duplicate
        assert run is not None
        self._record_activity(run)

        try:
            launch = self._launch_runas(operation)
        except OSError as exc:
            code = int(getattr(exc, "winerror", 0) or getattr(exc, "errno", 0) or 0)
            cancelled = code == _ERROR_CANCELLED
            error = str(exc) or (
                "The owner cancelled the Windows UAC request."
                if cancelled
                else "Could not request Windows UAC."
            )
            finished = self._finish_run(
                run.request_id,
                state="cancelled" if cancelled else "failed",
                error=error,
            )
            return {
                "ok": False,
                "error": error,
                "cancelled": cancelled,
                "winerror": code,
                "run": finished.to_dict() if finished else None,
            }
        except Exception as exc:  # noqa: BLE001 - OS error is returned to UI
            finished = self._finish_run(run.request_id, state="failed", error=str(exc))
            return {
                "ok": False,
                "error": f"Could not request Windows UAC: {exc}",
                "run": finished.to_dict() if finished else None,
            }

        with self._lock:
            run.state = "running"
            run.started_at = time.time()
            run.process_id = launch.process_id
        self._record_activity(run)
        self._start_monitor(run.request_id, launch.process_handle)
        return {
            "ok": True,
            "operation": operation.id,
            "request_id": run.request_id,
            "run": run.to_dict(),
            "message": "Windows approved the UAC request. WISE is monitoring the elevated operation until it exits.",
        }


_BROKER: Optional[UacBroker] = None


def get_uac_broker() -> UacBroker:
    global _BROKER
    if _BROKER is None:
        _BROKER = UacBroker()
    return _BROKER


__all__ = [
    "UacBroker",
    "UacLaunch",
    "UacOperation",
    "UacRun",
    "get_uac_broker",
]
