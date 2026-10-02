"""Adapter layer — native Python integrations for the major platforms.

Each adapter:

- Inherits from :class:`BaseAdapter` (auth resolution + retry + rate
  limit + tracing).
- Reads its credentials from (a) the universal :class:`KeyStore`,
  (b) ``$VAR`` environment fallbacks, or (c) a CLI subprocess fallback
  if no API credentials are available. The adapter NEVER prompts.
- Exposes a small set of high-signal methods (e.g.
  ``GitHubAdapter.list_issues``, ``S3Adapter.upload``).
- Registers a flat dict of tool callables via :func:`tools_for`.

Public entry points:

- :func:`build_adapter_tools(registry, awareness)` — wires every
  available adapter into a :class:`ToolRegistry`. Adapters whose
  credentials are missing are silently skipped (caller can opt in via
  the ``/admin/adapters`` endpoint to see why).
- :func:`adapter_status()` — what's enabled, what's missing.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import AdapterError, AdapterStatus, BaseAdapter

# Individual adapters
from .github import GitHubAdapter
from .aws import AWSAdapter
from .slack import SlackAdapter
from .email_adapter import EmailAdapter
from .browser import BrowserAdapter
from .gitlab import GitLabAdapter
from .discord import DiscordAdapter

_ALL_ADAPTERS = [
    GitHubAdapter,
    AWSAdapter,
    SlackAdapter,
    EmailAdapter,
    BrowserAdapter,
    GitLabAdapter,
    DiscordAdapter,
]


def adapter_status() -> List[Dict[str, Any]]:
    """One row per adapter: name, ready, why-not, capabilities."""
    out: List[Dict[str, Any]] = []
    for cls in _ALL_ADAPTERS:
        try:
            inst = cls()
            status = inst.status()
        except Exception as exc:  # noqa: BLE001
            status = AdapterStatus(
                name=cls.NAME, ready=False, reason=f"init error: {exc}",
                capabilities=[],
            )
        out.append({
            "name": status.name,
            "ready": status.ready,
            "reason": status.reason,
            "capabilities": list(status.capabilities),
        })
    return out


def build_adapter_tools() -> Dict[str, Any]:
    """Return ``{tool_name: callable}`` for every ready adapter."""
    tools: Dict[str, Any] = {}
    for cls in _ALL_ADAPTERS:
        try:
            inst = cls()
            if not inst.is_ready():
                continue
            tools.update(inst.tools_for())
        except Exception:
            continue
    return tools


__all__ = [
    "AdapterError",
    "AdapterStatus",
    "BaseAdapter",
    "GitHubAdapter",
    "AWSAdapter",
    "SlackAdapter",
    "EmailAdapter",
    "BrowserAdapter",
    "GitLabAdapter",
    "DiscordAdapter",
    "adapter_status",
    "build_adapter_tools",
]
