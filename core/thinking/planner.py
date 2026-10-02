"""Three-tier micro-planner.

The thinking engine consults the planner exactly once at the start of a
run. The planner classifies the task into one of three complexity buckets:

- ``trivial``   — a single tool call or a direct answer is enough.
                  We emit no plan; the engine goes straight to ReAct.
- ``moderate``  — a 2-4 step linear plan. We ask the model for a
                  compact JSON outline (no DAG, no retries — those are
                  the Workflow mode's job).
- ``complex``   — better handled by Workflow / AgentsTeam. We return
                  a hint instead of trying to plan it inline.

The classifier is heuristic by default (free, deterministic) and can
optionally consult the model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PlanComplexity(str, Enum):
    TRIVIAL = "trivial"
    MODERATE = "moderate"
    COMPLEX = "complex"


@dataclass
class MicroPlan:
    complexity: PlanComplexity
    rationale: str = ""
    steps: List[str] = field(default_factory=list)  # short text outlines
    suggested_mode: Optional[str] = None  # "workflow" | "agents_team"

    def render(self) -> str:
        if self.complexity is PlanComplexity.TRIVIAL or not self.steps:
            return ""
        lines = [f"## خطة مبدئية ({self.complexity.value})"]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"{i}. {s}")
        if self.rationale:
            lines.append(f"> {self.rationale}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Heuristic indicators
# --------------------------------------------------------------------------
_TRIVIAL_PAT = re.compile(
    r"\b(ما هو|عرّف|من هو|اشرح|كم|متى|أين|قائمة|list|define|what is|"
    r"who is|when|where|count|simple|بسيط|اطبع|أظهر)\b",
    re.IGNORECASE,
)
_COMPLEX_PAT = re.compile(
    r"\b(دمج|اربط|اختبر|انشر|deploy|integrate|migrate|orchestrate|"
    r"audit|refactor|تطوير كامل|build full|end-?to-?end|cross-?repo|"
    r"fork|clone .* and|تنفيذ متعدد|multi-?step plan|"
    r"on a schedule|كل يوم|يومياً|daily|hourly|weekly|في الخلفية)\b",
    re.IGNORECASE,
)


def _heuristic_complexity(task: str) -> PlanComplexity:
    if not task or len(task) < 12:
        return PlanComplexity.TRIVIAL
    if _COMPLEX_PAT.search(task):
        return PlanComplexity.COMPLEX
    # Multiple imperative verbs → moderate.
    verbs = re.findall(
        r"\b(ابحث|اكتب|عدّل|نفّذ|نظّف|حلّل|قارن|اختبر|أرسل|"
        r"search|write|edit|run|build|test|deploy|fetch|generate|"
        r"compare|summari[sz]e|merge|combine)\b",
        task, flags=re.IGNORECASE,
    )
    if len(verbs) >= 2 or len(task) > 120:
        return PlanComplexity.MODERATE
    if _TRIVIAL_PAT.search(task) and len(task) <= 80:
        return PlanComplexity.TRIVIAL
    return PlanComplexity.MODERATE


# --------------------------------------------------------------------------
class MicroPlanner:
    def __init__(self, model: Any = None, *, enable_llm: bool = True):
        self.model = model
        self.enable_llm = enable_llm and model is not None

    def classify(self, task: str) -> PlanComplexity:
        return _heuristic_complexity(task)

    def plan(self, task: str, *, tool_summary: str = "") -> MicroPlan:
        complexity = self.classify(task)

        if complexity is PlanComplexity.TRIVIAL:
            return MicroPlan(complexity=complexity, rationale="مهمة مباشرة.")

        if complexity is PlanComplexity.COMPLEX:
            return MicroPlan(
                complexity=complexity,
                rationale=(
                    "مهمة معقدة — يُفضّل التشغيل في Workflow أو AgentsTeam. "
                    "سأحاول تنفيذاً تفاعلياً مبسّطاً."
                ),
                suggested_mode="agents_team",
            )

        # Moderate: try the LLM for a 2-4 step outline.
        if self.enable_llm:
            try:
                steps = self._llm_outline(task, tool_summary)
            except Exception:
                steps = []
            if steps:
                return MicroPlan(
                    complexity=complexity,
                    rationale="خطة مختصرة قابلة للتنفيذ خطوة بخطوة.",
                    steps=steps,
                )
        # Fallback: heuristic split on conjunctions.
        return MicroPlan(
            complexity=complexity,
            rationale="خطة مستنبطة من مفردات الطلب (لا LLM).",
            steps=_heuristic_split(task),
        )

    # ------------------------------------------------------------------
    def _llm_outline(self, task: str, tool_summary: str) -> List[str]:
        prompt = (
            "أعطِ خطة مختصرة (2-4 خطوات نصية فقط، بدون tools) لإنجاز المهمة. "
            "ردّك يجب أن يكون JSON واحد بالشكل: "
            '{"steps":["...","..."]}\n\n'
            f"المهمة: {task}\n\nأدوات متاحة (للاستئناس):\n{tool_summary[:1000]}"
        )
        response = self.model([
            {"role": "system", "content": "أعد JSON واحد فقط."},
            {"role": "user", "content": prompt},
        ])
        m = re.search(r"\{[\s\S]*\}", response or "")
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        steps = data.get("steps")
        if not isinstance(steps, list):
            return []
        out: List[str] = []
        for s in steps[:6]:
            if isinstance(s, str) and s.strip():
                out.append(s.strip()[:200])
        return out


def _heuristic_split(task: str) -> List[str]:
    parts = re.split(r"\s*(?:،|,|ثم|then|بعدها|->|→|;)\s*", task)
    return [p.strip() for p in parts if p.strip()][:4]


__all__ = ["MicroPlanner", "MicroPlan", "PlanComplexity"]
