"""ThinkingEngine — orchestrates Plan → Act → Observe → Reflect → Decide.

This replaces the inlined ReAct loop in ``core.agent_loop.react_loop``.
The old ``react_loop`` function is now a thin wrapper that constructs
a :class:`ThinkingEngine` and calls it, keeping every existing call
site (Normal, Workflow's __react__ step, AgentsTeam workers, the
``/chat`` endpoint) working unchanged.

Design highlights:

- **Single LLM call per iteration** (plus at most one optional
  reflection critique). No hidden fan-out.
- **Token budget** enforced before every LLM call by
  :class:`ContextCompressor`. If the system prompt + scratchpad still
  exceed the budget after compression, we drop oldest steps from the
  scratchpad rather than truncate the user's task.
- **Hard caps**:
    - ``max_steps``                  default 8
    - ``max_consecutive_failures``   default 3, then forced Final Answer
    - ``max_empty_responses``        default 2, then forced Final Answer
- **Tracer integration**: each iteration emits ``think.iter.start`` /
  ``think.iter.end`` and reflections emit ``think.reflect``.
- **Plan cache**: identical (task + tools + overview + model) returns
  the previously cached final answer.
"""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.observability import Tracer
from core.optimization import (
    ContextCompressor,
    PromptOptimizer,
    get_plan_cache,
)
from core.optimization.token_counter import TokenCounter
from core.tool_feedback import get_feedback

from .parser import ParsedCall, ToolCallParser
from .planner import MicroPlan, MicroPlanner, PlanComplexity
from .reflector import Reflection, Reflector
from .scratchpad import Scratchpad, ThoughtStep


# Tool registries have grown organically, so their public names do not always
# match the canonical actions understood by WindowsSecurityGate.  Keep that
# translation at this single execution boundary: every ReAct tool invocation
# passes here, including the legacy /chat and workflow paths.
_TOOL_SECURITY_ACTIONS = {
    "execute_command": "run_command",
    "execute_powershell_script": "run_command",
    "run_script": "run_command",
    "shell_exec": "run_command",
    "powershell": "run_command",
    "cmd": "run_command",
    "safe_edit_file": "write_file",
    "apply_patch": "write_file",
    "create_file": "write_file",
    "remove_file": "delete_file",
    "remove_directory": "delete_directory",
    "pip_install": "install_tool",
    "apt_install": "install_tool",
    "npm_install": "install_tool",
    "git_clone_repo": "run_command",
    # The Windows desktop wrappers are registered with a ``computer_`` prefix,
    # but must receive the same policy as their canonical counterparts.
    "computer_run_command": "run_command",
    "computer_read_file": "read_file",
    "computer_write_file": "write_file",
    "computer_move_file": "move_file",
    "computer_open_app": "open_app",
    "computer_focus_window": "focus_window",
    "computer_close_app": "close_app",
    "computer_click": "click",
    "computer_type": "type_text",
    "computer_hotkey": "send_hotkey",
    "computer_get_state": "get_state",
    "computer_verify": "observe",
}

# Results from these adapters can contain instructions controlled by a web
# page, document, search result, or remote MCP server.  Once one is observed,
# the security gate prevents that content from authorising a later privileged
# action in the same agent run.
_UNTRUSTED_RESULT_TOOLS = {
    "search_knowledge", "web_search", "search_web", "fetch_url",
    "http_get", "read_email", "read_document", "mcp_call",
}


# ----------------------------------------------------------------------
@dataclass
class ThinkingResult:
    answer: str
    iterations: int = 0
    final_reason: str = ""             # "final_answer" | "max_steps" | "stop_with_partial" | "error"
    plan: Optional[MicroPlan] = None
    scratchpad: Optional[Scratchpad] = None
    reflections: List[Reflection] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False


