"""Robust extractor for ReAct outputs.

Handles all the failure modes we've actually seen in the wild:

- ``Action: search-tool`` / ``Action: search.tool`` (dotted/dashed names).
- ``Action Input:`` followed by a fenced ```json ... ``` block.
- ``Action Input:`` with single quotes, trailing commas, or smart quotes.
- ``"name": "search_knowledge"`` style (model writes the args as a JSON
  object with ``name`` + ``arguments`` keys).
- ``Action: searchKnowledge`` while the registry holds ``search_knowledge``
  (camelCase ↔ snake_case, missing/extra underscores, dashes ↔ underscores).
- ``Final Answer`` mixed in the same response — we still extract any
  pending tool calls before the final answer line.

The parser never raises. Each :class:`ParsedCall` carries an ``error``
field if anything went wrong so the engine can surface it as an
Observation.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ----------------------------------------------------------------------
# Regexes
# ----------------------------------------------------------------------
# We accept identifiers with letters, digits, underscores, dashes, dots.
_ACTION_HEADER = re.compile(
    r"Action\s*[:\-]?\s*(?P<tool>[\w\-./]+)"
    r"(?:[ \t]*\r?\n)+"
    r"Action\s*Input\s*[:\-]?\s*",
    re.IGNORECASE,
)


def _extract_args_block(text: str, start: int) -> tuple[str, int]:
    """Pull the next args block starting at ``text[start:]``.

    Handles fenced ``` blocks, bare JSON objects/arrays with balanced
    braces, and bare quoted strings. Returns ``(args_text, end_index)``.
    """
    s = text[start:]
    s_lstripped = s.lstrip()
    offset = len(s) - len(s_lstripped)
    if not s_lstripped:
        return "", start
    if s_lstripped.startswith("```"):
        m = re.match(r"```(?:json|json5|js|javascript)?\s*([\s\S]*?)\s*```",
                     s_lstripped)
        if m:
            return m.group(0), start + offset + m.end()
    first = s_lstripped[0]
    if first in "{[":
        close = "}" if first == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(s_lstripped):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == first:
                depth += 1
            elif ch == close:
                depth -= 1
                if depth == 0:
                    return s_lstripped[: i + 1], start + offset + i + 1
        # Unbalanced — return everything until newline-newline as best-effort.
        return s_lstripped.split("\n\n", 1)[0], start + len(s)
    if first in "\"'":
        m = re.match(r"(['\"])(?:\\.|(?!\1).)*\1", s_lstripped)
        if m:
            return m.group(0), start + offset + m.end()
    # Fallback: take until a blank line or known-keyword line.
    end_match = re.search(
        r"\n\s*(?:Observation|Action|Thought|Final\s*Answer|$)",
        s_lstripped,
    )
    cut = end_match.start() if end_match else len(s_lstripped)
    return s_lstripped[:cut].rstrip(), start + offset + cut

_FINAL_ANSWER = re.compile(r"Final\s*Answer\s*[:\-]\s*(.*)",
                           re.IGNORECASE | re.DOTALL)

_THOUGHT = re.compile(r"Thought\s*[:\-]\s*(.+?)(?=\n\s*(?:Action|Final|$))",
                      re.IGNORECASE | re.DOTALL)


# ----------------------------------------------------------------------
@dataclass
class ParsedCall:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    raw_args: str = ""
    error: Optional[str] = None
    fuzzy_from: Optional[str] = None  # original (unresolved) name when fuzzy-matched


# ----------------------------------------------------------------------
def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json|json5|js|javascript)?\s*([\s\S]*?)\s*```$", text)
    if m:
        return m.group(1).strip()
    return text


def _normalize_quotes(text: str) -> str:
    return (
        text.replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2018", "'").replace("\u2019", "'")
    )


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _try_json(text: str) -> Tuple[Optional[Any], Optional[str]]:
    """Try ``json.loads`` with a few defensive cleanups."""
    if not text:
        return None, "empty"
    cleaned = _strip_fence(text)
    cleaned = _normalize_quotes(cleaned)
    cleaned = _strip_trailing_commas(cleaned)
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError as e:
        # Try Python-literal fallback (handles single quotes).
        try:
            import ast
            return ast.literal_eval(cleaned), None
        except Exception:
            return None, f"json: {e.msg}"


def _normalize_tool_name(name: str) -> str:
    n = unicodedata.normalize("NFKC", name).strip()
    # Drop surrounding quotes if present.
    n = n.strip("'\"`")
    # Collapse internal whitespace.
    n = re.sub(r"\s+", "_", n)
    return n


