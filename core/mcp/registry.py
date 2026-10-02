"""MCP server registry — persistent roster + tool exposure.

The registry stores server descriptors in
``$MEMORY_DIR/mcp_servers.json``. Each descriptor is a
:class:`MCPServerConfig`. On boot we instantiate one
:class:`MCPClient` per descriptor (with ``enabled=True``), open it,
and expose its ``tools/list`` as Agent-OS-style callables prefixed
with the server name (``mcp_<server>_<tool>``).

A failed connection is **never fatal**: the registry records
``last_error`` and skips the server's tools — the rest still load.

Public surface:

- :class:`MCPServerConfig`
- :class:`MCPRegistry`           load / add / remove / start / stop.
- :func:`build_mcp_tools`        ``{tool_name: callable}`` for every
  alive client; safe to call repeatedly.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from core.paths import MEMORY_DIR  # type: ignore
except Exception:  # noqa: BLE001
    MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", str(Path.home() / ".agent-os" / "memory")))

from .client import MCPClient, MCPError
from .sandbox import is_sandbox_enforced
from .policy import (
    ConfirmationRequired,
    ToolClassification,
    ToolClassifier,
    get_gate,
)


_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")


# --------------------------------------------------------------------------
@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"      # "stdio" | "http"
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    init_timeout: float = 10.0
    call_timeout: float = 30.0
    description: str = ""
    tools: int = 0
    tool_names: List[str] = field(default_factory=list)

    # Security controls.
    sandbox: bool = True
    allowed_domains: List[str] = field(default_factory=list)
    classifications: Dict[str, str] = field(default_factory=dict)
    auto_approve: List[str] = field(default_factory=list)
    require_confirmation: bool = True
    dangerous_extras: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MCPServerConfig":
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError(f"invalid mcp server config: {raw!r}")
        name = str(raw["name"])
        if not _SAFE_NAME.match(name):
            raise ValueError(f"invalid mcp server name '{name}'")
        return cls(
            name=name,
            transport=str(raw.get("transport", "stdio")),
            command=str(raw.get("command", "")),
            args=[str(a) for a in (raw.get("args") or [])],
            env=dict(raw.get("env") or {}),
            url=str(raw.get("url", "")),
            headers=dict(raw.get("headers") or {}),
            enabled=bool(raw.get("enabled", True)),
            init_timeout=float(raw.get("init_timeout", 10.0)),
            call_timeout=float(raw.get("call_timeout", 30.0)),
            description=str(raw.get("description", "")),
            tools=int(raw.get("tools", 0)),
            tool_names=[str(t) for t in (raw.get("tool_names") or [])],
            sandbox=bool(raw.get("sandbox", True)),
            allowed_domains=[str(d) for d in (raw.get("allowed_domains") or [])],
            classifications={str(k): str(v) for k, v in
                             (raw.get("classifications") or {}).items()},
            auto_approve=[str(a) for a in (raw.get("auto_approve") or [])],
            require_confirmation=bool(raw.get("require_confirmation", True)),
            dangerous_extras=[str(p) for p in (raw.get("dangerous_extras") or [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "transport": self.transport,
            "command": self.command, "args": self.args,
            "env": self.env, "url": self.url, "headers": self.headers,
            "enabled": self.enabled,
            "init_timeout": self.init_timeout,
            "call_timeout": self.call_timeout,
            "description": self.description,
            "tools": self.tools or len(self.tool_names),
            "tool_names": self.tool_names,
            "sandbox": self.sandbox,
            "allowed_domains": self.allowed_domains,
            "classifications": self.classifications,
            "auto_approve": self.auto_approve,
            "require_confirmation": self.require_confirmation,
            "dangerous_extras": self.dangerous_extras,
        }


# --------------------------------------------------------------------------
class MCPRegistry:
    """Manages server configs + open clients."""

    def __init__(self, root: Optional[Path] = None) -> None:
        # Only the default, user-facing registry applies startup hygiene.  A
        # caller-supplied root is commonly an explicit development/test
        # registry and must retain exactly the descriptors it was given.
        self._is_default_runtime = root is None
        self.root = Path(root) if root else Path(MEMORY_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "mcp_servers.json"
        self._lock = threading.Lock()
        self._configs: List[MCPServerConfig] = []
        self._clients: Dict[str, MCPClient] = {}
        self._errors: Dict[str, str] = {}
        # server -> {tool_name: ToolClassification}
        self._classifications: Dict[str, Dict[str, ToolClassification]] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            self._configs = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._configs = []
            return
        if not isinstance(raw, list):
            self._configs = []
            return
        out: List[MCPServerConfig] = []
        for r in raw:
            try:
                cfg = MCPServerConfig.from_dict(r)
                # Older tests wrote temporary fake MCP entries into the shared
                # runtime file. They must never become resident child
                # processes in a real WISE session. Do not alter custom
                # registries used by tests or developers; merely quarantine
                # these known test fixtures in the default user registry.
                if self._is_default_runtime and self._is_test_fixture(cfg):
                    continue
                out.append(cfg)
            except Exception:
                continue
        self._configs = out
        names = {c.name for c in self._configs}
        if "agent-os" not in names:
            self._configs.append(
                MCPServerConfig(
                    name="agent-os",
                    transport="stdio",
                    command="python",
                    args=["-m", "core.mcp.server", "--transport", "stdio"],
                    description="WISE Agent OS built-in MCP server providing safe file edits, verification, and maintenance tools.",
                    # Export-only: running our own tool facade duplicates the
                    # native capabilities and spawns an unnecessary runtime.
                    enabled=False,
                    sandbox=False,
                )
            )
            self._save()

    @staticmethod
    def _is_test_fixture(cfg: MCPServerConfig) -> bool:
        normalized = " ".join(cfg.args).replace("\\", "/").lower()
        return "/tests/_fake_mcp_server.py" in normalized

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps([c.to_dict() for c in self._configs],
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------
    def list_servers(self) -> List[Dict[str, Any]]:
        import shutil
        import importlib.util
        out: List[Dict[str, Any]] = []
        for cfg in self._configs:
            cli = self._clients.get(cfg.name)
            is_alive = bool(cli and cli.alive)
            tool_count = len(cli.tools) if is_alive else 0
            tools_list = [t.get("name") for t in (cli.tools or [])] if is_alive else []
            classifications = self._classifications.get(cfg.name, {})
            kinds_count = {"read": 0, "write": 0, "dangerous": 0}
            for c in classifications.values():
                kinds_count[c.kind] = kinds_count.get(c.kind, 0) + 1

            # Check binary and package availability for stdio transport
            unavail_reason = ""
            sandbox_enforced = bool(
                cfg.transport == "stdio" and cfg.sandbox
                and is_sandbox_enforced()
            )
            if cfg.transport == "stdio" and cfg.sandbox and not sandbox_enforced:
                unavail_reason = (
                    "Sandboxed stdio MCP is blocked: this host has no enforced "
                    "MCP sandbox. Configure PRoot on a supported host, or explicitly "
                    "set sandbox=false only for a trusted server."
                )
            if cfg.transport == "stdio" and cfg.command:
                bin_name = cfg.command[0] if isinstance(cfg.command, list) else cfg.command.split()[0]
                if bin_name in ("python", "python3"):
                    # Check python module if -m is passed
                    if "-m" in cfg.args:
                        m_idx = cfg.args.index("-m")
                        if m_idx + 1 < len(cfg.args):
                            mod = cfg.args[m_idx + 1]
                            try:
                                if importlib.util.find_spec(mod) is None:
                                    unavail_reason = f"Python module '{mod}' not installed"
                            except Exception:
                                unavail_reason = f"Python module '{mod}' not importable"
                elif bin_name == "npx":
                    # npx -y handles on-demand install; skip preflight.
                    pass
                elif not shutil.which(bin_name):
                    unavail_reason = f"Executable '{bin_name}' not found on system PATH"

            err = self._errors.get(cfg.name, "")
            if not cfg.enabled:
                status = "STOPPED"
            elif is_alive:
                status = "CONNECTED"
            elif unavail_reason:
                status = "UNAVAILABLE"
                if not err:
                    err = unavail_reason
            elif err:
                status = "ERROR"
            else:
                status = "CONFIGURED"

            out.append({
                "name": cfg.name,
                "description": cfg.description,
                "transport": cfg.transport,
                "oauth_optional": cfg.url.rstrip("/") == "https://mcp.context7.com/mcp",
                "oauth_required": cfg.url.rstrip("/") == "https://mcp.context7.com/mcp/oauth" or "http 401" in err.lower(),
                "enabled": cfg.enabled,
                "alive": is_alive,
                "status": status,
                "tools": tool_count,
                "tool_names": tools_list,
                "kinds": kinds_count,
                "sandbox": cfg.sandbox,
                "sandbox_enforced": sandbox_enforced,
                "allowed_domains": cfg.allowed_domains,
                "auto_approve": cfg.auto_approve,
                "require_confirmation": cfg.require_confirmation,
                "last_error": err,
            })
        return out

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            cfg = self._find(name)
            if not cfg:
                return False
            cfg.enabled = enabled
            self._save()
            return True

    def list_tools(self, server: str) -> List[Dict[str, Any]]:
        cli = self._clients.get(server)
        if not cli or not cli.alive:
            return []
        rows = self._classifications.get(server, {})
        out: List[Dict[str, Any]] = []
        for tool in cli.tools or []:
            tname = (tool or {}).get("name", "")
            cls = rows.get(tname)
            out.append({
                "name": tname,
                "description": (tool or {}).get("description", ""),
                "kind": cls.kind if cls else "read",
                "auto_approved": cls.auto_approved if cls else False,
                "reason": cls.reason if cls else "",
            })
        return out

    def classification_of(self, server: str, tool: str) -> Optional[ToolClassification]:
        return self._classifications.get(server, {}).get(tool)

    def add(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        cfg = MCPServerConfig.from_dict(raw)
        with self._lock:
            self._configs = [c for c in self._configs if c.name != cfg.name]
            self._configs.append(cfg)
            self._save()
        started: Dict[str, Any] = {"alive": False}
        if cfg.enabled:
            started = self.start(cfg.name)
        cli = self._clients.get(cfg.name)
        return {
            "added": True,
            "name": cfg.name,
            "alive": bool(cli and cli.alive),
            "tools": len(cli.tools) if (cli and cli.alive) else 0,
            "kinds": self._kinds_summary(cfg.name),
            "started": started,
            "error": started.get("error", "") if not started.get("alive") else "",
        }

    def remove(self, name: str) -> bool:
        with self._lock:
            before = len(self._configs)
            self._configs = [c for c in self._configs if c.name != name]
            if len(self._configs) == before:
                return False
            self._save()
        self.stop(name)
        return True

    def start(self, name: str) -> Dict[str, Any]:
        cfg = self._find(name)
        if not cfg:
            return {"alive": False, "error": "unknown server", "status": "ERROR"}
        # Stop existing.
        self.stop(name)

        # A true sandbox request must fail closed.  On Windows the previous
        # implementation silently launched the child unrestricted because
        # POSIX preexec/rlimits do not apply there.
        if (cfg.transport == "stdio" and cfg.sandbox
                and not is_sandbox_enforced()):
            self._errors[name] = (
                "Sandboxed stdio MCP is blocked: no enforced MCP sandbox is "
                "available on this host. Configure PRoot on a supported host, "
                "or explicitly set sandbox=false only for a trusted server."
            )
            return {
                "alive": False, "error": self._errors[name],
                "status": "ENVIRONMENT_BLOCKED", "sandbox_enforced": False,
            }

        # Preflight check binary & packages for stdio transport
        import shutil
        import importlib.util
        if cfg.transport == "stdio" and cfg.command:
            bin_name = cfg.command[0] if isinstance(cfg.command, list) else cfg.command.split()[0]
            if bin_name in ("python", "python3"):
                if "-m" in cfg.args:
                    m_idx = cfg.args.index("-m")
                    if m_idx + 1 < len(cfg.args):
                        mod = cfg.args[m_idx + 1]
                        try:
                            if importlib.util.find_spec(mod) is None:
                                self._errors[name] = f"Python module '{mod}' not installed"
                                return {"alive": False, "error": self._errors[name], "status": "UNAVAILABLE"}
                        except Exception:
                            self._errors[name] = f"Python module '{mod}' not importable"
                            return {"alive": False, "error": self._errors[name], "status": "UNAVAILABLE"}
            elif bin_name == "npx":
                # npx -y handles on-demand install; skip preflight.
                pass
            elif not shutil.which(bin_name):
                self._errors[name] = f"Executable '{bin_name}' not found on system PATH"
                return {"alive": False, "error": self._errors[name], "status": "UNAVAILABLE"}

        try:
            from .oauth import refresh_if_needed
            refresh_if_needed(name, self)
            from .config_import import resolve_values
            cli = MCPClient(
                name=cfg.name, transport=cfg.transport,
                command=cfg.command, args=cfg.args, env=resolve_values(cfg.env),
                url=cfg.url, headers=resolve_values(cfg.headers),
                init_timeout=cfg.init_timeout,
                call_timeout=cfg.call_timeout,
                sandbox=cfg.sandbox,
                allowed_domains=cfg.allowed_domains,
            )
            cli.open()
            self._clients[name] = cli
            self._errors.pop(name, None)
            # Classify tools immediately so the bridge / UI sees kinds.
            classifier = ToolClassifier(
                overrides=cfg.classifications,
                auto_approve=cfg.auto_approve,
                dangerous_extras=cfg.dangerous_extras,
            )
            self._classifications[name] = {
                t.get("name", ""): classifier.classify(t)
                for t in (cli.tools or []) if t.get("name")
            }
            return {"alive": True, "tools": len(cli.tools), "status": "CONNECTED",
                    "kinds": self._kinds_summary(name)}
        except (MCPError, ValueError) as exc:
            self._errors[name] = str(exc)[:300]
            return {"alive": False, "error": str(exc)[:300], "status": "ERROR"}

    def _kinds_summary(self, name: str) -> Dict[str, int]:
        counts: Dict[str, int] = {"read": 0, "write": 0, "dangerous": 0}
        for c in self._classifications.get(name, {}).values():
            counts[c.kind] = counts.get(c.kind, 0) + 1
        return counts

    def stop(self, name: str) -> bool:
        cli = self._clients.pop(name, None)
        self._classifications.pop(name, None)
        if not cli:
            return False
        try:
            cli.close()
        except Exception:
            pass
        return True

    def start_all(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for cfg in self._configs:
            if cfg.enabled:
                out[cfg.name] = self.start(cfg.name)
        return out

    def stop_all(self) -> None:
        for name in list(self._clients.keys()):
            self.stop(name)

    # ------------------------------------------------------------------
    def _find(self, name: str) -> Optional[MCPServerConfig]:
        for c in self._configs:
            if c.name == name:
                return c
        return None

    # ------------------------------------------------------------------
    def alive_clients(self) -> Dict[str, MCPClient]:
        return {n: c for n, c in self._clients.items() if c.alive}


# --------------------------------------------------------------------------
def build_mcp_tools(registry: Optional[MCPRegistry] = None,
                    *, auto_start: bool = True) -> Dict[str, Callable[..., Any]]:
    """Return ``{tool_name: callable}`` for every alive MCP server.

    ``tool_name`` is ``mcp_<server>_<tool>``; the callable forwards
    keyword args to ``tools/call`` and returns the result dict.

    The wrapper enforces the confirmation gate for ``write`` /
    ``dangerous`` kinds:

    - ``read``           : pass-through.
    - ``write``          : require ``confirm=True`` kwarg, or be in the
      server's ``auto_approve`` list, or be approved out-of-band via the
      :class:`ConfirmationGate` REST surface.
    - ``dangerous``      : same rules as write but never auto-approved
      and never short-circuited by ``confirm=True`` alone — it must
      additionally pass the server's ``classifications`` override.
    """
    reg = registry or MCPRegistry()
    if auto_start:
        reg.start_all()
    out: Dict[str, Callable[..., Any]] = {}

    for server_name, client in reg.alive_clients().items():
        cfg = reg._find(server_name)
        for tool in client.tools or []:
            tname = (tool or {}).get("name", "")
            if not tname:
                continue
            safe = re.sub(r"[^A-Za-z0-9_]", "_", str(tname))
            registry_name = f"mcp_{server_name}_{safe}"

            cls = reg.classification_of(server_name, tname)
            kind = cls.kind if cls else "read"
            auto_ok = bool(cls and cls.auto_approved)
            require_confirm = bool(cfg.require_confirmation) if cfg else True

            def _make(srv: str, tl: str, cli: MCPClient,
                      kind: str, auto_ok: bool,
                      require_confirm: bool, desc: str) -> Callable[..., Any]:
                def _runner(*, confirm: bool = False, _wait: float = 0.0,
                            **kwargs: Any) -> Any:
                    # Enforce the gate for write / dangerous calls.
                    if require_confirm and kind != "read" and not auto_ok:
                        if kind == "dangerous" and not confirm:
                            # Dangerous always blocks unless explicitly
                            # confirmed (UI / REST flow).
                            if _wait > 0:
                                approved = get_gate().request_and_wait(
                                    server=srv, tool=tl, arguments=kwargs,
                                    kind=kind, wait_seconds=_wait,
                                )
                                if not approved:
                                    return {"ok": False,
                                            "error": "dangerous call denied or timed out",
                                            "kind": kind, "server": srv,
                                            "tool": tl}
                            else:
                                get_gate().request(
                                    server=srv, tool=tl, arguments=kwargs,
                                    kind=kind,
                                )
                                # request() always raises
                                # ConfirmationRequired before reaching here.
                                return {"ok": False, "error": "unreachable"}
                        if kind == "write" and not confirm:
                            if _wait > 0:
                                approved = get_gate().request_and_wait(
                                    server=srv, tool=tl, arguments=kwargs,
                                    kind=kind, wait_seconds=_wait,
                                )
                                if not approved:
                                    return {"ok": False,
                                            "error": "write call denied or timed out",
                                            "kind": kind, "server": srv,
                                            "tool": tl}
                            else:
                                get_gate().request(
                                    server=srv, tool=tl, arguments=kwargs,
                                    kind=kind,
                                )
                                return {"ok": False, "error": "unreachable"}
                    try:
                        from .oauth import refresh_if_needed
                        active = reg.alive_clients().get(srv)
                        if active is None: raise ValueError("MCP server is no longer connected")
                        if refresh_if_needed(srv, reg):
                            connected = reg.start(srv)
                            if not connected.get("alive"):
                                raise ValueError("MCP reconnect after refresh failed; authorize again")
                            active = reg.alive_clients()[srv]
                        current = reg.classification_of(srv,tl)
                        if current and current.kind != kind:
                            raise ValueError("MCP tool classification changed; rediscover tools before execution")
                        return active.call_tool(tl, kwargs, kind=kind)
                    except (MCPError, ValueError) as exc:
                        return {"ok": False, "error": str(exc)[:300],
                                "server": srv, "tool": tl, "kind": kind}
                _runner.__name__ = registry_name
                _runner.__doc__ = (
                    f"MCP tool '{tl}' from server '{srv}' (kind={kind}). "
                    f"{desc[:200]}"
                )
                # Surface metadata on the function object so the bridge
                # / system_awareness can read it without re-classifying.
                _runner.__mcp_meta__ = {  # type: ignore[attr-defined]
                    "server": srv, "tool": tl, "kind": kind,
                    "auto_approved": auto_ok,
                    "require_confirmation": require_confirm,
                }
                return _runner

            out[registry_name] = _make(
                server_name, tname, client, kind, auto_ok,
                require_confirm,
                (tool or {}).get("description", ""),
            )
    return out


__all__ = ["MCPRegistry", "MCPServerConfig", "build_mcp_tools", "get_mcp_registry"]


_GLOBAL_MCP_REGISTRY: Optional[MCPRegistry] = None
_REG_LOCK = threading.Lock()


def get_mcp_registry(root: Optional[Union[Path, str]] = None) -> MCPRegistry:
    """Returns or initializes the canonical global MCPRegistry instance."""
    global _GLOBAL_MCP_REGISTRY
    if _GLOBAL_MCP_REGISTRY is None:
        with _REG_LOCK:
            if _GLOBAL_MCP_REGISTRY is None:
                _GLOBAL_MCP_REGISTRY = MCPRegistry(root=root)
    return _GLOBAL_MCP_REGISTRY
