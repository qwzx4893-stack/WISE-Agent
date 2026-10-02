"""Skill-from-demonstration learner.

Inspired by Accomplish's skill-learning pipeline
(https://github.com/accomplish-ai/accomplish — MIT). All code in this
module is **original** Python; we adopt only the four-stage pattern
``observe → generalize → store → replay``.

Pipeline
--------

1. **observe**: caller submits a ``Demonstration`` describing what
   they did — a goal, ordered ``steps``, observed inputs/outputs, and
   optional tags.
2. **generalize**: produce a generalised, reusable skill description.
   When an LLM is configured, we ask it to draft a concise step-by-
   step procedure with named placeholders for the variable inputs.
   When no LLM is configured we fall back to the demonstration verbatim
   and mark the result ``generalization_skipped=True``.
3. **store**: write the result as a skill folder under
   ``SKILLS_DIR/learned/<slug>/SKILL.md`` (mirroring the existing
   skill-creator convention).
4. **replay**: list / load stored skills via :func:`list_learned` /
   :func:`load_learned`.

Each call is also recorded into the dashboard ``ActivityLog`` so
operators can inspect the cadence and outcomes from the UI.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import SKILLS_DIR

LOG = logging.getLogger("agent_os.learner")


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------
@dataclass
class Demonstration:
    goal: str
    steps: List[str]
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)


@dataclass
class LearnedSkill:
    id: str
    slug: str
    title: str
    path: str
    summary: str
    steps: List[str]
    tags: List[str]
    generalization_skipped: bool
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _learned_root() -> Path:
    root = SKILLS_DIR / "learned"
    root.mkdir(parents=True, exist_ok=True)
    return root


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", text.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "skill"


# ---------------------------------------------------------------------------
# Generalisation
# ---------------------------------------------------------------------------
def _heuristic_generalize(demo: Demonstration) -> Dict[str, Any]:
    """Trivial fallback when no LLM is available: keep the demonstration
    verbatim but normalise step text and surface placeholders for any
    declared inputs."""
    steps = [str(s).strip() for s in demo.steps if str(s).strip()]
    if demo.inputs:
        intro = ("Inputs: " + ", ".join(f"<{k}>" for k in demo.inputs.keys())
                 + ".")
        steps = [intro] + steps
    summary = demo.goal.strip() or "Learned skill"
    return {"title": summary, "summary": summary, "steps": steps,
             "generalization_skipped": True}


def _llm_generalize(demo: Demonstration) -> Optional[Dict[str, Any]]:
    """Best-effort LLM generalisation. Returns ``None`` if no provider
    is wired up so callers can fall back to the heuristic path."""
    try:
        from .llm.universal import UniversalLLM  # type: ignore
        try:
            from .llm.keystore import KeyStore
        except Exception:
            KeyStore = None  # type: ignore
    except Exception:
        return None
    try:
        # Only proceed when at least one provider key is actually set.
        if KeyStore is not None:
            store = KeyStore()
            keys = store.list(reveal=False) if hasattr(store, "list") else {}
            if not any(keys.values()):
                return None
        llm = UniversalLLM()
    except Exception:
        return None

    prompt = (
        "You are turning a single demonstration into a reusable skill.\n"
        "Goal: " + demo.goal.strip() + "\n"
        "Demonstrated steps (in order):\n"
        + "\n".join(f"- {s}" for s in demo.steps) + "\n"
        "Variable inputs that should become placeholders: "
        + (", ".join(demo.inputs.keys()) or "(none)") + "\n\n"
        "Reply with strict JSON: "
        '{"title": "...", "summary": "...", "steps": ["..."]}.'
    )
    try:
        raw = llm.generate(prompt) if hasattr(llm, "generate") else None
        if not raw:
            return None
        # Extract a JSON object from the reply.
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return None
        parsed = json.loads(match.group(0))
        steps = [str(s).strip() for s in parsed.get("steps") or []
                  if str(s).strip()]
        if not steps:
            return None
        return {
            "title": parsed.get("title") or demo.goal,
            "summary": parsed.get("summary") or demo.goal,
            "steps": steps,
            "generalization_skipped": False,
        }
    except Exception as e:
        LOG.warning("llm generalization failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public pipeline
# ---------------------------------------------------------------------------
def learn_from_demonstration(demo_dict: Dict[str, Any]) -> LearnedSkill:
    """Run the full pipeline and persist the resulting skill."""
    demo = Demonstration(
        goal=str(demo_dict.get("goal", "")).strip(),
        steps=[str(s) for s in demo_dict.get("steps") or []],
        inputs=dict(demo_dict.get("inputs") or {}),
        outputs=dict(demo_dict.get("outputs") or {}),
        tags=[str(t) for t in demo_dict.get("tags") or []],
    )
    if not demo.goal:
        raise ValueError("demonstration is missing 'goal'")
    if not demo.steps:
        raise ValueError("demonstration is missing 'steps'")

    generalized = _llm_generalize(demo) or _heuristic_generalize(demo)
    title = generalized["title"]
    summary = generalized["summary"]
    steps = generalized["steps"]

    sid = uuid.uuid4().hex[:10]
    slug = _slugify(title) + "-" + sid[:6]
    folder = _learned_root() / slug
    folder.mkdir(parents=True, exist_ok=True)

    md = ["# " + title, "",
            f"<!-- learned skill, generalization_skipped="
            f"{generalized['generalization_skipped']} -->",
            "", "## Summary", "", summary, "", "## Steps", ""]
    for i, s in enumerate(steps, start=1):
        md.append(f"{i}. {s}")
    if demo.inputs:
        md += ["", "## Inputs", ""]
        for k, v in demo.inputs.items():
            md.append(f"- `<{k}>` (example: `{v}`)")
    (folder / "SKILL.md").write_text("\n".join(md) + "\n",
                                        encoding="utf-8")
    (folder / "demo.json").write_text(
        json.dumps({"id": sid, **asdict(demo)}, ensure_ascii=False,
                    indent=2), encoding="utf-8")

    learned = LearnedSkill(
        id=sid, slug=slug, title=title,
        path=str(folder),
        summary=summary, steps=steps, tags=demo.tags,
        generalization_skipped=generalized["generalization_skipped"],
        created_at=time.time())

    try:
        from .dashboard import ActivityLog
        ActivityLog().record(
            "system", actor="learner",
            summary=f"learned skill: {title}",
            status="success",
            payload={"slug": slug,
                      "generalization_skipped":
                          learned.generalization_skipped})
    except Exception:
        pass
    return learned


def list_learned() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    root = _learned_root()
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        skill_md = folder / "SKILL.md"
        demo_json = folder / "demo.json"
        if not skill_md.exists():
            continue
        try:
            demo = (json.loads(demo_json.read_text(encoding="utf-8"))
                     if demo_json.exists() else {})
        except Exception:
            demo = {}
        out.append({
            "slug": folder.name,
            "path": str(folder),
            "id": demo.get("id", ""),
            "title": demo.get("goal") or folder.name,
            "tags": demo.get("tags", []),
        })
    return out


def load_learned(slug: str) -> Optional[Dict[str, Any]]:
    folder = _learned_root() / slug
    if not folder.is_dir():
        return None
    skill_md = folder / "SKILL.md"
    demo_json = folder / "demo.json"
    return {
        "slug": slug,
        "skill_md": skill_md.read_text(encoding="utf-8")
                       if skill_md.exists() else "",
        "demo": (json.loads(demo_json.read_text(encoding="utf-8"))
                  if demo_json.exists() else {}),
    }


__all__ = [
    "Demonstration", "LearnedSkill",
    "learn_from_demonstration", "list_learned", "load_learned",
]
