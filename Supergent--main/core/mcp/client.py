"""MCPClient — JSON-RPC 2.0 over stdio or HTTP.

Two transports are supported:

* **stdio**: ``transport="stdio"`` — we spawn the configured
  ``command`` + ``args`` and exchange JSON-RPC frames on stdin/stdout
  (newline-delimited JSON, the form used by the official MCP SDK
  examples). Each request is given a unique numeric ``id`` and waited
  on with a timeout. The subprocess is daemonised but reaped on
  ``close``.
* **http**: ``transport="http"`` — POST each JSON-RPC frame to a URL
  and read the JSON response. This works against
  Streamable-HTTP-style MCP servers; full SSE streaming isn't
  implemented (we only need the synchronous sub-set).

The client is **thread-safe**: every request acquires a lock so a
multi-threaded ``ThinkingEngine`` won't interleave frames.

Initialisation flow:

1. Open transport.
2. Send ``initialize`` with ``clientInfo`` and ``capabilities.tools``.
3. Send ``notifications/initialized``.
4. Send ``tools/list`` to populate ``self.tools``.

Failures during init raise :class:`MCPError` and the client is left
in ``self.alive == False`` so the registry can skip it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import httpx as _httpx  # type: ignore
except Exception:  # noqa: BLE001
    _httpx = None  # type: ignore

from core.observability import Tracer

from .policy import AllowlistError, validate_http_url
from .sandbox import make_preexec, wrap_command


CLIENT_NAME = "agent-os"
CLIENT_VERSION = "1.0"
PROTOCOL_VERSION = "2024-11-05"


class MCPError(Exception):
    """Raised on any MCP transport / protocol failure."""

    def __init__(self, message: str, *, code: Optional[int] = None,
                 data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _server_error(error: Any) -> MCPError:
    if not isinstance(error, dict):
        return MCPError("invalid server error")
    return MCPError(f"server error: {error.get('message', 'unknown error')}",
                    code=error.get("code"), data=error.get("data"))


class MCPClient:
    """A single connection to an MCP server."""

    def __init__(self, *, name: str, transport: str,
                 command: str = "", args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None,
                 url: str = "",
                 headers: Optional[Dict[str, str]] = None,
                 init_timeout: float = 10.0,
                 call_timeout: float = 30.0,
                 sandbox: bool = True,
                 allowed_domains: Optional[List[str]] = None,
                 cwd: str = "") -> None:
        self.name = name
        self.transport = transport
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.url = url
        self.headers = dict(headers or {})
        self.init_timeout = init_timeout
        self.call_timeout = call_timeout
        self.sandbox = bool(sandbox)
        self.allowed_domains = list(allowed_domains or [])
        self.cwd = cwd

        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._alive = False
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.tools: List[Dict[str, Any]] = []
        self.last_error: str = ""
        self._http_session_id: Optional[str] = None
        self._protocol_version = PROTOCOL_VERSION

    # ------------------------------------------------------------------
    @property
    def alive(self) -> bool:
        if self.transport == "stdio" and (not self._proc or self._proc.poll() is not None):
            return False
        return self._alive

    # ------------------------------------------------------------------
    def open(self) -> None:
        with Tracer.span(f"mcp.{self.name}.open", transport=self.transport):
            try:
                if self.transport == "stdio":
                    self._open_stdio()
                elif self.transport == "http":
                    self._open_http()
                else:
                    raise MCPError(f"unknown transport: {self.transport}")
                self._initialize()
                self._fetch_tools()
                self._alive = True
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)[:300]
                self.close()
                raise MCPError(self.last_error) from exc

    # ------------------------------------------------------------------
    def close(self) -> None:
        self._alive = False
        self._http_session_id = None
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                import psutil
                # Only descendants of this client's owned process are stopped.
                children = psutil.Process(proc.pid).children(recursive=True)
                for child in reversed(children):
                    try: child.terminate()
                    except psutil.NoSuchProcess: pass
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=2.0)
                _, remaining = psutil.wait_procs(children, timeout=1)
                for child in remaining:
                    try: child.kill(); child.wait(timeout=1)
                    except psutil.NoSuchProcess: pass
            except Exception:
                try: proc.terminate(); proc.wait(timeout=2)
                except Exception:
                    try: proc.kill(); proc.wait(timeout=2)
                    except Exception: pass
        self._proc = None
        with self._pending_lock:
            for ev in self._pending.values():
                ev["error"] = "client closed"
                ev["done"].set()
            self._pending.clear()

    # ------------------------------------------------------------------
    # stdio transport
    # ------------------------------------------------------------------
    def _open_stdio(self) -> None:
        if not self.command:
            raise MCPError("stdio transport: 'command' is required")
        # Only explicitly configured secrets are forwarded to external servers.
        system_keys = {
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
            "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
            "PROGRAMFILES(X86)", "PYTHONUTF8", "PYTHONIOENCODING", "LANG", "LC_ALL",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in system_keys}
        if self.name == "agent-os" and os.environ.get("WISE_RUNTIME_ROOT"):
            env["WISE_RUNTIME_ROOT"] = os.environ["WISE_RUNTIME_ROOT"]
        env.update(self.env)
        # JSON-RPC pipes are UTF-8 even on Windows Arabic/English code pages.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        import shutil
        import sys
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        curr_pypath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{curr_pypath}" if curr_pypath else repo_root

        resolved_cmd = self.command
        if self.command in ("python", "python3"):
            resolved_cmd = sys.executable
        else:
            resolved = shutil.which(self.command)
            if resolved:
                resolved_cmd = resolved

        launch_args = self.args
        if os.name == "nt" and self.command in {"npx", "npm"}:
            # Batch wrappers introduce cmd.exe parsing even with shell=False.
            # Invoke npm's JS launcher directly so user paths stay array args.
            node = shutil.which("node")
            script = Path(resolved_cmd).parent / "node_modules/npm/bin" / ("npx-cli.js" if self.command == "npx" else "npm-cli.js")
            if not node or not script.is_file(): raise MCPError("Install Node.js with npm; its direct JS launcher is unavailable")
            resolved_cmd, launch_args = node, [str(script), *self.args]
        elif os.name == "nt" and Path(resolved_cmd).suffix.lower() in {".bat", ".cmd"}:
            raise MCPError("Batch MCP launchers are unsupported; configure the executable/interpreter directly")
        cmdline = wrap_command(resolved_cmd, launch_args) if self.sandbox \
            else [resolved_cmd, *launch_args]
        preexec = make_preexec() if (self.sandbox and os.name != "nt") else None
        try:
            self._proc = subprocess.Popen(
                cmdline,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd or repo_root,
                env=env,
                bufsize=0,
                preexec_fn=preexec,
                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}),
            )
        except FileNotFoundError as exc:
            raise MCPError(f"command not found: {cmdline[0]}") from exc
        # Reader thread.
        self._reader_thread = threading.Thread(
            target=self._stdio_reader, daemon=True,
            name=f"mcp-reader-{self.name}",
        )
        self._reader_thread.start()
        # Installers/servers may fill stderr; drain it without storing secrets.
        def drain_errors():
            proc = self._proc
            if not proc or not proc.stderr: return
            try:
                while proc.stderr.read(4096): pass
            except (OSError, ValueError): pass
        threading.Thread(target=drain_errors,daemon=True,name=f"mcp-stderr-{self.name}").start()

    def _stdio_reader(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for raw in proc.stdout:  # newline-delimited JSON
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except Exception:
                    continue
                self._dispatch_frame(frame)
        except Exception:
            return

    # ------------------------------------------------------------------
    # http transport
    # ------------------------------------------------------------------
    def _open_http(self) -> None:
        if not self.url:
            raise MCPError("http transport: 'url' is required")
        if _httpx is None:
            raise MCPError("http transport: httpx not installed")
        try:
            validate_http_url(self.url, self.allowed_domains)
        except AllowlistError as exc:
            raise MCPError(f"network policy: {exc}") from exc

    def _http_post(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        assert _httpx is not None
        if frame.get("method") == "tools/call":
            from .policy import validate_remote_arguments, SensitiveRemoteArgumentsError
            try:
                validate_remote_arguments((frame.get("params") or {}).get("arguments", {}))
            except SensitiveRemoteArgumentsError as exc:
                raise MCPError(str(exc)) from exc
        try:
            from core.public_http import PublicTransport
            from urllib.parse import urlsplit
            # Local HTTP servers require explicit existing host allowlisting;
            # internet servers additionally pin checked public DNS to sockets.
            public = urlsplit(self.url).hostname not in {"localhost", "127.0.0.1", "::1"}
            with _httpx.Client(timeout=self.call_timeout, trust_env=False, follow_redirects=False,
                               transport=PublicTransport() if public else None) as cli:
                request_headers = {"Content-Type": "application/json",
                                   "Accept": "application/json, text/event-stream",
                                   "MCP-Protocol-Version": self._protocol_version, **self.headers}
                if self._http_session_id:
                    request_headers["Mcp-Session-Id"] = self._http_session_id
                with cli.stream("POST", self.url, headers=request_headers, json=frame) as resp:
                    if not 200 <= resp.status_code < 300:
                        raise MCPError(f"http {resp.status_code}")  # Do not echo credentials from error bodies.
                    if resp.headers.get("mcp-session-id"):
                        self._http_session_id = resp.headers["mcp-session-id"]
                    if "id" not in frame: return {}
                    if "text/event-stream" in resp.headers.get("content-type", ""):
                        buffer, budget, started = b"", 0, time.monotonic()
                        for chunk in resp.iter_bytes():
                            budget += len(chunk)
                            if budget > 4_000_000: raise MCPError("MCP response exceeds size limit")
                            if time.monotonic()-started > self.call_timeout: raise MCPError("MCP stream response timed out")
                            buffer = (buffer+chunk).replace(b"\r\n",b"\n")
                            while b"\n\n" in buffer:
                                event,buffer = buffer.split(b"\n\n",1)
                                data = b"\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:"))
                                if not data: continue
                                decoded = json.loads(data)
                                if decoded.get("id") == frame["id"]: return decoded
                        raise MCPError("SSE response contained no matching JSON-RPC result")
                    payload, started = bytearray(), time.monotonic()
                    for chunk in resp.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 4_000_000: raise MCPError("MCP response exceeds size limit")
                        if time.monotonic()-started > self.call_timeout: raise MCPError("MCP response timed out")
                    return json.loads(payload)
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"http request failed: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------
    # JSON-RPC core
    # ------------------------------------------------------------------
    def _next_request_id(self) -> int:
        with self._send_lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def _dispatch_frame(self, frame: Dict[str, Any]) -> None:
        rid = frame.get("id")
        if rid is None:
            # Notification — ignore.
            return
        with self._pending_lock:
            ev = self._pending.pop(int(rid), None)
        if not ev:
            return
        if "error" in frame:
            ev["error"] = frame["error"]
        else:
            ev["result"] = frame.get("result")
        ev["done"].set()

    def _send_stdio(self, frame: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        proc = self._proc
        if not proc or not proc.stdin:
            raise MCPError("stdio transport closed")
        rid = int(frame["id"])
        ev = {"done": threading.Event(), "result": None, "error": None}
        with self._pending_lock:
            self._pending[rid] = ev
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        with self._send_lock:
            try:
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise MCPError(f"stdio write failed: {exc}") from exc
        if not ev["done"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise MCPError(f"timed out after {timeout}s waiting for response")
        if ev["error"]:
            raise _server_error(ev["error"])
        return ev["result"] or {}

    def _send(self, method: str, params: Optional[Dict[str, Any]] = None,
              *, timeout: Optional[float] = None,
              notify: bool = False) -> Dict[str, Any]:
        timeout = timeout or self.call_timeout
        if notify:
            frame = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        else:
            frame = {
                "jsonrpc": "2.0", "id": self._next_request_id(),
                "method": method, "params": params or {},
            }
        if self.transport == "stdio":
            if notify:
                proc = self._proc
                if not proc or not proc.stdin:
                    raise MCPError("stdio transport closed")
                line = json.dumps(frame, ensure_ascii=False) + "\n"
                with self._send_lock:
                    proc.stdin.write(line.encode("utf-8"))
                    proc.stdin.flush()
                return {}
            return self._send_stdio(frame, timeout)
        if self.transport == "http":
            if notify:
                self._http_post(frame)
                return {}
            resp = self._http_post(frame)
            if "error" in resp:
                raise _server_error(resp["error"])
            return resp.get("result") or {}
        raise MCPError(f"unknown transport: {self.transport}")

    # ------------------------------------------------------------------
    # Protocol handshake
    # ------------------------------------------------------------------
    def _initialize(self) -> None:
        result = self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        }, timeout=self.init_timeout)
        negotiated = result.get("protocolVersion", PROTOCOL_VERSION)
        if negotiated not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"}:
            raise MCPError("Unsupported MCP protocol version")
        self._protocol_version = negotiated
        try:
            self._send("notifications/initialized", {}, notify=True)
        except Exception:
            pass

    def _fetch_tools(self) -> None:
        self.tools = []
        cursor, seen = None, set()
        for _ in range(20):
            result = self._send("tools/list", {"cursor":cursor} if cursor else {}, timeout=self.init_timeout)
            tools = result.get("tools") or []
            if not isinstance(tools, list) or any(not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in tools):
                raise MCPError("Invalid tools/list response")
            self.tools.extend(tools)
            if len(self.tools)>2000: raise MCPError("MCP tool directory exceeds budget")
            cursor = result.get("nextCursor")
            if not cursor: return
            if not isinstance(cursor, str) or cursor in seen: raise MCPError("Invalid MCP pagination")
            seen.add(cursor)
        raise MCPError("MCP tool pagination exceeds budget")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def call_tool(self, tool: str, arguments: Optional[Dict[str, Any]] = None,
                  *, timeout: Optional[float] = None,
                  kind: str = "", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call explicitly; optional protocol metadata never auto-approves/retries."""
        if not self._alive:
            raise MCPError(f"client {self.name} not alive")
        if metadata is not None and not isinstance(metadata, dict):
            raise MCPError("tool metadata must be an object")
        params = {"name": tool, "arguments": arguments or {}}
        if metadata is not None:
            params["_meta"] = dict(metadata)
        # The "mcp.call" event mirrors the bridge's classification for
        # easier filtering in the trace UI.
        with Tracer.span("mcp.call", server=self.name, tool=tool, tool_kind=kind):
            return self._send(
                "tools/call",
                params,
                timeout=timeout,
            )

    def ping(self) -> bool:
        try:
            self._send("ping", {}, timeout=2.0)
            return True
        except Exception:
            return False


__all__ = ["MCPClient", "MCPError"]
