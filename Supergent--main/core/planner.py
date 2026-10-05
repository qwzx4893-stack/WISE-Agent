"""Plan a task into a sequence of tool calls using :class:`TeamModel`.

This module used to call ``litellm`` against a local Ollama server. The
project now relies exclusively on the API-keyed models in TeamModel, so
the planner does too. JSON parsing is defensive; if the model returns
something that doesn't decode, we return an empty plan rather than
propagate the failure.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from pydantic import BaseModel

from core.api_models import TeamModel
from core.tool_intelligence import ToolIntelligence


class Step(BaseModel):
    tool: str
    args: dict
    reason: Optional[str] = None


class Plan(BaseModel):
    steps: List[Step]


_PROMPT_TEMPLATE = """أعطِ خطة JSON فقط (بدون أي شرح إضافي) بالشكل:
{{"steps": [{{"tool": "...", "args": {{...}}, "reason": "..."}}]}}

استخدم الأدوات التالية فقط:
{tools}

السياق: {context}
المهمة: {task}
"""


class Planner:
    """LLM-driven planner that emits a small JSON DAG of tool calls."""

    def __init__(self, model: TeamModel | None = None,
                 tool_intelligence: ToolIntelligence | None = None):
        self.model = model or TeamModel()
        self.ti = tool_intelligence or ToolIntelligence()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _strip_code_fence(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text, count=1).strip()
            if text.endswith("```"):
                text = text[:-3].strip()
        return text

    def _parse_plan(self, raw: str) -> Plan:
        cleaned = self._strip_code_fence(raw or "")
        try:
            data = json.loads(cleaned)
        except Exception:
            # Try to extract the first JSON object substring.
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not m:
                return Plan(steps=[])
            try:
                data = json.loads(m.group(0))
            except Exception:
                return Plan(steps=[])

        steps_data = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps_data, list):
            return Plan(steps=[])

        steps: List[Step] = []
        for entry in steps_data:
            if not isinstance(entry, dict):
                continue
            tool = entry.get("tool")
            args = entry.get("args") or {}
            if not isinstance(tool, str) or not isinstance(args, dict):
                continue
            steps.append(Step(tool=tool, args=args, reason=entry.get("reason")))
        return Plan(steps=steps)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate(self, task: str, context: str = "",
                 max_retries: int = 2) -> Plan:
        if not self.ti.policy.check_task(task):
            return Plan(steps=[])

        # Prefer tool-intelligence search; fall back to first 5 tools.
        tools_data = []
        try:
            import asyncio

            tools_data = asyncio.run(self.ti.registry.search(task, limit=5))
        except Exception:
            try:
                tools_data = list(self.ti.registry.tools.values())[:5]
                # ToolRegistry.tools is dict[name -> dict[version -> manifest]]
                flat = []
                for versions in tools_data:
                    if isinstance(versions, dict):
                        flat.extend(versions.values())
                    else:
                        flat.append(versions)
                tools_data = flat[:5]
            except Exception:
                tools_data = []

        tools_desc = "\n".join(
            f"- {getattr(t, 'name', '?')}: {getattr(t, 'description', '')}"
            for t in tools_data
        ) or "- (لا توجد أدوات متاحة)"

        prompt = _PROMPT_TEMPLATE.format(
            tools=tools_desc, context=context or "-", task=task
        )

        attempts = 0
        while attempts <= max_retries:
            try:
                response = self.model([
                    {"role": "system", "content": "أنت مخطط ينتج خطط JSON فقط."},
                    {"role": "user", "content": prompt},
                ])
            except Exception:
                attempts += 1
                continue

            plan = self._parse_plan(response)
            if plan.steps:
                return plan
            attempts += 1

        return Plan(steps=[])
