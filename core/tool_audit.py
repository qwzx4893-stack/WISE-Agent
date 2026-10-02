"""Tool Manifest Auditor
==========================

Validates and (optionally) repairs the JSON manifests under
``tools/packs/*.json`` so the rest of Agent OS can rely on a clean
schema.

The auditor checks:

* JSON parses, top-level is a list of tool dicts.
* Required fields are present and non-empty:
  ``name``, ``version``, ``category``, ``implementation_type``,
  ``description``, ``capabilities``, ``use_cases``, ``dependencies``.
* ``capabilities`` has 3-8 entries; ``use_cases`` has 2-5 entries.
* For ``implementation_type=='cli'`` the ``cli_command`` (or
  ``cli_binary``) field is parseable.
* No two manifests declare the same ``name`` unless the older one is
  flagged with ``replaces`` pointing at the newer ``version``.

The :func:`run` entrypoint returns a structured report. Passing
``apply=True`` writes the safe auto-fixes back to disk in place:

* Default empty ``dependencies`` to ``[]``.
* Pad short ``use_cases`` / ``capabilities`` lists by deriving entries
  from ``description`` and ``category`` (heuristic, marked with the
  ``needs_review`` tag).
* Trim oversize lists to the upper bound.
* Mark duplicates: keep the newest ``version`` as canonical, set
  ``replaces`` on older copies pointing at the canonical version.
"""

from __future__ import annotations

import json
import re
import shlex
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .paths import TOOLS_PACKS_DIR


REQUIRED_FIELDS = (
    "name", "version", "category", "implementation_type",
    "description", "capabilities", "use_cases", "dependencies",
)
CAPABILITIES_MIN, CAPABILITIES_MAX = 3, 8
USE_CASES_MIN, USE_CASES_MAX = 2, 5

# Standard categories accepted in the schema. Anything else is reported
# but not auto-rewritten (we don't want to erase intent).
STANDARD_CATEGORIES = {
    "Execution", "Knowledge", "Memory", "Communication",
    "Orchestration", "Database", "DevOps", "Security",
    "Web3", "Frontend", "Backend", "Mobile", "AI",
    "DataScience", "Business", "SelfModify", "Planning",
    "Testing", "Search", "Assistant",
}


# --------------------------------------------------------------------
# Issue dataclass
# --------------------------------------------------------------------
@dataclass
class AuditIssue:
    pack: str
    name: str
    code: str
    detail: str = ""
    fixed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
_SEMVER_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _semver_tuple(s: str) -> Tuple[int, int, int]:
    m = _SEMVER_RE.match(str(s or ""))
    if not m:
        return (0, 0, 0)
    return tuple(int(g or 0) for g in m.groups())  # type: ignore[return-value]


def _sentence_words(text: str) -> List[str]:
    return [w for w in re.split(r"[^A-Za-z0-9_\-]+", text or "") if w]


def _derive_capabilities(tool: Dict[str, Any]) -> List[str]:
    """Heuristic capability list when the manifest left it empty."""
    desc = (tool.get("description") or "").lower()
    name = (tool.get("name") or "").lower().replace("_", " ").replace(
        "-", " ")
    cat = (tool.get("category") or "").lower()
    seeds: List[str] = []
    if "scan" in desc or "scan" in name:
        seeds += ["scan target", "report findings"]
    if "search" in desc or "search" in name:
        seeds += ["search", "rank results"]
    if "read" in desc or "read" in name:
        seeds += ["read input", "return content"]
    if "write" in desc or "write" in name:
        seeds += ["write output", "persist data"]
    if "deploy" in desc or "deploy" in name:
        seeds += ["deploy artifact", "rollback"]
    if "build" in desc or "build" in name:
        seeds += ["build artifact", "produce output"]
    if "test" in desc or "test" in name:
        seeds += ["run tests", "report results"]
    if "audit" in desc or "audit" in name:
        seeds += ["audit input", "report findings"]
    if "fuzz" in desc or "fuzz" in name:
        seeds += ["fuzz inputs", "find regressions"]
    if cat:
        seeds.append(cat)
    seeds.append(tool.get("name") or "tool")
    # De-duplicate, keep order
    seen = set()
    out: List[str] = []
    for s in seeds:
        s = s.strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:CAPABILITIES_MAX]


def _derive_use_cases(tool: Dict[str, Any]) -> List[str]:
    desc = (tool.get("description") or "").strip()
    name = tool.get("name") or "tool"
    cat = (tool.get("category") or "").lower()
    bases: List[str] = []
    if desc:
        bases.append(desc.rstrip("."))
    bases.append(f"automate {name} operations in agent workflows")
    if cat:
        bases.append(f"integrate {name} into {cat} pipelines")
    bases.append(f"call {name} as a tool inside a multi-step plan")
    seen: set = set()
    out: List[str] = []
    for b in bases:
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out[:USE_CASES_MAX]


