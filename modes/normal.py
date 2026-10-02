"""Normal mode — single ReAct agent for simple tasks.

Use cases: search, write, edit, quick lookups, single-tool jobs.

Improvements over the previous draft:

- Trace each call (``mode.normal`` span) so it shows up in
  ``/traces/{trace_id}`` like Workflow / AgentsTeam runs.
- Optional ``select_tools_for_task`` curation: if ``awareness`` is
  available and the registry is large (>32 tools), narrow it down to
  the most relevant ~32 tools for the task. Keeps the system prompt
  small without removing tools the agent might genuinely need.
- All tool calls still go through ``ToolThrottle``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from core.observability import Tracer, new_trace_id
from core.throttle import default_throttle


class Normal:
    """Single-agent ReAct mode."""

    def __init__(self, model, tool_registry, system_awareness=None,
                 max_steps: int = 6, throttle=None,
                 max_tools: int = 32,
                 on_token: Optional[Callable[[str], None]] = None):
        self.model = model
        self.tool_registry = tool_registry
        self.system_awareness = system_awareness
        self.max_steps = max_steps
        self.throttle = throttle or default_throttle()
        self.max_tools = max_tools
        self.on_token = on_token

    # ------------------------------------------------------------------
    def run(self, task: str) -> str:
        from core.agent_loop import react_loop
        from core.task_router import filter_registry, select_tools_for_task

        registered = self.tool_registry.tools
        # When we have many tools, curate down to the most relevant subset.
        if (
            self.system_awareness is not None
            and len(registered) > self.max_tools
        ):
            try:
                manifests = select_tools_for_task(
                    task, self.system_awareness, k=self.max_tools,
                )
                names = [m["name"] for m in manifests if m["name"] in registered]
                curated = filter_registry(self.tool_registry, names) or dict(registered)
            except Exception:
                curated = dict(registered)
        else:
            curated = dict(registered)

        wrapped = {
            name: _throttle_wrap(name, fn, self.throttle)
            for name, fn in curated.items()
        }
        trace_id = new_trace_id()
        with Tracer.trace(trace_id):
            with Tracer.span("mode.normal", task_preview=task[:120],
                             tools=len(wrapped)):
                answer = react_loop(
                    user_input=task,
                    model=self.model,
                    tools=wrapped,
                    max_steps=self.max_steps,
                    system_awareness=self.system_awareness,
                    on_token=self.on_token,
                )
        return answer

    def __call__(self, task: str) -> str:
        return self.run(task)


def _throttle_wrap(name: str, fn, throttle):
    def _wrapped(*args, **kwargs):
        return throttle.execute(fn, *args, tool_name=name, **kwargs)
    _wrapped.__name__ = getattr(fn, "__name__", name)
    _wrapped.__doc__ = getattr(fn, "__doc__", "")
    return _wrapped


__all__ = ["Normal"]
