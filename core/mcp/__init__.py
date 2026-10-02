"""Model Context Protocol (MCP) support.

This module lets Agent OS connect to external MCP servers (stdio
subprocess **or** WebSocket / Streamable-HTTP) and re-expose their
``tools/list`` as native Agent OS tools.

Public surface:

- :class:`MCPClient`              one running connection.
- :class:`MCPRegistry`            manages the persistent server roster
  under ``$MEMORY_DIR/mcp_servers.json``.
- :func:`build_mcp_tools`         returns ``{tool_name: callable}`` for
  every connected server. Each callable is a thin proxy that calls
  ``tools/call`` on the right :class:`MCPClient`.
- :class:`MCPError`               normalised exception.

The implementation speaks JSON-RPC 2.0 (the wire protocol of MCP) and
stays *deliberately minimal* — we don't implement the full protocol
spec, only the methods Agent OS actually consumes (``initialize``,
``tools/list``, ``tools/call``, ``ping``).

Servers can be configured by JSON files dropped under
``$MEMORY_DIR/mcp_servers.json`` (an array of server descriptors), or
created at runtime via the ``/admin/mcp/servers`` API.
"""

from __future__ import annotations

from .client import MCPClient, MCPError
from .policy import (
    AllowlistError,
    ConfirmationGate,
    ConfirmationRequired,
    ToolClassification,
    ToolClassifier,
    get_gate,
    validate_http_url,
)
from .registry import MCPRegistry, MCPServerConfig, build_mcp_tools

# Process-wide singleton — every consumer (boot, /admin/mcp endpoints,
# hot-reload axis) shares the same registry so server lifecycles stay
# consistent.
def get_registry() -> MCPRegistry:
    from .registry import get_mcp_registry
    return get_mcp_registry()


__all__ = [
    "AllowlistError",
    "ConfirmationGate",
    "ConfirmationRequired",
    "MCPClient",
    "MCPError",
    "MCPRegistry",
    "MCPServerConfig",
    "ToolClassification",
    "ToolClassifier",
    "build_mcp_tools",
    "get_gate",
    "get_registry",
    "validate_http_url",
]
