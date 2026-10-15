"""Local preview HTTP server.

When the agent generates a website / static app inside the workspace,
it can call :func:`start_preview` (or the ``preview.serve`` agent
tool) to serve that directory and hand the user back a clickable
``http://127.0.0.1:<port>`` URL.

Design notes — please read before changing this file
====================================================

1. **Localhost only.** Every server is bound to ``127.0.0.1`` (never
   ``0.0.0.0``). The spec asks for "inside the sandbox" but our
   sandbox today (``core/sandbox.py``) uses ``--unshare-net`` which
   removes the network namespace entirely — no socket can be bound at
   all. Running the HTTP server in the host process bound to the
   loopback interface is the deliberate compromise: the preview is
   only reachable from the same machine, never the network.

2. **Workspace-scoped paths.** Every directory we serve must be
   inside ``WORKSPACE_DIR``. ``_resolve_path`` enforces this by
   resolving symlinks and re-checking ``Path.is_relative_to``. A
   request to serve ``/etc`` or ``../../`` is rejected with a clear
   error.

3. **Auto-cleanup.** :class:`PreviewManager` tracks every running
   server, its idle timer, and a global cap (``MAX_CONCURRENT``).
   :func:`stop_all` is wired into FastAPI's lifespan shutdown and
   into ``Session.close()`` so a crashed agent never leaves orphaned
   ports.

4. **Idle timeout.** Each server tracks the time of its last logged
   request. When the manager's reaper thread sees no traffic for
   ``ttl_seconds``, it kills the server. Default 30 minutes.
"""

from __future__ import annotations

import http.server
import datetime
import email.utils
import html
import io
import json
import os
import socket
import socketserver
import stat
import threading
import time
import uuid
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_TTL_SECONDS = 30 * 60          # 30 minutes idle
MAX_CONCURRENT = 8                       # global cap
PORT_RANGE_START = 8765
PORT_RANGE_END = 8800
HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
def _workspace_root() -> Path:
    from core.paths import WORKSPACE_DIR
    Path(WORKSPACE_DIR).mkdir(parents=True, exist_ok=True)
    return Path(WORKSPACE_DIR).resolve()


def _resolve_path(path: str) -> Path:
    """Resolve ``path`` against the workspace; reject escapes."""
    if not path:
        raise ValueError("'path' is required")
    root = _workspace_root()
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    try:
        p.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"path '{path}' is outside the workspace ({root})") from exc
    if not p.exists():
        raise ValueError(f"path '{path}' does not exist")
    return p


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------
def _is_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _allocate_port(preferred: Optional[int] = None) -> int:
    if preferred is not None and _is_free(int(preferred)):
        return int(preferred)
    for p in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if _is_free(p):
            return p
    raise RuntimeError("no free port in preview range "
                        f"({PORT_RANGE_START}-{PORT_RANGE_END})")


# ---------------------------------------------------------------------------
# Handle identity is checked BEFORE content or directory names are read. A
# resolve-then-open check alone does not protect a replaceable junction.
def _handle_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    size = function(handle, buffer, len(buffer), 0)
    if not size or size >= len(buffer):
        raise OSError("Cannot verify preview handle identity")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _opened_file_path(file: Any) -> Path:
    if os.name == "nt":
        import msvcrt
        return _handle_path(msvcrt.get_osfhandle(file.fileno()))
    # WISE's other supported desktop/test platform is Linux. Do not silently
    # fall back to file.name when the OS cannot establish descriptor identity.
    return Path(f"/proc/self/fd/{file.fileno()}").resolve(strict=True)


def _require_inside(path: Path, root: Path) -> None:
    # The handle path is already resolved by the OS: resolving it again would
    # perform a new path lookup and lose the identity we just verified.
    if not path.is_absolute() or not path.is_relative_to(root):
        raise PermissionError("Preview path is outside its served root")


