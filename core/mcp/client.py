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
                 allowed_domains: Optional[List[str]] = None) -> None:
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

    # ------------------------------------------------------------------
    @property
    def alive(self) -> bool:
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
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    proc.kill()
            except Exception:
                pass
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

        cmdline = wrap_command(resolved_cmd, self.args) if self.sandbox \
            else [resolved_cmd, *self.args]
        preexec = make_preexec() if (self.sandbox and os.name != "nt") else None
        try:
            self._proc = subprocess.Popen(
                cmdline,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=repo_root,
                env=env,
                bufsize=0,
                preexec_fn=preexec,
            )
        except FileNotFoundError as exc:
            raise MCPError(f"command not found: {cmdline[0]}") from exc
        # Reader thread.
        self._reader_thread = threading.Thread(
            target=self._stdio_reader, daemon=True,
            name=f"mcp-reader-{self.name}",
        )
        self._reader_thread.start()

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
        try:
            with _httpx.Client(timeout=self.call_timeout) as cli:
                request_headers = {"Content-Type": "application/json",
                                   "Accept": "application/json, text/event-stream",
                                   "MCP-Protocol-Version": PROTOCOL_VERSION, **self.headers}
                if self._http_session_id:
                    request_headers["Mcp-Session-Id"] = self._http_session_id
                resp = cli.post(self.url,
                                headers=request_headers,
                                json=frame)
            if resp.status_code >= 400:
                raise MCPError(f"http {resp.status_code}: {resp.text[:200]}")
            if resp.headers.get("mcp-session-id"):
                self._http_session_id = resp.headers["mcp-session-id"]
            if not resp.content and "id" not in frame:
                return {}
            if "text/event-stream" in resp.headers.get("content-type", ""):
                for event in resp.text.replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                    if not data:
                        continue
                    decoded = json.loads(data)
                    if decoded.get("id") == frame.get("id"):
                        return decoded
                raise MCPError("SSE response contained no matching JSON-RPC result")
            return resp.json()
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"http request failed: {exc}") from exc

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
            raise MCPError(f"server error: {ev['error']}")
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
                raise MCPError(f"server error: {resp['error']}")
            return resp.get("result") or {}
        raise MCPError(f"unknown transport: {self.transport}")

    # ------------------------------------------------------------------
    # Protocol handshake
    # ------------------------------------------------------------------
    def _initialize(self) -> None:
        self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        }, timeout=self.init_timeout)
        try:
            self._send("notifications/initialized", {}, notify=True)
        except Exception:
            pass

    def _fetch_tools(self) -> None:
        result = self._send("tools/list", {}, timeout=self.init_timeout)
        self.tools = list(result.get("tools") or [])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def call_tool(self, tool: str, arguments: Optional[Dict[str, Any]] = None,
                  *, timeout: Optional[float] = None,
                  kind: str = "") -> Dict[str, Any]:
        if not self._alive:
            raise MCPError(f"client {self.name} not alive")
        if self.name == "context7" and self.transport == "http":
            from core.security.transient_vault import redact_sensitive_payload
            arguments = redact_sensitive_payload(arguments or {})
        # The "mcp.call" event mirrors the bridge's classification for
        # easier filtering in the trace UI.
        with Tracer.span("mcp.call", server=self.name, tool=tool, tool_kind=kind):
            return self._send(
                "tools/call",
                {"name": tool, "arguments": arguments or {}},
                timeout=timeout,
            )

    def ping(self) -> bool:
        try:
            self._send("ping", {}, timeout=2.0)
            return True
        except Exception:
            return False


__all__ = ["MCPClient", "MCPError"]
