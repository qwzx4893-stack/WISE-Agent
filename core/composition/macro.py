"""Macros — durable, named compositions of tool calls.

Design goals:

- **Declarative**: a macro is JSON / dict, not Python code. Easy for
  the model to author at runtime (``POST /macros``) and for users to
  edit by hand.
- **DAG aware**: each step lists its dependencies; a topological sort
  decides execution order.
- **Templated args**: a step's args can reference earlier steps via
  Jinja-lite syntax ``{{step_id}}`` or ``{{step_id.json.field}}`` and
  macro-level inputs via ``{{inputs.<name>}}``.
- **Cached**: macro results are cached by a stable hash of
  ``(name, inputs, version)`` for the configured TTL — so repeating
  the same call returns the cached result without re-running tools.
- **Observable**: every step gets a ``Tracer.span("macro.<name>.<step>")``
  event, plus a final ``macro.<name>`` envelope event.
- **Safe**: tool names are validated against a passed-in registry; an
  unknown tool aborts the macro before any step runs (fail-fast).
- **Cancelable**: per-step ``timeout`` and overall ``MAX_STEPS`` cap.

The ``MacroStore`` writes to ``$MEMORY_DIR/macros/<name>.json``.
"""

from __future__ import annotations

import hashlib
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

from core.observability import Tracer


# --------------------------------------------------------------------------
class MacroError(Exception):
    """Raised when a macro definition is invalid or execution fails."""


# --------------------------------------------------------------------------
@dataclass
class MacroStep:
    id: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    deps: List[str] = field(default_factory=list)
    timeout: float = 60.0
    retries: int = 0
    on_failure: str = "stop"   # "stop" | "continue" | "skip"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "tool": self.tool, "args": self.args,
            "deps": self.deps, "timeout": self.timeout,
            "retries": self.retries, "on_failure": self.on_failure,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MacroStep":
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("tool"):
            raise MacroError(f"invalid step: {raw!r}")
        return cls(
            id=str(raw["id"]),
            tool=str(raw["tool"]),
            args=dict(raw.get("args") or {}),
            deps=list(raw.get("deps") or []),
            timeout=float(raw.get("timeout", 60.0)),
            retries=int(raw.get("retries", 0)),
            on_failure=str(raw.get("on_failure", "stop")),
        )


# --------------------------------------------------------------------------
@dataclass
class Macro:
    name: str
    steps: List[MacroStep]
    description: str = ""
    inputs: List[str] = field(default_factory=list)   # required input keys
    outputs: Dict[str, str] = field(default_factory=dict)  # name -> "{{step.json.x}}"
    version: int = 1
    cache_ttl: float = 300.0
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "version": self.version,
            "cache_ttl": self.cache_ttl,
            "tags": self.tags,
            "created_at": self.created_at,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Macro":
        if not isinstance(raw, dict) or "name" not in raw:
            raise MacroError(f"invalid macro definition: missing name")
        steps = [MacroStep.from_dict(s) for s in (raw.get("steps") or [])]
        if not steps:
            raise MacroError("macro must declare at least one step")
        return cls(
            name=_validate_name(str(raw["name"])),
            description=str(raw.get("description", "")),
            inputs=list(raw.get("inputs") or []),
            outputs=dict(raw.get("outputs") or {}),
            version=int(raw.get("version", 1)),
            cache_ttl=float(raw.get("cache_ttl", 300.0)),
            tags=list(raw.get("tags") or []),
            created_at=float(raw.get("created_at", time.time())),
            steps=steps,
        )


# --------------------------------------------------------------------------
@dataclass
class MacroResult:
    name: str
    ok: bool
    outputs: Dict[str, Any]
    steps: List[Dict[str, Any]]
    cached: bool = False
    duration_ms: float = 0.0
    error: str = ""


# --------------------------------------------------------------------------
class _Cache:
    """Tiny TTL cache, in-memory only."""

    def __init__(self) -> None:
        self._data: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            row = self._data.get(key)
            if not row:
                return None
            value, expires = row
            if time.time() > expires:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (value, time.time() + max(1.0, ttl))


_GLOBAL_CACHE = _Cache()


