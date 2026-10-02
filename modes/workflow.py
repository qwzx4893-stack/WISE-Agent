"""Workflow mode — pro-grade DAG planner & executor.

Improvements over the previous draft:

- **Strict plan schema** validated by pydantic. The model gets ONE retry
  with the validation error if its first answer doesn't parse.
- **Per-step controls**: ``timeout`` (s), ``retries``, ``retry_backoff``,
  ``on_failure`` ∈ ``{stop, continue, fallback}`` with a ``fallback_step``
  reference, and an optional ``when`` boolean expression evaluated
  against the scratchpad.
- **Status tracking**: every step transitions ``pending → running →
  ok | failed | skipped`` and the final answer ships the trail.
- **Shared scratchpad**: each step output is stored under
  ``scratchpad[step.id]`` (full string) and ``{{step_id}}`` placeholders
  resolve there. ``{{step_id.json.field}}`` allows JSON drilling when
  the previous step produced JSON.
- **Tool whitelist guard**: planner is told the exact tool list, but
  we also enforce it at execution: unknown tools mark the step
  ``failed`` instead of silently routing through the CLI fallback.
- **Throttle + observability**: every step is wrapped in a
  ``Tracer.span("workflow.step", …)`` and runs through ``ToolThrottle``.

Recurring plans persist as before; one-time runs never touch the disk.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.observability import Tracer, new_trace_id
from core.paths import MEMORY_DIR
from core.task_router import select_skills_for_task, select_tools_for_task
from core.throttle import default_throttle


WORKFLOWS_DIR = Path(MEMORY_DIR) / "workflows"
WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Schema (pydantic if available, otherwise defensive parsing).
# --------------------------------------------------------------------------
try:  # pydantic v2
    from pydantic import BaseModel, Field, ValidationError, field_validator

    _HAS_PYDANTIC = True

    class StepModel(BaseModel):
        id: str = Field(..., min_length=1, max_length=64)
        tool: str = Field(..., min_length=1, max_length=128)
        args: Dict[str, Any] = Field(default_factory=dict)
        deps: List[str] = Field(default_factory=list)
        description: str = ""
        timeout: float = Field(60.0, ge=1.0, le=600.0)
        retries: int = Field(0, ge=0, le=5)
        retry_backoff: float = Field(1.5, ge=0.0, le=60.0)
        on_failure: str = Field("stop", pattern="^(stop|continue|fallback)$")
        fallback_step: Optional[str] = None
        when: Optional[str] = None  # python-ish boolean expr over scratchpad

        @field_validator("id")
        @classmethod
        def _v_id(cls, v: str) -> str:
            if not re.match(r"^[\w\-]+$", v):
                raise ValueError("id must match [\\w-]+")
            return v

    class PlanModel(BaseModel):
        name: str = Field(..., min_length=1, max_length=120)
        description: str = ""
        steps: List[StepModel] = Field(..., min_length=1)
except Exception:  # pragma: no cover — pydantic is in requirements.txt
    _HAS_PYDANTIC = False
    BaseModel = object  # type: ignore
    ValidationError = Exception  # type: ignore


# --------------------------------------------------------------------------
# Internal dataclasses (live runtime view of the plan)
# --------------------------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class Step:
    id: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    deps: List[str] = field(default_factory=list)
    description: str = ""
    timeout: float = 60.0
    retries: int = 0
    retry_backoff: float = 1.5
    on_failure: str = "stop"
    fallback_step: Optional[str] = None
    when: Optional[str] = None
    status: str = STATUS_PENDING
    attempts: int = 0
    duration_ms: float = 0.0
    output: str = ""
    error: Optional[str] = None


@dataclass
class Plan:
    id: str
    name: str
    description: str
    task: str
    steps: List[Step]
    recurring: bool = False
    created_at: float = field(default_factory=time.time)
    trace_id: str = field(default_factory=new_trace_id)

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "task": self.task,
            "recurring": self.recurring,
            "created_at": self.created_at,
            "trace_id": self.trace_id,
            "steps": [
                {
                    "id": s.id, "tool": s.tool, "args": s.args, "deps": s.deps,
                    "description": s.description, "timeout": s.timeout,
                    "retries": s.retries, "retry_backoff": s.retry_backoff,
                    "on_failure": s.on_failure, "fallback_step": s.fallback_step,
                    "when": s.when,
                }
                for s in self.steps
            ],
        }

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Plan":
        steps: List[Step] = []
        for s in d.get("steps", []):
            steps.append(Step(
                id=str(s["id"]), tool=str(s["tool"]),
                args=dict(s.get("args") or {}),
                deps=list(s.get("deps") or []),
                description=str(s.get("description", "")),
                timeout=float(s.get("timeout", 60.0)),
                retries=int(s.get("retries", 0)),
                retry_backoff=float(s.get("retry_backoff", 1.5)),
                on_failure=str(s.get("on_failure", "stop")),
                fallback_step=s.get("fallback_step"),
                when=s.get("when"),
            ))
        return cls(
            id=d["id"], name=d.get("name", ""),
            description=d.get("description", ""),
            task=d.get("task", ""), steps=steps,
            recurring=bool(d.get("recurring", False)),
            created_at=d.get("created_at", time.time()),
            trace_id=d.get("trace_id") or new_trace_id(),
        )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def save_plan(plan: Plan) -> Path:
    path = WORKFLOWS_DIR / f"{plan.id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(plan.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def load_plan(plan_id: str) -> Optional[Plan]:
    path = WORKFLOWS_DIR / f"{plan_id}.json"
    if not path.exists():
        return None
    return Plan.from_json(json.loads(path.read_text(encoding="utf-8")))


def list_plans() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "id": d.get("id"), "name": d.get("name"),
                "recurring": d.get("recurring", False),
                "steps": len(d.get("steps", [])),
                "created_at": d.get("created_at"),
            })
        except Exception:
            continue
    return out


def delete_plan(plan_id: str) -> bool:
    path = WORKFLOWS_DIR / f"{plan_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# --------------------------------------------------------------------------
# Plan parsing
# --------------------------------------------------------------------------
_PLAN_RE = re.compile(r"\{[\s\S]*\}")


def _system_prompt(tool_lines: str, skill_lines: str) -> str:
    return (
        "أنت مهندس Workflow محترف. صمّم خطة DAG محكمة لتنفيذ مهمة المستخدم باستخدام الأدوات المتاحة فقط.\n\n"
        "المخطط (JSON صارم - بدون أي شرح خارج الـJSON):\n"
        '{\n'
        '  "name": "<عنوان قصير>",\n'
        '  "description": "<وصف مختصر>",\n'
        '  "steps": [\n'
        '    {\n'
        '      "id": "s1",                       // معرّف فريد [a-zA-Z0-9_-]+\n'
        '      "tool": "<اسم أداة من القائمة>",\n'
        '      "args": {<معطيات الأداة>},\n'
        '      "deps": [<ids للخطوات السابقة>],\n'
        '      "description": "<شرح الخطوة>",\n'
        '      "timeout": 60,                    // ثوانٍ - افتراضي 60\n'
        '      "retries": 1,                     // 0 إلى 5\n'
        '      "on_failure": "stop"              // stop | continue | fallback\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        "قواعد:\n"
        "- استعمل فقط الأدوات الموجودة في القائمة. استخدام أداة غير موجودة سيُرفض.\n"
        "- اربط الخطوات بـ`deps` عندما تعتمد إحداها على ناتج أخرى.\n"
        "- يمكنك تمرير ناتج خطوة سابقة عبر `{{step_id}}` داخل قيم الـargs.\n"
        "- كرّر `id` ممنوع. الدورات (cycles) ممنوعة.\n"
        "- اجعل الخطة مختصرة: 2-8 خطوات في الغالب.\n\n"
        f"الأدوات المتاحة:\n{tool_lines}\n\n"
        f"المهارات المرشحة (للاستلهام، لا للاستدعاء المباشر):\n{skill_lines}\n"
    )


def _extract_json(raw: str) -> Optional[str]:
    if not raw:
        return None
    # Prefer fenced ```json blocks.
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = _PLAN_RE.search(raw)
    return m.group(0) if m else None


def _validate_plan(data: Dict[str, Any], allowed_tools: set[str]) -> Tuple[Optional[PlanModel], List[str]]:  # type: ignore[name-defined]
    errors: List[str] = []
    if not _HAS_PYDANTIC:
        # Soft validation
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            errors.append("الجذر يجب أن يكون كائن JSON يحوي steps[]")
            return None, errors
        ids = [s.get("id") for s in data["steps"] if isinstance(s, dict)]
        if len(ids) != len(set(ids)):
            errors.append("معرّفات الخطوات (id) متكررة")
        for s in data["steps"]:
            t = s.get("tool")
            if t and allowed_tools and t not in allowed_tools:
                errors.append(f"أداة غير مسموحة: {t}")
        return (data, errors) if not errors else (None, errors)  # type: ignore[return-value]

    try:
        plan_model = PlanModel.model_validate(data)
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        return None, errors

    ids = [s.id for s in plan_model.steps]
    if len(ids) != len(set(ids)):
        errors.append("معرّفات الخطوات (id) متكررة")
    for s in plan_model.steps:
        if allowed_tools and s.tool not in allowed_tools:
            errors.append(f"أداة غير مسموحة: '{s.tool}' (ليست في القائمة)")
        for d in s.deps:
            if d not in ids:
                errors.append(f"الخطوة '{s.id}' تعتمد على '{d}' غير موجود")
        if s.fallback_step and s.fallback_step not in ids:
            errors.append(f"الخطوة '{s.id}' fallback_step '{s.fallback_step}' غير موجود")
    if errors:
        return None, errors
    return plan_model, []


def _llm_plan(
    model: Any,
    task: str,
    candidate_tools: List[Dict[str, Any]],
    candidate_skills: List[str],
    *,
    max_retries: int = 1,
) -> Plan:
    allowed = {t["name"] for t in candidate_tools if t.get("name")}
    tool_lines = "\n".join(
        f"- {m['name']}: {(m.get('description') or '')[:120]}"
        for m in candidate_tools
    ) or "(لا أدوات)"
    skill_lines = "\n".join(f"- {s}" for s in candidate_skills) or "(لا مهارات)"

    sys_prompt = _system_prompt(tool_lines, skill_lines)
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"المهمة: {task}"},
    ]
    last_errors: List[str] = []
    for attempt in range(max_retries + 1):
        with Tracer.span("workflow.plan", task_preview=task[:120], attempt=attempt):
            try:
                raw = model(messages)
            except Exception as e:
                last_errors = [f"model failure: {e}"]
                break
        chunk = _extract_json(raw or "")
        if chunk:
            try:
                data = json.loads(chunk)
            except json.JSONDecodeError as e:
                last_errors = [f"JSON decode: {e.msg}"]
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": (
                    f"خطأ في الـJSON: {e.msg}. أعد المحاولة بـJSON صالح فقط."
                )})
                continue
            plan_model, errs = _validate_plan(data, allowed)
            if plan_model is not None:
                steps = [
                    Step(
                        id=s.id, tool=s.tool, args=dict(s.args),
                        deps=list(s.deps), description=s.description,
                        timeout=float(s.timeout), retries=int(s.retries),
                        retry_backoff=float(s.retry_backoff),
                        on_failure=s.on_failure,
                        fallback_step=s.fallback_step, when=s.when,
                    ) if _HAS_PYDANTIC else Step(
                        id=str(s["id"]), tool=str(s["tool"]),
                        args=dict(s.get("args") or {}),
                        deps=list(s.get("deps") or []),
                        description=str(s.get("description", "")),
                        timeout=float(s.get("timeout", 60)),
                        retries=int(s.get("retries", 0)),
                        retry_backoff=float(s.get("retry_backoff", 1.5)),
                        on_failure=str(s.get("on_failure", "stop")),
                        fallback_step=s.get("fallback_step"),
                        when=s.get("when"),
                    )
                    for s in (plan_model.steps if _HAS_PYDANTIC else plan_model["steps"])
                ]
                return Plan(
                    id=str(uuid.uuid4())[:8],
                    name=plan_model.name if _HAS_PYDANTIC else plan_model.get("name", task[:40]),
                    description=plan_model.description if _HAS_PYDANTIC else plan_model.get("description", ""),
                    task=task,
                    steps=steps,
                )
            last_errors = errs
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": (
                "خطأ في التحقق من الخطة:\n- " + "\n- ".join(errs) +
                "\nأعد المحاولة بـJSON صالح فقط."
            )})
        else:
            last_errors = ["لم يتم العثور على JSON في الرد"]
            messages.append({"role": "assistant", "content": raw or ""})
            messages.append({"role": "user", "content": (
                "ردّك لم يحوِ JSON صالح. أعد بـJSON فقط دون شرح."
            )})

    # Fallback: a single-step plan delegating to ReAct.
    return Plan(
        id=str(uuid.uuid4())[:8],
        name=task[:40] or "workflow",
        description="(fallback) تخطيط فاشل: " + (last_errors[0] if last_errors else "غير معروف"),
        task=task,
        steps=[Step(id="s1", tool="__react__", args={"task": task},
                    description="نفّذ المهمة عبر حلقة ReAct (احتياطي).",
                    timeout=300.0)],
    )


# --------------------------------------------------------------------------
# DAG
# --------------------------------------------------------------------------
def _topological(steps: List[Step]) -> List[Step]:
    by_id = {s.id: s for s in steps}
    indeg: Dict[str, int] = defaultdict(int)
    children: Dict[str, List[str]] = defaultdict(list)
    for s in steps:
        for d in s.deps:
            if d in by_id:
                indeg[s.id] += 1
                children[d].append(s.id)
    queue = deque([s.id for s in steps if indeg[s.id] == 0])
    order: List[Step] = []
    while queue:
        sid = queue.popleft()
        order.append(by_id[sid])
        for c in children[sid]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    if len(order) != len(steps):
        return list(steps)
    return order


# --------------------------------------------------------------------------
# Workflow runtime
# --------------------------------------------------------------------------
class _StepTimeout(Exception):
    pass


def _run_with_timeout(fn, timeout: float) -> Tuple[bool, Any]:
    """Run ``fn`` in a daemon thread with a soft timeout."""
    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["out"] = fn()
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return False, _StepTimeout(f"تجاوز المهلة {timeout}s")
    if "err" in box:
        return False, box["err"]
    return True, box.get("out")


# --------------------------------------------------------------------------
class Workflow:
    """Plan + execute a multi-step workflow.

    ``ask_recurrence`` is the part of the protocol the API uses:

        wf = Workflow(model, registry, awareness)
        prompt = wf.ask_recurrence(task)        # text shown to the user
        # user replies "one-time" / "recurring"
        plan = wf.plan(task)
        result = wf.run(plan, recurring=...)
    """

    RECURRENCE_PROMPT = (
        "هل تريد تنفيذ هذه المهمة لمرة واحدة فقط، أم تشغيلها بشكل دوري في "
        "الخلفية؟ أجِب بـ`one-time` أو `recurring`."
    )

    def __init__(self, model, tool_registry, system_awareness=None,
                 max_react_steps: int = 5, throttle=None):
        self.model = model
        self.tool_registry = tool_registry
        self.awareness = system_awareness
        self.max_react_steps = max_react_steps
        self.throttle = throttle or default_throttle()

    # ------------------------------------------------------------------
    def ask_recurrence(self, task: str) -> str:
        return self.RECURRENCE_PROMPT

    def plan(self, task: str, *, k_tools: int = 12, k_skills: int = 8) -> Plan:
        if self.awareness is None:
            tools: List[Dict[str, Any]] = []
            skills: List[str] = []
        else:
            tools = select_tools_for_task(task, self.awareness, k=k_tools)
            skills = select_skills_for_task(task, self.awareness, k=k_skills)
        return _llm_plan(self.model, task, tools, skills)

    # ------------------------------------------------------------------
    def run(self, plan: Plan, *, recurring: bool = False) -> str:
        plan.recurring = recurring
        if recurring:
            save_plan(plan)

        order = _topological(plan.steps)
        scratchpad: Dict[str, str] = {}
        plan_lines: List[str] = [
            f"# Workflow: {plan.name}",
            f"trace_id: `{plan.trace_id}`",
        ]
        if plan.description:
            plan_lines.append(plan.description)
        plan_lines.append("")

        with Tracer.trace(plan.trace_id):
            Tracer.emit("workflow.start", workflow_id=plan.id,
                        recurring=recurring, total_steps=len(order))
            stop = False
            for step in order:
                if stop:
                    step.status = STATUS_SKIPPED
                    plan_lines.append(self._fmt_step(step, ""))
                    continue
                if not self._when_satisfied(step, scratchpad):
                    step.status = STATUS_SKIPPED
                    plan_lines.append(self._fmt_step(step, "(تخطّى: شرط `when` لم يتحقق)"))
                    continue
                ok, output = self._execute_step(step, scratchpad, plan)
                scratchpad[step.id] = output
                plan_lines.append(self._fmt_step(step, output))
                if not ok:
                    if step.on_failure == "continue":
                        continue
                    if step.on_failure == "fallback" and step.fallback_step:
                        fb = next((s for s in order if s.id == step.fallback_step), None)
                        if fb is not None:
                            ok2, out2 = self._execute_step(fb, scratchpad, plan)
                            scratchpad[fb.id] = out2
                            plan_lines.append(self._fmt_step(fb, out2))
                            if ok2:
                                continue
                    stop = True
            Tracer.emit("workflow.end", workflow_id=plan.id,
                        statuses={s.id: s.status for s in plan.steps})

        plan_lines.append("\n## ملخص الحالة")
        for s in plan.steps:
            plan_lines.append(
                f"- `{s.id}` ({s.tool}) → **{s.status}** "
                f"({s.attempts} محاولة، {int(s.duration_ms)}ms)"
            )
        return "\n".join(plan_lines)

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------
    def _execute_step(self, step: Step, scratchpad: Dict[str, str],
                      plan: Plan) -> Tuple[bool, str]:
        step.status = STATUS_RUNNING
        attempt = 0
        last_error = ""
        while attempt <= step.retries:
            step.attempts = attempt + 1
            args = self._resolve_refs(step.args, scratchpad)
            t0 = time.time()
            with Tracer.span(
                "workflow.step",
                workflow_id=plan.id,
                step_id=step.id,
                tool=step.tool,
                attempt=attempt,
            ):
                try:
                    with self.throttle.slot(step.tool):
                        if step.tool == "__react__":
                            ok, out = _run_with_timeout(
                                lambda: self._react_step(args.get("task", plan.task)),
                                step.timeout,
                            )
                        else:
                            ok, out = _run_with_timeout(
                                lambda: self._invoke(step.tool, args),
                                step.timeout,
                            )
                except Exception as e:  # noqa: BLE001
                    ok, out = False, e
            step.duration_ms += (time.time() - t0) * 1000
            if ok:
                step.status = STATUS_OK
                step.output = str(out) if out is not None else ""
                return True, step.output
            last_error = str(out)
            step.error = last_error
            attempt += 1
            if attempt <= step.retries and step.retry_backoff > 0:
                time.sleep(step.retry_backoff * attempt)
        step.status = STATUS_FAILED
        step.output = f"❌ {last_error}"
        return False, step.output

    def _invoke(self, name: str, args: Dict[str, Any]) -> str:
        fn = self.tool_registry.get(name)
        if fn is not None:
            try:
                return str(fn(**args))
            except TypeError:
                try:
                    return str(fn(*args.values()))
                except Exception as e:
                    raise RuntimeError(f"فشل {name}: {e}") from e
            except Exception as e:
                raise RuntimeError(f"فشل {name}: {e}") from e

        if self.awareness is not None and self.awareness.get_tool(name):
            try:
                from core.tool_intelligence import ToolIntelligence
                ti = ToolIntelligence()
                return str(ti.execute(name, args))
            except Exception as e:
                raise RuntimeError(f"فشل {name} (CLI): {e}") from e
        raise RuntimeError(f"الأداة غير معروفة: {name}")

    def _react_step(self, task: str) -> str:
        from core.agent_loop import react_loop
        return react_loop(
            user_input=task,
            model=self.model,
            tools=self.tool_registry.tools,
            max_steps=self.max_react_steps,
            system_awareness=self.awareness,
        )

    # ------------------------------------------------------------------
    # Scratchpad & expressions
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_refs(args: Dict[str, Any], scratchpad: Dict[str, str]) -> Dict[str, Any]:
        """Replace ``{{step_id[.json.path]}}`` placeholders in string args."""
        def _lookup(token: str) -> str:
            parts = token.split(".")
            sid = parts[0]
            if sid not in scratchpad:
                return "{{" + token + "}}"
            value: Any = scratchpad[sid]
            if len(parts) > 1:
                try:
                    value = json.loads(value) if isinstance(value, str) else value
                    for p in parts[1:]:
                        if isinstance(value, dict):
                            value = value.get(p, "")
                        elif isinstance(value, list) and p.isdigit():
                            value = value[int(p)] if int(p) < len(value) else ""
                        else:
                            value = ""
                except Exception:
                    return scratchpad[sid]
            return str(value)

        out: Dict[str, Any] = {}
        for k, v in args.items():
            if isinstance(v, str):
                v = re.sub(r"\{\{\s*([\w.\-]+)\s*\}\}", lambda m: _lookup(m.group(1)), v)
            out[k] = v
        return out

    @staticmethod
    def _when_satisfied(step: Step, scratchpad: Dict[str, str]) -> bool:
        if not step.when:
            return True
        # very small + safe expression engine: support `step_id.contains:'foo'`,
        # `step_id.empty`, `step_id.ok`, `step_id.failed`. This avoids eval().
        expr = step.when.strip()
        m = re.match(r"^([\w\-]+)\.(empty|ok|failed)$", expr)
        if m:
            sid, op = m.group(1), m.group(2)
            text = scratchpad.get(sid, "")
            if op == "empty":
                return not text
            if op == "ok":
                return bool(text) and not text.lstrip().startswith("❌")
            if op == "failed":
                return text.lstrip().startswith("❌")
        m = re.match(r"^([\w\-]+)\.contains:\s*[\"'](.+)[\"']$", expr)
        if m:
            sid, needle = m.group(1), m.group(2)
            return needle in scratchpad.get(sid, "")
        # Unknown shape → fail-safe: skip the step.
        return False

    # ------------------------------------------------------------------
    @staticmethod
    def _fmt_step(step: Step, output: str) -> str:
        head = f"## [{step.id}] {step.tool} — {step.status}"
        body: List[str] = [head]
        if step.description:
            body.append(step.description)
        if output:
            short = output if len(output) <= 600 else output[:600] + "…"
            body.append(f"```\n{short}\n```")
        return "\n".join(body)


__all__ = [
    "Workflow", "Plan", "Step",
    "save_plan", "load_plan", "list_plans", "delete_plan",
    "STATUS_PENDING", "STATUS_RUNNING", "STATUS_OK",
    "STATUS_FAILED", "STATUS_SKIPPED",
]
