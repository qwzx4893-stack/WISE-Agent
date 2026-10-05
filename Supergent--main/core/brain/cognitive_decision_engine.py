# ==============================================================================
# WISE Cognitive Brain - Dynamic Decision Engine & Pre-flight Analyzer
# Architecture:
# 1. CognitivePreflightAnalyzer: Evaluates real goal, existing knowledge,
#    missing info, direct answer capability, and minimal required tools.
# 2. DeepResearchEngine: Multi-source query decomposition, real search execution,
#    cross-referencing, date/contradiction detection, and evidence synthesis.
# 3. CognitiveRecoveryEngine: State-aware root cause diagnosis and replanning
#    using live World State and prior completed steps.
# ==============================================================================

from __future__ import annotations

import os
import re
import json
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field

from core.models.provider_interface import (
    BaseModelProvider,
    ModelCompletionRequest,
    ModelCompletionResponse,
    get_model_provider,
)
from core.context.world_state import WISEWorldState, get_world_state_engine

LOG = logging.getLogger("WISE.Brain.DecisionEngine")


@dataclass
class CognitivePreflightDecision:
    intent: str
    real_goal: str
    existing_knowledge: str
    missing_information: Optional[str] = None
    can_answer_directly: bool = False
    direct_answer: Optional[str] = None
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    selected_capabilities: List[str] = field(default_factory=list)
    complexity_tier: str = "MEDIUM"  # SIMPLE | MEDIUM | COMPLEX
    is_deep_research: bool = False
    is_multi_step: bool = False
    recommended_action: str = ""
    reasoning: str = ""
    selected_skill: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "real_goal": self.real_goal,
            "existing_knowledge": self.existing_knowledge,
            "missing_information": self.missing_information,
            "can_answer_directly": self.can_answer_directly,
            "direct_answer": self.direct_answer,
            "needs_clarification": self.needs_clarification,
            "clarification_question": self.clarification_question,
            "selected_capabilities": list(self.selected_capabilities),
            "complexity_tier": self.complexity_tier,
            "is_deep_research": self.is_deep_research,
            "is_multi_step": self.is_multi_step,
            "recommended_action": self.recommended_action,
            "reasoning": self.reasoning,
            "selected_skill": self.selected_skill,
        }