# --------------------------------------------------------------------------
class MacroEngine:
    """Executes :class:`Macro` definitions against a tool registry.

    The registry must expose ``.tools`` (``Dict[name, callable]``) — this
    matches Agent OS' native :class:`agent_core.ToolRegistry`.
    """

    MAX_STEPS = 64

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    # ------------------------------------------------------------------
    def run(self, macro: Macro,
            inputs: Optional[Dict[str, Any]] = None,
            *, use_cache: bool = True) -> MacroResult:
        inputs = dict(inputs or {})
        self._validate(macro, inputs)

        cache_key = _hash_call(macro, inputs)
        if use_cache:
            cached = _GLOBAL_CACHE.get(cache_key)
            if cached is not None:
                cached_result = MacroResult(**cached)
                cached_result.cached = True
                return cached_result

        order = _topological(macro.steps)
        if len(order) > self.MAX_STEPS:
            raise MacroError(f"macro exceeds MAX_STEPS={self.MAX_STEPS}")

        scratch: Dict[str, Any] = {"inputs": inputs}
        step_results: List[Dict[str, Any]] = []
        ok = True
        err_msg = ""
        t0 = time.time()

        with Tracer.span(f"macro.{macro.name}",
                         steps=len(macro.steps), inputs=list(inputs.keys())):
            for sid in order:
                step = next(s for s in macro.steps if s.id == sid)
                row = self._exec_step(macro.name, step, scratch)
                step_results.append(row)
                if row["status"] == "ok":
                    scratch[step.id] = row["output"]
                else:
                    if step.on_failure == "continue":
                        scratch[step.id] = None
                        continue
                    if step.on_failure == "skip":
                        scratch[step.id] = None
                        continue
                    ok = False
                    err_msg = row.get("error", "")
                    break

            outputs: Dict[str, Any] = {}
            if ok:
                for k, expr in (macro.outputs or {}).items():
                    outputs[k] = _render(expr, scratch)

        result = MacroResult(
            name=macro.name, ok=ok, outputs=outputs,
            steps=step_results, duration_ms=(time.time() - t0) * 1000.0,
            error=err_msg,
        )
        if ok and use_cache and macro.cache_ttl > 0:
            _GLOBAL_CACHE.put(cache_key, _result_to_dict(result), macro.cache_ttl)
        return result

    # ------------------------------------------------------------------
    def _validate(self, macro: Macro, inputs: Dict[str, Any]) -> None:
        # All required inputs present.
        missing = [k for k in macro.inputs if k not in inputs]
        if missing:
            raise MacroError(f"missing inputs: {', '.join(missing)}")
        # All tools known.
        tools = getattr(self.registry, "tools", {})
        for s in macro.steps:
            if s.tool not in tools:
                raise MacroError(
                    f"unknown tool '{s.tool}' (used by step '{s.id}')",
                )
        # Step IDs unique + deps reachable.
        ids = {s.id for s in macro.steps}
        if len(ids) != len(macro.steps):
            raise MacroError("duplicate step ids")
        for s in macro.steps:
            for d in s.deps:
                if d not in ids:
                    raise MacroError(f"step '{s.id}' depends on unknown '{d}'")

    # ------------------------------------------------------------------
    def _exec_step(self, macro_name: str, step: MacroStep,
                   scratch: Dict[str, Any]) -> Dict[str, Any]:
        rendered_args = _render_args(step.args, scratch)
        attempt = 0
        last_err = ""
        t0 = time.time()
        while attempt <= max(0, step.retries):
            attempt += 1
            with Tracer.span(f"macro.{macro_name}.{step.id}",
                             tool=step.tool, attempt=attempt):
                try:
                    out = _run_with_timeout(
                        self.registry.tools[step.tool],
                        kwargs=rendered_args,
                        timeout=step.timeout,
                    )
                    return {
                        "id": step.id, "tool": step.tool, "status": "ok",
                        "output": out,
                        "duration_ms": (time.time() - t0) * 1000.0,
                        "attempts": attempt,
                    }
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)[:500]
                    if attempt > step.retries:
                        break
                    time.sleep(0.4 * attempt)
        return {
            "id": step.id, "tool": step.tool, "status": "failed",
            "error": last_err,
            "duration_ms": (time.time() - t0) * 1000.0,
            "attempts": attempt,
        }


