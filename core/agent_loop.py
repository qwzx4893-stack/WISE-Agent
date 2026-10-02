"""ReAct loop for Agent OS — integrated with the optimization layer.

Behaviour:

- The system prompt is built once per call, then squeezed by
  :class:`PromptOptimizer` (section-aware) before being sent.
- Tool docs are emitted via :meth:`PromptOptimizer.select_tools_for_prompt`
  so we never list 200 tools verbatim — we keep the top-N by registration
  order and reference the rest by category. The agent can call
  ``search_tools`` if it needs more.
- Every iteration runs the messages through
  :meth:`ContextCompressor.compress_with_report`. The cumulative report
  is exposed as ``react_loop.last_compression`` and aggregated into
  ``optimization_stats()``.
- Successful ``Final Answer`` results are stored in
  :class:`PlanCache` keyed by ``(task, tool_set, overview, model_name)``
  so identical follow-up requests skip the LLM entirely.
- The loop refuses to call ``(tool, args)`` twice in a row.
- Tool-call extraction is robust to inline ``Final Answer`` blocks.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from core.optimization import (
    CompressionReport,
    ContextCompressor,
    PromptOptimizer,
    get_plan_cache,
)


# ----------------------------------------------------------------------
# Model-name detection (best-effort across our universal LLM adapter
# and the older TeamModel.)
# ----------------------------------------------------------------------
def _get_model_name(model: Any) -> str:
    for attr in ("model_name", "name"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    try:
        if hasattr(model, "active_client") and model.active_client:
            return model.active_client.get("model_name", "gpt-4o-mini")
        if hasattr(model, "clients") and model.clients:
            return model.clients[0].get("model_name", "gpt-4o-mini")
    except Exception:
        pass
    return "gpt-4o-mini"


# ----------------------------------------------------------------------
# Tool-call parser
# ----------------------------------------------------------------------
_ACTION_PAT = re.compile(
    r"Action:\s*(?P<tool>[\w\-./]+)\s*\n\s*"
    r"Action Input:\s*(?P<args>\{.*?\}|\".*?\"|\'.*?\')"
    r"\s*(?=\n\s*(?:Observation|Action|Thought|Final|$))",
    re.DOTALL | re.IGNORECASE,
)


def extract_tool_calls(text: str) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for m in _ACTION_PAT.finditer(text):
        tool_name = m.group("tool").strip()
        args_str = m.group("args").strip()
        if (args_str.startswith('"') and args_str.endswith('"')) or (
            args_str.startswith("'") and args_str.endswith("'")
        ):
            args_str = args_str[1:-1]
        try:
            args = json.loads(args_str)
        except Exception:
            args = {"_json_error": True, "raw": args_str[:200]}
        calls.append({"tool": tool_name, "args": args})
    return calls


# ----------------------------------------------------------------------
# Tool docs
# ----------------------------------------------------------------------
def _doc_first_line(fn: Any) -> str:
    doc = (fn.__doc__ or "").strip()
    for line in doc.splitlines():
        line = line.strip()
        if line and not line.startswith(("Args:", "Returns:")):
            return line[:160]
    return "لا يوجد وصف."


def _build_tool_lines(
    tools: Dict[str, Any],
    awareness: Any = None,
) -> List[tuple[str, str, str]]:
    triples: List[tuple[str, str, str]] = []
    for name, fn in tools.items():
        cat = "core"
        desc = _doc_first_line(fn)
        if awareness is not None:
            try:
                m = awareness.get_tool(name) or {}
                cat = m.get("category", cat) or cat
                desc = m.get("description", desc) or desc
            except Exception:
                pass
        triples.append((name, desc, cat))
    return triples


def build_tools_description(
    tools: Dict[str, Any],
    *,
    awareness: Any = None,
    keep: int = 24,
    budget_tokens: int = 1200,
    model_name: str = "gpt-4o-mini",
) -> str:
    triples = _build_tool_lines(tools, awareness=awareness)
    return PromptOptimizer(model_name).select_tools_for_prompt(
        triples, keep=keep, budget_tokens=budget_tokens,
    )


def build_system_prompt(
    tools_description: str,
    model_name: str = "gpt-4o-mini",
    system_overview: str = "",
    *,
    max_tokens: int = 2200,
) -> str:
    overview_block = f"\n{system_overview}\n" if system_overview else ""
    try:
        from core.system_integration import system_integration_policy_prompt
        integration_policy = system_integration_policy_prompt()
    except Exception:
        integration_policy = ""
    prompt = f"""أنت وكيل ذكي يعمل داخل Agent OS. مهمتك الأساسية هي استخدام الأدوات المتاحة لحل مهام المستخدم.
{overview_block}
{integration_policy}
## الأدوات المتاحة (مختصرة):
{tools_description}

