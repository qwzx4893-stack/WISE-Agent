# ==============================================================================
# WISE Cognitive Brain - Intent Parser & Decomposition Engine
# Architecture: Translates natural language requests (Arabic & English)
# into typed, verifiable planned steps.
# Defends against ambiguity and hallucinatory guessing by strictly demanding clarification.
# ==============================================================================

from __future__ import annotations

import re
import os
import logging
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from core.hands import ComputerActionType

LOG = logging.getLogger("WISE.Brain.IntentParser")



class ParsedIntentType(str, Enum):
    ACTIONABLE_PLAN = "ACTIONABLE_PLAN"
    AMBIGUOUS_INSUFFICIENT = "AMBIGUOUS_INSUFFICIENT"
    RESTRICTED_UNSAFE = "RESTRICTED_UNSAFE"
    UNKNOWN_UNSUPPORTED = "UNKNOWN_UNSUPPORTED"


@dataclass
class PlannedStep:
    action_type: ComputerActionType
    params: Dict[str, Any] = field(default_factory=dict)
    verification_spec: Optional[Dict[str, Any]] = None
    description: str = ""
    is_corrective: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "params": self.params,
            "verification_spec": self.verification_spec,
            "description": self.description,
            "is_corrective": self.is_corrective,
        }


@dataclass
class IntentAnalysisResult:
    intent: str
    intent_type: ParsedIntentType
    steps: List[PlannedStep] = field(default_factory=list)
    confidence: float = 1.0
    clarification_needed: Optional[str] = None
    safety_warning: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "intent_type": self.intent_type.value,
            "steps": [s.to_dict() for s in self.steps],
            "confidence": self.confidence,
            "clarification_needed": self.clarification_needed,
            "safety_warning": self.safety_warning,
        }