def _directory_entries(path: Path, root: Path) -> List[tuple[str, bool, bool]]:
    entries = []
    if os.name == "nt":
        import win32file
        import pywintypes
        handle = win32file.CreateFile(str(path), win32file.GENERIC_READ,
            win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE | win32file.FILE_SHARE_DELETE,
            None, win32file.OPEN_EXISTING, win32file.FILE_FLAG_BACKUP_SEMANTICS, None)
        try:
            _require_inside(_handle_path(int(handle)), root)
            while True:
                try:
                    batch = win32file.GetFileInformationByHandleEx(handle, win32file.FileIdBothDirectoryInfo)
                except pywintypes.error as exc:
                    if exc.winerror == 18:  # ERROR_NO_MORE_FILES
                        break
                    raise
                if not batch:
                    break
                for item in batch:
                    name = item["FileName"]
                    if name not in {".", ".."}:
                        attrs = item["FileAttributes"]
                        entries.append((name, bool(attrs & 16), bool(attrs & 1024)))
                if len(entries) > 10000:
                    raise PermissionError("Preview directory exceeds listing limit")
        finally:
            handle.Close()
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _require_inside(Path(f"/proc/self/fd/{descriptor}").resolve(strict=True), root)
            names = os.listdir(descriptor)
            if len(names) > 10000:
                raise PermissionError("Preview directory exceeds listing limit")
            for name in names:
                try:
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except OSError:
                    continue
                entries.append((name, stat.S_ISDIR(info.st_mode), stat.S_ISLNK(info.st_mode)))
        finally:
            os.close(descriptor)
    return entries


