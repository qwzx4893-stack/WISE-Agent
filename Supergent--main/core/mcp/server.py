"""Expose Agent OS as an MCP server.

Two transports:

* **stdio** — the canonical MCP transport. Run with::

      python -m core.mcp.server --transport stdio

  Each line on stdin is a JSON-RPC 2.0 request; responses are written
  as newline-delimited JSON on stdout. This is what Claude Desktop /
  Codex CLI / other MCP clients expect.

* **HTTP / SSE** — wired into the existing FastAPI server. Two
  endpoints are mounted by ``api/server.py`` (see ``register_routes``
  below):

  - ``POST /mcp/jsonrpc`` — fire one JSON-RPC frame, get the result
    back. Used by clients that don't need streaming.
  - ``GET  /mcp/sse``     — Server-Sent Events stream. The client opens
    one long-lived GET; subsequent ``POST /mcp/sse`` messages on the
    same ``session_id`` are dispatched and the result is pushed down
    the stream as ``data:`` events. Matches the WG SSE binding.

Both transports share the same :class:`MCPServer` core. The server
filters every exposed tool against:

1. The allowlisted toolset (default: every tool registered in the
   process) minus the deny-list (``MCP_SERVER_DENY``, defaults below).
2. The kernel's ``DANGEROUS_PATTERNS`` regex applied to the tool's
   ``run_shell``/``execute_command``-style command argument when present.
3. The MCP confirmation gate: write/dangerous tools demand an explicit
   ``confirm: true`` field in the call args.

Bridge note: ``api/server.py`` calls :func:`register_routes(app)` once
at import time so the HTTP transport is always available alongside the
existing REST surface.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from core.observability import Tracer

from .policy import ToolClassifier, get_gate

logger = logging.getLogger("agent_os.mcp.server")


# Built-in deny: tools that are too risky to expose to remote clients
# even when classified as `write`. Operators can override via
# MCP_SERVER_DENY env (comma list) or by passing ``deny=...`` to
# :class:`MCPServer`.
DEFAULT_DENY = {
    "execute_command", "run_shell", "shell", "exec",
    "self_modify_code", "rollback_self", "apt_install",
}


# --------------------------------------------------------------------------
# Core server
# --------------------------------------------------------------------------
@dataclass
class MCPServer:
    """JSON-RPC 2.0 dispatcher exposing :class:`agent_core.ToolRegistry`."""

    tool_registry: Any = None
    server_name: str = "agent-os"
    server_version: str = "1.0"
    allow: Optional[Iterable[str]] = None
    deny: Optional[Iterable[str]] = None
    require_confirm: bool = True
    classifier: ToolClassifier = field(default_factory=ToolClassifier)

    def __post_init__(self) -> None:
        # Resolve the registry at boot from agent_core if the caller
        # didn't pass one. This keeps the stdio entrypoint usable
        # without first wiring up FastAPI.
        if self.tool_registry is None:
            from agent_core import tool_registry as _reg  # noqa: WPS433
            self.tool_registry = _reg
        self._allow = set(self.allow) if self.allow is not None else None
        self._deny = set(self.deny) if self.deny is not None else set(DEFAULT_DENY)
        self._tool_meta_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------
    def _is_exposed(self, name: str) -> bool:
        if not name or name.startswith("_"):
            return False
        if name in self._deny:
            return False
        if self._allow is not None and name not in self._allow:
            return False
        return True

    def _build_tool_descriptor(self, name: str, fn: Callable[..., Any]
                                ) -> Dict[str, Any]:
        if name in self._tool_meta_cache:
            return self._tool_meta_cache[name]
        sig: Optional[inspect.Signature]
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None
        properties: Dict[str, Any] = {}
        required: List[str] = []
        if sig is not None:
            for param in sig.parameters.values():
                if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                                  inspect.Parameter.VAR_KEYWORD):
                    continue
                schema_type = "string"
                ann = param.annotation
                if ann in (int,):
                    schema_type = "integer"
                elif ann in (float,):
                    schema_type = "number"
                elif ann in (bool,):
                    schema_type = "boolean"
                elif ann in (list, List):
                    schema_type = "array"
                elif ann in (dict, Dict):
                    schema_type = "object"
                properties[param.name] = {"type": schema_type}
                if param.default is inspect.Parameter.empty:
                    required.append(param.name)
        descriptor = {
            "name": name,
            "description": (inspect.getdoc(fn) or f"Agent OS tool '{name}'.")[:600],
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }
        cls = self.classifier.classify(descriptor)
        descriptor["x-agent-os"] = {"kind": cls.kind, "reason": cls.reason}
        self._tool_meta_cache[name] = descriptor
        return descriptor

    def list_tools(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        names = list(self.tool_registry.tools.keys()) \
            if hasattr(self.tool_registry, "tools") else \
            list(self.tool_registry.list_tools())
        for name in names:
            if not self._is_exposed(name):
                continue
            fn = self.tool_registry.get(name) if hasattr(self.tool_registry, "get") \
                else self.tool_registry.tools[name]
            out.append(self._build_tool_descriptor(name, fn))
        return out

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------
    def _confirm_required(self, descriptor: Dict[str, Any],
                           arguments: Dict[str, Any]) -> bool:
        if not self.require_confirm:
            return False
        kind = ((descriptor.get("x-agent-os") or {}).get("kind") or "read")
        if kind == "read":
            return False
        if arguments.get("confirm") is True:
            return False
        return True

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
        arguments = dict(arguments or {})
        if not self._is_exposed(name):
            raise _MCPError(-32601,
                            f"tool '{name}' not exposed by this server")
        fn = self.tool_registry.get(name) if hasattr(self.tool_registry, "get") \
            else self.tool_registry.tools.get(name)
        if fn is None:
            raise _MCPError(-32601, f"tool '{name}' not registered")
        descriptor = self._build_tool_descriptor(name, fn)
        kind = ((descriptor.get("x-agent-os") or {}).get("kind") or "read")

        # 1. DANGEROUS_PATTERNS sweep — applies regardless of kind so
        # an operator can't get around it via a confirm flag. Mirrors
        # the policy enforced inside the kernel's run_shell.
        try:
            from agent_core import DANGEROUS_PATTERNS as _DP  # noqa: WPS433
        except Exception:  # noqa: BLE001
            _DP = []
        for v in arguments.values():
            if not isinstance(v, str):
                continue
            for pat in _DP:
                if re.search(pat, v):
                    raise _MCPError(-32001,
                                    f"argument matches dangerous pattern '{pat}'")

        # 2. Confirmation gate.
        if self._confirm_required(descriptor, arguments):
            # Register a pending call so the UI / REST flow can resolve.
            try:
                get_gate().request(server=self.server_name, tool=name,
                                    arguments=arguments, kind=kind)
            except Exception as exc:
                # `request` always raises ConfirmationRequired; surface
                # its `call_id` to the MCP client so it can resolve via
                # /admin/mcp/confirm/{call_id}.
                call_id = getattr(exc, "call_id", "")
                raise _MCPError(
                    -32002,
                    f"confirmation required for {kind} tool '{name}'",
                    data={"call_id": call_id, "kind": kind,
                          "server": self.server_name},
                )

        arguments.pop("confirm", None)
        with Tracer.span("mcp.serve", server=self.server_name,
                          tool=name, tool_kind=kind):
            try:
                result = fn(**arguments)
            except TypeError as exc:
                raise _MCPError(-32602, f"invalid arguments: {exc}") from exc
            except Exception as exc:
                raise _MCPError(-32000, f"tool failure: {exc}") from exc
        return _wrap_tool_result(result)

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------
    def dispatch(self, frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = frame.get("method", "")
        rid = frame.get("id")
        params = frame.get("params") or {}
        if method == "initialize":
            return _ok(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.server_name,
                               "version": self.server_version},
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _ok(rid, {})
        if method == "tools/list":
            return _ok(rid, {"tools": self.list_tools()})
        if method == "tools/call":
            try:
                result = self.call_tool(params.get("name", ""),
                                         params.get("arguments") or {})
                return _ok(rid, result)
            except _MCPError as exc:
                return _err(rid, exc.code, exc.message, exc.data)
            except Exception as exc:  # noqa: BLE001
                return _err(rid, -32000, f"internal error: {exc}")
        if rid is None:
            return None  # Unknown notification
        return _err(rid, -32601, f"unknown method '{method}'")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class _MCPError(Exception):
    def __init__(self, code: int, message: str,
                 data: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _ok(rid: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str,
         data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _wrap_tool_result(result: Any) -> Dict[str, Any]:
    """Conform tool output to ``content[].type:'text'`` MCP shape."""
    if isinstance(result, dict) and "content" in result:
        return result
    if isinstance(result, str):
        return {"content": [{"type": "text", "text": result}]}
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(result)
    return {"content": [{"type": "text", "text": text}]}


# --------------------------------------------------------------------------
# stdio entrypoint
# --------------------------------------------------------------------------
def run_stdio(server: Optional[MCPServer] = None) -> None:
    server = server or MCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except Exception:
            continue
        resp = server.dispatch(frame)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------
# HTTP / SSE transport
# --------------------------------------------------------------------------
_singleton: Optional[MCPServer] = None
_sse_sessions: Dict[str, Dict[str, Any]] = {}
_sse_lock = threading.Lock()


def get_server() -> MCPServer:
    global _singleton
    if _singleton is None:
        _singleton = MCPServer()
    return _singleton


def register_routes(app: Any) -> None:
    """Attach ``/mcp/jsonrpc`` and ``/mcp/sse`` to a FastAPI app."""
    try:
        from fastapi import Query
        from fastapi.requests import Request as _FastAPIRequest
        from fastapi.responses import JSONResponse, StreamingResponse
    except Exception:  # noqa: BLE001
        return

    @app.post("/mcp/jsonrpc")
    async def mcp_jsonrpc(payload: Dict[str, Any]) -> Any:
        srv = get_server()
        resp = srv.dispatch(payload)
        return resp if resp is not None else {"jsonrpc": "2.0",
                                              "id": payload.get("id"),
                                              "result": {}}

    @app.get("/mcp/sse")
    async def mcp_sse(session_id: Optional[str] = Query(default=None)):
        """Open a long-lived SSE channel.

        Clients dispatch frames by POSTing to ``/mcp/sse?session_id=…``;
        the handler enqueues a response which this generator drains.
        """
        sid = session_id or uuid.uuid4().hex
        queue: List[Dict[str, Any]] = []
        cond = threading.Condition()
        with _sse_lock:
            _sse_sessions[sid] = {
                "queue": queue, "cond": cond, "closed": False,
            }

        async def event_stream():
            yield f"event: ready\ndata: {json.dumps({'session_id': sid})}\n\n"
            srv = get_server()
            yield "event: tools\ndata: " + json.dumps(srv.list_tools()) + "\n\n"
            while True:
                with cond:
                    if not queue and not _sse_sessions[sid]["closed"]:
                        cond.wait(timeout=15.0)
                    if _sse_sessions[sid]["closed"]:
                        break
                    if not queue:
                        # heartbeat
                        yield ": ping\n\n"
                        continue
                    frame = queue.pop(0)
                yield "event: message\ndata: " + json.dumps(frame) + "\n\n"
            with _sse_lock:
                _sse_sessions.pop(sid, None)

        return StreamingResponse(event_stream(),
                                  media_type="text/event-stream")

    @app.post("/mcp/sse")
    async def mcp_sse_dispatch(payload: Dict[str, Any],
                                session_id: str = Query(...)) -> Any:
        with _sse_lock:
            session = _sse_sessions.get(session_id)
        if session is None:
            return JSONResponse(
                {"error": "no such session_id"}, status_code=404,
            )
        srv = get_server()
        resp = srv.dispatch(payload)
        if resp is not None:
            with session["cond"]:
                session["queue"].append(resp)
                session["cond"].notify()
        return {"queued": resp is not None, "session_id": session_id}

    @app.delete("/mcp/sse")
    async def mcp_sse_close(session_id: str = Query(...)) -> Any:
        with _sse_lock:
            session = _sse_sessions.get(session_id)
            if session:
                session["closed"] = True
                with session["cond"]:
                    session["cond"].notify_all()
        return {"closed": True, "session_id": session_id}


# --------------------------------------------------------------------------
# Module entrypoint
# --------------------------------------------------------------------------
def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="core.mcp.server")
    parser.add_argument("--transport", choices=("stdio",), default="stdio")
    parser.add_argument("--allow", default="",
                         help="comma-separated tool allowlist")
    parser.add_argument("--deny", default="",
                         help="comma-separated tool denylist (added to defaults)")
    args = parser.parse_args(argv)

    allow = [a for a in args.allow.split(",") if a.strip()] or None
    deny = set(DEFAULT_DENY)
    deny.update(d for d in args.deny.split(",") if d.strip())

    server = MCPServer(allow=allow, deny=deny)
    if args.transport == "stdio":
        run_stdio(server)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "DEFAULT_DENY",
    "MCPServer",
    "get_server",
    "register_routes",
    "run_stdio",
]