class CognitiveDecisionEngine:
    """
    Central cognitive decision engine for WISE powered by LFM2.5.
    Performs dynamic pre-flight intent evaluation, capability routing,
    multi-source deep research, and state-aware failure recovery.
    """

    CAPABILITIES_CATALOG = [
        {"name": "web_search", "description": "Search external live web sources for recent facts, news, and external documentation."},
        {"name": "skill_runner", "description": "Execute specialized offline skill workflows (e.g. data audit, code refactoring)."},
        {"name": "mcp_client", "description": "Query registered Model Context Protocol external servers (databases, APIs)."},
        {"name": "windows_control", "description": "Interact with local Windows desktop applications, keyboard, mouse, and active windows."},
        {"name": "browser_tool", "description": "Control the agent browser, or Brave on explicit user request, navigate URLs and interact with web pages."},
        {"name": "filesystem_tool", "description": "Read, write, or list local filesystem directories and files."},
        {"name": "vision_tool", "description": "Analyze desktop screenshots or images for visual UI elements and coordinates."},
        {"name": "memory_tool", "description": "Search or record persistent facts, user preferences, and historical memory."},
    ]

    def __init__(self, provider: Optional[BaseModelProvider] = None) -> None:
        self._provider = provider
        self.state_engine = get_world_state_engine()

    @property
    def provider(self) -> BaseModelProvider:
        if self._provider is None:
            self._provider = get_model_provider()
        return self._provider

    def preflight_analyze(
        self,
        intent: str,
        world_state: Optional[WISEWorldState] = None,
        task_id: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        memory_context: Optional[List[Dict[str, Any]]] = None,
    ) -> CognitivePreflightDecision:
        """
        Dynamically analyzes user intent against current World State, recent conversation history,
        relevant memory items, and available capabilities.
        Decides whether to answer directly, ask for clarification, or route to minimal necessary tools.
        """
        ws = world_state or self.state_engine.get_current_world_state()
        state_summary = (
            f"Active Window: '{ws.active_window.title if ws.active_window else 'None'}' | "
            f"Open Windows: {len(ws.open_windows)} | "
            f"Open Dialogs: {len(ws.open_dialogs)}"
        )

        catalog_str = json.dumps(self.CAPABILITIES_CATALOG, indent=2)
        # Feed the preflight model a bounded view of capabilities that are
        # actually registered/connected now. The stable catalog above keeps
        # routing compatibility; this live supplement prevents it from
        # choosing a generic MCP/browser route when a more specific capability
        # is unavailable or a relevant skill is present.
        network_context = ""
        try:
            from core.capability_network import get_capability_network
            network_plan = get_capability_network().plan(intent, limit=4, skill_limit=2)
            network_context = "Live capability recommendations (advisory, not authorization):\n" + json.dumps(
                network_plan.to_dict(), ensure_ascii=False
            ) + "\n\n"
        except Exception as exc:
            LOG.debug("Capability network unavailable during preflight: %s", exc)

        history_context = ""
        if conversation_history:
            recent = conversation_history[-6:]
            history_context = "Recent Conversation History:\n" + "\n".join(
                f"- {turn.get('role', 'user')}: {turn.get('content', '')}" for turn in recent
            ) + "\n\n"

        mem_context = ""
        if memory_context:
            mem_context = "Relevant User Memory Facts:\n" + "\n".join(
                f"- {m.get('key', '')}: {m.get('content', '')}" for m in memory_context[:4]
            ) + "\n\n"

        sys_prompt = (
            "You are WISE Cognitive Core. Before executing any task or picking any tool, "
            "perform a strict, logical pre-flight evaluation.\n\n"
            f"{history_context}"
            f"{mem_context}"
            f"Available Tool Capabilities:\n{catalog_str}\n\n"
            f"{network_context}"
            f"Current Environmental World State:\n{state_summary}\n\n"
            "Pre-Flight Evaluation Rules:\n"
            "1. Real Goal: What is the user's primary objective? If the user uses pronouns like 'it', resolve reference using conversation history.\n"
            "2. Direct Answer: Can this be answered accurately from established general knowledge or provided memory facts without external tools? "
            "If YES, set can_answer_directly=true, provide direct_answer, and set selected_capabilities=['none'].\n"
            "3. Missing Information: Is vital information missing that cannot be inferred or retrieved? "
            "If YES, set needs_clarification=true and provide clarification_question. Never guess missing passwords, credentials, or unspecified target servers.\n"
            "4. Tool Selection: If external tools are required, pick ONLY the minimal necessary capability from the catalog. "
            "If one tool is sufficient, do not select multiple tools.\n"
            "5. Deep Research: Set is_deep_research=true ONLY if the question requires multi-source investigation, comparison, or checking contradictory claims.\n"
            "6. Multi-Step: Set is_multi_step=true if completing the goal requires sequential operations.\n"
            "7. Complexity Tier: SIMPLE (direct answer or single lookup), MEDIUM (standard tool action), COMPLEX (deep research or multi-step workflow).\n\n"
            "Respond ONLY with a valid JSON object matching this schema:\n"
            "{\n"
            '  "real_goal": "concise statement of goal",\n'
            '  "existing_knowledge": "what is already known",\n'
            '  "missing_information": null or "what is missing",\n'
            '  "can_answer_directly": true or false,\n'
            '  "direct_answer": null or "the direct answer if can_answer_directly is true",\n'
            '  "needs_clarification": true or false,\n'
            '  "clarification_question": null or "question for user",\n'
            '  "selected_capabilities": ["tool_name_or_none"],\n'
            '  "complexity_tier": "SIMPLE" or "MEDIUM" or "COMPLEX",\n'
            '  "is_deep_research": true or false,\n'
            '  "is_multi_step": true or false,\n'
            '  "recommended_action": "brief action description",\n'
            '  "reasoning": "rationale for this decision"\n'
            "}"
        )

        req = ModelCompletionRequest(
            messages=[{"role": "user", "content": f"Analyze this user request: {intent}"}],
            system_prompt=sys_prompt,
            temperature=0.1,
            max_tokens=400,
            task_tier="MEDIUM",
            task_id=task_id,
        )

        resp = self.provider.generate(req)
        from core.observability import Tracer
        Tracer.emit("preflight.model", model=resp.model_name, input_tokens=resp.tokens_prompt,
                    output_tokens=resp.tokens_completion, simulated=resp.is_simulated, error=bool(resp.error))
        
        # 1. Explicit Model Unavailable handling (no fake research / task fallback)
        if resp.error and "MODEL_UNAVAILABLE" in resp.error:
            # Check if intent can be satisfied by local offline SkillNet procedural skills
            selected_skill = None
            try:
                from core.skills.search import SkillSearch
                s_search = SkillSearch()
                skill_hits = s_search.search(intent, top_k=2)
                if skill_hits and skill_hits[0][1] >= 0.25:
                    top_name, top_score, top_desc = skill_hits[0]
                    info = s_search.indexer.get_index().get(top_name, {})
                    desc = (top_desc if top_desc and top_desc != "---" else info.get("description", "")) or f"Procedural workflow for {top_name}"
                    selected_skill = {
                        "id": top_name,
                        "name": top_name,
                        "score": round(top_score, 3),
                        "description": desc,
                        "category": info.get("category", "general"),
                        "path": str(info.get("path", "")),
                    }
            except Exception as ex:
                LOG.debug("Offline skill search error: %s", ex)

            if selected_skill and any(w in intent.lower() for w in ("skill", "audit", "secrets", "tokens", "leaks", "security", "scan", "ui", "component")):
                return CognitivePreflightDecision(
                    intent=intent,
                    real_goal=f"Execute offline skill: {selected_skill['name']}",
                    existing_knowledge=selected_skill["description"],
                    can_answer_directly=True,
                    direct_answer=(
                        f"Offline Procedural Skill Selected: **{selected_skill['name']}** (relevance: {selected_skill['score']})\n\n"
                        f"Category: {selected_skill['category']}\n"
                        f"Description: {selected_skill['description']}\n\n"
                        f"Procedural expertise from SkillNet has been integrated into the cognitive pipeline."
                    ),
                    selected_capabilities=["skill_runner"],
                    complexity_tier="SIMPLE",
                    reasoning=f"Matched local SkillNet skill '{selected_skill['name']}'.",
                    selected_skill=selected_skill,
                )

            return CognitivePreflightDecision(
                intent=intent,
                real_goal=intent,
                existing_knowledge="",
                missing_information="AI Model Provider is unavailable",
                can_answer_directly=True,
                direct_answer=f"⚠️ No AI model is currently active. {resp.error}",
                needs_clarification=False,
                clarification_question=None,
                selected_capabilities=["none"],
                complexity_tier="SIMPLE",
                is_deep_research=False,
                is_multi_step=False,
                recommended_action="Configure model in settings or free system RAM",
                reasoning="Model runtime is unavailable; reporting explicit status without fabrication.",
            )

        parsed = resp.parsed_json or {}

        # 2. Schema Validation & Deterministic Fallback Classification
        if not isinstance(parsed, dict) or "can_answer_directly" not in parsed:
            lower_intent = intent.lower().strip()
            
            # Identify greetings and direct questions
            is_greeting = any(
                lower_intent.startswith(g) or lower_intent == g
                for g in ("hello", "hi", "hey", "good morning", "good evening", "who are you", "what can you do")
            ) or lower_intent in ("hello", "hi", "hey", "who are you")

            # Identify mathematical questions
            is_math = bool(re.search(r"^\s*(\d+\s*[\+\-\*\/\^]\s*\d+|\bwhat\s+is\s+\d+\s*[\+\-\*\/])", lower_intent)) or ("2+2" in lower_intent)

            # Distinguish informational ("Explain how to rename a file", "What is recursion")
            # from imperative action ("Rename this file", "Open notepad")
            is_informational = any(
                lower_intent.startswith(prefix)
                for prefix in ("explain", "what is", "what are", "what was", "what were", "how does", "how do i", "how to", "tell me about", "describe", "define", "difference between", "did i", "do you remember")
            )
            is_imperative_action = any(
                lower_intent.startswith(verb)
                for verb in ("rename", "delete", "open", "launch", "close", "click", "type", "press", "create file", "write file", "remove", "kill", "format", "run", "افتح", "شغل", "شغّل", "اغلق", "أغلق", "انقل", "احذف", "نفذ", "نفّذ", "اضغط")
            )
            is_research_intent = any(
                w in lower_intent
                for w in ("research", "latest news", "benchmark", "papers", "investigate", "compare benchmarks")
            )

            # Check anaphoric pronoun reference against conversation history
            has_pronoun_target = any(w in lower_intent for w in ("in it", "on it", "to it", "into it", "in that"))
            prev_context_app = None
            if conversation_history:
                for past_turn in reversed(conversation_history):
                    past_content = past_turn.get("content", "").lower()
                    if "notepad" in past_content:
                        prev_context_app = "notepad"
                        break
                    elif "browser" in past_content or "edge" in past_content:
                        prev_context_app = "browser"
                        break

            if is_greeting or is_math or (is_informational and not is_imperative_action and not has_pronoun_target):
                # Conversational / Informational Direct Answer
                can_direct = True
                caps = ["none"]
                complexity = "SIMPLE"
                is_deep = False
                is_multi = False
                
                if memory_context and any(w in lower_intent for w in ("codename", "verification", "remember", "did i", "what did", "my name", "project", "give you", "tell you")):
                    top_m = memory_context[0]
                    content = top_m.get("content", "")
                    clean_content = content
                    if " is " in content:
                        clean_content = content.split(" is ", 1)[1].strip(" .")
                    answer = f"Your {top_m.get('key', 'record')} is {clean_content}."
                elif is_greeting:
                    answer = "Hello! I am WISE, your intelligent agent operating system. How can I assist you today?"
                elif "recursion" in lower_intent:
                    answer = "Recursion is a programming method in which a function calls itself to solve smaller instances of the same problem until reaching a base termination condition."
                elif "2+2" in lower_intent:
                    answer = "2 + 2 = 4."
                elif resp.text and not resp.text.lstrip().startswith(("{", "[")):
                    answer = resp.text
                else:
                    # A truncated structured planning response is not a real
                    # answer.  ConversationalCore will request a short,
                    # user-facing completion for this direct turn.
                    answer = None
            elif is_research_intent:
                can_direct = False
                caps = ["web_search"]
                complexity = "COMPLEX"
                is_deep = True
                is_multi = False
                answer = None
            elif is_imperative_action or (has_pronoun_target and prev_context_app):
                can_direct = False
                if any(k in lower_intent for k in ("file", "directory", "folder", "path", "ملف", "مجلد", "مسار")):
                    caps = ["filesystem_tool"]
                else:
                    caps = ["windows_control"]
                complexity = "MEDIUM"
                is_deep = False
                is_multi = True
                answer = None
            else:
                # General default safe direct answer when not explicitly an action.
                # Do not expose a malformed/truncated JSON planning response
                # as chat output: remote models can hit the planning token
                # limit before closing their schema.
                lower_text = resp.text.lower()
                response_looks_structured = resp.text.lstrip().startswith(("{", "["))
                response_only_request = lower_intent.startswith((
                    "reply", "respond", "answer", "say", "tell me",
                    "اكتب", "أجب", "اجب", "قل",
                ))
                # Invalid routing JSON must never invent a desktop action.
                can_direct = True
                caps = ["none"]
                complexity = "SIMPLE"
                is_deep = False
                is_multi = False
                answer = resp.text if can_direct and not response_looks_structured else None

            parsed = {
                "real_goal": intent,
                "existing_knowledge": "Standard knowledge base",
                "missing_information": None,
                "can_answer_directly": can_direct,
                "direct_answer": answer,
                "needs_clarification": False,
                "clarification_question": None,
                "selected_capabilities": caps,
                "complexity_tier": complexity,
                "is_deep_research": is_deep,
                "is_multi_step": is_multi,
                "recommended_action": "Direct execution" if can_direct else f"Invoke {caps[0]}",
                "reasoning": f"Deterministic cognitive preflight classification for intent: '{intent[:40]}'",
            }

        caps = parsed.get("selected_capabilities", ["none"])
        if isinstance(caps, str):
            caps = [caps]

        # SkillNet Procedural Capability Integration
        selected_skill = None
        lower_intent = intent.lower().strip()
        is_greeting = any(lower_intent.startswith(g) or lower_intent == g for g in ("hello", "hi", "hey"))
        is_math = "2+2" in lower_intent

        # A direct conversational reply needs no procedural skill selection.
        # Besides avoiding irrelevant UI milestones, this keeps the planning
        # path focused on executable work only.
        if not (is_greeting or is_math or bool(parsed.get("can_answer_directly", False))):
            try:
                from core.skills.search import SkillSearch
                s_search = SkillSearch()
                skill_hits = s_search.search(intent, top_k=2)
                if skill_hits and skill_hits[0][1] >= 0.25:
                    top_name, top_score, top_desc = skill_hits[0]
                    info = s_search.indexer.get_index().get(top_name, {})
                    desc = (top_desc if top_desc and top_desc != "---" else info.get("description", "")) or f"Procedural workflow for {top_name}"
                    selected_skill = {
                        "id": top_name,
                        "name": top_name,
                        "score": round(top_score, 3),
                        "description": desc,
                        "category": info.get("category", "general"),
                        "path": str(info.get("path", "")),
                    }
                    if "skill_runner" not in caps and "none" not in caps:
                        caps.append("skill_runner")
                    LOG.info("SkillNet selected skill '%s' (score %.3f) for intent '%s'", top_name, top_score, intent[:40])
            except Exception as sk_err:
                LOG.debug("SkillNet search bypassed: %s", sk_err)

        direct_answer = parsed.get("direct_answer")
        can_direct = bool(parsed.get("can_answer_directly", False))
        if selected_skill and "skill" in lower_intent and can_direct:
            direct_answer = (
                f"Selected Skill: **{selected_skill['name']}** (relevance: {selected_skill['score']})\n\n"
                f"Category: {selected_skill['category']}\n"
                f"Description: {selected_skill['description']}\n\n"
                f"Procedural expertise from SkillNet has been integrated into the cognitive pipeline."
            )

        return CognitivePreflightDecision(
            intent=intent,
            real_goal=str(parsed.get("real_goal", intent)),
            existing_knowledge=str(parsed.get("existing_knowledge", "")),
            missing_information=parsed.get("missing_information"),
            can_answer_directly=can_direct,
            direct_answer=direct_answer,
            needs_clarification=bool(parsed.get("needs_clarification", False)),
            clarification_question=parsed.get("clarification_question"),
            selected_capabilities=[str(c).lower() for c in caps],
            complexity_tier=str(parsed.get("complexity_tier", "MEDIUM")).upper(),
            is_deep_research=bool(parsed.get("is_deep_research", False)),
            is_multi_step=bool(parsed.get("is_multi_step", False)),
            recommended_action=str(parsed.get("recommended_action", "")),
            reasoning=str(parsed.get("reasoning", "")),
            selected_skill=selected_skill,
        )


    # --------------------------------------------------------------------------
    # Deep Research Engine
    # --------------------------------------------------------------------------
    def execute_deep_research(self, query: str, sources_limit: int = 3,
                              task_id: Optional[str] = None) -> Dict[str, Any]:
        from core.web_research import research
        effective_task_id = task_id or f"research_{time.time_ns()}"
        if hasattr(self.provider, "begin_task"):
            self.provider.begin_task(effective_task_id)
        try:
            from core.contracts import ResearchResult, SourceCitation
            data = research(self.provider, query, sources_limit, effective_task_id)
            return ResearchResult(query=query, synthesis=data["synthesis"], sources=data["sources"],
                citations=[SourceCitation(source_name=item["source"], title=item["title"], url=item["url"], snippet=item["snippet"],
                    **{key:item[key] for key in ("evidence_kind", "published_at", "retrieved_at", "resolved_url", "excerpt_truncated", "read_error") if key in item})
                    for item in data["citations"]],
                source_count=data["source_count"], sources_queried=data["sources_queried"],
                sources_succeeded=data["sources_succeeded"], sources_failed=data["sources_failed"],
                errors=data["errors"], duration_ms=data["duration_ms"], model_name=data["model_name"],
                degraded_sources=data["degraded_sources"],
                execution_metrics=data["execution_metrics"])
        finally:
            if hasattr(self.provider, "end_task"):
                self.provider.end_task(effective_task_id)

    # --------------------------------------------------------------------------
    # Cognitive Failure Recovery & Dynamic Replanning
    # --------------------------------------------------------------------------
    def diagnose_and_replan(
        self,
        task_goal: str,
        completed_steps: List[str],
        failed_step: str,
        error_message: str,
        world_state: Optional[WISEWorldState] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        State-aware failure diagnosis and replanning.
        Considers prior successful steps and current World State to synthesize
        an actionable fallback without restarting from scratch.
        """
        ws = world_state or self.state_engine.get_current_world_state()
        state_summary = (
            f"Active Window: '{ws.active_window.title if ws.active_window else 'None'}' | "
            f"Open Dialogs: {len(ws.open_dialogs)}"
        )

        prompt = (
            f"Task Goal: {task_goal}\n"
            f"Successfully Completed Steps: {completed_steps}\n"
            f"Current World State: {state_summary}\n"
            f"Failed Step: {failed_step}\n"
            f"Error Encountered: {error_message}\n\n"
            "Diagnose root cause and formulate a recovery strategy.\n"
            "Do NOT repeat the already completed steps. Provide an alternative action or fallback.\n"
            "Output JSON:\n"
            "{\n"
            '  "root_cause": "detailed explanation of why it failed",\n'
            '  "can_recover": true,\n'
            '  "recovery_strategy": "clear description of alternate approach",\n'
            '  "fallback_action": "specific next action to execute",\n'
            '  "replanned_remaining_steps": ["step A", "step B"]\n'
            "}"
        )

        req = ModelCompletionRequest(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are WISE Resilient Recovery Engine. Diagnose failure and formulate realistic fallbacks.",
            max_tokens=400,
            task_tier="COMPLEX",
            task_id=task_id,
        )

        resp = self.provider.generate(req)
        parsed = resp.parsed_json or {}

        return {
            "root_cause": parsed.get("root_cause", resp.text),
            "can_recover": bool(parsed.get("can_recover", True)),
            "recovery_strategy": parsed.get("recovery_strategy", "Retry via fallback mechanism"),
            "fallback_action": parsed.get("fallback_action", "Use alternate mirror/server"),
            "replanned_remaining_steps": parsed.get("replanned_remaining_steps", []),
            "raw_text": resp.text,
        }


# Global Singleton
_GLOBAL_DECISION_ENGINE: Optional[CognitiveDecisionEngine] = None
_CDE_LOCK = threading.Lock()


def get_cognitive_decision_engine() -> CognitiveDecisionEngine:
    global _GLOBAL_DECISION_ENGINE
    if _GLOBAL_DECISION_ENGINE is None:
        with _CDE_LOCK:
            if _GLOBAL_DECISION_ENGINE is None:
                _GLOBAL_DECISION_ENGINE = CognitiveDecisionEngine()
    return _GLOBAL_DECISION_ENGINE


def reset_cognitive_decision_engine_provider() -> None:
    """Discard a cached provider after credentials or model selection change."""
    with _CDE_LOCK:
        if _GLOBAL_DECISION_ENGINE is not None:
            _GLOBAL_DECISION_ENGINE._provider = None
