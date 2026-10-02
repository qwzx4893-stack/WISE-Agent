"""Structured scratchpad — replaces the flat Thought/Action/Observation
text blob with a queryable record.

The model still *receives* a rendered text block (the engine renders
the scratchpad before each LLM call), but the engine itself reads the
structured form: it knows which tools were called, what failed, what
hypotheses are open, and which observations contradict each other.

Public API:

- :class:`ThoughtStep`           one (Thought, Action, Observation, Reflection?)
- :class:`Scratchpad`            ordered list of steps + facts + open hypotheses
- :meth:`Scratchpad.render`      pretty-print for the LLM context
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ThoughtStep:
    iteration: int
    thought: str = ""
    action: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    observation: str = ""
    reflection: str = ""
    duration_ms: float = 0.0
    error: Optional[str] = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iteration": self.iteration,
            "thought": self.thought,
            "action": self.action,
            "args": self.args,
            "observation": self.observation[:600],
            "reflection": self.reflection,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


@dataclass
class Scratchpad:
    task: str = ""
    steps: List[ThoughtStep] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)
    hypotheses: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def add_step(self, step: ThoughtStep) -> None:
        self.steps.append(step)
        if step.error:
            self.errors.append(f"step {step.iteration}: {step.error}")

    def add_fact(self, fact: str) -> None:
        fact = fact.strip()
        if fact and fact not in self.facts:
            self.facts.append(fact[:400])

    def add_hypothesis(self, h: str) -> None:
        h = h.strip()
        if h and h not in self.hypotheses:
            self.hypotheses.append(h[:400])

    # ------------------------------------------------------------------
    def last_action(self) -> Optional[str]:
        for step in reversed(self.steps):
            if step.action:
                return step.action
        return None

    def repeated_action(self, action: str, args: Optional[Dict[str, Any]],
                        within: int = 3) -> bool:
        """Did the same (action, args) fire in the last `within` steps?"""
        target = (action, _stable_repr(args))
        count = 0
        for step in reversed(self.steps):
            count += 1
            if count > within:
                break
            if step.action and (step.action, _stable_repr(step.args)) == target:
                return True
        return False

    def consecutive_failures(self) -> int:
        """How many of the most recent steps failed in a row?"""
        n = 0
        for step in reversed(self.steps):
            if step.error or (step.observation or "").lstrip().startswith("❌"):
                n += 1
            else:
                break
        return n

    # ------------------------------------------------------------------
    def render(self, *, max_steps: int = 6, include_facts: bool = True) -> str:
        """Render a compact text view for the LLM."""
        lines: List[str] = []
        if self.task:
            lines.append(f"# المهمة\n{self.task}\n")
        if include_facts and self.facts:
            lines.append("## حقائق ثبتت")
            for f in self.facts[-10:]:
                lines.append(f"- {f}")
            lines.append("")
        if self.hypotheses:
            lines.append("## فرضيات مفتوحة")
            for h in self.hypotheses[-5:]:
                lines.append(f"- {h}")
            lines.append("")
        if self.steps:
            lines.append("## آخر الخطوات")
            for step in self.steps[-max_steps:]:
                lines.append(f"### خطوة {step.iteration}")
                if step.thought:
                    lines.append(f"Thought: {step.thought}")
                if step.action:
                    args_text = _short_json(step.args)
                    lines.append(f"Action: {step.action}")
                    lines.append(f"Action Input: {args_text}")
                if step.observation:
                    obs = step.observation
                    if len(obs) > 500:
                        obs = obs[:500] + "…"
                    lines.append(f"Observation: {obs}")
                if step.reflection:
                    lines.append(f"Reflection: {step.reflection}")
                lines.append("")
        return "\n".join(lines).strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "steps": [s.to_dict() for s in self.steps],
            "facts": list(self.facts),
            "hypotheses": list(self.hypotheses),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------
def _stable_repr(d: Optional[Dict[str, Any]]) -> str:
    if not d:
        return ""
    try:
        import json
        return json.dumps(d, sort_keys=True, ensure_ascii=False)
    except Exception:
        return repr(sorted(d.items()))


def _short_json(d: Optional[Dict[str, Any]]) -> str:
    if not d:
        return "{}"
    try:
        import json
        text = json.dumps(d, ensure_ascii=False)
        return text if len(text) <= 200 else text[:200] + "…"
    except Exception:
        return str(d)[:200]


__all__ = ["ThoughtStep", "Scratchpad"]