# --------------------------------------------------------------------------
class MacroStore:
    """Persists macros to disk under ``$MEMORY_DIR/macros``."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else (Path(MEMORY_DIR) / "macros")
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def path_for(self, name: str) -> Path:
        return self.root / f"{_validate_name(name)}.json"

    def list(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                out.append({
                    "name": raw.get("name"),
                    "description": raw.get("description", ""),
                    "version": raw.get("version", 1),
                    "steps": len(raw.get("steps") or []),
                    "tags": raw.get("tags") or [],
                })
            except Exception:
                continue
        return out

    def get(self, name: str) -> Macro:
        p = self.path_for(name)
        if not p.exists():
            raise MacroError(f"macro '{name}' not found")
        return Macro.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save(self, macro: Macro) -> None:
        p = self.path_for(macro.name)
        with self._lock:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(macro.to_dict(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, p)

    def delete(self, name: str) -> bool:
        p = self.path_for(name)
        if not p.exists():
            return False
        p.unlink()
        return True

    def all(self) -> List[Macro]:
        out: List[Macro] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(Macro.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return out


# --------------------------------------------------------------------------
def build_macro_tools(registry: Any,
                      store: Optional[MacroStore] = None) -> Dict[str, Callable[..., Any]]:
    """Return ``{macro_name: callable}`` for every persisted macro.

    Each callable accepts ``**inputs`` and returns the macro outputs (or
    raises :class:`MacroError`). The closure binds ``registry`` so the
    macro re-runs against the live tool set.
    """
    store = store or MacroStore()
    engine = MacroEngine(registry)
    out: Dict[str, Callable[..., Any]] = {}

    for macro in store.all():
        # Bind the macro definition into a closure.
        def _make(m: Macro) -> Callable[..., Any]:
            def _runner(**inputs: Any) -> Dict[str, Any]:
                res = engine.run(m, inputs=inputs)
                return {
                    "ok": res.ok,
                    "outputs": res.outputs,
                    "cached": res.cached,
                    "duration_ms": round(res.duration_ms, 2),
                    "error": res.error,
                    "steps": [
                        {"id": s["id"], "tool": s["tool"], "status": s["status"]}
                        for s in res.steps
                    ],
                }
            _runner.__name__ = f"macro_{m.name}"
            _runner.__doc__ = m.description or f"Macro {m.name}"
            return _runner
        out[m_name(macro)] = _make(macro)
    return out


def m_name(macro: Macro) -> str:
    return f"macro_{macro.name}"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise MacroError(
            f"invalid macro name '{name}' (must match [A-Za-z][A-Za-z0-9_-]{{0,63}})"
        )
    return name


def _topological(steps: List[MacroStep]) -> List[str]:
    """Kahn's algorithm — raises on cycles."""
    deps: Dict[str, set] = {s.id: set(s.deps) for s in steps}
    incoming: Dict[str, set] = {s.id: set(s.deps) for s in steps}
    ready = [sid for sid, dset in incoming.items() if not dset]
    order: List[str] = []
    while ready:
        ready.sort()
        sid = ready.pop(0)
        order.append(sid)
        for other_id, dset in incoming.items():
            if sid in dset:
                dset.discard(sid)
                if not dset and other_id not in order and other_id not in ready:
                    ready.append(other_id)
    if len(order) != len(steps):
        raise MacroError("macro DAG has a cycle or unresolved dependency")
    return order


_REF_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*\}\}")


def _render(value: Any, scratch: Dict[str, Any]) -> Any:
    """Replace ``{{path.to.value}}`` tokens against the scratchpad."""
    if isinstance(value, str):
        # Whole-string ref — return raw object (don't stringify).
        m = _REF_RE.fullmatch(value.strip())
        if m:
            return _resolve(m.group(1), scratch)
        return _REF_RE.sub(
            lambda mm: _stringify(_resolve(mm.group(1), scratch)),
            value,
        )
    if isinstance(value, list):
        return [_render(v, scratch) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, scratch) for k, v in value.items()}
    return value


def _render_args(args: Dict[str, Any], scratch: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        out[k] = _render(v, scratch)
    return out


def _resolve(path: str, scratch: Dict[str, Any]) -> Any:
    parts = path.split(".")
    cur: Any = scratch
    for p in parts:
        # ``.json`` and ``.output`` are no-op aliases that let callers
        # write ``{{step.json.field}}`` for readability.
        if p in ("json", "output", "result"):
            continue
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except Exception:
                return None
        else:
            return None
    return cur


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def _hash_call(macro: Macro, inputs: Dict[str, Any]) -> str:
    payload = json.dumps(
        {"n": macro.name, "v": macro.version, "i": inputs},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _run_with_timeout(fn: Callable[..., Any], *,
                      kwargs: Dict[str, Any], timeout: float) -> Any:
    """Run a sync callable in a thread with a hard timeout.

    Note: Python threads cannot be force-killed; on timeout we abandon
    the worker and raise ``MacroError``. The thread still runs in the
    background until completion.
    """
    if timeout <= 0:
        return fn(**kwargs)
    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["v"] = fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise MacroError(f"step timed out after {timeout}s")
    if "e" in box:
        raise box["e"]  # type: ignore[misc]
    return box.get("v")


def _result_to_dict(r: MacroResult) -> Dict[str, Any]:
    return {
        "name": r.name, "ok": r.ok, "outputs": r.outputs,
        "steps": r.steps, "cached": False,
        "duration_ms": r.duration_ms, "error": r.error,
    }


__all__ = [
    "Macro", "MacroEngine", "MacroError", "MacroResult", "MacroStep",
    "MacroStore", "build_macro_tools",
]
