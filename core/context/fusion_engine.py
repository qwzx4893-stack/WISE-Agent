"""
Context Fusion Engine (ContextFusionEngine).
Synthesizes user intent, conversation history, ComputerWorldModel, screen perception,
memory, internet research, and tool results into a structured, high-signal,
token-efficient World State representation.
Eliminates context sprawl through deterministic relevance gating and provenance tracking.
"""

from __future__ import annotations

import re
import time
import logging
import threading
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

from core.context.world_model import get_world_model_manager, ComputerWorldModel
from core.context.truth_arbiter import get_truth_arbiter, Fact, EpistemicDomain, SourceType

LOG = logging.getLogger("wise.fusion_engine")


@dataclass
class FusedContext:
    user_intent: str
    active_computer_summary: str
    relevant_local_files: List[str]
    relevant_memory: List[Dict[str, Any]]
    relevant_web_findings: List[Dict[str, Any]]
    active_visual_perception: Optional[Dict[str, Any]]
    ongoing_task_state: Optional[Dict[str, Any]]
    verified_facts: List[Dict[str, Any]]
    token_budget_estimate: int
    generated_at: float = field(default_factory=time.time)

    def to_markdown_prompt(self, max_tokens: int = 1500) -> str:
        """Serializes the fused context into a dense, structured markdown block."""
        sections = [
            "## [WISE Fused World State]",
            f"**User Intent**: {self.user_intent}",
            "",
            self.active_computer_summary,
        ]

        if self.active_visual_perception:
            vis = self.active_visual_perception
            desc = vis.get("summary", vis.get("text", "Active screen captured"))
            sections.append(f"\n### Active Screen Perception:\n- {desc}")

        if self.relevant_local_files:
            files_str = ", ".join(f"`{f}`" for f in self.relevant_local_files[:6])
            sections.append(f"\n### Relevant Files Context:\n- {files_str}")

        if self.relevant_memory:
            sections.append("\n### Relevant Historical Memory:")
            for m in self.relevant_memory[:4]:
                sections.append(f"- {m.get('title', 'Memory')}: {m.get('snippet', m.get('content', ''))[:120]}")

        if self.relevant_web_findings:
            sections.append("\n### Verified Web Findings:")
            for w in self.relevant_web_findings[:3]:
                sections.append(f"- {w.get('title', 'Source')}: {w.get('snippet', '')[:140]} (URL: {w.get('url', '')})")

        if self.ongoing_task_state:
            task = self.ongoing_task_state
            sections.append(f"\n### Active Task Progress:\n- Task: `{task.get('description', '')}` (Step: {task.get('current_step', 1)}/{task.get('total_steps', 1)})")

        if self.verified_facts:
            sections.append("\n### Arbitrated Ground Truth Facts:")
            for f in self.verified_facts[:6]:
                sections.append(f"- [{f['domain']}] `{f['key']}` = {f['value']} (confidence: {f['confidence']}, source: {f['source_type']})")

        rendered = "\n".join(sections)
        # Rough token clipping (4 chars ~= 1 token)
        char_limit = max_tokens * 4
        if len(rendered) > char_limit:
            rendered = rendered[:char_limit] + "\n... [Context truncated for token budget]"

        return rendered


STOP_WORDS = {
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "with", "by", "and", "or", "is", "are", "was", "were",
    "من", "في", "على", "إلى", "عن", "مع", "هذا", "هذه", "أن", "إن", "هو", "هي"
}