def _cli_first_token(cmd: str) -> str:
    if not cmd:
        return ""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    for p in parts:
        if "=" in p and not p.startswith("-"):
            # ENV=val prefix — skip
            continue
        return p
    return ""


# --------------------------------------------------------------------
# Auditor
# --------------------------------------------------------------------
class ToolAuditor:
    """Audit + (optionally) fix the manifest packs in place."""

    def __init__(self,
                 packs_dir: Optional[Path] = None) -> None:
        self.packs_dir = Path(packs_dir) if packs_dir else TOOLS_PACKS_DIR

    # ---------------------------- API
    def run(self, apply: bool = False) -> Dict[str, Any]:
        packs: Dict[str, List[Dict[str, Any]]] = {}
        issues: List[AuditIssue] = []
        if not self.packs_dir.exists():
            return {
                "packs_dir": str(self.packs_dir),
                "total_tools": 0, "unique_tools": 0,
                "issues": [], "fixed_count": 0,
                "by_code": {},
            }
        for f in sorted(self.packs_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                issues.append(AuditIssue(
                    pack=f.name, name="<file>",
                    code="json_parse_error",
                    detail=str(e)))
                continue
            if not isinstance(data, list):
                issues.append(AuditIssue(
                    pack=f.name, name="<file>",
                    code="not_a_list",
                    detail=f"top-level type is {type(data).__name__}"))
                continue
            packs[f.name] = data

        # Per-tool validation
        all_tools: List[Tuple[str, Dict[str, Any]]] = []
        for pack_name, tools in packs.items():
            for idx, tool in enumerate(tools):
                if not isinstance(tool, dict):
                    issues.append(AuditIssue(
                        pack=pack_name, name=f"<idx-{idx}>",
                        code="entry_not_a_dict"))
                    continue
                self._validate_tool(pack_name, tool, issues, apply)
                all_tools.append((pack_name, tool))

        # Cross-pack duplicates
        by_name: Dict[str, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
        for pack_name, tool in all_tools:
            n = tool.get("name")
            if n:
                by_name[n].append((pack_name, tool))
        for n, group in by_name.items():
            if len(group) <= 1:
                continue
            # newest version wins
            group_sorted = sorted(
                group,
                key=lambda x: _semver_tuple(x[1].get("version", "")),
                reverse=True)
            canonical_pack, canonical = group_sorted[0]
            canonical_ver = canonical.get("version", "")
            for pack_name, tool in group_sorted[1:]:
                detail = (f"superseded by {canonical_pack}@"
                          f"{canonical_ver}")
                fixed = False
                if apply and tool.get("replaces") != canonical_ver:
                    tool["replaces"] = canonical_ver
                    fixed = True
                issues.append(AuditIssue(
                    pack=pack_name, name=n,
                    code="duplicate",
                    detail=detail, fixed=fixed))

        if apply:
            for pack_name, tools in packs.items():
                target = self.packs_dir / pack_name
                try:
                    target.write_text(
                        json.dumps(tools, indent=2, ensure_ascii=False)
                        + "\n",
                        encoding="utf-8")
                except Exception as e:
                    issues.append(AuditIssue(
                        pack=pack_name, name="<file>",
                        code="write_error", detail=str(e)))

        unique = len(by_name)
        fixed_count = sum(1 for i in issues if i.fixed)
        by_code = Counter(i.code for i in issues)
        return {
            "packs_dir": str(self.packs_dir),
            "total_tools": sum(len(v) for v in packs.values()),
            "unique_tools": unique,
            "applied": bool(apply),
            "fixed_count": fixed_count,
            "issues": [i.to_dict() for i in issues],
            "by_code": dict(by_code),
        }

    # ---------------------------- internals
    def _validate_tool(self,
                       pack_name: str,
                       tool: Dict[str, Any],
                       issues: List[AuditIssue],
                       apply: bool) -> None:
        name = tool.get("name") or "<unnamed>"
        # Required fields presence. ``dependencies`` may legitimately be
        # an empty list (built-ins), so we treat ``[]`` as valid there.
        for fld in REQUIRED_FIELDS:
            value = tool.get(fld)
            present = fld in tool
            if fld == "dependencies":
                missing = not present or value is None
            else:
                missing = (not present
                           or value in (None, "", []))
            if not missing:
                continue
            fixed = False
            if apply:
                if fld == "dependencies":
                    tool["dependencies"] = []
                    fixed = True
                elif fld == "capabilities":
                    tool["capabilities"] = _derive_capabilities(tool)
                    if "needs_review" not in tool:
                        tool["needs_review"] = []
                    if "capabilities" not in tool["needs_review"]:
                        tool["needs_review"].append("capabilities")
                    fixed = True
                elif fld == "use_cases":
                    tool["use_cases"] = _derive_use_cases(tool)
                    if "needs_review" not in tool:
                        tool["needs_review"] = []
                    if "use_cases" not in tool["needs_review"]:
                        tool["needs_review"].append("use_cases")
                    fixed = True
                # ``category`` and other free-form fields are NOT
                # auto-filled — we don't want to invent intent.
            issues.append(AuditIssue(
                pack=pack_name, name=name,
                code="missing_field",
                detail=fld, fixed=fixed))
        # capability bounds
        cap = tool.get("capabilities") or []
        if isinstance(cap, list):
            if cap and len(cap) < CAPABILITIES_MIN:
                fixed = False
                if apply:
                    seeds = _derive_capabilities(tool)
                    merged = list(cap)
                    for s in seeds:
                        if s not in merged:
                            merged.append(s)
                        if len(merged) >= CAPABILITIES_MIN:
                            break
                    tool["capabilities"] = merged
                    if "needs_review" not in tool:
                        tool["needs_review"] = []
                    if "capabilities" not in tool["needs_review"]:
                        tool["needs_review"].append("capabilities")
                    fixed = True
                issues.append(AuditIssue(
                    pack=pack_name, name=name,
                    code="capabilities_too_few",
                    detail=f"have={len(cap)} min={CAPABILITIES_MIN}",
                    fixed=fixed))
            if len(cap) > CAPABILITIES_MAX:
                fixed = False
                if apply:
                    tool["capabilities"] = cap[:CAPABILITIES_MAX]
                    fixed = True
                issues.append(AuditIssue(
                    pack=pack_name, name=name,
                    code="capabilities_too_many",
                    detail=f"have={len(cap)} max={CAPABILITIES_MAX}",
                    fixed=fixed))
        # use_cases bounds
        uc = tool.get("use_cases") or []
        if isinstance(uc, list):
            if uc and len(uc) < USE_CASES_MIN:
                fixed = False
                if apply:
                    seeds = _derive_use_cases(tool)
                    merged = list(uc)
                    for s in seeds:
                        if s not in merged:
                            merged.append(s)
                        if len(merged) >= USE_CASES_MIN:
                            break
                    tool["use_cases"] = merged
                    if "needs_review" not in tool:
                        tool["needs_review"] = []
                    if "use_cases" not in tool["needs_review"]:
                        tool["needs_review"].append("use_cases")
                    fixed = True
                issues.append(AuditIssue(
                    pack=pack_name, name=name,
                    code="use_cases_too_few",
                    detail=f"have={len(uc)} min={USE_CASES_MIN}",
                    fixed=fixed))
            if len(uc) > USE_CASES_MAX:
                fixed = False
                if apply:
                    tool["use_cases"] = uc[:USE_CASES_MAX]
                    fixed = True
                issues.append(AuditIssue(
                    pack=pack_name, name=name,
                    code="use_cases_too_many",
                    detail=f"have={len(uc)} max={USE_CASES_MAX}",
                    fixed=fixed))
        # category against the standard set
        cat = tool.get("category")
        if cat and cat not in STANDARD_CATEGORIES:
            issues.append(AuditIssue(
                pack=pack_name, name=name,
                code="non_standard_category",
                detail=str(cat)))
        # CLI command sanity
        if tool.get("implementation_type") == "cli":
            cmd = tool.get("cli_command") or tool.get("cli_binary") or ""
            if not cmd:
                issues.append(AuditIssue(
                    pack=pack_name, name=name,
                    code="cli_command_missing"))
            else:
                first = _cli_first_token(cmd)
                # ``{placeholder}`` as the entire first token is OK — it
                # marks generic runners (e.g. run_shell). Anything else
                # must look like a binary path.
                placeholder = bool(re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}",
                                                  first))
                if not placeholder and not re.match(
                        r"^[A-Za-z0-9_./\-]+$", first):
                    issues.append(AuditIssue(
                        pack=pack_name, name=name,
                        code="cli_command_unparseable",
                        detail=f"first_token={first!r}"))


def run_audit(apply: bool = False,
              packs_dir: Optional[Path] = None) -> Dict[str, Any]:
    return ToolAuditor(packs_dir=packs_dir).run(apply=apply)


__all__ = [
    "AuditIssue", "ToolAuditor", "run_audit",
    "REQUIRED_FIELDS", "STANDARD_CATEGORIES",
]
