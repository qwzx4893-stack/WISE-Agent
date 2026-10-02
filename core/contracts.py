"""
Canonical Internal Contracts Layer for WISE.

Provides strict, consolidated, typed data contracts for all core subsystems:
ModelProvider, Cognition/Preflight, Conversation, Tooling, Research, Memory,
TaskEngine, Computer Use, Security, Events, and Voice.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union, Tuple


# ==============================================================================
# 1. Model Runtime Contracts
# ==============================================================================

class ModelProviderType(str, Enum):
    LOCAL_GGUF = "LOCAL_GGUF"
    LOCAL_EMBEDDED = "LOCAL_EMBEDDED"
    LOCAL_MOE = "LOCAL_MOE"
    LFM2_5_COGNITIVE_CORE = "LFM2_5_COGNITIVE_CORE"
    CLOUD_OPENAI_COMPATIBLE = "CLOUD_OPENAI_COMPATIBLE"
    OLLAMA = "OLLAMA"
    GROQ = "GROQ"
    DEEPSEEK = "DEEPSEEK"
    SIMULATED_TEST = "SIMULATED_TEST"
    UNAVAILABLE = "UNAVAILABLE"


class ModelWorkloadType(str, Enum):
    INTENT_CLASSIFICATION = "INTENT_CLASSIFICATION"
    SIMPLE_CONVERSATION = "SIMPLE_CONVERSATION"
    PLANNING = "PLANNING"
    TOOL_SELECTION = "TOOL_SELECTION"
    RECOVERY_REASONING = "RECOVERY_REASONING"
    SUMMARIZATION = "SUMMARIZATION"
    CODING = "CODING"
    RESEARCH_SYNTHESIS = "RESEARCH_SYNTHESIS"
    VISUAL_GROUNDING = "VISUAL_GROUNDING"
    VERIFICATION_REASONING = "VERIFICATION_REASONING"
    LONG_CONTEXT_ANALYSIS = "LONG_CONTEXT_ANALYSIS"


@dataclass
class ModelRequest:
    messages: List[Dict[str, str]]
    system_prompt: str = ""
    temperature: float = 0.2
    max_tokens: int = 1024
    json_schema: Optional[Dict[str, Any]] = None
    task_tier: str = "MEDIUM"  # SIMPLE | MEDIUM | COMPLEX
    task_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "messages": self.messages,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "json_schema": self.json_schema,
            "task_tier": self.task_tier,
            "task_id": self.task_id,
        }


@dataclass
class ModelResponse:
    text: str
    parsed_json: Optional[Union[Dict[str, Any], List[Any]]] = None
    tokens_prompt: int = 0
    tokens_completion: int = 0
    latency_ms: float = 0.0
    provider_type: ModelProviderType = ModelProviderType.UNAVAILABLE
    model_name: str = "unknown"
    is_simulated: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "parsed_json": self.parsed_json,
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "latency_ms": round(self.latency_ms, 2),
            "provider_type": self.provider_type.value if hasattr(self.provider_type, "value") else str(self.provider_type),
            "model_name": self.model_name,
            "is_simulated": self.is_simulated,
            "error": self.error,
        }


# ==============================================================================
# 2. Cognition & Preflight Contracts
# ==============================================================================

class IntentCategory(str, Enum):
    DIRECT = "DIRECT"
    CLARIFY = "CLARIFY"
    RESEARCH = "RESEARCH"
    TOOL = "TOOL"
    BROWSER = "BROWSER"
    DESKTOP = "DESKTOP"
    SCHEDULE = "SCHEDULE"
    VOICE = "VOICE"


@dataclass
class PreflightDecision:
    real_goal: str
    can_answer_directly: bool
    direct_answer: Optional[str] = None
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    selected_capabilities: List[str] = field(default_factory=list)
    complexity_tier: str = "SIMPLE"  # SIMPLE | MEDIUM | COMPLEX
    is_deep_research: bool = False
    is_multi_step: bool = False
    recommended_action: str = ""
    reasoning: str = ""
    intent_category: IntentCategory = IntentCategory.DIRECT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "real_goal": self.real_goal,
            "can_answer_directly": self.can_answer_directly,
            "direct_answer": self.direct_answer,
            "needs_clarification": self.needs_clarification,
            "clarification_question": self.clarification_question,
            "selected_capabilities": self.selected_capabilities,
            "complexity_tier": self.complexity_tier,
            "is_deep_research": self.is_deep_research,
            "is_multi_step": self.is_multi_step,
            "recommended_action": self.recommended_action,
            "reasoning": self.reasoning,
            "intent_category": self.intent_category.value if hasattr(self.intent_category, "value") else str(self.intent_category),
        }


# ==============================================================================
# 3. Conversation & Session Contracts
# ==============================================================================

@dataclass
class ConversationRequest:
    message: str
    session_id: Optional[str] = None
    user_id: Optional[str] = "default_user"
    modality: str = "chat"  # chat | voice | api | scheduler
    stream: bool = False
    context_attachments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ConversationResponse:
    session_id: str
    reply_text: str
    intent: str
    modality: str = "chat"
    action_type: str = "DIRECT"
    task_id: Optional[str] = None
    task_status: Optional[str] = None
    milestones: List[Dict[str, Any]] = field(default_factory=list)
    working_items: List[Dict[str, Any]] = field(default_factory=list)
    artifacts_created: List[Dict[str, Any]] = field(default_factory=list)
    paused_for_human: bool = False
    intervention_details: Optional[Dict[str, Any]] = None
    latency_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "reply_text": self.reply_text,
            "intent": self.intent,
            "modality": self.modality,
            "action_type": self.action_type,
            "task_id": self.task_id,
            "task_status": self.task_status,
            "milestones": self.milestones,
            "working_items": self.working_items,
            "artifacts_created": self.artifacts_created,
            "paused_for_human": self.paused_for_human,
            "intervention_details": self.intervention_details,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
        }


@dataclass
class SessionContext:
    """Canonical Unified Session Model across Chat, Voice, Scheduler, and API."""
    session_id: str
    history: List[Dict[str, str]] = field(default_factory=list)  # {"role": "user"|"assistant", "content": str}
    working_items: List[Dict[str, Any]] = field(default_factory=list)
    active_tasks: List[str] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    memory_context: List[Dict[str, Any]] = field(default_factory=list)
    voice_turns: List[Dict[str, Any]] = field(default_factory=list)
    hitl_state: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    model_context: Dict[str, Any] = field(default_factory=dict)
    last_query: str = ""
    last_response: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def active_task_id(self) -> Optional[str]:
        return self.active_tasks[-1] if self.active_tasks else None

    @active_task_id.setter
    def active_task_id(self, val: Optional[str]) -> None:
        if val and val not in self.active_tasks:
            self.active_tasks.append(val)
        elif val is None and self.active_tasks:
            self.active_tasks.pop()

    def add_turn(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        self.updated_at = time.time()
        if role == "user":
            self.last_query = content
        elif role == "assistant":
            self.last_response = content

    def add_task(self, task_id: str) -> None:
        if task_id not in self.active_tasks:
            self.active_tasks.append(task_id)
        self.updated_at = time.time()

    def add_artifact(self, artifact: Union[str, Dict[str, Any]]) -> None:
        if isinstance(artifact, str):
            self.artifacts.append({"path": artifact, "timestamp": time.time()})
        else:
            self.artifacts.append(artifact)
        self.updated_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "id": self.session_id,
            "title": self.metadata.get("title") or (self.last_query[:80] if self.last_query else "Session"),
            "history": list(self.history),
            "messages": list(self.history),
            "working_items": list(self.working_items),
            "active_tasks": list(self.active_tasks),
            "active_task_id": self.active_task_id,
            "artifacts": list(self.artifacts),
            "memory_context": list(self.memory_context),
            "voice_turns": list(self.voice_turns),
            "hitl_state": dict(self.hitl_state),
            "metadata": dict(self.metadata),
            "last_query": self.last_query,
            "last_response": self.last_response,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ==============================================================================
# 4. Tool & Capability Contracts
# ==============================================================================

class CapabilitySource(str, Enum):
    NATIVE = "NATIVE"
    TOOL_PACK = "TOOL_PACK"
    SKILLNET = "SKILLNET"
    MCP = "MCP"
    RAG = "RAG"
    BROWSER = "BROWSER"
    COMPUTER_USE = "COMPUTER_USE"
    MEMORY = "MEMORY"
    SCHEDULER = "SCHEDULER"


@dataclass
class ToolCall:
    tool_id: str
    capability_source: CapabilitySource
    parameters: Dict[str, Any]
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class ToolResult:
    call_id: str
    tool_id: str
    success: bool
    output: Any
    error: Optional[str] = None
    duration_ms: float = 0.0

    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "metadata": self.metadata,
        }


@dataclass
class CapabilityMetadata:
    id: str
    source: CapabilitySource
    name: str
    description: str
    parameters_schema: Dict[str, Any] = field(default_factory=dict)
    risk_level: str = "LOW"  # LOW | MEDIUM | HIGH | CRITICAL
    available: bool = True
    missing_dependency: Optional[str] = None


# ==============================================================================
# 5. Research & RAG Contracts
# ==============================================================================

@dataclass
class ResearchRequest:
    query: str
    sources_to_query: List[str] = field(default_factory=list)
    max_results_per_source: int = 3
    depth: str = "BALANCED"  # FAST | BALANCED | EXHAUSTIVE


@dataclass
class SourceCitation:
    source_name: str
    title: str
    url: str
    snippet: str
    timestamp: Optional[str] = None
    confidence: float = 1.0
    evidence_kind: str = "search_snippet"
    published_at: str = ""
    retrieved_at: str = ""
    resolved_url: str = ""
    excerpt_truncated: bool = False
    read_error: str = ""


@dataclass
class ResearchResult:
    """
    Canonical ResearchResult schema.
    Produces canonical 'synthesis' and full source/error metadata.
    Provides backward-compatibility dict access and '.summary' alias.
    """
    query: str
    synthesis: str
    citations: List[SourceCitation] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    source_count: int = 0
    sources_queried: List[str] = field(default_factory=list)
    sources_succeeded: List[str] = field(default_factory=list)
    sources_failed: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    degraded_sources: List[str] = field(default_factory=list)
    timing_metadata: Dict[str, float] = field(default_factory=dict)
    duration_ms: float = 0.0
    model_name: str = ""
    execution_metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """Backward-compatibility property for legacy consumers."""
        return self.synthesis

    def __getitem__(self, key: str) -> Any:
        d = self.to_dict()
        return d[key]

    def get(self, key: str, default: Any = None) -> Any:
        d = self.to_dict()
        return d.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "synthesis": self.synthesis,
            "summary": self.synthesis,  # Legacy alias
            "source_count": self.source_count or len(self.sources) or len(self.citations),
            "sources": self.sources or [
                {
                    "source": c.source_name,
                    "title": c.title,
                    "url": c.url,
                    "snippet": c.snippet,
                }
                for c in self.citations
            ],
            "citations": [
                {
                    "source": c.source_name,
                    "title": c.title,
                    "url": c.url,
                    "snippet": c.snippet,
                    "evidence_kind": c.evidence_kind,
                    "published_at": c.published_at,
                    "retrieved_at": c.retrieved_at,
                    "resolved_url": c.resolved_url,
                    "excerpt_truncated": c.excerpt_truncated,
                    "read_error": c.read_error,
                }
                for c in self.citations
            ],
            "sources_queried": self.sources_queried,
            "sources_succeeded": self.sources_succeeded,
            "sources_failed": self.sources_failed,
            "errors": self.errors,
            "degraded_sources": self.degraded_sources,
            "timing_metadata": self.timing_metadata,
            "duration_ms": round(self.duration_ms, 2),
            "model_name": self.model_name,
            "execution_metrics": self.execution_metrics,
        }


# ==============================================================================
# 6. Memory Contracts
# ==============================================================================

class MemoryTier(str, Enum):
    WORKING_SESSION = "WORKING_SESSION"
    USER_PREFERENCE = "USER_PREFERENCE"
    PROJECT = "PROJECT"
    TASK = "TASK"
    EPISODIC = "EPISODIC"


@dataclass
class MemoryItem:
    key: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    relevance_score: float = 1.0
    tier: MemoryTier = MemoryTier.EPISODIC
    superseded_by: Optional[str] = None
    access_count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "relevance_score": round(self.relevance_score, 3),
            "tier": self.tier.value if hasattr(self.tier, "value") else str(self.tier),
            "superseded_by": self.superseded_by,
            "access_count": self.access_count,
        }


@dataclass
class MemoryQuery:
    query_text: str
    session_id: Optional[str] = None
    tier: Optional[MemoryTier] = None
    limit: int = 5
    min_relevance: float = 0.5


@dataclass
class MemoryResult:
    items: List[MemoryItem] = field(default_factory=list)
    query_text: str = ""
    duration_ms: float = 0.0


# ==============================================================================
# 7. Action, Verification & Security Contracts
# ==============================================================================

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class VerificationType(str, Enum):
    ACTIVE_WINDOW = "active_window"
    WINDOW_EXISTS = "window_exists"
    TEXT_PRESENT = "text_present"
    TEXT_VISIBLE = "text_visible"
    TEXT_CHANGED = "text_changed"
    FILE_EXISTS = "file_exists"
    FILE_CONTENT = "file_content"
    PROCESS_RUNNING = "process_running"
    PROCESS_TERMINATED = "process_terminated"
    UI_ELEMENT_EXISTS = "ui_element_exists"
    UI_ELEMENT_DISAPPEARED = "ui_element_disappeared"
    DOM_ELEMENT = "dom_element"
    URL_MATCHES = "url_matches"
    URL_MATCH = "url_match"
    PAGE_TITLE = "page_title"
    DOWNLOAD_EXISTS = "download_exists"
    APPLICATION_STATE = "application_state"
    TOOL_RESULT = "tool_result"
    CUSTOM = "custom"


@dataclass
class VerificationSpec:
    """Canonical Verification Specification for Closed-Loop Hands Execution."""
    type: str = VerificationType.ACTIVE_WINDOW.value
    expected_title: Optional[str] = None
    expected_active_app: Optional[str] = None
    path: Optional[str] = None
    text: Optional[str] = None
    process: Optional[str] = None
    url: Optional[str] = None
    content: Optional[str] = None
    timeout_seconds: float = 5.0
    custom_evaluator: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "type": self.type,
            "expected_title": self.expected_title,
            "title": self.expected_title or self.expected_active_app,
            "expected_active_app": self.expected_active_app,
            "path": self.path,
            "text": self.text,
            "process": self.process,
            "url": self.url,
            "content": self.content,
            "timeout_seconds": self.timeout_seconds,
            "custom_evaluator": self.custom_evaluator,
            "parameters": self.parameters,
        }
        # Prune None entries
        return {k: v for k, v in d.items() if v is not None}


def normalize_verification_spec(raw: Union[Dict[str, Any], VerificationSpec, None]) -> Dict[str, Any]:
    """
    Normalizes arbitrary planner verification specifications into a canonical dictionary
    guaranteeing a valid 'type' key recognized by WISEHands.verify_condition.
    """
    if raw is None:
        return {"type": "always_true"}

    if isinstance(raw, VerificationSpec):
        return raw.to_dict()

    if not isinstance(raw, dict):
        return {"type": "always_true"}

    d = dict(raw)
    # If type is already present, normalize aliases
    t = d.get("type", "").lower()
    if t:
        if t in ("window_active", "active_window"):
            d["type"] = "window_active"
            if not d.get("title") and d.get("expected_title"):
                d["title"] = d["expected_title"]
            elif not d.get("title") and d.get("expected_active_app"):
                d["title"] = d["expected_active_app"]
        elif t in ("text_present", "text_visible"):
            d["type"] = "text_visible"
        elif t in ("text_changed", "changed_text"):
            d["type"] = "text_changed"
        elif t in ("url_matches", "url_match"):
            d["type"] = "url_match"
        elif t in ("process_terminated", "process_exited", "process_dead"):
            d["type"] = "process_terminated"
        elif t in ("ui_element_disappeared", "element_disappeared", "element_gone"):
            d["type"] = "ui_element_disappeared"
        elif t in ("ui_element_exists", "element_exists"):
            d["type"] = "ui_element_exists"
        elif t in ("page_title", "title_matches"):
            d["type"] = "page_title"
        elif t in ("download_exists", "download_complete", "download_completed"):
            d["type"] = "download_exists"
        return d

    # Infer type from keys
    if "expected_active_app" in d or "expected_title" in d or "title" in d:
        d["type"] = "window_active"
        if not d.get("title"):
            d["title"] = d.get("expected_title") or d.get("expected_active_app")
    elif "path" in d:
        d["type"] = "file_exists"
    elif "text" in d:
        d["type"] = "text_visible"
    elif "process" in d:
        d["type"] = "process_running"
    elif "url" in d:
        d["type"] = "url_match"
    else:
        d["type"] = "window_active"
        d["title"] = ""

    return d


class ActionLoopDecision(str, Enum):
    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    ALTERNATE_METHOD = "ALTERNATE_METHOD"
    REPLAN = "REPLAN"
    ASK_USER = "ASK_USER"
    PAUSE_HUMAN = "PAUSE_HUMAN"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"


class FailureType(str, Enum):
    TRANSIENT_UI = "TRANSIENT_UI"           # Focus lost, animation delay (Retryable)
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"   # Element missing (Alternate method / Replan)
    ZERO_EFFECT = "ZERO_EFFECT"             # Action executed but state unchanged (Alternate)
    LOOP_DETECTED = "LOOP_DETECTED"         # Repetitive cycle (Replan)
    SECURITY_BLOCKED = "SECURITY_BLOCKED"   # Policy violation (Terminal without elevation)
    STALE_FRAME = "STALE_FRAME"             # Window moved or observation out of date (Re-observe)
    TERMINAL_ERROR = "TERMINAL_ERROR"       # Unhandled exception, missing binary (Terminal)


@dataclass
class PerceptionGrounding:
    screen_width: int
    screen_height: int
    dpi_scale: float
    monitor_id: int
    active_hwnd: int
    active_window_title: str
    active_window_rect: Tuple[int, int, int, int]
    elements: List[Dict[str, Any]] = field(default_factory=list)
    frame_hash: str = ""
    frame_timestamp: float = field(default_factory=time.time)
    source: str = "UIA"  # UIA | DOM | OCR | VLM | NATIVE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "dpi_scale": round(self.dpi_scale, 2),
            "monitor_id": self.monitor_id,
            "active_hwnd": self.active_hwnd,
            "active_window_title": self.active_window_title,
            "active_window_rect": list(self.active_window_rect),
            "elements_count": len(self.elements),
            "frame_hash": self.frame_hash,
            "frame_timestamp": self.frame_timestamp,
            "source": self.source,
        }


@dataclass
class VisualTarget:
    """Canonical Visual Target candidate produced by visual grounding / OCR / UIA / DOM."""
    semantic_description: str
    candidate_bounding_boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)  # (x, y, w, h)
    confidence: float = 0.0
    source: str = "COORDINATE"  # DOM | UIA | OCR | VLM | TEMPLATE | COORDINATE
    frame_id: str = ""
    coordinate_space: str = "SCREEN"  # SCREEN | WINDOW | CLIENT
    associated_ocr_text: Optional[str] = None
    associated_element: Optional[Dict[str, Any]] = None
    best_center: Optional[Tuple[int, int]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "semantic_description": self.semantic_description,
            "candidate_bounding_boxes": [list(b) for b in self.candidate_bounding_boxes],
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "frame_id": self.frame_id,
            "coordinate_space": self.coordinate_space,
            "associated_ocr_text": self.associated_ocr_text,
            "associated_element": self.associated_element,
            "best_center": list(self.best_center) if self.best_center else None,
        }


@dataclass
class PerceptionSnapshot:
    """
    Canonical Unified Perception Snapshot (Phase 2.1).
    Combines native OS, UIA, DOM, OCR, and Visual candidate state.
    """
    frame_id: str = field(default_factory=lambda: f"frame_{uuid.uuid4().hex[:8]}")
    timestamp: float = field(default_factory=time.time)
    frame_signature: str = ""
    active_hwnd: int = 0
    process_name: str = ""
    window_title: str = ""
    window_rect: Tuple[int, int, int, int] = (0, 0, 0, 0)
    monitor_id: int = 0
    dpi_scale: float = 1.0
    uia_tree: List[Dict[str, Any]] = field(default_factory=list)
    accessibility_elements: List[Dict[str, Any]] = field(default_factory=list)
    browser_dom: Optional[Dict[str, Any]] = None
    ocr_regions: List[Dict[str, Any]] = field(default_factory=list)
    detected_text: List[str] = field(default_factory=list)
    visual_candidates: List[VisualTarget] = field(default_factory=list)
    cursor_position: Tuple[int, int] = (0, 0)
    focused_element: Optional[Dict[str, Any]] = None
    browser_url: Optional[str] = None
    page_title: Optional[str] = None
    application_state: Dict[str, Any] = field(default_factory=dict)
    screenshot_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "frame_signature": self.frame_signature,
            "active_hwnd": self.active_hwnd,
            "process_name": self.process_name,
            "window_title": self.window_title,
            "window_rect": list(self.window_rect),
            "monitor_id": self.monitor_id,
            "dpi_scale": round(self.dpi_scale, 2),
            "elements_count": len(self.uia_tree) + len(self.accessibility_elements),
            "ocr_regions_count": len(self.ocr_regions),
            "detected_text": self.detected_text[:20],
            "visual_candidates_count": len(self.visual_candidates),
            "cursor_position": list(self.cursor_position),
            "browser_url": self.browser_url,
            "page_title": self.page_title,
            "application_state": self.application_state,
            "screenshot_path": self.screenshot_path,
        }


@dataclass
class Milestone:
    """Canonical Milestone node for hierarchical long-horizon planning."""
    milestone_id: str = field(default_factory=lambda: f"ms_{uuid.uuid4().hex[:8]}")
    title: str = ""
    description: str = ""
    subgoals: List[str] = field(default_factory=list)
    status: str = "PENDING"  # PENDING | IN_PROGRESS | COMPLETED | FAILED | SKIPPED
    dependencies: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    failure_reason: Optional[str] = None
    artifacts: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "title": self.title,
            "description": self.description,
            "subgoals": list(self.subgoals),
            "status": self.status,
            "dependencies": list(self.dependencies),
            "success_criteria": list(self.success_criteria),
            "failure_reason": self.failure_reason,
            "artifacts": list(self.artifacts),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass
class ArtifactVerificationSpec:
    """Validation contract for physical artifacts (files, images, 3D models)."""
    artifact_path: str
    expected_type: Optional[str] = None  # FILE | CODE | DOCUMENT | IMAGE | BLENDER_SCENE | BLENDER_RENDER
    min_size_bytes: int = 1
    required_patterns: List[str] = field(default_factory=list)
    verify_structure: bool = True
    verify_recent_mtime: bool = True
    mtime_threshold_seconds: float = 600.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_path": self.artifact_path,
            "expected_type": self.expected_type,
            "min_size_bytes": self.min_size_bytes,
            "required_patterns": self.required_patterns,
            "verify_structure": self.verify_structure,
            "verify_recent_mtime": self.verify_recent_mtime,
            "mtime_threshold_seconds": self.mtime_threshold_seconds,
        }


@dataclass
class ArtifactRecord:
    """Canonical tracking record for created artifacts."""
    artifact_id: str = field(default_factory=lambda: f"art_{uuid.uuid4().hex[:8]}")
    path: str = ""
    artifact_type: str = "FILE"
    size_bytes: int = 0
    mtime: float = field(default_factory=time.time)
    is_verified: bool = False
    verification_notes: str = ""
    created_by_step_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "artifact_type": self.artifact_type,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "is_verified": self.is_verified,
            "verification_notes": self.verification_notes,
            "created_by_step_id": self.created_by_step_id,
            "created_at": self.created_at,
        }


@dataclass
class ActionRequest:
    action_type: str
    parameters: Dict[str, Any]
    target_domain: str = "WINDOWS_OS"  # WINDOWS_OS | BROWSER | FILESYSTEM | PROCESS | KERNEL | MCP | TOOL
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    subgoal_id: Optional[str] = None
    step_id: Optional[str] = None
    caller: str = "system"
    confirmation_token: Optional[str] = None


@dataclass
class SecurityDecision:
    allowed: bool
    risk_level: RiskLevel
    reason: str
    requires_confirmation: bool = False
    confirmation_token: Optional[str] = None
    quarantined: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk_level": self.risk_level.value if hasattr(self.risk_level, "value") else str(self.risk_level),
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_token": self.confirmation_token,
            "quarantined": self.quarantined,
        }


@dataclass
class ActionResult:
    action_type: str
    success: bool
    output: Any
    error: Optional[str] = None
    duration_ms: float = 0.0
    pre_observation: Optional[Dict[str, Any]] = None
    post_observation: Optional[Dict[str, Any]] = None


@dataclass
class ActionExecutionRecord:
    action_type: str
    parameters: Dict[str, Any]
    success: bool
    verified: bool
    duration_ms: float
    retry_count: int = 0
    failure_type: Optional[FailureType] = None
    pre_state_hash: str = ""
    post_state_hash: str = ""
    execution_strategy: str = "UIA"  # NATIVE | UIA | DOM | OCR | COORDINATE
    timestamp: float = field(default_factory=time.time)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type,
            "parameters": self.parameters,
            "success": self.success,
            "verified": self.verified,
            "duration_ms": round(self.duration_ms, 2),
            "retry_count": self.retry_count,
            "failure_type": self.failure_type.value if self.failure_type else None,
            "pre_state_hash": self.pre_state_hash,
            "post_state_hash": self.post_state_hash,
            "execution_strategy": self.execution_strategy,
            "timestamp": self.timestamp,
            "error": self.error,
        }


@dataclass
class VerificationResult:
    verified: bool
    confidence: float = 1.0
    details: str = ""
    retry_recommended: bool = False
    suggested_alternative: Optional[str] = None


@dataclass
class ModelRuntimeTelemetry:
    active_provider: str
    model_name: str
    is_simulated: bool
    total_ram_gb: float
    available_ram_gb: float
    ram_percent: float
    vram_available_gb: Optional[float] = None
    context_length: int = 2048
    load_state: str = "RESIDENT"  # UNLOADED | LOADING | RESIDENT | UNAVAILABLE
    ttft_ms: float = 0.0
    tps: float = 0.0
    last_error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_provider": self.active_provider,
            "model_name": self.model_name,
            "is_simulated": self.is_simulated,
            "total_ram_gb": round(self.total_ram_gb, 2),
            "available_ram_gb": round(self.available_ram_gb, 2),
            "ram_percent": round(self.ram_percent, 1),
            "vram_available_gb": round(self.vram_available_gb, 2) if self.vram_available_gb is not None else None,
            "context_length": self.context_length,
            "load_state": self.load_state,
            "ttft_ms": round(self.ttft_ms, 2),
            "tps": round(self.tps, 2),
            "last_error": self.last_error,
            "timestamp": self.timestamp,
        }


# ==============================================================================
# 8. Event Stream Contracts
# ==============================================================================

class StreamEventType(str, Enum):
    CONVERSATION_STARTED = "conversation.started"
    CONVERSATION_COMPLETED = "conversation.completed"
    THINKING_STATUS = "thinking.status"
    ASSISTANT_TOKEN = "assistant.token"
    ASSISTANT_MESSAGE = "assistant.message"
    MEMORY_RETRIEVED = "memory.retrieved"
    MEMORY_STORED = "memory.stored"
    RESEARCH_STARTED = "research.started"
    RESEARCH_SOURCE_COMPLETED = "research.source.completed"
    RESEARCH_COMPLETED = "research.completed"
    SKILL_SELECTED = "skill.selected"
    MCP_CONNECTED = "mcp.connected"
    MCP_TOOL_STARTED = "mcp.tool.started"
    MCP_TOOL_COMPLETED = "mcp.tool.completed"
    MCP_TOOL_FAILED = "mcp.tool.failed"
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_STEP_STARTED = "task.step.started"
    TASK_STEP_COMPLETED = "task.step.completed"
    TASK_STEP_FAILED = "task.step.failed"
    TASK_COMPLETED = "task.completed"
    TASK_PAUSED = "task.paused"
    SECURITY_CONFIRMATION = "security.confirmation_required"
    SECURITY_BLOCKED = "security.blocked"
    COMPUTER_OBSERVATION = "computer.observation"
    ARTIFACT_CREATED = "artifact.created"
    VOICE_STARTED = "voice.started"
    VOICE_TRANSCRIPT = "voice.transcript"
    VOICE_COMPLETED = "voice.completed"
    VOICE_STOPPED = "voice.stopped"
    MODEL_AVAILABLE = "model.available"
    MODEL_UNAVAILABLE = "model.unavailable"
    MODEL_STATUS = "model.status"
    SCHEDULER_TRIGGERED = "scheduler.triggered"
    SYSTEM_DEGRADED = "system.degraded"
    SYSTEM_ERROR = "system.error"
    ERROR = "error"


@dataclass
class StreamEvent:
    event_type: StreamEventType
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    correlation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event_type.value if hasattr(self.event_type, "value") else str(self.event_type),
            "data": self.payload,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "correlation_id": self.correlation_id,
        }


AgentEvent = StreamEvent
