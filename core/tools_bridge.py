"""
Lightweight Python implementations for tool-pack entries that don't yet have a
runtime binding.

These are intentionally minimal and side-effect contained: they read/write
inside ``WORKSPACE_DIR``, run shell snippets through the kernel's
``execute_command`` and never reach outside the workspace.
"""

from __future__ import annotations

import os
import re
import json
import subprocess
import sys
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict

from .paths import WORKSPACE_DIR

_EXECUTION_CONTEXT = ContextVar("wise_builtin_execution_context", default=None)


@contextmanager
def builtin_security_context(context, action=None, params=None):
    token = _EXECUTION_CONTEXT.set((context, action, copy.deepcopy(params)))
    try:
        yield
    finally:
        _EXECUTION_CONTEXT.reset(token)


def _authorize_code(name, args):
    from .security.security_gate import get_security_gate, SecurityContext
    approval = _EXECUTION_CONTEXT.get()
    if approval and approval[1] == name and approval[2] == args:
        return  # The router already consumed the bound, single-use approval.
    ctx = approval[0] if approval else SecurityContext(caller="tools_bridge")
    decision = get_security_gate().evaluate(name, args, ctx)
    if not decision.allowed:
        raise PermissionError(decision.reason)


# Cache the heavy Memory instance (it loads SentenceTransformer on init).
_MEMORY_SINGLETON = None
_MEMORY_LOCK = __import__("threading").Lock()


def _get_memory():
    global _MEMORY_SINGLETON
    if _MEMORY_SINGLETON is None:
        with _MEMORY_LOCK:
            if _MEMORY_SINGLETON is None:
                from .memory import Memory
                _MEMORY_SINGLETON = Memory()
    return _MEMORY_SINGLETON


def _resolve(path: str) -> Path:
    target = (WORKSPACE_DIR / path).resolve()
    workspace = WORKSPACE_DIR.resolve()
    if workspace not in target.parents and target != workspace:
        raise ValueError("OUTSIDE WORKSPACE BLOCKED")
    return target


def _read_file(args: Dict[str, Any]) -> str:
    p = _resolve(args["path"])
    return p.read_text(encoding="utf-8", errors="replace")