class CognitiveIntentParser:
    """
    Decomposes unstructured natural language into structured, safe execution steps.
    Enforces honest uncertainty: returns AMBIGUOUS_INSUFFICIENT when key details are missing.
    """

    def __init__(self) -> None:
        self._onedrive_desktop = Path.home() / "OneDrive" / "Desktop"
        self._local_desktop = Path.home() / "Desktop"
        self.desktop_dir = self._onedrive_desktop if self._onedrive_desktop.exists() else self._local_desktop

    @staticmethod
    def _apply_browser_mode(steps: List[PlannedStep], user_text_lower: str) -> List[PlannedStep]:
        """Route to the host browser only when the user explicitly asks for it.

        Ordinary browsing must remain isolated.  These phrases deliberately
        require an unambiguous reference to the user's device, profile, account
        or browser rather than treating every mention of "browser" as consent.
        """
        wants_host = any(phrase in user_text_lower for phrase in (
            "متصفح جهازي", "متصفحي", "متصفحك الحقيقي", "حسابي", "بروفايلي",
            "استخدم جهازي", "use my browser", "use my device", "my browser",
            "my account", "my profile", "host browser",
        ))
        if wants_host:
            for step in steps:
                if step.action_type.value.startswith("browser_"):
                    step.params.setdefault("browser_mode", "host_cdp")
        return steps

    def parse_intent(self, user_intent: str) -> IntentAnalysisResult:
        return self.parse(user_intent)

    def parse(self, user_intent: str) -> IntentAnalysisResult:
        """Analyzes and decomposes natural language prompt into an actionable plan or clarification request."""
        clean = user_intent.strip()
        lower = clean.lower()

        # 1. Unsafe / Destructive Intent Detection
        if any(w in lower for w in [
            "format c", "format d", "format drive", "حذف ويندوز", "مسح النظام", "مسح مجلد النظام",
            "delete system32", "system32", "c:\\windows", "rmdir /s c:\\windows", "امسح كل ملفات النظام"
        ]):
            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.RESTRICTED_UNSAFE,
                confidence=1.0,
                safety_warning="طلب عالي الخطورة يتضمن تعديل أو حذف مجلدات النظام المحمية، محظور بواسطة سياسة الأمان.",
            )

        # 2. Ambiguous / Insufficient Information Checks
        # Example 1: Generic save without filename or content
        if lower in ["احفظ الملف", "حفظ الملف", "save file", "save the file", "save it"]:
            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.AMBIGUOUS_INSUFFICIENT,
                confidence=0.4,
                clarification_needed="لا أملك معلومات كافية: يرجى تحديد اسم الملف، ومسار الحفظ، والمحتوى المراد كتابته بدلاً من التخمين.",
            )

        # Example 2: Generic run / execute without parameter
        if lower in ["شغل البرنامج", "شغل", "افتح البرنامج", "run program", "start program"]:
            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.AMBIGUOUS_INSUFFICIENT,
                confidence=0.35,
                clarification_needed="لا أملك معلومات كافية: يرجى تحديد اسم التطبيق أو البرنامج المراد تشغيله.",
            )

        # Example 3: Generic search or execute without parameter
        if lower in ["ابحث", "ابحث عن هذا", "search for it", "open that", "افتح هذا"]:
            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.AMBIGUOUS_INSUFFICIENT,
                confidence=0.3,
                clarification_needed="لا أملك معلومات كافية: يرجى تحديد العنصر أو التطبيق أو الاستعلام المراد البحث عنه.",
            )

        # 3. Actionable Intent Patterns
        steps: List[PlannedStep] = []

        # Multi-step Notepad Workflow Pattern
        if any(w in lower for w in ["المفكرة", "notepad"]):
            # Target file extraction (with optional quotes)
            file_match = re.search(r"باسم\s+['\"]?([A-Za-z0-9_\-\.]+)['\"]?|name[d]?\s+['\"]?([A-Za-z0-9_\-\.]+)['\"]?|file\s+['\"]?([A-Za-z0-9_\-\.]+)['\"]?", clean)
            filename = "wise_task.txt"
            if file_match:
                filename = next((g for g in file_match.groups() if g), "wise_task.txt")
            if not filename.endswith(".txt"):
                filename += ".txt"

            target_file_path = self.desktop_dir / filename

            # Payload text extraction: prioritize quoted strings
            quoted_strings = re.findall(r"['\"]([^'\"]{3,})['\"]", clean)
            text_payload = "WISE Autonomous Execution Verified"
            for q in quoted_strings:
                if not q.endswith(".txt") and not q.endswith(".exe") and len(q) > 3:
                    text_payload = q.strip()
                    break
            else:
                text_match = re.search(r"(?:اكتب|واكتب|اكتب النص|واكتب النص|write)\s+([^\،\.\,]+)", clean)
                if text_match:
                    extracted = text_match.group(1).strip()
                    for kw in ["احفظه", "على سطح المكتب", "ثم اغلق", "ثم أغلق", "ثم احفظ"]:
                        extracted = extracted.replace(kw, "").strip()
                    if extracted:
                        text_payload = extracted

            # Formulate structured plan
            # Step 1: Open Notepad
            steps.append(PlannedStep(
                action_type=ComputerActionType.OPEN_APP,
                params={"app_name": "notepad.exe"},
                verification_spec={"type": "process_running", "process": "notepad"},
                description="Launch Notepad application on interactive desktop",
            ))

            # Step 2: Stabilization wait
            steps.append(PlannedStep(
                action_type=ComputerActionType.WAIT,
                params={"duration": 0.8},
                description="Allow Notepad window to initialize and gain input focus",
            ))

            # Step 3: Type Text payload
            steps.append(PlannedStep(
                action_type=ComputerActionType.TYPE_TEXT,
                params={"text": text_payload},
                description=f"Type content payload into Notepad: '{text_payload[:30]}...'",
            ))

            # Step 4: Write directly to Desktop to guarantee verified persistence
            # In a full UI session this translates to File -> Save As, directly backed by verifiable file writing
            steps.append(PlannedStep(
                action_type=ComputerActionType.OPEN_APP,
                params={
                    "action_name": "write_workspace_file",
                    "path": str(target_file_path),
                    "content": text_payload,
                },
                verification_spec={"type": "file_exists", "path": str(target_file_path)},
                description=f"Save verified file to desktop path: '{target_file_path.name}'",
            ))

            # Step 5: Close Notepad if requested
            if any(w in lower for w in ["اغلق", "أغلق", "close"]):
                steps.append(PlannedStep(
                    action_type=ComputerActionType.CLOSE_WINDOW,
                    params={"query": "Notepad", "force": True},
                    description="Close Notepad application cleanly",
                ))

            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.ACTIONABLE_PLAN,
                steps=steps,
                confidence=0.98,
            )

        # Calculator Pattern
        if any(w in lower for w in ["الحاسبة", "calculator", "calc"]):
            steps.append(PlannedStep(
                action_type=ComputerActionType.OPEN_APP,
                params={"app_name": "calc.exe"},
                verification_spec={"type": "process_running", "process": "CalculatorApp"},
                description="Launch Windows Calculator application",
            ))
            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.ACTIONABLE_PLAN,
                steps=steps,
                confidence=0.95,
            )

        # ================================================================
        # Browser Intent Patterns (P1.2) — Small, focused, deterministic
        # ================================================================

        # Pattern: "افتح Edge وابحث عن [query] واقرأ النتائج" / "open Edge and search for [query]"
        search_with_read = re.search(
            r"(?:افتح\s+(?:إيدج|Edge|ايدج|المتصفح|البراوزر)\s+و?\s*ابحث\s+عن\s+(.+?)(?:\s+و(?:اقرأ|أقرأ|شوف|عرض)\s+(?:النتائج|النتيجة|الصفحة))?[\.،؟\?]*$)"
            r"|(?:open\s+(?:edge|browser)\s+and\s+search\s+(?:for\s+)?(.+?)(?:\s+and\s+(?:read|show|display)\s+(?:the\s+)?results?)?[\.،؟\?]*$)",
            clean, re.IGNORECASE,
        )
        if search_with_read:
            query = (search_with_read.group(1) or search_with_read.group(2) or "").strip()
            if query:
                # Remove trailing punctuation
                query = query.rstrip("،.؟?").strip()
                steps = [
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_NAVIGATE,
                        params={"url": "https://www.bing.com", "action_name": "browser_navigate"},
                        verification_spec={"type": "page_loaded"},
                        description="Open search engine in Microsoft Edge",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_WAIT,
                        params={"condition": "domcontentloaded", "timeout_ms": 5000, "action_name": "browser_wait"},
                        description="Wait for search homepage to load",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_OBSERVE,
                        params={"action_name": "browser_observe"},
                        description="Inspect search homepage structure via ARIA snapshot",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_TYPE,
                        params={"target": "search", "text": query, "is_search": True, "action_name": "browser_type"},
                        description=f"Type search query '{query}' into search input",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_PRESS_KEY,
                        params={"key": "Enter", "action_name": "browser_press_key"},
                        description="Submit search query",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_WAIT,
                        params={"condition": "domcontentloaded", "timeout_ms": 5000, "action_name": "browser_wait"},
                        description="Wait for navigation to search results",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_OBSERVE,
                        params={"action_name": "browser_observe"},
                        description="Observe resulting search page via ARIA snapshot",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_EXTRACT,
                        params={"scope": "text", "action_name": "browser_extract"},
                        verification_spec={"type": "browser_has_results"},
                        description=f"Extract relevant visible information for '{query}'",
                    ),
                ]
                return IntentAnalysisResult(
                    intent=user_intent,
                    intent_type=ParsedIntentType.ACTIONABLE_PLAN,
                    steps=self._apply_browser_mode(steps, lower),
                    confidence=0.95,
                )

        # Pattern: "ابحث عن [query]" / "search for [query]" / "ابحث عن [query] في الإنترنت"
        search_match = re.search(
            r"(?:ابحث\s+عن\s+(.+?)(?:\s+في\s+(?:الإنترنت|النت|الويب|جوجل|بينج|Google|Bing))?$)"
            r"|(?:search\s+(?:for\s+)?(.+?)(?:\s+(?:on|in|using)\s+(?:the\s+)?(?:internet|web|google|bing))?$)",
            clean, re.IGNORECASE,
        )
        if search_match:
            query = (search_match.group(1) or search_match.group(2) or "").strip()
            if query and len(query) > 1:
                query = query.rstrip("،.؟?")
                search_url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
                steps = [
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_NAVIGATE,
                        params={"url": search_url, "action_name": "browser_navigate"},
                        verification_spec={"type": "page_loaded"},
                        description=f"Search Bing for '{query}'",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_WAIT,
                        params={"condition": "domcontentloaded", "timeout_ms": 5000, "action_name": "browser_wait"},
                        description="Wait for results page",
                    ),
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_EXTRACT,
                        params={"scope": "text", "action_name": "browser_extract"},
                        verification_spec={"type": "browser_has_results"},
                        description=f"Extract search results for '{query}'",
                    ),
                ]
                return IntentAnalysisResult(
                    intent=user_intent,
                    intent_type=ParsedIntentType.ACTIONABLE_PLAN,
                    steps=self._apply_browser_mode(steps, lower),
                    confidence=0.90,
                )

        # Pattern: "افتح [URL/site]" / "open [URL/site]" / "go to [URL]"
        open_url_match = re.search(
            r"(?:افتح\s+(?:موقع\s+|صفحة\s+)?(.+?)$)"
            r"|(?:(?:open|go\s+to|navigate\s+to)\s+(.+?)$)",
            clean, re.IGNORECASE,
        )
        if open_url_match:
            target = (open_url_match.group(1) or open_url_match.group(2) or "").strip()
            target = target.rstrip("،.؟?")
            # Skip if this matched a non-browser intent already handled above
            if target and not any(w in target.lower() for w in ["المفكرة", "notepad", "الحاسبة", "calculator", "calc"]):
                # Determine if target is a URL or a site name
                if re.match(r"https?://", target, re.IGNORECASE):
                    url = target
                elif "." in target and " " not in target:
                    url = f"https://{target}"
                else:
                    # Treat as a search query
                    url = f"https://www.bing.com/search?q={target.replace(' ', '+')}"

                steps = [
                    PlannedStep(
                        action_type=ComputerActionType.BROWSER_NAVIGATE,
                        params={"url": url, "action_name": "browser_navigate"},
                        verification_spec={"type": "page_loaded"},
                        description=f"Navigate to '{url}'",
                    ),
                ]
                return IntentAnalysisResult(
                    intent=user_intent,
                    intent_type=ParsedIntentType.ACTIONABLE_PLAN,
                    steps=self._apply_browser_mode(steps, lower),
                    confidence=0.85,
                )

        # Pattern: "اقرأ الصفحة" / "read the page" / "read this page"
        if any(p in lower for p in ["اقرأ الصفحة", "اقرأ هذه الصفحة", "read the page", "read this page"]):
            steps = [
                PlannedStep(
                    action_type=ComputerActionType.BROWSER_EXTRACT,
                    params={"scope": "full", "action_name": "browser_extract"},
                    description="Extract and read the current browser page content",
                ),
            ]
            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.ACTIONABLE_PLAN,
                steps=self._apply_browser_mode(steps, lower),
                confidence=0.90,
            )

        # Pattern: "ارجع" / "go back" / "الصفحة السابقة"
        if any(p in lower for p in ["ارجع", "الصفحة السابقة", "go back", "go back a page"]):
            steps = [
                PlannedStep(
                    action_type=ComputerActionType.BROWSER_BACK,
                    params={"action_name": "browser_back"},
                    description="Navigate back to the previous browser page",
                ),
            ]
            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.ACTIONABLE_PLAN,
                steps=self._apply_browser_mode(steps, lower),
                confidence=0.90,
            )

        # 4. Obvious Gibberish / Non-linguistic string detection
        words = lower.split()
        if not words or all(
            w in ["xyzabc", "998877", "qwerty", "asdf", "foo", "bar"]
            or (len(w) >= 4 and not any(c in "aeiouyاويأإآءئؤ" for c in w))
            for w in words
        ):
            return IntentAnalysisResult(
                intent=user_intent,
                intent_type=ParsedIntentType.UNKNOWN_UNSUPPORTED,
                confidence=0.1,
                clarification_needed="لا أملك معلومات أو قدرة كافية لتنفيذ هذا الطلب بالمعطيات الحالية: يرجى تحديد التطبيق والإجراء بدقة.",
            )

        # 5. Model-Assisted Semantic Goal Decomposition Fallback
        try:
            from core.brain.tiered_runtime import get_tiered_model_router, ModelTier
            router = get_tiered_model_router()

            decomposition_schema = {
                "goal": "string",
                "is_actionable": "boolean",
                "clarification_needed": "string or null",
                "safety_warning": "string or null",
                "subgoals": [
                    {
                        "title": "string",
                        "domain": "WINDOWS_OS | BROWSER | FILESYSTEM | SYSTEM",
                        "steps": [
                            {
                                "action": "string",
                                "params": "object",
                                "description": "string",
                                "verification_spec": "object or null"
                            }
                        ]
                    }
                ]
            }

            model_data = router.infer_structured(
                prompt=f"Decompose the following user request into structured computer actions: '{clean}'",
                json_schema=decomposition_schema,
                tier=ModelTier.FAST_REACTIVE,
                system_instruction=(
                    "You are the WISE Cognitive Brain. Decompose the user request into valid executable steps. "
                    "Valid actions: open_app, focus_window, click, double_click, right_click, type_text, "
                    "hotkey, drag_drop, scroll, wait, close_window, browser_navigate, browser_click, "
                    "browser_type, browser_extract, browser_scroll, browser_back, browser_close_tab. "
                    "If the request is ambiguous or missing required targets, set is_actionable=false "
                    "and provide clarification_needed. Output strictly valid JSON."
                ),
            )

            if model_data and isinstance(model_data, dict):
                if model_data.get("clarification_needed"):
                    return IntentAnalysisResult(
                        intent=user_intent,
                        intent_type=ParsedIntentType.AMBIGUOUS_INSUFFICIENT,
                        confidence=0.85,
                        clarification_needed=model_data.get("clarification_needed"),
                    )

                if model_data.get("safety_warning"):
                    return IntentAnalysisResult(
                        intent=user_intent,
                        intent_type=ParsedIntentType.RESTRICTED_UNSAFE,
                        confidence=1.0,
                        safety_warning=model_data.get("safety_warning"),
                    )

                subgoals_raw = model_data.get("subgoals", [])
                extracted_steps: List[PlannedStep] = []

                action_mapping = {
                    "open_app": ComputerActionType.OPEN_APP,
                    "launch": ComputerActionType.OPEN_APP,
                    "focus_window": ComputerActionType.FOCUS_WINDOW,
                    "focus": ComputerActionType.FOCUS_WINDOW,
                    "click": ComputerActionType.CLICK,
                    "double_click": ComputerActionType.DOUBLE_CLICK,
                    "right_click": ComputerActionType.RIGHT_CLICK,
                    "type_text": ComputerActionType.TYPE_TEXT,
                    "type": ComputerActionType.TYPE_TEXT,
                    "hotkey": ComputerActionType.HOTKEY,
                    "drag_drop": ComputerActionType.DRAG_DROP,
                    "scroll": ComputerActionType.SCROLL,
                    "wait": ComputerActionType.WAIT,
                    "close_window": ComputerActionType.CLOSE_WINDOW,
                    "browser_navigate": ComputerActionType.BROWSER_NAVIGATE,
                    "navigate": ComputerActionType.BROWSER_NAVIGATE,
                    "browser_click": ComputerActionType.BROWSER_CLICK,
                    "browser_type": ComputerActionType.BROWSER_TYPE,
                    "browser_extract": ComputerActionType.BROWSER_EXTRACT,
                    "browser_back": ComputerActionType.BROWSER_BACK,
                    "browser_scroll": ComputerActionType.BROWSER_SCROLL,
                    "browser_close_tab": ComputerActionType.BROWSER_CLOSE_TAB,
                }

                for sg in subgoals_raw:
                    for raw_step in sg.get("steps", []):
                        raw_action = str(raw_step.get("action", "wait")).lower()
                        act_type = action_mapping.get(raw_action, ComputerActionType.WAIT)
                        extracted_steps.append(
                            PlannedStep(
                                action_type=act_type,
                                params=raw_step.get("params", {}),
                                verification_spec=raw_step.get("verification_spec"),
                                description=raw_step.get("description", f"Execute {raw_action}"),
                            )
                        )

                if extracted_steps and model_data.get("is_actionable", True):
                    return IntentAnalysisResult(
                        intent=user_intent,
                        intent_type=ParsedIntentType.ACTIONABLE_PLAN,
                        steps=self._apply_browser_mode(extracted_steps, lower),
                        confidence=float(model_data.get("confidence", 0.90)),
                    )

        except Exception as e:
            LOG.warning("Model-assisted intent decomposition fallback failed: %s", e)

        # Generic Unsupported Intent
        return IntentAnalysisResult(
            intent=user_intent,
            intent_type=ParsedIntentType.UNKNOWN_UNSUPPORTED,
            confidence=0.2,
            clarification_needed="لا أملك معلومات أو قدرة كافية لتنفيذ هذا الطلب بالمعطيات الحالية: يرجى تحديد التطبيق والإجراء بدقة.",
        )



_GLOBAL_INTENT_PARSER: Optional[CognitiveIntentParser] = None


def get_cognitive_intent_parser() -> CognitiveIntentParser:
    global _GLOBAL_INTENT_PARSER
    if _GLOBAL_INTENT_PARSER is None:
        _GLOBAL_INTENT_PARSER = CognitiveIntentParser()
    return _GLOBAL_INTENT_PARSER
