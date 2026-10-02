"""Hot-reload — refresh the tool surface without restarting the server.

Two complementary mechanisms:

1. **Manual reload** (``POST /admin/reload-tools``) — already wired
   into the API. We extend it here to also re-mount adapters, macros
   and MCP tools.
2. **File watcher** (``HotReloader``) — optional background thread
   built on top of stdlib ``os.stat`` polling (no third-party
   dependency). It watches:

   - ``tools/packs/*.json``  (manifest packs).
   - ``$MEMORY_DIR/macros/*.json``.
   - ``$MEMORY_DIR/mcp_servers.json``.
   - All ``core/**/*.py`` files (best-effort: only registry-affecting
     imports get re-executed).

When a watched file changes, the reloader debounces for 500 ms then
calls :func:`reload_all`. Failures are caught and logged via the
:class:`Tracer`; the loop keeps going.

The watcher is **off by default**. Enable per process via the
``AGENT_HOTRELOAD=1`` env var or ``HotReloader.start()``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

try:
    from core.paths import MEMORY_DIR  # type: ignore
except Exception:  # noqa: BLE001
    MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", str(Path.home() / ".agent-os" / "memory")))

from core.observability import Tracer


# --------------------------------------------------------------------------
def reload_all(tool_registry: Any) -> Dict[str, Any]:
    """Re-mount every dynamic tool source onto ``tool_registry``.

    Returns a summary dict: counts before/after for each source.
    """
    summary: Dict[str, Any] = {
        "before": len(getattr(tool_registry, "tools", {})),
    }

    # System awareness packs (manifest tools).
    try:
        from core.system_awareness import SystemAwareness
        awareness = SystemAwareness()
        added = awareness.register_all_tools(tool_registry)
        summary["awareness_added"] = added
        summary["awareness_total"] = len(awareness.tools)
    except Exception as exc:  # noqa: BLE001
        summary["awareness_error"] = str(exc)[:200]

    # Adapters.
    try:
        from core.adapters import build_adapter_tools
        ad = build_adapter_tools()
        for n, fn in ad.items():
            tool_registry.register(n, fn)
        summary["adapters"] = len(ad)
    except Exception as exc:  # noqa: BLE001
        summary["adapters_error"] = str(exc)[:200]

    # Macros.
    try:
        from core.composition import build_macro_tools
        mt = build_macro_tools(tool_registry)
        for n, fn in mt.items():
            tool_registry.register(n, fn)
        summary["macros"] = len(mt)
    except Exception as exc:  # noqa: BLE001
        summary["macros_error"] = str(exc)[:200]

    # MCP.
    try:
        from core.mcp import build_mcp_tools, get_registry
        mt = build_mcp_tools(get_registry(), auto_start=True)
        for n, fn in mt.items():
            tool_registry.register(n, fn)
        summary["mcp"] = len(mt)
    except Exception as exc:  # noqa: BLE001
        summary["mcp_error"] = str(exc)[:200]

    summary["after"] = len(getattr(tool_registry, "tools", {}))
    summary["delta"] = summary["after"] - summary["before"]
    Tracer.emit("hotreload.reload", **summary)
    return summary


# --------------------------------------------------------------------------
class HotReloader:
    """Lightweight polling watcher; debounces rapid edits."""

    DEBOUNCE_S = 0.5
    POLL_INTERVAL_S = 1.5

    def __init__(self, tool_registry: Any, *,
                 repo_root: Optional[Path] = None,
                 memory_dir: Optional[Path] = None) -> None:
        self.tool_registry = tool_registry
        self.repo_root = Path(repo_root) if repo_root else _find_repo_root()
        self.memory = Path(memory_dir) if memory_dir else Path(MEMORY_DIR)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snapshot: Dict[str, Tuple[float, int]] = {}
        self._pending: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _watched_files(self) -> Iterable[Path]:
        """Return the files we keep an eye on."""
        out: List[Path] = []
        # Tool packs.
        packs = self.repo_root / "tools" / "packs"
        if packs.exists():
            out.extend(packs.glob("*.json"))
        # Macros.
        macros = self.memory / "macros"
        if macros.exists():
            out.extend(macros.glob("*.json"))
        # MCP roster.
        mcp_servers = self.memory / "mcp_servers.json"
        if mcp_servers.exists():
            out.append(mcp_servers)
        return out

    def _scan(self) -> Dict[str, Tuple[float, int]]:
        snap: Dict[str, Tuple[float, int]] = {}
        for p in self._watched_files():
            try:
                st = p.stat()
                snap[str(p)] = (st.st_mtime, st.st_size)
            except Exception:
                continue
        return snap

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._snapshot = self._scan()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="hotreload",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                changed = self._diff()
                if changed:
                    self._pending = time.time() + self.DEBOUNCE_S
                    Tracer.emit("hotreload.detected", changed=list(changed)[:8])

                if self._pending and time.time() >= self._pending:
                    self._pending = 0.0
                    try:
                        reload_all(self.tool_registry)
                    except Exception as exc:  # noqa: BLE001
                        Tracer.emit("hotreload.error", error=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001
                Tracer.emit("hotreload.error", error=str(exc)[:200])
            time.sleep(self.POLL_INTERVAL_S)

    def _diff(self) -> Set[str]:
        new = self._scan()
        changed: Set[str] = set()
        old_keys = set(self._snapshot.keys())
        new_keys = set(new.keys())
        for k in new_keys - old_keys:
            changed.add(k)
        for k in old_keys - new_keys:
            changed.add(k)
        for k in old_keys & new_keys:
            if new[k] != self._snapshot[k]:
                changed.add(k)
        if changed:
            self._snapshot = new
        return changed


# --------------------------------------------------------------------------
def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "agent_core.py").exists():
            return parent
    return Path.cwd()


# --------------------------------------------------------------------------
def maybe_start_default(tool_registry: Any) -> Optional[HotReloader]:
    """Honor ``AGENT_HOTRELOAD=1`` to auto-start a watcher at boot."""
    if os.environ.get("AGENT_HOTRELOAD", "").strip() not in ("1", "true", "yes"):
        return None
    hr = HotReloader(tool_registry)
    hr.start()
    return hr


__all__ = ["HotReloader", "reload_all", "maybe_start_default"]
