"""Admin tools — agent-callable wrappers around configuration endpoints.

The reactive ReAct loop in :func:`core.agent_loop.react_loop` consumes
``tools: Dict[str, Callable]`` where every callable accepts kwargs and
returns a string the agent can read. This module provides a curated
set of tools that lets the agent **modify its own configuration**
directly from chat — adding API keys, registering MCP servers,
enabling channels, scheduling tasks, repairing config, running
self-tests, and so on.

All tools share four contracts:

1.  They are **safe to import** in any desktop mode (Lite / Pro).
    Heavy backends are imported lazily inside each function so that
    importing this module never pulls in optional deps.
2.  They **never raise** — exceptions are converted into a structured
    error string. The agent then has the freedom to reflect, correct
    its arguments, and try again.
3.  Destructive tools (``delete``, ``set_mode``, ``set_resource_limits``)
    require an explicit ``confirmed=True`` kwarg. Without it, they
    return a "are-you-sure" message describing exactly what will
    change. This is the bridge between unconstrained natural-language
    requests and irreversible system changes.
4.  Every result is a JSON-serialised string of the form
    ``{"ok": bool, "summary": str, ...}``. Callers (the agent, the
    test suite, the FastAPI thin wrappers) can parse a single
    consistent shape.

Usage::

    from agent_core import tool_registry
    from core.admin_tools import register_admin_tools

    register_admin_tools(tool_registry)

After registration, the agent may call e.g.::

    Action: admin.add_api_key
    Action Input: {"name": "openai-prod", "api_key": "sk-...",
                   "provider": "openai"}
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------
def _ok(summary: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"ok": True, "summary": summary}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _err(summary: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"ok": False, "summary": summary}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _need_confirm(summary: str, action: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {
        "ok": False,
        "needs_confirmation": True,
        "summary": summary,
        "next_action": (f"Re-issue the same call with confirmed=true "
                         f"to {action}."),
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------
def admin_add_api_key(*, name: str, api_key: str,
                       provider: Optional[str] = None,
                       model: Optional[str] = None,
                       base_url: Optional[str] = None,
                       enabled: bool = True,
                       priority: int = 0,
                       **_unused: Any) -> str:
    """Persist an LLM provider key in the encrypted KeyStore."""
    if not name or not api_key:
        return _err("'name' and 'api_key' are required")
    try:
        from core.llm.keystore import KeyStore
        view = KeyStore().add(name=name, api_key=api_key,
                               provider=provider, model=model,
                               base_url=base_url, enabled=enabled,
                               priority=priority)
        return _ok(f"API key '{name}' saved for provider "
                    f"{view.get('provider', 'auto')}.",
                   applied=view)
    except Exception as exc:
        return _err(f"failed to add api key: {exc}")


def admin_remove_api_key(*, name: str, confirmed: bool = False,
                          **_unused: Any) -> str:
    if not name:
        return _err("'name' is required")
    if not confirmed:
        return _need_confirm(
            f"About to permanently delete API key '{name}'.",
            f"delete '{name}'",
            target=name)
    try:
        from core.llm.keystore import KeyStore
        ok = KeyStore().remove(name)
        if ok:
            return _ok(f"API key '{name}' deleted.", target=name)
        return _err(f"no api key named '{name}'")
    except Exception as exc:
        return _err(f"failed to delete api key: {exc}")


def admin_list_api_keys(**_unused: Any) -> str:
    try:
        from core.llm.keystore import KeyStore
        keys = KeyStore().list(reveal=False)
        return _ok(f"{len(keys)} API key(s) registered.",
                   keys=keys)
    except Exception as exc:
        return _err(f"failed to list keys: {exc}")


# ---------------------------------------------------------------------------
# MCP server management
# ---------------------------------------------------------------------------
def admin_add_mcp_server(*, name: str,
                          kind: str = "http",
                          url: Optional[str] = None,
                          command: Optional[str] = None,
                          args: Optional[List[str]] = None,
                          enabled: bool = True,
                          **_unused: Any) -> str:
    if not name:
        return _err("'name' is required")
    if kind == "http" and not url:
        return _err("kind='http' requires 'url'")
    if kind == "stdio" and not command:
        return _err("kind='stdio' requires 'command'")
    try:
        from core.mcp.registry import MCPRegistry
        out = MCPRegistry().add({
            "name": name, "kind": kind, "url": url or "",
            "command": command or "", "args": list(args or []),
            "enabled": enabled,
        })
        return _ok(f"MCP server '{name}' registered "
                    f"(kind={kind}, alive={out.get('alive')}).",
                   applied=out)
    except Exception as exc:
        return _err(f"failed to register MCP server: {exc}")


def admin_remove_mcp_server(*, name: str, confirmed: bool = False,
                             **_unused: Any) -> str:
    if not name:
        return _err("'name' is required")
    if not confirmed:
        return _need_confirm(
            f"About to remove MCP server '{name}' from the registry.",
            f"remove MCP server '{name}'",
            target=name)
    try:
        from core.mcp.registry import MCPRegistry
        ok = MCPRegistry().remove(name)
        if ok:
            return _ok(f"MCP server '{name}' removed.", target=name)
        return _err(f"no MCP server named '{name}'")
    except Exception as exc:
        return _err(f"failed to remove MCP server: {exc}")


def admin_list_mcp_servers(**_unused: Any) -> str:
    try:
        from core.mcp.registry import MCPRegistry
        servers = MCPRegistry().list_servers()
        return _ok(f"{len(servers)} MCP server(s).", servers=servers)
    except Exception as exc:
        return _err(f"failed to list MCP servers: {exc}")


# ---------------------------------------------------------------------------
# Notification channels (Apprise)
# ---------------------------------------------------------------------------
def admin_add_channel(*, name: str, url: str,
                       **_unused: Any) -> str:
    if not name or not url:
        return _err("'name' and 'url' are required")
    try:
        from core.channels.unified import register_channel, list_channels
        register_channel(name=name, url=url)
        return _ok(f"Channel '{name}' registered.",
                   total=len(list_channels()))
    except Exception as exc:
        return _err(f"failed to register channel: {exc}")


def admin_remove_channel(*, name: str, confirmed: bool = False,
                          **_unused: Any) -> str:
    if not name:
        return _err("'name' is required")
    if not confirmed:
        return _need_confirm(
            f"About to remove notification channel '{name}'.",
            f"remove channel '{name}'",
            target=name)
    try:
        from core.channels.unified import unregister_channel
        ok = unregister_channel(name)
        if ok:
            return _ok(f"Channel '{name}' removed.", target=name)
        return _err(f"no channel named '{name}'")
    except Exception as exc:
        return _err(f"failed to remove channel: {exc}")


def admin_list_channels(**_unused: Any) -> str:
    try:
        from core.channels.unified import list_channels
        info = list_channels()
        configured = info.get("configured") or []
        return _ok(f"{len(configured)} channel(s) registered.",
                   configured=configured,
                   apprise_available=info.get("apprise_available", False),
                   schemes=info.get("available_schemes", []))
    except Exception as exc:
        return _err(f"failed to list channels: {exc}")


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------
_RESOURCE_KEYS = {
    "cpu_seconds", "memory_mb", "max_processes", "parallel_tools",
    "network_timeout_s", "disk_mb", "compression_enabled",
    "compression_method", "compression_ratio",
}


def admin_set_resource_limits(*, confirmed: bool = False,
                               **fields: Any) -> str:
    """Update one or more resource-limit fields."""
    overrides = {k: v for k, v in fields.items() if k in _RESOURCE_KEYS}
    if not overrides:
        return _err(
            "no recognised resource fields provided",
            allowed=sorted(_RESOURCE_KEYS))
    if not confirmed:
        return _need_confirm(
            f"About to update resource settings: {overrides}.",
            "apply resource limits",
            preview=overrides)
    try:
        from core.platform_manager import get_platform_manager
        from core.resource_settings import get_store
        store = get_store()
        mode = get_platform_manager().mode
        current = store.load(mode=mode)
        for k, v in overrides.items():
            setattr(current, k, v)
        current.mode_at_save = mode
        store.save(current)
        return _ok(f"Updated {len(overrides)} resource field(s).",
                   applied=overrides, mode=mode)
    except Exception as exc:
        return _err(f"failed to update resources: {exc}")


def admin_get_resources(**_unused: Any) -> str:
    try:
        from core.platform_manager import get_platform_manager
        from core.resource_settings import get_store
        s = get_store().load(mode=get_platform_manager().mode)
        return _ok("Current resource settings.", settings=s.to_dict())
    except Exception as exc:
        return _err(f"failed to read resources: {exc}")


# ---------------------------------------------------------------------------
# Mode switching
# ---------------------------------------------------------------------------
def admin_set_mode(*, mode: str, confirmed: bool = False,
                    **_unused: Any) -> str:
    norm = (mode or "").strip().lower()
    if norm not in ("lite", "pro"):
        return _err("mode must be 'lite' or 'pro'", got=mode)
    if not confirmed:
        return _need_confirm(
            f"About to switch Agent OS mode to '{norm}'. This affects "
            "resource ceilings, available tools, and compression.",
            f"switch mode to '{norm}'",
            target_mode=norm)
    try:
        from core.platform_manager import get_platform_manager
        pm = get_platform_manager()
        pm.set_mode(norm) if hasattr(pm, "set_mode") else setattr(
            pm, "mode", norm)
        os.environ["AGENT_OS_MODE"] = norm
        return _ok(f"Agent OS mode set to '{norm}'.", mode=norm)
    except Exception as exc:
        return _err(f"failed to switch mode: {exc}")


# ---------------------------------------------------------------------------
# Self-test + repair
# ---------------------------------------------------------------------------
def admin_run_self_test(*, only: Optional[str] = None,
                         **_unused: Any) -> str:
    try:
        from core.self_test import run_self_test
        selected: Optional[List[str]] = None
        if isinstance(only, str) and only.strip():
            selected = [s.strip() for s in only.split(",") if s.strip()]
        elif isinstance(only, list):
            selected = [str(s) for s in only]
        report = run_self_test(only=selected)
        return _ok(f"Self-test {report.status}: "
                    f"{report.summary.get('ok', 0)} ok / "
                    f"{report.summary.get('degraded', 0)} degraded / "
                    f"{report.summary.get('failed', 0)} failed.",
                   report=report.to_dict())
    except Exception as exc:
        return _err(f"self-test crashed: {exc}")


def admin_repair(*, target: str = "config",
                  error_text: str = "",
                  confirmed: bool = False,
                  **_unused: Any) -> str:
    if target not in ("config", "sandbox", "tools", "channels"):
        return _err("target must be one of "
                    "config|sandbox|tools|channels", got=target)
    if not confirmed:
        return _need_confirm(
            f"About to attempt automatic repair on '{target}'. This "
            "may overwrite a corrupted config file with defaults.",
            f"repair {target}",
            target=target)
    try:
        from core.self_healing import get_self_healing
        report = get_self_healing().repair(target=target,
                                            error_text=error_text or "")
        return _ok(f"Repair {('succeeded' if report.success else 'failed')}: "
                    f"{report.summary if hasattr(report, 'summary') else target}",
                   target=target,
                   success=bool(report.success))
    except Exception as exc:
        return _err(f"repair crashed: {exc}")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def admin_schedule_task(*, name: str, cron: str, target: str,
                         payload: Optional[Dict[str, Any]] = None,
                         tz: str = "UTC", enabled: bool = True,
                         **_unused: Any) -> str:
    if not name or not cron or not target:
        return _err("'name', 'cron' and 'target' are required")
    try:
        from core.scheduler import Scheduler
        s = Scheduler().add(name=name, cron=cron, target=target,
                              payload=payload or {}, tz=tz,
                              enabled=enabled)
        return _ok(f"Schedule '{name}' added (id={s.id}, cron='{cron}').",
                   schedule_id=s.id, name=s.name, cron=s.cron,
                   target=s.target,
                   next_run_at=getattr(s, "next_run_at", None))
    except Exception as exc:
        return _err(f"failed to schedule task: {exc}")


def admin_list_schedules(**_unused: Any) -> str:
    try:
        from core.scheduler import Scheduler
        items = Scheduler().list()
        return _ok(f"{len(items)} schedule(s) registered.",
                   schedules=[{"id": s.id, "name": s.name,
                                "cron": s.cron, "target": s.target,
                                "enabled": s.enabled,
                                "next_run_at": getattr(s, "next_run_at",
                                                        None)}
                                for s in items])
    except Exception as exc:
        return _err(f"failed to list schedules: {exc}")


def admin_delete_schedule(*, schedule_id: str,
                           confirmed: bool = False,
                           **_unused: Any) -> str:
    if not schedule_id:
        return _err("'schedule_id' is required")
    if not confirmed:
        return _need_confirm(
            f"About to delete schedule '{schedule_id}'.",
            f"delete schedule '{schedule_id}'",
            schedule_id=schedule_id)
    try:
        from core.scheduler import Scheduler
        ok = Scheduler().delete(schedule_id)
        if ok:
            return _ok(f"Schedule '{schedule_id}' deleted.",
                       schedule_id=schedule_id)
        return _err(f"no schedule with id '{schedule_id}'")
    except Exception as exc:
        return _err(f"failed to delete schedule: {exc}")


# ---------------------------------------------------------------------------
# Public registration API
# ---------------------------------------------------------------------------
ADMIN_TOOLS: Dict[str, Callable[..., str]] = {
    "admin.add_api_key":         admin_add_api_key,
    "admin.remove_api_key":      admin_remove_api_key,
    "admin.list_api_keys":       admin_list_api_keys,
    "admin.add_mcp_server":      admin_add_mcp_server,
    "admin.remove_mcp_server":   admin_remove_mcp_server,
    "admin.list_mcp_servers":    admin_list_mcp_servers,
    "admin.add_channel":         admin_add_channel,
    "admin.remove_channel":      admin_remove_channel,
    "admin.list_channels":       admin_list_channels,
    "admin.set_resource_limits": admin_set_resource_limits,
    "admin.get_resources":       admin_get_resources,
    "admin.set_mode":            admin_set_mode,
    "admin.run_self_test":       admin_run_self_test,
    "admin.repair":              admin_repair,
    "admin.schedule_task":       admin_schedule_task,
    "admin.list_schedules":      admin_list_schedules,
    "admin.delete_schedule":     admin_delete_schedule,
}


def register_admin_tools(tool_registry: Any) -> List[str]:
    """Register every admin tool on the supplied tool registry.

    Returns the list of tool names that were actually registered. The
    registry is duck-typed to whatever exposes ``register(name, fn)``.
    Tools already present are left untouched so callers can override
    individual entries before calling this function.
    """
    registered: List[str] = []
    for name, fn in ADMIN_TOOLS.items():
        try:
            existing = None
            try:
                existing = tool_registry.get(name)
            except Exception:
                existing = None
            if existing is not None:
                continue
            tool_registry.register(name, fn)
            registered.append(name)
        except Exception:
            # A single tool failing should not poison the others.
            continue
    return registered


__all__ = [
    "ADMIN_TOOLS",
    "register_admin_tools",
    "admin_add_api_key",
    "admin_remove_api_key",
    "admin_list_api_keys",
    "admin_add_mcp_server",
    "admin_remove_mcp_server",
    "admin_list_mcp_servers",
    "admin_add_channel",
    "admin_remove_channel",
    "admin_list_channels",
    "admin_set_resource_limits",
    "admin_get_resources",
    "admin_set_mode",
    "admin_run_self_test",
    "admin_repair",
    "admin_schedule_task",
    "admin_list_schedules",
    "admin_delete_schedule",
]
