"""Per-session tool feedback loop.

When a tool call fails, we want Agent OS to:

* Record the failure (Tracer event ``tool.call.end status=error``).
* Suggest alternatives the next time the model proposes that tool.
* Hard-stop: if a single tool fails 3 or more times in the same
  session, ban it for the remainder of the session and tell the model.

State is keyed by ``session_id`` (a logical identifier; ``Tracer``
trace_ids work fine, or any string the caller picks). Banned tools are
exposed via :meth:`get_banned_tools` so the ranker can drop them and
``agent_loop`` can short-circuit before invoking the tool.

The class is process-local and thread-safe. We do NOT persist banned
state across restarts on purpose — a freshly-restarted session deserves
a fresh chance.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from .observability import Tracer


FAILURE_BAN_THRESHOLD = 3


@dataclass
class _SessionState:
    failures: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    banned: Set[str] = field(default_factory=set)
    suggestion_cache: Dict[str, List[str]] = field(default_factory=dict)


class ToolFeedback:
    """Track failures, ban over-failing tools, suggest alternatives."""

    def __init__(self, threshold: int = FAILURE_BAN_THRESHOLD) -> None:
        self.threshold = max(1, threshold)
        self._lock = threading.Lock()
        self._state: Dict[str, _SessionState] = defaultdict(_SessionState)

    # ------------------------------------------------------------------
    def record_failure(self, session_id: str, tool: str,
                       error: str = "") -> Dict[str, Any]:
        """Bump a tool's failure count. Ban if threshold reached."""
        with self._lock:
            st = self._state[session_id]
            st.failures[tool] += 1
            count = st.failures[tool]
            banned_now = False
            if count >= self.threshold and tool not in st.banned:
                st.banned.add(tool)
                banned_now = True
        Tracer.emit("tool.failure",
                    session=session_id, tool=tool,
                    error=error[:240], failures=count,
                    banned=banned_now)
        return {"tool": tool, "failures": count, "banned": banned_now}

    def record_success(self, session_id: str, tool: str) -> None:
        with self._lock:
            st = self._state[session_id]
            # Successes lightly heal the counter so transient errors
            # don't permanently ban a tool that recovered.
            if st.failures.get(tool):
                st.failures[tool] = max(0, st.failures[tool] - 1)

    def is_banned(self, session_id: str, tool: str) -> bool:
        with self._lock:
            return tool in self._state[session_id].banned

    def get_banned_tools(self, session_id: str) -> Set[str]:
        with self._lock:
            return set(self._state[session_id].banned)

    def failure_count(self, session_id: str, tool: str) -> int:
        with self._lock:
            return int(self._state[session_id].failures.get(tool, 0))

    def reset(self, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_id is None:
                self._state.clear()
            else:
                self._state.pop(session_id, None)

    # ------------------------------------------------------------------
    def suggest_alternatives(self,
                             session_id: str,
                             failed_tool: str,
                             *,
                             task: str,
                             tools: Dict[str, Dict[str, Any]],
                             history: Optional[Iterable[Dict[str, Any]]] = None,
                             k: int = 3) -> List[str]:
        """Return up to ``k`` ranked alternatives to ``failed_tool``.

        Bans (including ``failed_tool`` itself if banned) are excluded.
        Result is cached per (session, tool) so repeated suggestions in
        the same session are stable.
        """
        cache_key = f"{session_id}::{failed_tool}::{task}"
        with self._lock:
            cached = self._state[session_id].suggestion_cache.get(cache_key)
        if cached is not None:
            return cached

        # Lazy import to avoid circular dependency at module-load time.
        from .tool_intelligence.ranker import rank_tools_for_task

        banned = self.get_banned_tools(session_id) | {failed_tool}
        ranked = rank_tools_for_task(
            task, tools, history=history, banned=banned)
        out: List[str] = []
        for s in ranked:
            if s.is_banned:
                continue
            if s.name == failed_tool:
                continue
            out.append(s.name)
            if len(out) >= k:
                break
        with self._lock:
            self._state[session_id].suggestion_cache[cache_key] = out
        return out


# --------------------------------------------------------------------
# Module-level singleton (matches the rest of Agent OS conventions).
# --------------------------------------------------------------------
_feedback = ToolFeedback()


def get_feedback() -> ToolFeedback:
    return _feedback


__all__ = [
    "ToolFeedback", "get_feedback", "FAILURE_BAN_THRESHOLD",
]