# Server primitive
# ---------------------------------------------------------------------------
class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    """Handler that silences default stderr logging and pokes a callback
    on every request so the manager can update its idle clock.
    """

    activity_callback: Any = None  # set per-instance by the manager
    preview_root: Optional[Path] = None

    def _request_authority_allowed(self) -> bool:
        """A loopback bind does not stop DNS rebinding from a browser page.

        Match the actual listener's port without resolving any supplied name.
        Same-site embedding by WISE and origin-less native clients still work;
        another browser origin cannot read workspace content through this host.
        """
        from core.security.http_origin import _origin

        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        sites = self.headers.get_all("Sec-Fetch-Site", [])
        if len(hosts) != 1 or len(origins) > 1 or len(sites) > 1:
            return False
        authority = _origin("http://" + hosts[0])
        if (authority is None or authority[1] not in {"127.0.0.1", "localhost"}
                or authority[2] != self.server.server_address[1]):
            return False
        if origins and _origin(origins[0]) != authority:
            return False
        return not sites or sites[0].lower() != "cross-site"

    def _checked_path(self) -> Path:
        root = self.preview_root
        if root is None:
            raise PermissionError("Preview root identity is missing")
        parts = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).replace("\\", "/").split("/")
        for part in parts:
            base = part.split(".", 1)[0].rstrip(" .").upper()
            if (":" in part or "\x00" in part or base in {"CON", "PRN", "AUX", "NUL"}
                    or base in {f"{kind}{n}" for kind in ("COM", "LPT") for n in range(1, 10)}):
                raise PermissionError("Unsafe preview path")
        path = Path(self.translate_path(self.path)).resolve(strict=True)
        _require_inside(path, root)
        return path

    def send_head(self) -> Any:
        stream = None
        try:
            if not self._request_authority_allowed():
                self.send_error(403, "Preview request authority denied")
                return None
            path = self._checked_path()
            if path.is_dir():
                url = urllib.parse.urlsplit(self.path)
                if not url.path.endswith(("/", "%2f", "%2F")):
                    self.send_response(301)
                    # Only emit a relative location, never a user-controlled authority.
                    self.send_header("Location", urllib.parse.urlunsplit(("", "", url.path + "/", url.query, "")))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                for name in ("index.html", "index.htm"):
                    index = path / name
                    if index.is_file():
                        path = index.resolve(strict=True)
                        _require_inside(path, self.preview_root)
                        break
                else:
                    return self.list_directory(str(path))
            # Non-regular files must not be served (devices, sockets, FIFOs).
            if not stat.S_ISREG(path.stat().st_mode):
                raise PermissionError("Not a regular preview file")
            stream = open(path, "rb")
            _require_inside(_opened_file_path(stream), self.preview_root)
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError("Not a regular preview file")
            if "If-Modified-Since" in self.headers and "If-None-Match" not in self.headers:
                try:
                    since = email.utils.parsedate_to_datetime(self.headers["If-Modified-Since"])
                    if since.tzinfo is None:
                        since = since.replace(tzinfo=datetime.timezone.utc)
                    modified = datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc).replace(microsecond=0)
                    if since.tzinfo is datetime.timezone.utc and modified <= since:
                        self.send_response(304); self.end_headers()
                        stream.close()
                        return None
                except (TypeError, ValueError, IndexError, OverflowError):
                    pass
            self.send_response(200)
            self.send_header("Content-type", self.guess_type(str(path)))
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Last-Modified", self.date_time_string(info.st_mtime))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return stream
        except FileNotFoundError:
            if stream is not None:
                stream.close()
            self.send_error(404, "File not found")
        except (OSError, RuntimeError, ValueError):
            if stream is not None:
                stream.close()
            self.send_error(403, "Preview access denied")
        return None

    def list_directory(self, path: str) -> Any:
        try:
            entries = _directory_entries(Path(path), self.preview_root)
        except (OSError, RuntimeError, ValueError, ImportError):
            self.send_error(403, "Preview directory access denied")
            return None
        title = html.escape(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
        lines = [f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body><ul>']
        for name, is_dir, is_link in sorted(entries, key=lambda item: item[0].casefold()):
            link = name + ("/" if is_dir else "")
            display = link + ("@" if is_link else "")
            lines.append(f'<li><a href="{urllib.parse.quote(link, safe="/")}">{html.escape(display)}</a></li>')
        lines.append("</ul></body></html>")
        data = "\n".join(lines).encode("utf-8", errors="replace")
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        return io.BytesIO(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        return  # silence

    def end_headers(self) -> None:
        try:
            cb = type(self).activity_callback
            if cb is not None:
                cb()
        except Exception:
            pass
        super().end_headers()


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Server bookkeeping
# ---------------------------------------------------------------------------
@dataclass
class PreviewServerInfo:
    """Public, JSON-safe view of a running preview server."""

    server_id: str
    url: str
    port: int
    directory: str
    started_at: float
    last_activity: float
    ttl_seconds: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server_id": self.server_id,
            "url": self.url,
            "port": self.port,
            "directory": self.directory,
            "started_at": self.started_at,
            "last_activity": self.last_activity,
            "ttl_seconds": self.ttl_seconds,
            "idle_seconds": max(0, int(time.time() - self.last_activity)),
        }


@dataclass
class _RunningServer:
    info: PreviewServerInfo
    httpd: _ThreadingTCPServer
    thread: threading.Thread
    handler_cls: type
    session_id: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class PreviewManager:
    """Singleton holder for running preview servers.

    Thread-safe; the public methods can be called from FastAPI handlers,
    the ReAct loop, or the reaper thread.
    """

    _instance: Optional["PreviewManager"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, *a: Any, **kw: Any) -> "PreviewManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialised = False
            return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialised", False):
            return
        self._initialised = True
        self._servers: Dict[str, _RunningServer] = {}
        self._lock = threading.RLock()
        self._reaper_started = False

    # -- Public API ---------------------------------------------------------
    def start(self, directory: str, *,
              port: Optional[int] = None,
              ttl_seconds: int = DEFAULT_TTL_SECONDS,
              session_id: Optional[str] = None) -> PreviewServerInfo:
        """Start a server rooted at ``directory``."""
        target = _resolve_path(directory)
        if target.is_file():
            target = target.parent
        with self._lock:
            if len(self._servers) >= MAX_CONCURRENT:
                raise RuntimeError(
                    f"preview server cap reached "
                    f"({MAX_CONCURRENT}). Stop one first.")

            chosen_port = _allocate_port(port)
            sid = uuid.uuid4().hex[:12]

            # Build a fresh handler class per-server so each one
            # serves its own directory and has its own activity hook.
            handler_cls = type(
                f"_PreviewHandler_{sid}", (_SilentHandler,),
                {"activity_callback": None, "preview_root": target},
            )

            httpd = _ThreadingTCPServer(
                (HOST, chosen_port),
                lambda *args, **kwargs: handler_cls(  # type: ignore[misc]
                    *args, directory=str(target), **kwargs),
            )

            now = time.time()
            info = PreviewServerInfo(
                server_id=sid,
                url=f"http://{HOST}:{chosen_port}",
                port=chosen_port,
                directory=str(target),
                started_at=now,
                last_activity=now,
                ttl_seconds=int(ttl_seconds),
            )
            running = _RunningServer(
                info=info, httpd=httpd, thread=None,  # type: ignore[arg-type]
                handler_cls=handler_cls,
                session_id=session_id)

            def _bump() -> None:
                running.info.last_activity = time.time()

            handler_cls.activity_callback = staticmethod(_bump)

            t = threading.Thread(
                target=httpd.serve_forever,
                name=f"preview-{sid}",
                daemon=True)
            running.thread = t
            t.start()

            self._servers[sid] = running
            self._ensure_reaper()
            return info

    def stop(self, server_id: str) -> bool:
        with self._lock:
            running = self._servers.pop(server_id, None)
        if running is None:
            return False
        try:
            running.httpd.shutdown()
        except Exception:
            pass
        try:
            running.httpd.server_close()
        except Exception:
            pass
        return True

    def stop_session(self, session_id: str) -> int:
        """Stop every server attached to ``session_id``. Returns count."""
        if not session_id:
            return 0
        with self._lock:
            ids = [sid for sid, r in self._servers.items()
                   if r.session_id == session_id]
        n = 0
        for sid in ids:
            if self.stop(sid):
                n += 1
        return n

    def stop_all(self) -> int:
        with self._lock:
            ids = list(self._servers.keys())
        return sum(1 for sid in ids if self.stop(sid))

    def list(self) -> List[PreviewServerInfo]:
        with self._lock:
            return [r.info for r in self._servers.values()]

    # -- Internals ----------------------------------------------------------
    def _ensure_reaper(self) -> None:
        if self._reaper_started:
            return
        self._reaper_started = True

        def _loop() -> None:
            while True:
                time.sleep(30)
                try:
                    self._reap_idle()
                except Exception:
                    pass

        t = threading.Thread(target=_loop, name="preview-reaper",
                              daemon=True)
        t.start()

    def _reap_idle(self) -> None:
        now = time.time()
        to_kill: List[str] = []
        with self._lock:
            for sid, r in self._servers.items():
                idle = now - r.info.last_activity
                if idle >= r.info.ttl_seconds:
                    to_kill.append(sid)
        for sid in to_kill:
            self.stop(sid)


# ---------------------------------------------------------------------------
# Module-level convenience API + agent tool
# ---------------------------------------------------------------------------
_manager: Optional[PreviewManager] = None


def get_manager() -> PreviewManager:
    global _manager
    if _manager is None:
        _manager = PreviewManager()
    return _manager


def start_preview(directory: str, *, port: Optional[int] = None,
                   ttl_seconds: int = DEFAULT_TTL_SECONDS,
                   session_id: Optional[str] = None) -> PreviewServerInfo:
    return get_manager().start(directory,
                                port=port,
                                ttl_seconds=ttl_seconds,
                                session_id=session_id)


def stop_preview(server_id: str) -> bool:
    return get_manager().stop(server_id)


def stop_session_previews(session_id: str) -> int:
    return get_manager().stop_session(session_id)


def stop_all_previews() -> int:
    return get_manager().stop_all()


def list_previews() -> List[Dict[str, Any]]:
    return [info.to_dict() for info in get_manager().list()]


# -- Agent-callable tool ----------------------------------------------------
def preview_serve(*, path: str, port: Optional[int] = None,
                   ttl_seconds: int = DEFAULT_TTL_SECONDS,
                   **_unused: Any) -> str:
    """Agent tool wrapper. Returns a JSON status string.

    The agent calls this as ``preview.serve`` from the ReAct loop.
    Output schema mirrors :mod:`core.admin_tools`:
    ``{"ok": bool, "summary": str, "url": "...", "server_id": "..."}``.
    """
    if not path:
        return json.dumps({"ok": False,
                            "summary": "'path' is required"})
    try:
        info = start_preview(path, port=port,
                              ttl_seconds=int(ttl_seconds))
    except Exception as exc:
        return json.dumps({"ok": False, "summary": str(exc)})
    return json.dumps({
        "ok": True,
        "summary": f"Preview running at {info.url} "
                    f"(server_id={info.server_id}). "
                    f"Stops automatically after "
                    f"{info.ttl_seconds}s of inactivity.",
        "url": info.url,
        "server_id": info.server_id,
        "port": info.port,
    })


def preview_stop(*, server_id: str, **_unused: Any) -> str:
    if not server_id:
        return json.dumps({"ok": False,
                            "summary": "'server_id' is required"})
    ok = stop_preview(server_id)
    return json.dumps({
        "ok": ok,
        "summary": (f"Preview {server_id} stopped." if ok
                     else f"No preview with id {server_id}."),
    })


def preview_list(**_unused: Any) -> str:
    items = list_previews()
    return json.dumps({
        "ok": True,
        "summary": f"{len(items)} preview server(s) running.",
        "previews": items,
    })


PREVIEW_TOOLS = {
    "preview.serve": preview_serve,
    "preview.stop": preview_stop,
    "preview.list": preview_list,
}


def register_preview_tools(tool_registry: Any) -> List[str]:
    out: List[str] = []
    for name, fn in PREVIEW_TOOLS.items():
        try:
            try:
                if tool_registry.get(name) is not None:
                    continue
            except Exception:
                pass
            tool_registry.register(name, fn)
            out.append(name)
        except Exception:
            continue
    return out


__all__ = [
    "PreviewServerInfo",
    "PreviewManager",
    "get_manager",
    "start_preview",
    "stop_preview",
    "stop_session_previews",
    "stop_all_previews",
    "list_previews",
    "preview_serve",
    "preview_stop",
    "preview_list",
    "PREVIEW_TOOLS",
    "register_preview_tools",
    "DEFAULT_TTL_SECONDS",
    "MAX_CONCURRENT",
]
