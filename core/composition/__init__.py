"""Tool composition layer — macros + pipelines.

A macro is a named, persistent tool composed of other tools. The agent
or the user defines a macro once; from then on it shows up in
``ToolRegistry`` / ``SystemAwareness`` as a single high-level tool.

Macros are stored under ``$MEMORY_DIR/macros/<name>.json`` and reloaded
on startup via :func:`load_all_macros`. Each macro owns its own DAG of
``MacroStep`` definitions, optional ``inputs`` schema, ``outputs`` map,
result caching, and full :class:`Tracer` integration.

Public surface:

- :class:`Macro`               in-memory definition (name, steps, …).
- :class:`MacroStep`           one DAG node.
- :class:`MacroEngine`         executes a macro against a tool registry.
- :class:`MacroStore`          load / save / list / delete macros.
- :func:`build_macro_tools`    returns ``{macro_name: callable}`` for
  every persisted macro.
"""

from __future__ import annotations

from .macro import (
    Macro,
    MacroEngine,
    MacroError,
    MacroResult,
    MacroStep,
    MacroStore,
    build_macro_tools,
)

__all__ = [
    "Macro",
    "MacroEngine",
    "MacroError",
    "MacroResult",
    "MacroStep",
    "MacroStore",
    "build_macro_tools",
]