## تنسيق الاستجابة (التزم به بدقة):
Thought: [شرح مختصر لخطوتك التالية]
Action: [اسم الأداة]
Action Input: [JSON صحيح يحتوي على المعطيات]

مثال:
Thought: سأبحث عن القمر في ويكيبيديا.
Action: search_knowledge
Action Input: {{"query": "القمر", "sources": "wikipedia"}}

بعد تنفيذ الأداة، ستصلك Observation بالنتيجة.

عند الانتهاء:
Thought: لدي الإجابة.
Final Answer: [الإجابة النهائية]
"""
    return PromptOptimizer(model_name, max_tokens).optimize(prompt)


# ----------------------------------------------------------------------
# Compression wrapper
# ----------------------------------------------------------------------
def truncate_context(
    messages: List[Dict[str, str]],
    model_name: str = "gpt-4o-mini",
    max_tokens: int = 6000,
    *,
    aggregate_into: Optional[CompressionReport] = None,
) -> List[Dict[str, str]]:
    compressor = ContextCompressor(model_name, max_tokens)
    result = compressor.compress_with_report(messages)
    if aggregate_into is not None:
        aggregate_into.before_tokens += result.report.before_tokens
        aggregate_into.after_tokens += result.report.after_tokens
        aggregate_into.dropped += result.report.dropped
        aggregate_into.summarised += result.report.summarised
        aggregate_into.truncated += result.report.truncated
        aggregate_into.deduplicated += result.report.deduplicated
        aggregate_into.retained_facts += result.report.retained_facts
    return result.messages


# ----------------------------------------------------------------------
# ReAct loop
# ----------------------------------------------------------------------
def react_loop(
    user_input: str,
    model: Any,
    tools: Dict[str, Any],
    max_steps: int = 8,
    system_awareness: Any = None,
    *,
    use_cache: bool = True,
    max_prompt_tokens: int = 2200,
    max_context_tokens: int = 6000,
    enable_planner: bool = True,
    enable_reflector: bool = True,
    reflector_uses_llm: bool = False,
    return_result: bool = False,
    on_token: Optional[Callable[[str], None]] = None,
    compression_enabled: Optional[bool] = None,
    compression_method: Optional[str] = None,
    compression_ratio: Optional[float] = None,
) -> Any:
    """Thin compatibility wrapper around :class:`ThinkingEngine`.

    The legacy signature is preserved (returns ``str`` by default) so
    every existing caller — Normal mode, Workflow ``__react__`` step,
    AgentsTeam workers, the ``/chat`` endpoint — keeps working.

    Pass ``return_result=True`` to receive the full
    :class:`ThinkingResult` (iterations / scratchpad / reflections /
    token usage).
    """
    from core.thinking import ThinkingEngine

    # Read compression settings from ResourceSettings when the caller
    # does not supply explicit overrides, so the /admin/resources
    # endpoint controls them live without restarting the server.
    if (compression_enabled is None
            or compression_method is None
            or compression_ratio is None):
        try:
            from core.platform_manager import get_platform_manager
            from core.resource_settings import get_store
            pm = get_platform_manager()
            s = get_store().load(mode=pm.mode)
            if compression_enabled is None:
                compression_enabled = s.compression_enabled
            if compression_method is None:
                compression_method = s.compression_method
            if compression_ratio is None:
                compression_ratio = s.compression_ratio
        except Exception:
            compression_enabled = bool(compression_enabled)
            compression_method = compression_method or "auto"
            compression_ratio = (
                0.5 if compression_ratio is None else compression_ratio)

    engine = ThinkingEngine(
        model=model,
        tools=tools,
        system_awareness=system_awareness,
        max_steps=max_steps,
        max_prompt_tokens=max_prompt_tokens,
        max_context_tokens=max_context_tokens,
        use_cache=use_cache,
        enable_planner=enable_planner,
        enable_reflector=enable_reflector,
        reflector_uses_llm=reflector_uses_llm,
        on_token=on_token,
        compression_enabled=bool(compression_enabled),
        compression_method=compression_method or "auto",
        compression_ratio=(0.5 if compression_ratio is None
                            else float(compression_ratio)),
    )
    result = engine.run(user_input)
    # Backwards-compat attributes for callers that introspect the old loop.
    report = CompressionReport()
    report.before_tokens = result.tokens_in
    report.after_tokens = result.tokens_out
    react_loop.last_compression = report  # type: ignore[attr-defined]
    react_loop.last_result = result  # type: ignore[attr-defined]
    if return_result:
        return result
    return result.answer


# ----------------------------------------------------------------------
# Legacy in-file ReAct loop (kept for fallback debugging only).
# ----------------------------------------------------------------------
def _legacy_react_loop(
    user_input: str,
    model: Any,
    tools: Dict[str, Any],
    max_steps: int = 6,
    system_awareness: Any = None,
    *,
    use_cache: bool = True,
    max_prompt_tokens: int = 2200,
    max_context_tokens: int = 6000,
) -> str:
    model_name = _get_model_name(model)
    overview = ""
    if system_awareness is not None:
        try:
            overview = system_awareness.get_full_system_description()
        except Exception:
            overview = ""

    tool_names = sorted(tools.keys())
    cache = get_plan_cache() if use_cache else None
    if cache is not None:
        cached = cache.get(
            user_input, tools=tool_names, overview=overview, model_name=model_name,
        )
        if cached is not None:
            return cached

    tools_desc = build_tools_description(
        tools, awareness=system_awareness, model_name=model_name,
    )
    system_prompt = build_system_prompt(
        tools_desc, model_name, overview, max_tokens=max_prompt_tokens,
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input},
    ]
    seen_calls: set[str] = set()
    report = CompressionReport()

    for _ in range(max_steps):
        messages = truncate_context(
            messages, model_name, max_context_tokens, aggregate_into=report,
        )
        try:
            response = model(messages)
        except Exception as e:
            return f"❌ خطأ في النموذج: {e}"

        messages.append({"role": "assistant", "content": response})

        final_match = re.search(
            r"Final Answer:\s*(.*)", response, re.IGNORECASE | re.DOTALL,
        )
        if final_match:
            final_answer = final_match.group(1).strip()
            if cache is not None:
                try:
                    # Legacy loop follows the same safety rule as
                    # ThinkingEngine: never replay a response that followed
                    # an executed tool, because it may be stateful or stale.
                    if not seen_calls:
                        cache.set(
                            user_input, final_answer,
                            tools=tool_names, overview=overview,
                            model_name=model_name,
                        )
                except Exception:
                    pass
            react_loop.last_compression = report  # type: ignore[attr-defined]
            return final_answer

        tool_calls = extract_tool_calls(response)
        if not tool_calls:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "التنسيق غير صحيح. استخدم: Action: [الأداة]\n"
                        "Action Input: [JSON]"
                    ),
                }
            )
            continue

        observations: List[str] = []
        for call in tool_calls:
            tool_name = call["tool"]
            args = call["args"]
            if isinstance(args, dict) and args.get("_json_error"):
                observations.append(f"❌ JSON غير صالح: {args['raw']}")
                continue
            if tool_name not in tools:
                observations.append(f"❌ أداة غير معروفة: {tool_name}")
                continue
            sig = f"{tool_name}::{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            if sig in seen_calls:
                observations.append(
                    f"⚠️ تم استدعاء {tool_name} بنفس المعطيات مسبقاً - "
                    "غيّر الخطة أو أنهِ بـ Final Answer."
                )
                continue
            seen_calls.add(sig)
            try:
                result = tools[tool_name](**args) if isinstance(args, dict) else tools[tool_name](args)
                observations.append(str(result))
            except TypeError:
                try:
                    if isinstance(args, dict):
                        result = tools[tool_name](*args.values())
                        observations.append(str(result))
                    else:
                        observations.append(f"❌ خطأ: لا تتطابق المعطيات")
                except Exception as e:
                    observations.append(f"❌ خطأ: {e}")
            except Exception as e:
                observations.append(f"❌ خطأ: {e}")

        obs_text = "\n".join(observations)
        messages.append({"role": "user", "content": f"Observation: {obs_text}"})

    return "⚠️ لم يتم التوصل لإجابة نهائية. حاول مجددًا."


# Public attribute populated after each call so callers can inspect savings.
react_loop.last_compression = CompressionReport()  # type: ignore[attr-defined]


__all__ = [
    "react_loop",
    "extract_tool_calls",
    "build_tools_description",
    "build_system_prompt",
    "truncate_context",
]