# ----------------------------------------------------------------------
class ThinkingEngine:
    """Plan → Act → Observe → Reflect → Decide."""

    def __init__(
        self,
        model: Any,
        tools: Dict[str, Callable[..., Any]],
        *,
        system_awareness: Any = None,
        max_steps: int = 8,
        max_prompt_tokens: int = 2200,
        max_context_tokens: int = 6000,
        use_cache: bool = True,
        enable_planner: bool = True,
        planner_uses_llm: bool = False,
        enable_reflector: bool = True,
        reflector_uses_llm: bool = False,  # off by default to save tokens
        max_consecutive_failures: int = 3,
        max_empty_responses: int = 2,
        on_token: Optional[Callable[[str], None]] = None,
        compression_enabled: bool = False,
        compression_method: str = "auto",
        compression_ratio: float = 0.5,
    ):
        self.model = model
        self.tools = tools
        self.awareness = system_awareness
        self.max_steps = max(1, int(max_steps))
        self.max_prompt_tokens = int(max_prompt_tokens)
        self.max_context_tokens = int(max_context_tokens)
        self.use_cache = use_cache
        self.enable_planner = enable_planner
        self.enable_reflector = enable_reflector
        self.max_consecutive_failures = int(max_consecutive_failures)
        self.max_empty_responses = int(max_empty_responses)
        # Optional per-token callback (Phase 8 Part 2). When set, the
        # engine prefers ``model.stream(...)`` and forwards each delta
        # in real time, so the SSE stream can emit true token events.
        # Deltas are only forwarded for the final-answer portion of
        # each step (tool-planning "Thought:" chatter is accumulated
        # silently to avoid leaking reasoning).
        self.on_token = on_token
        self.compression_enabled = bool(compression_enabled)
        self.compression_method = compression_method or "auto"
        self.compression_ratio = max(0.05, min(1.0, float(compression_ratio)))

        self.model_name = self._get_model_name(model)
        self.counter = TokenCounter(self.model_name)
        self.compressor = ContextCompressor(
            self.model_name, self.max_context_tokens,
        )
        self.prompt_optimizer = PromptOptimizer(
            self.model_name, self.max_prompt_tokens,
        )
        self.parser = ToolCallParser(tools.keys())
        # The heuristic planner is sufficient for routing and costs no extra
        # request. LLM planning remains an explicit opt-in for callers that
        # value a richer outline over request economy.
        self.planner = MicroPlanner(model=model, enable_llm=planner_uses_llm)
        self.reflector = Reflector(
            model=model if reflector_uses_llm else None,
            enable_llm=reflector_uses_llm,
            token_counter=self.counter,
        ) if enable_reflector else None

    # ------------------------------------------------------------------
    @staticmethod
    def _get_model_name(model: Any) -> str:
        for attr in ("model_name", "name"):
            if hasattr(model, attr):
                value = getattr(model, attr)
                if value:
                    return str(value)
        return "gpt-4o-mini"

    # ------------------------------------------------------------------
    def _apply_advanced_compression(
        self, messages: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        """Optionally shrink a non-policy system message.

        The deterministic compressor and prompt optimiser already remove
        redundancy.  Learned compressors can paraphrase away a negation or
        consent rule, so automatic mode deliberately does not apply one. A
        caller must choose a concrete backend and the target must not contain
        policy/safety language.
        """
        try:
            from core.optimization import compress_prompt, record_stats
        except Exception:  # noqa: BLE001
            return messages
        if not messages:
            return messages
        if self.compression_method in ("", "auto", "none"):
            return messages
        target = None
        for msg in messages:
            if msg.get("role") == "system":
                target = msg
                break
        if target is None:
            return messages
        text = target.get("content") or ""
        if not text.strip():
            return messages
        protected_markers = (
            "أمان", "سلامة", "موافقة", "إذن", "لا تنفذ", "لا تحذف",
            "security", "safety", "consent", "permission", "do not",
        )
        if any(marker in text.lower() for marker in protected_markers):
            return messages
        outcome = compress_prompt(
            text,
            method=self.compression_method,
            ratio=self.compression_ratio,
            model_name=self.model_name,
        )
        try:
            record_stats(outcome)
        except Exception:  # noqa: BLE001
            pass
        if outcome.text and outcome.after_tokens < outcome.before_tokens:
            target["content"] = outcome.text
        return messages

    # ------------------------------------------------------------------
    def _invoke_model(self, messages: List[Dict[str, str]]) -> str:
        """Call the model, forwarding token deltas when streaming.

        The callback fires only for the final-answer text — not for
        tool-planning "Thought:" / "Action:" preamble — so the UI
        renders the user-visible answer as it arrives without leaking
        reasoning traces. We accumulate the full response either way
        so the ReAct parser still sees the complete output.
        """
        stream_fn = getattr(self.model, "stream", None)
        if self.on_token is None or not callable(stream_fn):
            from core.optimization import coalesce_model_call
            fingerprint = json.dumps(messages, ensure_ascii=False, sort_keys=True)
            key = hashlib.sha256(
                (str(id(self.model)) + "\x00" + fingerprint).encode("utf-8")
            ).hexdigest()
            return coalesce_model_call(key, lambda: self.model(messages) or "")
        acc: List[str] = []
        buf = ""
        forwarding = False
        anything_forwarded = False
        final_marker = "Final Answer:"

        def _forward(chunk: str) -> None:
            nonlocal anything_forwarded
            if not chunk:
                return
            if not anything_forwarded:
                chunk = chunk.lstrip()
                if not chunk:
                    return
            anything_forwarded = True
            try:
                self.on_token(chunk)
            except Exception:  # noqa: BLE001
                pass

        try:
            for delta in stream_fn(messages):
                if not delta:
                    continue
                acc.append(delta)
                if forwarding:
                    _forward(delta)
                    continue
                buf += delta
                idx = buf.lower().find(final_marker.lower())
                if idx >= 0:
                    tail = buf[idx + len(final_marker):]
                    _forward(tail)
                    forwarding = True
                    buf = ""
        except Exception:
            # Streaming failed mid-flight; fall back to sync so the
            # parser still gets something usable.
            if not acc:
                return self.model(messages) or ""
        return "".join(acc)

    # ------------------------------------------------------------------
    def run(self, task: str) -> ThinkingResult:
        from core.agent_loop import build_system_prompt, build_tools_description
        from core.observability import new_trace_id

        # Session id for the feedback loop; reuse any active trace id.
        self._current_task = task
        self._current_session_id = (
            Tracer.current_trace_id() or new_trace_id())
        self._feedback = get_feedback()
        # Taint is intentionally scoped to one user task.  Do not let a prior
        # run weaken normal operation, and never let a model reset this flag.
        self._untrusted_content_seen = False
        self._taint_sources: set[str] = set()

        overview = ""
        if self.awareness is not None:
            try:
                if hasattr(self.awareness,
                           "get_full_system_description"):
                    try:
                        overview = self.awareness.get_full_system_description(
                            task=task)
                    except TypeError:
                        overview = self.awareness.get_full_system_description()
            except Exception:
                overview = ""

        tool_names = sorted(self.tools.keys())

        # 1) Plan cache
        cache = get_plan_cache() if self.use_cache else None
        if cache is not None:
            cached = cache.get(
                task, tools=tool_names,
                overview=overview, model_name=self.model_name,
            )
            if cached is not None:
                Tracer.emit("think.cache_hit",
                            tools=len(tool_names), task_preview=task[:80])
                return ThinkingResult(
                    answer=cached, iterations=0,
                    final_reason="cache", cached=True,
                )

        # 2) Plan
        plan: Optional[MicroPlan] = None
        if self.enable_planner:
            tool_summary = self._tool_summary()
            try:
                with Tracer.span("think.plan"):
                    plan = self.planner.plan(task, tool_summary=tool_summary)
            except Exception:
                plan = None

        # 3) Build prompts
        tools_desc = build_tools_description(
            self.tools, awareness=self.awareness, model_name=self.model_name,
        )
        system_prompt_full = build_system_prompt(
            tools_desc, self.model_name, overview,
            max_tokens=self.max_prompt_tokens,
        )
        if plan and plan.render():
            # Inject the plan as a user reminder, not into the system
            # prompt, so it can be compressed away if context tight.
            plan_msg = {"role": "user", "content": plan.render()}
        else:
            plan_msg = None

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt_full},
            {"role": "user", "content": task},
        ]
        if plan_msg:
            messages.append(plan_msg)

        # Intent hint: when the user message looks like a configuration
        # command (e.g. "save my OpenAI key sk-..."), surface the
        # matching admin tool with high-priority context. The classifier
        # is deterministic, regex-based, and free — see
        # core/thinking/intent.py.
        try:
            from core.thinking.intent import classify, format_intent_hint
            match = classify(task)
            if match is not None and match.tool in self.tools:
                hint_text = (
                    "[intent-hint] " + format_intent_hint(match) +
                    "\nIf the user gave consent, call this tool. "
                    "If destructive, ask for confirmation first.")
                messages.append(
                    {"role": "system", "content": hint_text})
                Tracer.emit("think.intent_match",
                            tool=match.tool,
                            confidence=match.confidence,
                            reason=match.reason)
        except Exception:
            pass

        # 4) ReAct loop
        pad = Scratchpad(task=task)
        reflections: List[Reflection] = []
        empty_responses = 0
        iterations = 0
        tokens_in = 0
        tokens_out = 0

        for i in range(self.max_steps):
            iterations = i + 1
            with Tracer.span("think.iter", iteration=iterations):
                # Compress before each call.
                compressed = self.compressor.compress_with_report(messages)
                messages = compressed.messages
                tokens_in += compressed.report.before_tokens

                if self.compression_enabled:
                    messages = self._apply_advanced_compression(messages)

                try:
                    response = self._invoke_model(messages)
                except Exception as e:
                    Tracer.emit("think.model_error", error=str(e)[:200])
                    return ThinkingResult(
                        answer=f"❌ خطأ في النموذج: {e}",
                        iterations=iterations,
                        final_reason="error",
                        plan=plan, scratchpad=pad,
                        reflections=reflections,
                        tokens_in=tokens_in, tokens_out=tokens_out,
                    )
                tokens_out += self.counter.count(response or "")
                messages.append({"role": "assistant", "content": response or ""})

                # Final answer?
                final = self.parser.extract_final_answer(response or "")
                if final is not None:
                    if cache is not None:
                        try:
                            # Executed tools can be stateful or time-sensitive;
                            # never replay their final prose from cache.
                            if not pad.steps:
                                cache.set(
                                    task, final, tools=tool_names,
                                    overview=overview, model_name=self.model_name,
                                    tokens_in=tokens_in, tokens_out=tokens_out,
                                )
                        except Exception:
                            pass
                    return ThinkingResult(
                        answer=final, iterations=iterations,
                        final_reason="final_answer",
                        plan=plan, scratchpad=pad,
                        reflections=reflections,
                        tokens_in=tokens_in, tokens_out=tokens_out,
                    )

                # Empty response?
                if not (response or "").strip():
                    empty_responses += 1
                    if empty_responses >= self.max_empty_responses:
                        return self._force_finish(
                            "stop_with_partial",
                            "النموذج أرجع رداً فارغاً مرتين.",
                            messages, pad, plan, reflections,
                            iterations, tokens_in, tokens_out,
                        )
                    messages.append(self._reminder(
                        "ردك كان فارغاً. التزم بصيغة Thought / Action / "
                        "Action Input أو Final Answer."
                    ))
                    continue

                # Extract calls
                thought = self.parser.extract_thought(response or "")
                calls = self.parser.extract_calls(response or "")

                if not calls:
                    # No structured action — nudge.
                    pad.add_step(ThoughtStep(
                        iteration=iterations, thought=thought,
                        observation="(لم تُستخرج أي Action)",
                        error="format",
                    ))
                    messages.append(self._reminder(
                        "التنسيق غير صحيح. استخدم بالضبط:\n"
                        "Thought: ...\nAction: <اسم الأداة>\n"
                        "Action Input: {\"key\":\"value\"}\n"
                        "أو أنهِ بـ Final Answer: ..."
                    ))
                    continue

                # Execute calls
                obs_chunks: List[str] = []
                step = ThoughtStep(iteration=iterations, thought=thought)
                iteration_call_signatures: set[str] = set()
                for call in calls:
                    obs, step_error = self._execute_call(
                        call, pad, iteration_call_signatures,
                    )
                    obs_chunks.append(obs)
                    step.action = call.tool
                    step.args = call.args
                    if step_error and step.error is None:
                        step.error = step_error
                step.observation = "\n---\n".join(obs_chunks)
                pad.add_step(step)
                messages.append({
                    "role": "user",
                    "content": f"Observation: {step.observation}",
                })

                # Reflect
                if self.reflector is not None:
                    refl = self.reflector.reflect(pad)
                    reflections.append(refl)
                    step.reflection = f"[{refl.decision}@{refl.confidence:.2f}] {refl.advice}"
                    Tracer.emit("think.reflect",
                                decision=refl.decision,
                                confidence=refl.confidence,
                                source=refl.source)
                    if refl.decision == "stop_with_partial":
                        return self._force_finish(
                            "stop_with_partial",
                            refl.advice or "تم الإيقاف وفقاً للمراجعة.",
                            messages, pad, plan, reflections,
                            iterations, tokens_in, tokens_out,
                        )
                    if refl.decision == "change_tactic" and refl.advice:
                        messages.append(self._reminder(
                            f"ملاحظة مراجعة: {refl.advice}"
                        ))
                else:
                    step.reflection = ""

                # Hard cap on consecutive failures.
                if pad.consecutive_failures() >= self.max_consecutive_failures:
                    return self._force_finish(
                        "stop_with_partial",
                        f"{pad.consecutive_failures()} محاولات فاشلة متتالية.",
                        messages, pad, plan, reflections,
                        iterations, tokens_in, tokens_out,
                    )

        # Out of steps
        return self._force_finish(
            "max_steps",
            f"بلغت السقف الأقصى ({self.max_steps} خطوات).",
            messages, pad, plan, reflections,
            iterations, tokens_in, tokens_out,
        )

    def __call__(self, task: str) -> str:
        return self.run(task).answer

    # ------------------------------------------------------------------
    def _execute_call(
        self,
        call: ParsedCall,
        pad: Scratchpad,
        iteration_call_signatures: Optional[set[str]] = None,
    ) -> tuple[str, Optional[str]]:
        if call.error:
            err = f"❌ JSON غير صالح: {call.error}"
            return err, "json"
        if call.tool not in self.tools:
            err = f"❌ أداة غير معروفة: {call.tool}"
            if call.fuzzy_from:
                err += f" (لم تُحلّ من '{call.fuzzy_from}')"
            return err, "unknown_tool"
        if pad.repeated_action(call.tool, call.args, within=3):
            return (
                f"⚠️ كرّرتَ {call.tool} بنفس المعطيات مؤخراً. "
                "غيّر المعطيات أو أنهِ بـ Final Answer.",
                "repeat",
            )

        # A model can emit the same side-effecting tool call twice in one
        # response.  Scratchpad history is populated only after this loop, so
        # its existing repeat guard cannot see those duplicates.  Treat calls
        # as idempotent within a single iteration and never execute the same
        # action/arguments pair twice.
        try:
            call_signature = f"{call.tool}:{json.dumps(call.args, sort_keys=True, default=str)}"
        except Exception:
            call_signature = f"{call.tool}:{repr(call.args)}"
        if iteration_call_signatures is not None:
            if call_signature in iteration_call_signatures:
                return (
                    f"⚠️ تم تجاهل استدعاء مكرر لـ {call.tool} بنفس المعطيات في نفس الخطوة.",
                    "duplicate_in_iteration",
                )
            iteration_call_signatures.add(call_signature)

        # Feedback ban check: refuse to invoke a tool that already
        # failed too many times in this session.
        session_id = getattr(self, "_current_session_id", "default")
        feedback = getattr(self, "_feedback", None) or get_feedback()
        if feedback.is_banned(session_id, call.tool):
            alts = feedback.suggest_alternatives(
                session_id, call.tool,
                task=getattr(self, "_current_task", ""),
                tools=self._awareness_tools_view(),
                history=Tracer.events(kind="tool.call", limit=200),
                k=3,
            )
            alt_text = ("بدائل مقترحة: " + ", ".join(alts)) if alts else \
                "لا توجد بدائل مرشَّحة الآن."
            return (
                f"⛔ الأداة '{call.tool}' محظورة لهذه الجلسة بعد "
                f"{feedback.failure_count(session_id, call.tool)} فشلاً. {alt_text}",
                "banned",
            )

        fn = self.tools[call.tool]
        action = _TOOL_SECURITY_ACTIONS.get(call.tool, call.tool)
        params = call.args if isinstance(call.args, dict) else {"value": call.args}
        # Normalize the desktop wrapper's historical src/dst spellings so the
        # filesystem governor validates *both* ends of a move.
        if action == "move_file":
            params = dict(params)
            if "src" in params and "path" not in params:
                params["path"] = params["src"]
            if "dst" in params and "destination" not in params:
                params["destination"] = params["dst"]
        try:
            from core.security.security_gate import SecurityContext, get_security_gate

            taint_sources = sorted(getattr(self, "_taint_sources", set()))
            evaluation = get_security_gate().evaluate_action(
                action,
                params,
                SecurityContext(
                    caller="thinking_engine",
                    session_id=session_id,
                    is_untrusted_content=bool(
                        getattr(self, "_untrusted_content_seen", False)),
                    taint_sources=taint_sources,
                ),
            )
            if not evaluation.allowed:
                Tracer.emit(
                    "tool.call.blocked", tool=call.tool, action=action,
                    reason=evaluation.reason[:200], session=session_id,
                )
                return f"⛔ مُنع {call.tool}: {evaluation.reason}", "security"
        except Exception as exc:  # noqa: BLE001
            # Failing open here would turn an observability/import failure into
            # arbitrary tool execution.  A user can retry after the security
            # subsystem is healthy.
            Tracer.emit("tool.call.blocked", tool=call.tool, action=action,
                        reason="security gate unavailable", session=session_id)
            return f"⛔ تعذر التحقق الأمني قبل {call.tool}: {exc}", "security"

        t0 = time.time()
        try:
            Tracer.emit("tool.call.start", tool=call.tool,
                        session=session_id)
            if isinstance(call.args, dict):
                result = fn(**call.args)
            else:
                result = fn(call.args)
            text = str(result) if result is not None else ""
            note = ""
            if call.fuzzy_from:
                note = f"(تم التحويل من '{call.fuzzy_from}' إلى '{call.tool}') "
            duration_ms = (time.time() - t0) * 1000
            Tracer.emit("tool.call.end", tool=call.tool,
                        status="ok",
                        duration_ms=round(duration_ms, 1),
                        session=session_id)
            feedback.record_success(session_id, call.tool)
            if self._result_is_untrusted(call.tool):
                self._untrusted_content_seen = True
                self._taint_sources.add(call.tool)
            return f"{note}{text}", None
        except TypeError as e:
            # Do not retry a malformed keyword call positionally.  Argument
            # order is not a reliable contract for LLM-generated JSON and a
            # positional retry can turn a safe call into a different,
            # side-effecting operation.
            err = f"❌ تعارض في تواقيع الأداة {call.tool}: {e}"
            duration_ms = (time.time() - t0) * 1000
            Tracer.emit("tool.call.end", tool=call.tool,
                        status="error", duration_ms=round(duration_ms, 1),
                        error=str(e)[:200], session=session_id)
            self._handle_tool_failure(feedback, session_id, call.tool, str(e))
            return err, "signature"
        except Exception as e:  # noqa: BLE001
            duration_ms = (time.time() - t0) * 1000
            Tracer.emit("tool.call.end", tool=call.tool,
                        status="error",
                        duration_ms=round(duration_ms, 1),
                        error=str(e)[:200],
                        session=session_id)
            extra = self._handle_tool_failure(
                feedback, session_id, call.tool, str(e))
            return f"❌ فشل {call.tool}: {e}{extra}", "exec"

    @staticmethod
    def _result_is_untrusted(tool: str) -> bool:
        """Return whether a tool result is external content, not authority."""
        normalized = (tool or "").strip().lower()
        return (
            normalized in _UNTRUSTED_RESULT_TOOLS
            or normalized.startswith(("browser_", "mcp_", "web_", "http_"))
        )

    def _handle_tool_failure(self, feedback, session_id: str,
                             tool: str, err: str) -> str:
        """Update the feedback loop and produce a model-facing hint."""
        info = feedback.record_failure(session_id, tool, err)
        if info.get("banned"):
            alts = feedback.suggest_alternatives(
                session_id, tool,
                task=getattr(self, "_current_task", ""),
                tools=self._awareness_tools_view(),
                history=Tracer.events(kind="tool.call", limit=200),
                k=3,
            )
            alt_text = ", ".join(alts) if alts else "—"
            return (
                f"\n⛔ تم حظر '{tool}' بعد {info['failures']} فشلاً. "
                f"بدائل: {alt_text}")
        if info.get("failures", 0) == 1:
            alts = feedback.suggest_alternatives(
                session_id, tool,
                task=getattr(self, "_current_task", ""),
                tools=self._awareness_tools_view(),
                history=Tracer.events(kind="tool.call", limit=200),
                k=2,
            )
            if alts:
                return f"\n💡 جرّب بدائل: {', '.join(alts)}."
        return ""

    def _awareness_tools_view(self) -> Dict[str, Dict[str, Any]]:
        """Return ``{name: manifest}`` for ranking, falling back to ``{}``."""
        if self.awareness is not None and getattr(
                self.awareness, "tools", None):
            return self.awareness.tools  # type: ignore[return-value]
        # No manifest source — synthesise minimal entries from registry.
        return {n: {"name": n} for n in self.tools.keys()}

    # ------------------------------------------------------------------
    def _force_finish(
        self,
        reason: str,
        advice: str,
        messages: List[Dict[str, str]],
        pad: Scratchpad,
        plan: Optional[MicroPlan],
        reflections: List[Reflection],
        iterations: int,
        tokens_in: int,
        tokens_out: int,
    ) -> ThinkingResult:
        """Ask the model for one last Final Answer using what we have."""
        messages.append(self._reminder(
            f"{advice} الآن أنهِ المهمة بـ Final Answer: مع أفضل تلخيص "
            "ممكن مما توصّلت إليه. لا تستدعِ أي أداة جديدة."
        ))
        try:
            response = self.model(messages)
            tokens_out += self.counter.count(response or "")
            final = self.parser.extract_final_answer(response or "")
        except Exception as e:
            final = None
            response = f"❌ خطأ في الإنهاء: {e}"
        if not final:
            # Extract last useful observation as a degraded answer.
            last_obs = next(
                (s.observation for s in reversed(pad.steps) if s.observation),
                "",
            )
            final = response or last_obs or "تعذّر الوصول إلى إجابة نهائية."
        return ThinkingResult(
            answer=final, iterations=iterations,
            final_reason=reason,
            plan=plan, scratchpad=pad,
            reflections=reflections,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _reminder(text: str) -> Dict[str, str]:
        return {"role": "user", "content": text}

    def _tool_summary(self) -> str:
        names = sorted(self.tools.keys())
        if len(names) <= 12:
            return ", ".join(names)
        return ", ".join(names[:12]) + f", … (+{len(names) - 12})"


__all__ = ["ThinkingEngine", "ThinkingResult"]
