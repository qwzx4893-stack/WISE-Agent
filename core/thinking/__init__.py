"""Pro thinking layer for Agent OS.

Public surface:

- :class:`Scratchpad`        — structured thought buffer (steps, facts,
                               hypotheses, errors).
- :class:`ToolCallParser`    — robust extractor: regex + JSON repair +
                               fuzzy tool-name matching + fenced code
                               handling. Always returns a list of
                               :class:`ParsedCall` (possibly with
                               ``error`` populated so the engine can
                               surface a useful Observation).
- :class:`Reflector`         — heuristic + optional LLM critique of the
                               last Observation; suggests whether to
                               keep going, change tactic, or stop.
- :class:`MicroPlanner`      — three-tier planner: skip for trivial
                               tasks, single LLM JSON call for medium,
                               full DAG for complex.
- :class:`ThinkingEngine`    — orchestrates Plan → Act → Observe →
                               Reflect → Decide with a hard token + step
                               budget and full observability via
                               :data:`Tracer`.

The engine is callable like the old ``react_loop`` so existing call
sites (Normal, Workflow's __react__ step, AgentsTeam workers) keep
working with no changes.
"""

from .scratchpad import Scratchpad, ThoughtStep
from .parser import ParsedCall, ToolCallParser
from .reflector import Reflection, Reflector
from .planner import MicroPlan, MicroPlanner, PlanComplexity
from .engine import ThinkingEngine, ThinkingResult

__all__ = [
    "Scratchpad",
    "ThoughtStep",
    "ParsedCall",
    "ToolCallParser",
    "Reflection",
    "Reflector",
    "MicroPlan",
    "MicroPlanner",
    "PlanComplexity",
    "ThinkingEngine",
    "ThinkingResult",
]