def _candidates_for(name: str) -> List[str]:
    """Generate a small set of name variants to try against the registry."""
    n = _normalize_tool_name(name)
    out = {n}
    out.add(n.lower())
    # camelCase → snake_case
    out.add(re.sub(r"(?<!^)(?=[A-Z])", "_", n).lower())
    # dashes ↔ underscores
    out.add(n.replace("-", "_"))
    out.add(n.replace("_", "-"))
    out.add(n.lower().replace("-", "_"))
    out.add(n.lower().replace("_", "-"))
    # collapse repeated separators
    out.add(re.sub(r"[-_]+", "_", n).lower())
    out.add(re.sub(r"[-_]+", "-", n).lower())
    return [v for v in out if v]


# ----------------------------------------------------------------------
class ToolCallParser:
    def __init__(self, registry_names: Iterable[str]):
        self._names = list(registry_names)
        self._lc_index: Dict[str, str] = {n.lower(): n for n in self._names}
        self._norm_index: Dict[str, str] = {
            re.sub(r"[-_]+", "_", n.lower()): n for n in self._names
        }

    # ------------------------------------------------------------------
    def resolve_name(self, raw: str) -> Tuple[str, Optional[str]]:
        """Return (resolved_name, fuzzy_origin_or_None)."""
        if not raw:
            return raw, None
        if raw in self._lc_index.values():
            return raw, None
        for cand in _candidates_for(raw):
            if cand in self._lc_index:
                resolved = self._lc_index[cand]
                return resolved, (raw if resolved != raw else None)
            if cand in self._norm_index:
                resolved = self._norm_index[cand]
                return resolved, (raw if resolved != raw else None)
        # Difflib fuzzy fallback.
        match = get_close_matches(
            raw.lower(), list(self._lc_index.keys()), n=1, cutoff=0.82,
        )
        if match:
            resolved = self._lc_index[match[0]]
            return resolved, raw
        return raw, None

    # ------------------------------------------------------------------
    def extract_thought(self, response: str) -> str:
        m = _THOUGHT.search(response or "")
        return (m.group(1).strip() if m else "")[:600]

    def extract_final_answer(self, response: str) -> Optional[str]:
        m = _FINAL_ANSWER.search(response or "")
        if not m:
            return None
        return m.group(1).strip()

    def extract_calls(self, response: str) -> List[ParsedCall]:
        if not response:
            return []
        calls: List[ParsedCall] = []
        pos = 0
        while True:
            m = _ACTION_HEADER.search(response, pos)
            if not m:
                break
            raw_name = m.group("tool").strip()
            raw_args, end_idx = _extract_args_block(response, m.end())
            pos = max(end_idx, m.end() + 1)
            raw_args = raw_args.strip()
            resolved, fuzzy = self.resolve_name(raw_name)
            args, err = self._parse_args(raw_args)
            # Special case: model emitted {"name": "...", "arguments": {...}}
            if isinstance(args, dict) and "name" in args and "arguments" in args:
                inner = args.get("arguments") or {}
                inner_name = str(args.get("name") or "")
                if inner_name and inner_name != resolved:
                    inner_resolved, inner_fuzzy = self.resolve_name(inner_name)
                    if inner_resolved in self._lc_index.values():
                        resolved = inner_resolved
                        fuzzy = fuzzy or inner_fuzzy
                if isinstance(inner, dict):
                    args = inner
            calls.append(ParsedCall(
                tool=resolved,
                args=args if isinstance(args, dict) else {"value": args},
                raw_args=raw_args,
                error=err,
                fuzzy_from=fuzzy,
            ))
        return calls

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_args(raw_args: str) -> Tuple[Any, Optional[str]]:
        if not raw_args:
            return {}, None
        s = raw_args.strip()
        # Strip surrounding bare quotes ("key"="value" form not handled).
        if (s.startswith('"') and s.endswith('"')) or (
            s.startswith("'") and s.endswith("'")
        ):
            return s[1:-1], None
        data, err = _try_json(s)
        if data is None:
            return {"_raw": s[:300]}, err
        return data, None


__all__ = ["ToolCallParser", "ParsedCall"]