def _write_file(args: Dict[str, Any]) -> str:
    p = _resolve(args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    return "OK"


def _list_directory(args: Dict[str, Any]) -> str:
    p = _resolve(args.get("path", "."))
    return "\n".join(sorted(os.listdir(p)))


def _grep_file(args: Dict[str, Any]) -> str:
    p = _resolve(args["path"])
    pattern = args["pattern"]
    matches = [line for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
               if re.search(pattern, line)]
    return "\n".join(matches) if matches else "NO MATCHES"


def _ask_user(args: Dict[str, Any]) -> str:
    return input(f"[AGENT] {args.get('question', '?')}: ")


def _execute_python(args: Dict[str, Any]) -> str:
    _authorize_code("execute_python", args)
    code = args["code"]
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=int(args.get("timeout", 30)),
            cwd=str(WORKSPACE_DIR),
        )
        if proc.returncode:
            raise RuntimeError(f"Python exited with code {proc.returncode}: {(proc.stderr or '')[:2000]}")
        return (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Python execution timed out") from exc


def _git_clone(args: Dict[str, Any]) -> str:
    repo = args["repo"]
    dest = _resolve(args.get("dest", Path(repo).name.replace(".git", "")))
    try:
        proc = subprocess.run(
            ["git", "clone", repo, str(dest)],
            capture_output=True, text=True, timeout=int(args.get("timeout", 120)),
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        return str(e)


def _web_fetch(args: Dict[str, Any]) -> str:
    from .web_research import read_web_evidence
    from urllib.parse import urlsplit
    if urlsplit(args["url"]).scheme not in {"http", "https"}:
        raise ValueError("web_fetch requires an HTTP(S) URL")
    return json.dumps(read_web_evidence(args["url"]), ensure_ascii=False)


def _web_search(args: Dict[str, Any]) -> str:
    from .web_research import search_web
    hits = search_web(args["query"], max_results=int(args.get("max_results", 5)))
    if not hits:
        raise RuntimeError("No web search evidence was retrieved; do not infer that the topic does not exist")
    return json.dumps(hits, ensure_ascii=False)


def _run_shell(args: Dict[str, Any]) -> str:
    # A working directory is not a sandbox; raw host code needs authorization.
    _authorize_code("run_shell", args)
    cmd = args["command"]
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=int(args.get("timeout", 60)), cwd=str(WORKSPACE_DIR),
        )
        if proc.returncode:
            raise RuntimeError(f"Command exited with code {proc.returncode}: {(proc.stderr or '')[:2000]}")
        return (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Shell execution timed out") from exc


def _search_memory(args: Dict[str, Any]) -> str:
    try:
        return "\n".join(_get_memory().search(args["query"]))
    except Exception as e:
        return f"❌ search_memory غير متاح: {e}"


def _remember(args: Dict[str, Any]) -> str:
    try:
        _get_memory().add(args["key"], args["value"])
        return "REMEMBERED"
    except Exception as e:
        return f"❌ remember غير متاح: {e}"


def _leon_speak(args: Dict[str, Any]) -> str:
    try:
        from .adapters.leon_adapter import get_leon_adapter
        res = get_leon_adapter().speak(args["text"], voice=args.get("voice", "default"), lang=args.get("lang", "en"))
        return json.dumps(res, ensure_ascii=False)
    except Exception as e:
        return f"❌ leon_speak error: {e}"


def _leon_get_context(args: Dict[str, Any]) -> str:
    try:
        from .adapters.leon_adapter import get_leon_adapter
        res = get_leon_adapter().get_context(category=args.get("category", "all"))
        return json.dumps(res, ensure_ascii=False)
    except Exception as e:
        return f"❌ leon_get_context error: {e}"


def _leon_get_memory(args: Dict[str, Any]) -> str:
    try:
        from .adapters.leon_adapter import get_leon_adapter
        res = get_leon_adapter().get_memory(args["query"], max_results=int(args.get("max_results", 5)))
        return json.dumps(res, ensure_ascii=False)
    except Exception as e:
        return f"❌ leon_get_memory error: {e}"


def _leon_send_ui(args: Dict[str, Any]) -> str:
    try:
        from .adapters.leon_adapter import get_leon_adapter
        res = get_leon_adapter().send_ui_message(args["message"], title=args.get("title", ""))
        return json.dumps(res, ensure_ascii=False)
    except Exception as e:
        return f"❌ leon_send_ui error: {e}"


_DISPATCH = {
    "read_file": _read_file,
    "write_file": _write_file,
    "list_directory": _list_directory,
    "grep_file": _grep_file,
    "ask_user": _ask_user,
    "execute_python": _execute_python,
    "git_clone": _git_clone,
    "web_fetch": _web_fetch,
    "web_search": _web_search,
    "run_shell": _run_shell,
    "search_memory": _search_memory,
    "remember": _remember,
    "leon_speak": _leon_speak,
    "leon_get_context": _leon_get_context,
    "leon_get_memory": _leon_get_memory,
    "leon_send_ui": _leon_send_ui,
    "browse_skills": lambda args: __import__("core.capability_catalog", fromlist=["browse_skills"]).browse_skills(**args),
    "browse_tools": lambda args: __import__("core.capability_catalog", fromlist=["browse_tools"]).browse_tools(**args),
    "browse_resources": lambda args: __import__("core.capability_catalog", fromlist=["browse_resources"]).browse_resources(**args),
    "security_scan": lambda args: __import__("core.security.local_scanners", fromlist=["scan"]).scan(**args),
}


def call_python_builtin_strict(name: str, args: Dict[str, Any]) -> str:
    """Typed execution boundary: failures raise, never masquerade as content."""
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ValueError(f"No local implementation for tool '{name}'")
    return fn(args)


def call_python_builtin(name: str, args: Dict[str, Any]) -> str:
    fn = _DISPATCH.get(name)
    if not fn:
        return f"❌ لا يوجد تنفيذ Python محلي للأداة '{name}'."
    try:
        return fn(args)
    except Exception as e:
        return f"❌ {name}: {e}"


__all__ = ["call_python_builtin", "call_python_builtin_strict"]