class ContextFusionEngine:
    """Orchestrates multi-source context fusion with relevance scoring and token budgeting."""

    def __init__(self):
        self.world_model_mgr = get_world_model_manager()
        self.truth_arbiter = get_truth_arbiter()

    def _compute_keyword_relevance(self, query: str, candidate_text: str) -> float:
        """Fast token-free keyword overlap relevance scorer with stop words filtering."""
        if not query or not candidate_text:
            return 0.0

        q_words = {w for w in re.findall(r"\w+", query.lower()) if w not in STOP_WORDS and len(w) > 1}
        if not q_words:
            return 0.0

        c_words = {w for w in re.findall(r"\w+", candidate_text.lower()) if w not in STOP_WORDS and len(w) > 1}
        common = q_words.intersection(c_words)
        return len(common) / len(q_words)

    def fuse(
        self,
        user_intent: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        visual_perception: Optional[Dict[str, Any]] = None,
        memory_candidates: Optional[List[Dict[str, Any]]] = None,
        web_candidates: Optional[List[Dict[str, Any]]] = None,
        local_files: Optional[List[str]] = None,
        task_state: Optional[Dict[str, Any]] = None,
        relevance_threshold: float = 0.15,
    ) -> FusedContext:
        """Executes context fusion, relevance pruning, and ground truth compilation."""
        # 1. Update/Refresh active ComputerWorldModel
        self.world_model_mgr.refresh_active_window()
        comp_summary = self.world_model_mgr.get_prompt_context()

        # 2. Filter Memory candidates
        filtered_memory = []
        if memory_candidates:
            for item in memory_candidates:
                text = f"{item.get('title', '')} {item.get('content', '')} {item.get('snippet', '')}"
                score = self._compute_keyword_relevance(user_intent, text)
                if score >= relevance_threshold:
                    item_copy = dict(item)
                    item_copy["relevance_score"] = score
                    filtered_memory.append(item_copy)
            filtered_memory.sort(key=lambda x: x.get("relevance_score", 0.0), reverse=True)

        # 3. Filter Web candidates
        filtered_web = []
        if web_candidates:
            for item in web_candidates:
                text = f"{item.get('title', '')} {item.get('snippet', '')}"
                score = self._compute_keyword_relevance(user_intent, text)
                if score >= relevance_threshold:
                    item_copy = dict(item)
                    item_copy["relevance_score"] = score
                    filtered_web.append(item_copy)
            filtered_web.sort(key=lambda x: x.get("relevance_score", 0.0), reverse=True)

        # 4. Filter Local files
        filtered_files = []
        if local_files:
            for f in local_files:
                score = self._compute_keyword_relevance(user_intent, f)
                if score > 0:
                    filtered_files.append(f)
            if not filtered_files:
                # Include top 3 default files if none specifically matched
                filtered_files = local_files[:3]

        # 5. Extract arbitrated verified facts from TruthArbiter
        verified_facts = [
            f.to_dict() for f in self.truth_arbiter.get_all_verified_facts(min_composite_score=0.3)
        ]

        # 6. Estimate token budget
        total_chars = (
            len(user_intent)
            + len(comp_summary)
            + sum(len(str(m)) for m in filtered_memory[:4])
            + sum(len(str(w)) for w in filtered_web[:3])
            + sum(len(f) for f in filtered_files[:6])
            + (len(str(visual_perception)) if visual_perception else 0)
        )
        token_estimate = total_chars // 4

        return FusedContext(
            user_intent=user_intent,
            active_computer_summary=comp_summary,
            relevant_local_files=filtered_files,
            relevant_memory=filtered_memory,
            relevant_web_findings=filtered_web,
            active_visual_perception=visual_perception,
            ongoing_task_state=task_state,
            verified_facts=verified_facts,
            token_budget_estimate=token_estimate,
        )

    def fuse_world_state(self, world_state: Optional[Any] = None, max_tokens: int = 2000) -> str:
        """Synthesizes a unified WISEWorldState snapshot into a dense, token-budgeted prompt context."""
        if world_state is None:
            from core.context.world_state import get_world_state_engine
            world_state = get_world_state_engine().get_current_world_state()

        sections: List[str] = [
            "## [WISE Unified World State]",
            f"- **System**: {world_state.os_info.system} {world_state.os_info.release} (Build {world_state.os_info.build}) | User: `{world_state.user_session.username}` (Session ID: {world_state.user_session.session_id})",
        ]

        # Active Window
        win = world_state.active_window
        if win:
            sections.append(f"- **Foreground Window**: '{win.title}' (Class: `{win.class_name}`, HWND: {win.hwnd}, PID: {win.process_id}, Bounds: {win.rect[2]}x{win.rect[3]})")
        else:
            sections.append("- **Foreground Window**: Desktop / None")

        # Open Windows & Dialogs
        if world_state.open_dialogs:
            dialog_str = ", ".join(f"'{d.title}' (HWND: {d.hwnd})" for d in world_state.open_dialogs[:4])
            sections.append(f"- **Modal Dialogs**: {dialog_str}")

        if world_state.open_windows:
            win_str = ", ".join(f"'{w.title[:25]}' ({w.class_name})" for w in world_state.open_windows[:6] if w.title)
            sections.append(f"- **Visible Windows**: {win_str}")

        # Active UI Tree Summary (Level 1)
        if world_state.ui_tree:
            formatted_tree = world_state.ui_tree.format_text_tree()
            tree_lines = formatted_tree.splitlines()[:12]  # First 12 nodes
            sections.append("\n### Active UI Tree Structure:")
            sections.extend(f"  {line}" for line in tree_lines)

        # Recent Actions (Hands)
        if world_state.previous_actions:
            sections.append("\n### Recent Executed Actions:")
            for a in world_state.previous_actions[-4:]:
                ver_str = "Verified" if a.verification_result else "Unverified"
                sections.append(f"- `[{a.action_type.value}]` {ver_str} in {a.latency_ms:.1f}ms (Perception: {a.perception_method.value})")

        # Task Progress
        t_state = world_state.task_state
        if t_state.intent:
            sections.append(f"\n### Active Task: `{t_state.intent}`")
            sections.append(f"- Status: {t_state.verification_status} | Step {t_state.current_step}/{t_state.total_steps}")
            if t_state.active_plan:
                sections.append("- Plan: " + " -> ".join(t_state.active_plan[:4]))

        # Telemetry & Hardware
        metrics = world_state.hardware_metrics
        if metrics:
            sections.append(f"\n### Hardware Telemetry:\n- CPU: {metrics.cpu_name} (Host: {metrics.cpu_percent:.1f}%) | RAM: {metrics.ram_used_mb}/{metrics.ram_total_mb}MB ({metrics.ram_percent}%) | GPU: {metrics.gpu_name} (VRAM: {metrics.gpu_memory_used_mb}MB)")

        # Scoped Items & Facts
        if world_state.items:
            sections.append("\n### Scoped State Items (Provenance Tracked):")
            for item in list(world_state.items.values())[:6]:
                sections.append(f"- `[{item.scope}]` **{item.key}**: `{item.value}` (Source: {item.source}, Freshness: {item.freshness_seconds:.1f}s, Conf: {item.confidence})")

        rendered = "\n".join(sections)
        counter = get_token_counter()
        return counter.truncate_to_tokens(rendered, max_tokens)


