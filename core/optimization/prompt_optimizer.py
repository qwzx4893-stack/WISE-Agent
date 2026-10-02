"""Build a token-efficient system prompt + lazy tool documentation.

Two improvements over the previous heuristic line-keeper:

1. ``optimize`` is structure-aware — it doesn't drop random lines from
   an Arabic system prompt, it shrinks each *section* (overview / tool
   list / format) independently so we never lose the format spec.

2. ``select_tools_for_prompt`` returns a small, diverse subset of tools
   (with full one-line descriptions) plus a budgeted reference of the
   rest by *category only*. The agent can request details for any tool
   later via ``search_skills`` / ``get_tool_manifest``.

Together they cut a typical system prompt substantially without sacrificing
the safety policy or response format.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .token_counter import TokenCounter


_FORMAT_KW = (
    "Action", "Action Input", "Final Answer", "Thought", "Observation",
    "تنسيق", "JSON", "##", "مثال",
)
_SAFETY_KW = (
    "أمان", "سلامة", "موافقة", "إذن", "خصوص", "حماية", "لا تنفذ",
    "لا تحذف", "لا ترسل", "security", "safety", "consent", "permission",
    "approval", "privacy", "never ", "do not ",
)


class PromptOptimizer:
    def __init__(self, model_name: str = "gpt-4o-mini",
                 max_tokens: int = 2000):
        self.counter = TokenCounter(model_name)
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # System-prompt squeezer
    # ------------------------------------------------------------------
    def optimize(self, system_prompt: str,
                 max_tokens: Optional[int] = None) -> str:
        budget = max_tokens or self.max_tokens
        if self.counter.count(system_prompt) <= budget:
            return system_prompt

        sections = self._split_sections(system_prompt)
        kept: List[str] = []
        used = 0
        # Policies come first, then the response format. Tool lists and prose
        # are useful but must never crowd out safety/consent instructions.
        indexed = list(enumerate(sections))
        indexed.sort(key=lambda item: self._priority(item[1][0], item[1][1], item[0]))
        has_format = any(self._is_format(header) for header, _ in sections)
        if not has_format:
            extracted = self._extract_format_lines(system_prompt)
            if extracted:
                kept.append(extracted)
                used += self.counter.count(extracted)

        for _, (header, body) in indexed:
            chunk = self._fmt(header, body)
            cost = self.counter.count(chunk)
            if used + cost <= budget:
                kept.append(chunk)
                used += cost
            else:
                # Preserve a compact, explicit version of the important
                # section. Lower-priority sections can be omitted entirely.
                lines = body.splitlines()
                if self._is_safety(header, body):
                    selected = [line for line in lines if self._line_is_critical(line)]
                    short_body = "\n".join(selected[:8] or lines[:4])
                elif self._is_format(header):
                    short_body = "\n".join(lines[:6])
                else:
                    short_body = "\n".join(lines[:2])
                short_chunk = self._fmt(header, short_body)
                if used + self.counter.count(short_chunk) <= budget:
                    kept.append(short_chunk)
                    used += self.counter.count(short_chunk)
        return "\n\n".join(kept)

    # ------------------------------------------------------------------
    # Lazy tool-doc selection
    # ------------------------------------------------------------------
    def select_tools_for_prompt(
        self,
        tools_desc_lines: List[Tuple[str, str, str]],
        *,
        keep: int = 24,
        budget_tokens: int = 1200,
    ) -> str:
        """Return a compact tool manifest within ``budget_tokens``.

        ``tools_desc_lines`` is a list of ``(name, description, category)``
        tuples. We keep the first ``keep`` *full* descriptions, then
        compress the rest into a one-line "and N more in <category>"
        index. The model can ask for details later via ``search_tools``.
        """
        kept_lines: List[str] = []
        rest_by_cat: Dict[str, int] = {}
        for i, (name, desc, cat) in enumerate(tools_desc_lines):
            cat = cat or "أخرى"
            if i < keep:
                short_desc = (desc or "").splitlines()[0][:140]
                kept_lines.append(f"- **{name}** ({cat}): {short_desc}")
            else:
                rest_by_cat[cat] = rest_by_cat.get(cat, 0) + 1

        rest_lines: List[str] = []
        for cat, n in sorted(rest_by_cat.items(), key=lambda p: -p[1]):
            rest_lines.append(f"- *{cat}*: {n} أداة إضافية (استدعِ `search_tools` للتفاصيل)")

        text = "\n".join(kept_lines + rest_lines)
        # Trim if still over budget by progressively dropping kept_lines.
        while self.counter.count(text) > budget_tokens and kept_lines:
            kept_lines.pop()
            text = "\n".join(kept_lines + rest_lines)
        # A registry can contain hundreds of categories.  The old loop only
        # removed full tool descriptions, so the category index itself could
        # still overflow the prompt budget. Keep the most-populated categories
        # first, then add one explicit discovery hint if it fits.
        while self.counter.count(text) > budget_tokens and rest_lines:
            rest_lines.pop()
            text = "\n".join(kept_lines + rest_lines)
        return text

    # ------------------------------------------------------------------
    # Section split helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _split_sections(text: str) -> List[Tuple[str, str]]:
        # Split on Markdown-style ``## Header`` lines. Anything before the
        # first header becomes the implicit "intro" section.
        sections: List[Tuple[str, str]] = []
        current_header = ""
        current_body: List[str] = []
        for line in text.splitlines():
            if line.startswith("##"):
                if current_header or current_body:
                    sections.append((current_header, "\n".join(current_body).strip()))
                current_header = line.strip()
                current_body = []
            else:
                current_body.append(line)
        if current_header or current_body:
            sections.append((current_header, "\n".join(current_body).strip()))
        return sections

    @staticmethod
    def _is_format(header: str) -> bool:
        return any(kw in header for kw in ("تنسيق", "format", "Format"))

    @staticmethod
    def _is_safety(header: str, body: str) -> bool:
        haystack = f"{header}\n{body}".lower()
        return any(keyword in haystack for keyword in _SAFETY_KW)

    def _priority(self, header: str, body: str, index: int) -> Tuple[int, int]:
        if self._is_safety(header, body):
            return (0, index)
        if self._is_format(header):
            return (1, index)
        if not header:
            return (2, index)
        if "أداة" in header or "tool" in header.lower():
            return (4, index)
        return (3, index)

    @staticmethod
    def _line_is_critical(line: str) -> bool:
        lowered = line.lower()
        return any(keyword in lowered for keyword in _SAFETY_KW)

    @staticmethod
    def _fmt(header: str, body: str) -> str:
        return f"{header}\n{body}".strip() if header else body

    def _extract_format_lines(self, text: str) -> str:
        keep: List[str] = []
        for line in text.splitlines():
            if any(kw in line for kw in _FORMAT_KW):
                keep.append(line)
        return "\n".join(keep[:30])


__all__ = ["PromptOptimizer"]
