"""Agents Team mode — strongest mode, professional version.

Pipeline:

    1. coordinator.decompose(task)       -> List[RoleSpec]   (LLM-driven)
    2. for each role, in parallel (capped by ToolThrottle):
         worker.run(role)                -> WorkerReport     (ReAct loop)
       Workers see a *shared bus* of facts contributed by earlier-finished
       workers (cooperative), but never see each other's full transcripts.
    3. reviewer.audit(reports)           -> AuditNotes
    4. coordinator.synthesize(reports, audit) -> final answer

Defaults:

- Decomposition: tries the model first; if the model returns nothing
  parseable we fall back to the static planner/researcher/executor/reviewer
  team — exactly the previous behaviour, so this is strictly an upgrade.
- Each worker gets a curated tool subset chosen by ``select_tools_for_task``
  with role keywords (planner / researcher / etc.), respecting the
  ``must_include`` whitelist.
- Concurrency: workers run in a ``ThreadPoolExecutor`` with
  ``max_workers = min(throttle.max_concurrent, len(roles))`` so we never
  exceed mobile-friendly limits.
- Each step is wrapped in a ``Tracer.span`` keyed on the team's trace_id.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.observability import Tracer, new_trace_id
from core.task_router import (
    filter_registry,
    select_skills_for_task,
    select_tools_for_task,
)
from core.throttle import default_throttle


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
@dataclass
class AgentRole:
    name: str
    persona: str
    keywords: List[str] = field(default_factory=list)
    must_include_tools: List[str] = field(default_factory=list)
    max_steps: int = 4
    tools_per_role: int = 10


_DEFAULT_ROLES: List[AgentRole] = [
    AgentRole(
        name="planner",
        persona="أنت مخطط استراتيجي. ضع خطوات واضحة قابلة للتنفيذ.",
        keywords=["plan", "design", "architecture", "خطة"],
    ),
    AgentRole(
        name="researcher",
        persona="أنت باحث. اجمع الحقائق والمصادر من الأدوات المتاحة.",
        keywords=["search", "research", "knowledge", "بحث", "مصادر"],
        must_include_tools=["search_knowledge", "search_skills"],
    ),
    AgentRole(
        name="executor",
        persona="أنت منفذ. شغّل الأوامر، عدّل الملفات، طبّق الـpatches.",
        keywords=["execute", "run", "build", "patch", "edit"],
        must_include_tools=[
            "execute_command", "safe_edit_file",
            "apply_patch", "run_self_check",
        ],
    ),
    AgentRole(
        name="reviewer",
        persona="أنت مراجع نقدي. افحص النتائج، اكتشف الثغرات، اقترح تحسينات.",
        keywords=["review", "audit", "verify", "test", "مراجعة"],
        must_include_tools=["verify_python", "verify_json", "run_self_check"],
    ),
]


# --------------------------------------------------------------------------
# Shared bus
# --------------------------------------------------------------------------
class SharedBus:
    """Append-only fact bus shared between sub-agents.

    Each contribution is timestamped + tagged with the source role.
    Workers read a frozen snapshot at the moment they start their loop;
    if a worker spawns later, it benefits from the earlier facts.
    """

    def __init__(self) -> None:
        self._items: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def post(self, role: str, content: str) -> None:
        if not content:
            return
        with self._lock:
            self._items.append(
                {"ts": time.time(), "role": role, "content": content[:2000]},
            )

    def render(self) -> str:
        with self._lock:
            if not self._items:
                return ""
            lines = []
            for it in self._items:
                lines.append(f"- ({it['role']}) {it['content']}")
            return "\n".join(lines)


# --------------------------------------------------------------------------
# Worker report
# --------------------------------------------------------------------------
@dataclass
class WorkerReport:
    role: str
    answer: str
    tools_used: List[str] = field(default_factory=list)
    duration_ms: float = 0.0
    error: Optional[str] = None
    skills_consulted: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Throttling helpers
# --------------------------------------------------------------------------
def _wrap_with_throttle(tools: Dict[str, Callable], throttle) -> Dict[str, Callable]:
    out: Dict[str, Callable] = {}
    for name, fn in tools.items():
        def _make(n: str, f: Callable):
            def _w(*args, **kwargs):
                return throttle.execute(f, *args, tool_name=n, **kwargs)
            _w.__name__ = getattr(f, "__name__", n)
            _w.__doc__ = getattr(f, "__doc__", "")
            return _w
        out[name] = _make(name, fn)
    return out


# --------------------------------------------------------------------------
# Coordinator: model-driven role decomposition
# --------------------------------------------------------------------------
_DECOMPOSE_RE = re.compile(r"\[[\s\S]*\]")


def _llm_decompose(model: Any, task: str,
                   default_roles: List[AgentRole]) -> List[AgentRole]:
    """Ask the model which roles to spawn.

    Falls back to ``default_roles`` on any parse failure. This is the
    'professional' upgrade the user asked for: the team composition is
    decided per-task, not hard-coded.
    """
    sys_prompt = (
        "أنت منسّق فريق وكلاء (Team Coordinator). اقترح أدواراً مناسبة "
        "لتنفيذ المهمة. ردّك يجب أن يكون JSON صالحاً فقط بالشكل:\n"
        '[{"name":"planner","persona":"...","keywords":["..."]},...]\n'
        "حد أقصى 4 أدوار. كل دور: name (عربي/إنجليزي بسيط)، persona (وصف "
        "مختصر)، keywords (3-6 مفاتيح بحث للأدوات المناسبة)، must_include "
        "(اختياري: قائمة أسماء أدوات محددة)."
    )
    try:
        raw = model([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"المهمة: {task}"},
        ])
    except Exception:
        return default_roles
    m = _DECOMPOSE_RE.search(raw or "")
    if not m:
        return default_roles
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return default_roles
    if not isinstance(data, list) or not data:
        return default_roles
    roles: List[AgentRole] = []
    for d in data[:4]:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        persona = str(d.get("persona") or "").strip()
        if not name or not persona:
            continue
        roles.append(
            AgentRole(
                name=re.sub(r"[^\w\-]+", "_", name)[:32] or "agent",
                persona=persona[:300],
                keywords=[str(k)[:32] for k in (d.get("keywords") or [])][:6],
                must_include_tools=[
                    str(t)[:64] for t in (d.get("must_include") or [])
                ][:6],
            )
        )
    return roles or default_roles


# --------------------------------------------------------------------------
# AgentsTeam
# --------------------------------------------------------------------------
class AgentsTeam:
    """Strongest mode — coordinator + workers + shared bus + reviewer."""

    def __init__(
        self,
        model,
        tool_registry,
        system_awareness=None,
        *,
        roles: Optional[List[AgentRole]] = None,
        max_steps: int = 4,
        max_workers: Optional[int] = None,
        tools_per_role: int = 10,
        throttle=None,
        decompose: bool = True,
        review: bool = True,
    ):
        self.model = model
        self.tool_registry = tool_registry
        self.awareness = system_awareness
        self.fixed_roles = roles
        self.default_roles = _DEFAULT_ROLES
        self.max_steps = max_steps
        self.tools_per_role = tools_per_role
        self.throttle = throttle or default_throttle()
        self.max_workers = max_workers or self.throttle.max_concurrent
        self.decompose = decompose
        self.review = review

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def run(self, task: str) -> str:
        trace_id = new_trace_id()
        with Tracer.trace(trace_id):
            Tracer.emit("team.start", task_preview=task[:120])
            roles = self._select_roles(task)
            bus = SharedBus()
            reports = self._run_workers(task, roles, bus)
            audit = self._review(task, reports) if self.review else ""
            answer = self._synthesize(task, reports, audit, trace_id)
            Tracer.emit("team.end", roles=[r.role for r in reports])
            return answer

    def __call__(self, task: str) -> str:
        return self.run(task)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def _select_roles(self, task: str) -> List[AgentRole]:
        if self.fixed_roles:
            return self.fixed_roles
        if not self.decompose:
            return self.default_roles
        with Tracer.span("team.decompose"):
            return _llm_decompose(self.model, task, self.default_roles)

    def _curate_tools_for(self, role: AgentRole, task: str) -> Dict[str, Callable]:
        registered = set(self.tool_registry.tools.keys())
        if self.awareness is None:
            base = dict(self.tool_registry.tools)
        else:
            bias = " ".join([task] + role.keywords)
            manifests = select_tools_for_task(
                bias, self.awareness,
                k=role.tools_per_role or self.tools_per_role,
                must_include=role.must_include_tools,
            )
            names = [m["name"] for m in manifests if m["name"] in registered]
            if not names:
                names = list(registered)
            base = filter_registry(self.tool_registry, names)
            for name in role.must_include_tools:
                if name in registered and name not in base:
                    base[name] = self.tool_registry.tools[name]
        return _wrap_with_throttle(base, self.throttle)

    def _run_one(self, role: AgentRole, task: str, bus: SharedBus) -> WorkerReport:
        from core.agent_loop import react_loop

        t0 = time.time()
        skills: List[str] = []
        if self.awareness is not None:
            try:
                skills = select_skills_for_task(task, self.awareness, k=4)
            except Exception:
                skills = []

        bus_view = bus.render()
        framing_parts: List[str] = [
            f"[الدور: {role.name}]",
            role.persona,
            f"المهمة: {task}",
        ]
        if skills:
            framing_parts.append(
                "مهارات مرشحة (راجعها عبر `load_skill` عند الحاجة): "
                + ", ".join(skills)
            )
        if bus_view:
            framing_parts.append("ما توصّل إليه باقي الفريق حتى الآن:\n" + bus_view)
        framed = "\n".join(framing_parts)

        tools = self._curate_tools_for(role, task)
        report = WorkerReport(
            role=role.name, answer="",
            tools_used=list(tools.keys()),
            skills_consulted=skills,
        )
        with Tracer.span("team.worker", role=role.name, tools=len(tools)):
            try:
                answer = react_loop(
                    user_input=framed,
                    model=self.model,
                    tools=tools,
                    max_steps=role.max_steps or self.max_steps,
                    system_awareness=self.awareness,
                )
                report.answer = answer
                bus.post(role.name, _summarise_for_bus(answer))
            except Exception as e:  # noqa: BLE001
                report.error = str(e)
                report.answer = f"❌ فشل الدور {role.name}: {e}"
        report.duration_ms = (time.time() - t0) * 1000
        return report

    def _run_workers(self, task: str, roles: List[AgentRole],
                     bus: SharedBus) -> List[WorkerReport]:
        max_workers = max(1, min(self.max_workers, len(roles)))
        reports: List[WorkerReport] = []
        with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(self._run_one, r, task, bus) for r in roles]
            for f in cf.as_completed(futures):
                reports.append(f.result())
        # Stable ordering matches the role list for the synthesizer.
        order = {r.name: i for i, r in enumerate(roles)}
        reports.sort(key=lambda r: order.get(r.role, 9999))
        return reports

    # ------------------------------------------------------------------
    def _review(self, task: str, reports: List[WorkerReport]) -> str:
        if not reports:
            return ""
        bullets = "\n\n".join(
            f"### {r.role}\n{r.answer[:1500]}" for r in reports
        )
        prompt = (
            "أنت مراجع نقدي. اقرأ مخرجات الفريق وحدّد:\n"
            "1) النقاط المتوافقة بين الوكلاء (high confidence).\n"
            "2) التناقضات أو الفجوات.\n"
            "3) ما يحتاج التحقق منه قبل التسليم النهائي.\n"
            "اختصر الرد إلى 5-10 أسطر فقط.\n\n"
            f"المهمة: {task}\n\n{bullets}"
        )
        with Tracer.span("team.review"):
            try:
                return self.model([
                    {"role": "system", "content": "أنت مراجع جودة لفريق وكلاء."},
                    {"role": "user", "content": prompt},
                ])
            except Exception as e:
                return f"(تعذّر إنشاء المراجعة: {e})"

    def _synthesize(self, task: str, reports: List[WorkerReport],
                    audit: str, trace_id: str) -> str:
        bullets = "\n\n".join(
            f"### {r.role} (took {int(r.duration_ms)}ms)\n{r.answer}"
            for r in reports
        )
        prompt = (
            "اجمع الإجابات في ردّ نهائي عملي.\n"
            "- ابدأ بإجابة مباشرة في 2-3 جمل.\n"
            "- ثم تفاصيل (heading per role) عند الحاجة.\n"
            "- إن كانت هناك خطوات يدوية للمستخدم، أوردها كـ checklist.\n\n"
            f"المهمة الأصلية: {task}\n\n{bullets}"
        )
        if audit:
            prompt += f"\n\n## ملاحظات مراجعة الجودة\n{audit}"
        with Tracer.span("team.synthesize"):
            try:
                final = self.model([
                    {"role": "system", "content": "أنت منسّق نهائي لفريق وكلاء."},
                    {"role": "user", "content": prompt},
                ])
            except Exception as e:
                final = f"❌ فشل التجميع: {e}\n\n{bullets}"
        return f"{final}\n\n---\n_team trace: `{trace_id}`_"


# --------------------------------------------------------------------------
def _summarise_for_bus(answer: str, max_len: int = 600) -> str:
    """Strip throttle/observation chatter and keep the substance."""
    if not answer:
        return ""
    text = answer.strip()
    # Take the post-final-answer block when present.
    if "Final Answer:" in text:
        text = text.split("Final Answer:", 1)[1]
    return text[:max_len]


__all__ = ["AgentsTeam", "AgentRole", "SharedBus", "WorkerReport"]