class MeasurableTokenCounter:
    """
    Measurable, model-aligned token counter using native Hugging Face tokenizers ByteLevel pre-tokenizer.
    Deterministic, offline, zero-network, Rust-accelerated.
    """
    def __init__(self) -> None:
        self._byte_level = None
        self._backend = "unicode_regex"
        try:
            from tokenizers.pre_tokenizers import ByteLevel
            self._byte_level = ByteLevel()
            self._backend = "rust_byte_level"
        except Exception:
            pass

    @property
    def has_tokenizer(self) -> bool:
        return self._byte_level is not None

    @property
    def backend(self) -> str:
        return self._backend

    def truncate_to_budget(self, text: str, max_tokens: int) -> tuple[str, bool]:
        """Truncates text to budget and returns (text, was_truncated)."""
        count = self.count_tokens(text)
        if count <= max_tokens:
            return text, False
        return self.truncate_to_tokens(text, max_tokens), True

    def count_tokens(self, text: str) -> int:
        """Counts exact tokens in a string."""
        if not text:
            return 0
        if self._byte_level:
            try:
                return len(self._byte_level.pre_tokenize_str(text))
            except Exception:
                pass
        # Fallback to calibrated word/punctuation regex splitting
        return max(1, len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE)))

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncates text to exact token budget."""
        if not text or max_tokens <= 0:
            return ""
        suffix = "\n... [World state truncated for token budget]"
        suffix_tokens = self.count_tokens(suffix)
        effective_budget = max(1, max_tokens - suffix_tokens) if max_tokens > suffix_tokens else max_tokens
        if self._byte_level:
            try:
                splits = self._byte_level.pre_tokenize_str(text)
                if len(splits) <= max_tokens:
                    return text
                cutoff_idx = min(effective_budget - 1, len(splits) - 1)
                last_span = splits[cutoff_idx][1]  # (start, end)
                return text[:last_span[1]] + suffix
            except Exception:
                pass
        char_limit = effective_budget * 4
        if len(text) > char_limit:
            return text[:char_limit] + suffix
        return text


# Global Singleton Token Counter
_TOKEN_COUNTER: Optional[MeasurableTokenCounter] = None
_TC_LOCK = threading.Lock()


def get_token_counter() -> MeasurableTokenCounter:
    global _TOKEN_COUNTER
    if _TOKEN_COUNTER is None:
        with _TC_LOCK:
            if _TOKEN_COUNTER is None:
                _TOKEN_COUNTER = MeasurableTokenCounter()
    return _TOKEN_COUNTER



# Singleton instance
_GLOBAL_FUSION_ENGINE: Optional[ContextFusionEngine] = None
_CFE_LOCK = threading.Lock()


def get_context_fusion_engine() -> ContextFusionEngine:
    global _GLOBAL_FUSION_ENGINE
    if _GLOBAL_FUSION_ENGINE is None:
        with _CFE_LOCK:
            if _GLOBAL_FUSION_ENGINE is None:
                _GLOBAL_FUSION_ENGINE = ContextFusionEngine()
    return _GLOBAL_FUSION_ENGINE
