# ==============================================================================
# WISE Cognitive Brain - Model Provider Abstraction
# Architecture: Provider-agnostic model runtime interface supporting:
# - FAST_REACTIVE, GENERAL_REASONER, VISION_MODEL tiers
# - Local native embedded inference, Cloud OpenAI-compatible endpoints,
#   and deterministic test simulation providers.
# - Enforces strict schema validation, structured JSON outputs, and explicit
#   simulation labeling (never fake real AI capability).
# ==============================================================================

from __future__ import annotations

import os
import sys
import json
import time
import logging
import urllib.request
import urllib.error
import urllib.parse
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field

import psutil
from core.contracts import ModelRuntimeTelemetry

LOG = logging.getLogger("WISE.Models.Provider")


class ModelProviderType(str, Enum):
    LOCAL_EMBEDDED = "LOCAL_EMBEDDED"
    LOCAL_MOE = "LOCAL_MOE"
    LFM2_5_COGNITIVE_CORE = "LFM2_5_COGNITIVE_CORE"
    OXCODER_9B_TEST_PROVIDER = "OXCODER_9B_TEST_PROVIDER"
    LLAMA_3_2_MOE_TEST_PROVIDER = "LLAMA_3_2_MOE_TEST_PROVIDER"
    CLOUD_OPENAI_COMPATIBLE = "CLOUD_OPENAI_COMPATIBLE"
    SIMULATED_TEST = "SIMULATED_TEST"
    UNAVAILABLE = "UNAVAILABLE"



@dataclass
class ModelCompletionRequest:
    messages: List[Dict[str, str]]
    system_prompt: str = ""
    temperature: float = 0.2
    max_tokens: int = 1024
    json_schema: Optional[Dict[str, Any]] = None
    tier: str = "FAST_REACTIVE"  # FAST_REACTIVE | HEAVY_REASONER | VISION_MODEL
    task_tier: str = "MEDIUM"     # SIMPLE | MEDIUM | COMPLEX
    task_id: Optional[str] = None

    def __post_init__(self) -> None:
        from core.user_instructions import apply_user_instructions
        self.system_prompt = apply_user_instructions(self.system_prompt)


@dataclass
class ModelCompletionResponse:
    text: str
    parsed_json: Optional[Union[Dict[str, Any], List[Any]]] = None
    tokens_prompt: int = 0
    tokens_completion: int = 0
    latency_ms: float = 0.0
    provider_type: ModelProviderType = ModelProviderType.UNAVAILABLE
    model_name: str = "unavailable"
    is_simulated: bool = False
    error: Optional[str] = None
    finish_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "parsed_json": self.parsed_json,
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "latency_ms": round(self.latency_ms, 2),
            "provider_type": self.provider_type.value,
            "model_name": self.model_name,
            "is_simulated": self.is_simulated,
            "error": self.error,
            "finish_reason": self.finish_reason,
        }


class BaseModelProvider(ABC):
    """Abstract interface for all model execution providers in WISE."""

    @abstractmethod
    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        """Executes completion against the underlying runtime."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Returns whether this provider is configured and ready for live execution."""
        pass

    @abstractmethod
    def get_telemetry(self) -> ModelRuntimeTelemetry:
        """Returns structured runtime and hardware telemetry for this provider."""
        pass


class SimulatedTestProvider(BaseModelProvider):
    """
    Deterministic test provider for automated testing and CI.
    Explicitly tags all responses with is_simulated=True.
    Never pretends to be real model inference.
    """

    def __init__(self, custom_responses: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.custom_responses = custom_responses or {}
        self.call_history: List[ModelCompletionRequest] = []

    def get_provider_type(self) -> ModelProviderType:
        return ModelProviderType.SIMULATED_TEST

    @property
    def provider_type(self) -> ModelProviderType:
        return self.get_provider_type()

    def is_available(self) -> bool:
        return True

    def get_telemetry(self) -> ModelRuntimeTelemetry:
        vm = psutil.virtual_memory()
        return ModelRuntimeTelemetry(
            active_provider="SimulatedTestProvider",
            model_name="simulation-harness",
            is_simulated=True,
            total_ram_gb=round(vm.total / (1024 ** 3), 2),
            available_ram_gb=round(vm.available / (1024 ** 3), 2),
            ram_percent=round(vm.percent, 1),
            context_length=2048,
            load_state="RESIDENT",
        )

    def register_response(self, keyword_trigger: str, structured_json: Dict[str, Any]) -> None:
        """Allows test fixtures to set expected structured responses for specific intents."""
        self.custom_responses[keyword_trigger.lower()] = structured_json

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()
        self.call_history.append(request)

        # Inspect prompt text for triggers
        user_text = ""
        for m in request.messages:
            if m.get("role") == "user":
                user_text += " " + m.get("content", "")
        user_text = user_text.strip().lower()

        matched_json: Optional[Dict[str, Any]] = None
        for trigger, resp in self.custom_responses.items():
            if trigger in user_text:
                matched_json = resp
                break

        is_preflight = (
            "pre-flight evaluation" in (request.system_prompt or "").lower()
            or "wise cognitive core" in (request.system_prompt or "").lower()
            or "analyze this user request:" in user_text
        )

        # Fallback default responses
        if matched_json is None:
            if is_preflight:
                # Extract clean user intent from "analyze this user request: <intent>"
                clean_intent = user_text
                if "analyze this user request:" in user_text:
                    clean_intent = user_text.split("analyze this user request:", 1)[1].strip()

                # 1. Direct conversation / greetings / common questions / math
                conversational_triggers = ("hello", "hi", "hey", "who are you", "what can you do", "explain", "what is", "2+2", "how to")
                is_informational = any(
                    clean_intent.startswith(p)
                    for p in ("explain", "what is", "what are", "how does", "how do i", "how to", "tell me about", "describe", "define", "difference between")
                )
                is_action = (not is_informational) and any(
                    act in clean_intent for act in ("rename", "delete", "open", "launch", "click", "format", "kill", "remove", "type", "press")
                )
                is_research = any(res in clean_intent for res in ("research", "latest news", "benchmark", "paper", "investigate", "arxiv"))
                
                if (any(trig in clean_intent for trig in conversational_triggers) or is_informational) and not is_research and not is_action:
                    if "hello" in clean_intent or "who are you" in clean_intent or "hi" in clean_intent:
                        answer = "Hello! I am WISE, your agentic operating system assistant. How can I assist you today?"
                    elif "recursion" in clean_intent:
                        answer = "Recursion is a programming technique in which a function calls itself to solve smaller subproblems until reaching a base condition."
                    elif "rename" in clean_intent:
                        answer = "To rename a file on Windows, you can select it and press F2, or right-click and choose Rename, or use the PowerShell command: Rename-Item oldname newname."
                    elif "2+2" in clean_intent:
                        answer = "2 + 2 = 4."
                    else:
                        answer = f"Direct answer regarding: {clean_intent}"


                    matched_json = {
                        "real_goal": clean_intent,
                        "existing_knowledge": "Established core knowledge",
                        "missing_information": None,
                        "can_answer_directly": True,
                        "direct_answer": answer,
                        "needs_clarification": False,
                        "clarification_question": None,
                        "selected_capabilities": ["none"],
                        "complexity_tier": "SIMPLE",
                        "is_deep_research": False,
                        "is_multi_step": False,
                        "recommended_action": "Direct answer",
                        "reasoning": "Conversational greeting or informational request can be answered directly.",
                    }
                elif is_research:
                    matched_json = {
                        "real_goal": clean_intent,
                        "existing_knowledge": "Partial knowledge",
                        "missing_information": None,
                        "can_answer_directly": False,
                        "direct_answer": None,
                        "needs_clarification": False,
                        "clarification_question": None,
                        "selected_capabilities": ["web_search"],
                        "complexity_tier": "COMPLEX",
                        "is_deep_research": True,
                        "is_multi_step": False,
                        "recommended_action": "Execute multi-source research",
                        "reasoning": "Research query requires external source retrieval.",
                    }
                else:
                    # Action requiring tools
                    matched_json = {
                        "real_goal": clean_intent,
                        "existing_knowledge": "Windows desktop environment",
                        "missing_information": None,
                        "can_answer_directly": False,
                        "direct_answer": None,
                        "needs_clarification": False,
                        "clarification_question": None,
                        "selected_capabilities": ["windows_control"],
                        "complexity_tier": "MEDIUM",
                        "is_deep_research": False,
                        "is_multi_step": True,
                        "recommended_action": "Execute desktop action",
                        "reasoning": "Action requires interacting with desktop environment.",
                    }
            else:
                matched_json = {
                    "goal": "Execute user instruction via general computer use",
                    "subgoals": [
                        {
                            "title": "Analyze and prepare environment",
                            "domain": "WINDOWS_OS",
                            "steps": [
                                {
                                    "action": "focus_window",
                                    "params": {"query": "active"},
                                    "description": "Ensure workspace is active",
                                }
                            ],
                        }
                    ],
                    "clarification_needed": None,
                    "confidence": 0.95,
                }

        latency = (time.perf_counter() - t0) * 1000
        output_str = json.dumps(matched_json, ensure_ascii=False)

        return ModelCompletionResponse(
            text=output_str,
            parsed_json=matched_json,
            tokens_prompt=len(user_text.split()),
            tokens_completion=len(output_str.split()),
            latency_ms=latency,
            provider_type=ModelProviderType.SIMULATED_TEST,
            model_name="simulated-test-v1",
            is_simulated=True,
        )


class UnavailableModelProvider(BaseModelProvider):
    """
    Explicit provider returned when no real local model (due to safety gate or missing file)
    and no configured external cloud endpoints are available, and simulation mode is disabled.
    Never pretends to be functional or returns fake intelligence.
    """

    def __init__(self, reason: str = "No model provider is configured or available.") -> None:
        self.reason = reason

    def get_provider_type(self) -> ModelProviderType:
        return ModelProviderType.UNAVAILABLE

    @property
    def provider_type(self) -> ModelProviderType:
        return self.get_provider_type()

    def is_available(self) -> bool:
        return False

    def get_telemetry(self) -> ModelRuntimeTelemetry:
        vm = psutil.virtual_memory()
        return ModelRuntimeTelemetry(
            active_provider="UnavailableModelProvider",
            model_name="none",
            is_simulated=False,
            total_ram_gb=round(vm.total / (1024 ** 3), 2),
            available_ram_gb=round(vm.available / (1024 ** 3), 2),
            ram_percent=round(vm.percent, 1),
            context_length=0,
            load_state="UNAVAILABLE",
            last_error=self.reason,
        )

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        return ModelCompletionResponse(
            text="",
            error=f"MODEL_UNAVAILABLE: {self.reason}",
            provider_type=ModelProviderType.UNAVAILABLE,
            is_simulated=False,
        )



class HttpOpenAICompatibleProvider(BaseModelProvider):
    """
    Standard HTTP provider for any OpenAI-compatible completions endpoint.
    Supports local runtimes (Ollama, vLLM, LM Studio) or cloud endpoints
    (OpenAI, Groq, LiteLLM, Gemini OpenAI-compatible API).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("WISE_LLM_API_KEY", "")
        self.base_url = (base_url or os.environ.get("WISE_LLM_BASE_URL", "http://localhost:11434/v1")).rstrip("/")
        self.model_name = model_name or os.environ.get("WISE_LLM_MODEL", "llama3.2")
        self.timeout = timeout_seconds

    def is_available(self) -> bool:
        # Considered available if base_url is set and (if remote) api_key is present
        if not self.base_url:
            return False
        if "localhost" in self.base_url or "127.0.0.1" in self.base_url:
            return True
        return bool(self.api_key)

    def get_telemetry(self) -> ModelRuntimeTelemetry:
        vm = psutil.virtual_memory()
        return ModelRuntimeTelemetry(
            active_provider="HttpOpenAICompatibleProvider",
            model_name=self.model_name,
            is_simulated=False,
            total_ram_gb=round(vm.total / (1024 ** 3), 2),
            available_ram_gb=round(vm.available / (1024 ** 3), 2),
            ram_percent=round(vm.percent, 1),
            context_length=8192,
            load_state="RESIDENT" if self.is_available() else "UNAVAILABLE",
        )

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()

        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "WISE-Cognitive-Brain/1.3",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload_messages = []
        if request.system_prompt:
            payload_messages.append({"role": "system", "content": request.system_prompt})
        payload_messages.extend(request.messages)

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": payload_messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        # If json schema requested and supported
        if request.json_schema:
            payload["response_format"] = {"type": "json_object"}
        if urllib.parse.urlsplit(self.base_url).hostname == "openrouter.ai":
            payload["usage"] = {"include": True}

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data_bytes, headers=headers, method="POST")

        try:
            budget = None
            if os.environ.get("WISE_QA_BUDGET_PATH"):
                from qa.acceptance.live_budget import provider_gate
                budget = provider_gate(self.api_key, self.base_url)
            reservation = budget.reserve(self.model_name, input_byte_bound=len(data_bytes),
                                         max_tokens=request.max_tokens) if budget else None
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status_code = resp.getcode()
                resp_body = resp.read().decode("utf-8")

                if status_code != 200:
                    return ModelCompletionResponse(
                        text="",
                        error=f"HTTP {status_code}: {resp_body}",
                        latency_ms=(time.perf_counter() - t0) * 1000,
                        provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                        model_name=self.model_name,
                        is_simulated=False,
                    )

                parsed = json.loads(resp_body)
                if budget:
                    budget.settle(reservation, parsed)
                choices = parsed.get("choices", [])
                if not choices:
                    return ModelCompletionResponse(
                        text="",
                        error="No choices returned from model endpoint",
                        latency_ms=(time.perf_counter() - t0) * 1000,
                        provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                        model_name=self.model_name,
                        is_simulated=False,
                    )

                content = choices[0].get("message", {}).get("content", "")
                parsed_json = None
                try:
                    parsed_json = json.loads(content)
                except Exception:
                    # Attempt markdown code block extraction
                    if "```json" in content:
                        raw = content.split("```json")[1].split("```")[0].strip()
                        try:
                            parsed_json = json.loads(raw)
                        except Exception:
                            pass

                usage = parsed.get("usage", {})
                latency = (time.perf_counter() - t0) * 1000

                return ModelCompletionResponse(
                    text=content,
                    parsed_json=parsed_json,
                    tokens_prompt=usage.get("prompt_tokens", 0),
                    tokens_completion=usage.get("completion_tokens", 0),
                    latency_ms=latency,
                    provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                    model_name=self.model_name,
                    is_simulated=False,
                    finish_reason=choices[0].get("finish_reason"),
                )

        except urllib.error.HTTPError as he:
            latency = (time.perf_counter() - t0) * 1000
            detail = he.read().decode("utf-8", errors="replace")[:1000]
            return ModelCompletionResponse(
                text="",
                error=f"HTTP {he.code}: {detail}",
                latency_ms=latency,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name,
                is_simulated=False,
            )
        except urllib.error.URLError as ue:
            latency = (time.perf_counter() - t0) * 1000
            LOG.warning("Model endpoint connection error (%s): %s", endpoint, ue)
            return ModelCompletionResponse(
                text="",
                error=f"Endpoint connection failed: {ue}",
                latency_ms=latency,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name,
                is_simulated=False,
            )
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            LOG.error("Unexpected error during model inference: %s", e)
            return ModelCompletionResponse(
                text="",
                error=f"Inference error: {e}",
                latency_ms=latency,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name,
                is_simulated=False,
            )


def _parse_structured_text(content: str) -> Optional[Union[Dict[str, Any], List[Any]]]:
    """Extract JSON without treating a parse failure as a provider failure."""
    try:
        return json.loads(content)
    except Exception:
        if "```json" in content:
            try:
                return json.loads(content.split("```json", 1)[1].split("```", 1)[0].strip())
            except Exception:
                pass
    return None


class AnthropicMessagesProvider(BaseModelProvider):
    """Native Anthropic Messages API transport (not OpenAI compatibility)."""

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 model_name: Optional[str] = None, timeout_seconds: float = 30.0) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self.model_name = model_name or "claude-sonnet-4-6"
        self.timeout = timeout_seconds

    def is_available(self) -> bool:
        return bool(self.api_key and self.base_url)

    def get_telemetry(self) -> ModelRuntimeTelemetry:
        vm = psutil.virtual_memory()
        return ModelRuntimeTelemetry(
            active_provider="AnthropicMessagesProvider", model_name=self.model_name,
            is_simulated=False, total_ram_gb=round(vm.total / (1024 ** 3), 2),
            available_ram_gb=round(vm.available / (1024 ** 3), 2),
            ram_percent=round(vm.percent, 1), context_length=8192,
            load_state="RESIDENT" if self.is_available() else "UNAVAILABLE",
        )

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()
        messages = [
            {"role": "assistant" if m.get("role") == "assistant" else "user", "content": m.get("content", "")}
            for m in request.messages if m.get("role") != "system"
        ]
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt
        req = urllib.request.Request(
            f"{self.base_url}/messages", data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01", "User-Agent": "WISE-Cognitive-Brain/1.3"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = "".join(
                str(part.get("text", "")) for part in body.get("content", [])
                if isinstance(part, dict) and part.get("type") == "text"
            )
            usage = body.get("usage", {})
            return ModelCompletionResponse(
                text=content, parsed_json=_parse_structured_text(content),
                tokens_prompt=int(usage.get("input_tokens", 0) or 0),
                tokens_completion=int(usage.get("output_tokens", 0) or 0),
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name, is_simulated=False,
                error=None if content else "No text content returned by Anthropic Messages API",
                finish_reason=body.get("stop_reason"),
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            return ModelCompletionResponse(text="", error=f"Anthropic HTTP {exc.code}: {detail}",
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE, model_name=self.model_name, is_simulated=False)
        except urllib.error.URLError as exc:
            return ModelCompletionResponse(text="", error=f"Anthropic connection failed: {exc.reason}",
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE, model_name=self.model_name, is_simulated=False)
        except Exception as exc:
            return ModelCompletionResponse(text="", error=f"Anthropic inference error: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE, model_name=self.model_name, is_simulated=False)


class GeminiGenerateContentProvider(BaseModelProvider):
    """Native Gemini ``models/*:generateContent`` transport."""

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 model_name: Optional[str] = None, timeout_seconds: float = 30.0) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.model_name = (model_name or "gemini-2.0-flash").removeprefix("models/")
        self.timeout = timeout_seconds

    def is_available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model_name)

    def get_telemetry(self) -> ModelRuntimeTelemetry:
        vm = psutil.virtual_memory()
        return ModelRuntimeTelemetry(
            active_provider="GeminiGenerateContentProvider", model_name=self.model_name,
            is_simulated=False, total_ram_gb=round(vm.total / (1024 ** 3), 2),
            available_ram_gb=round(vm.available / (1024 ** 3), 2),
            ram_percent=round(vm.percent, 1), context_length=8192,
            load_state="RESIDENT" if self.is_available() else "UNAVAILABLE",
        )

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()
        contents = []
        for message in request.messages:
            role = message.get("role", "user")
            if role == "system":
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": message.get("content", "")}],
            })
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": request.temperature, "maxOutputTokens": request.max_tokens},
        }
        if request.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}
        if request.json_schema:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        endpoint = f"{self.base_url}/models/{urllib.parse.quote(self.model_name, safe='')}:generateContent"
        req = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key,
                     "User-Agent": "WISE-Cognitive-Brain/1.3"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            candidates = body.get("candidates", [])
            parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
            content = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
            usage = body.get("usageMetadata", {})
            return ModelCompletionResponse(
                text=content, parsed_json=_parse_structured_text(content),
                tokens_prompt=int(usage.get("promptTokenCount", 0) or 0),
                tokens_completion=int(usage.get("candidatesTokenCount", 0) or 0),
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name, is_simulated=False,
                error=None if content else "No text candidate returned by Gemini GenerateContent API",
                finish_reason=candidates[0].get("finishReason") if candidates else None,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            return ModelCompletionResponse(text="", error=f"Gemini HTTP {exc.code}: {detail}",
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE, model_name=self.model_name, is_simulated=False)
        except urllib.error.URLError as exc:
            return ModelCompletionResponse(text="", error=f"Gemini connection failed: {exc.reason}",
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE, model_name=self.model_name, is_simulated=False)
        except Exception as exc:
            return ModelCompletionResponse(text="", error=f"Gemini inference error: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE, model_name=self.model_name, is_simulated=False)


class AzureOpenAIProvider(HttpOpenAICompatibleProvider):
    """Azure OpenAI v1 transport, which uses ``api-key`` instead of Bearer."""

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()
        endpoint = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "api-key": self.api_key,
                   "User-Agent": "WISE-Cognitive-Brain/1.3"}
        messages = ([{"role": "system", "content": request.system_prompt}] if request.system_prompt else []) + request.messages
        payload: Dict[str, Any] = {"model": self.model_name, "messages": messages,
                                   "temperature": request.temperature, "max_tokens": request.max_tokens}
        if request.json_schema:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices", [])
            content = choices[0].get("message", {}).get("content", "") if choices else ""
            usage = body.get("usage", {})
            return ModelCompletionResponse(text=content, parsed_json=_parse_structured_text(content),
                tokens_prompt=int(usage.get("prompt_tokens", 0) or 0), tokens_completion=int(usage.get("completion_tokens", 0) or 0),
                latency_ms=(time.perf_counter() - t0) * 1000, provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name, is_simulated=False, error=None if content else "No choices returned by Azure OpenAI",
                finish_reason=choices[0].get("finish_reason") if choices else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            return ModelCompletionResponse(text="", error=f"Azure OpenAI HTTP {exc.code}: {detail}",
                latency_ms=(time.perf_counter() - t0) * 1000, provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name, is_simulated=False)
        except urllib.error.URLError as exc:
            return ModelCompletionResponse(text="", error=f"Azure OpenAI connection failed: {exc.reason}",
                latency_ms=(time.perf_counter() - t0) * 1000, provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name, is_simulated=False)
        except Exception as exc:
            return ModelCompletionResponse(text="", error=f"Azure OpenAI inference error: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000, provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name=self.model_name, is_simulated=False)


def create_external_model_provider(*, provider_id: Optional[str], api_key: Optional[str],
                                   base_url: Optional[str], model_name: Optional[str]) -> BaseModelProvider:
    """Instantiate the correct live transport for one catalogue configuration."""
    from core.llm.providers import get_provider

    info = get_provider(provider_id or "")
    if info and info.transport == "anthropic":
        return AnthropicMessagesProvider(api_key or "", base_url or info.base_url, model_name or info.default_model)
    if info and info.transport == "gemini":
        return GeminiGenerateContentProvider(api_key or "", base_url or info.base_url, model_name or info.default_model)
    if info and info.id == "azure":
        return AzureOpenAIProvider(api_key, base_url or info.base_url, model_name or info.default_model)
    return HttpOpenAICompatibleProvider(api_key, base_url or (info.base_url if info else None), model_name or (info.default_model if info else None))


class MoEModelProvider(BaseModelProvider):
    """
    Provider exposing the native 3-tier sparse MoE runtime to the Cognitive Brain.
    Supports on-demand awakening and zero-cost idle suspension.
    """

    def __init__(self, runtime_adapter: Optional[Any] = None) -> None:
        # The previous default was a synthetic MoE benchmark.  It is retained
        # only as an explicit test fixture, never as an inference provider.
        self.adapter = runtime_adapter

    def is_available(self) -> bool:
        return bool(self.adapter and self.adapter.is_loaded())

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()
        if not self.is_available():
            return ModelCompletionResponse(
                text="",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                provider_type=ModelProviderType.LOCAL_MOE,
                model_name="wise-moe-runtime",
                is_simulated=False,
                error="A real MoE backend is not configured; synthetic benchmark inference is disabled.",
            )
        from core.models.model_runtime import InferenceConfig
        cfg = InferenceConfig(max_tokens=min(request.max_tokens, 30))

        user_content = ""
        for m in request.messages:
            if m.get("role") == "user":
                user_content += " " + m.get("content", "")
        prompt = user_content.strip() or "WISE MoE Command"

        res = self.adapter.generate(prompt=prompt, config=cfg)
        latency = (time.perf_counter() - t0) * 1000.0

        return ModelCompletionResponse(
            text=res.text,
            tokens_prompt=res.prompt_tokens,
            tokens_completion=res.tokens_generated,
            latency_ms=latency,
            provider_type=ModelProviderType.LOCAL_MOE,
            model_name="wise-moe-runtime-v1",
            is_simulated=False,
        )


class Oxcoder9BTestProvider(BaseModelProvider):
    """
    Temporary test provider exposing the real local OxCoder 9B model
    via the LlamaCppRealBackend adapter for Phase P1.4-B.0 validation.
    Does NOT permanently bind WISE to OxCoder 9B.
    """

    def __init__(self, backend: Optional[Any] = None) -> None:
        if backend is None:
            from core.models.runtime.real_backend_adapter import LlamaCppRealBackend
            self.backend = LlamaCppRealBackend()
            self.backend.load_model()
        else:
            self.backend = backend

    def is_available(self) -> bool:
        return self.backend.is_loaded()

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()
        user_content = ""
        for m in request.messages:
            if m.get("role") == "user":
                user_content += " " + m.get("content", "")
        prompt = user_content.strip() or "Hello"

        res = self.backend.generate(
            prompt=prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )

        parsed_json: Optional[Dict[str, Any]] = None
        if request.json_schema or "{" in res.text:
            try:
                raw_json = res.text.strip()
                if "```json" in raw_json:
                    raw_json = raw_json.split("```json")[1].split("```")[0].strip()
                elif "```" in raw_json:
                    raw_json = raw_json.split("```")[1].split("```")[0].strip()
                parsed_json = json.loads(raw_json)
            except Exception:
                pass

        latency = (time.perf_counter() - t0) * 1000.0
        return ModelCompletionResponse(
            text=res.text,
            parsed_json=parsed_json,
            tokens_prompt=res.prompt_tokens,
            tokens_completion=res.tokens_generated,
            latency_ms=latency,
            provider_type=ModelProviderType.OXCODER_9B_TEST_PROVIDER,
            model_name="OxCoder-9B-Q5_0",
            is_simulated=False,
            error=res.error,
        )

    def unload(self) -> None:
        if self.backend:
            self.backend.unload_model()


# Alias for backward compatibility
OxCoder9BTestProvider = Oxcoder9BTestProvider


class LFM25CognitiveProvider(BaseModelProvider):
    """
    Official Native Cognitive Core Provider for WISE powered by
    LiquidAI/LFM2.5-8B-A1B Q4_K_M GGUF via standard llama.cpp resident runtime.
    Provides natural reasoning, planning, tool selection, and closed-loop execution.
    """

    def __init__(self, backend: Optional[Any] = None) -> None:
        if backend is not None:
            self.backend = backend
        else:
            from .runtime.lfm25_backend import LFM25NativeBackend
            self.backend = LFM25NativeBackend()

    @property
    def provider_name(self) -> str:
        return "LiquidAI/LFM2.5-8B-A1B-Q4_K_M"

    @property
    def provider_type(self) -> ModelProviderType:
        return self.get_provider_type()

    def get_provider_type(self) -> ModelProviderType:
        return ModelProviderType.LFM2_5_COGNITIVE_CORE

    def is_available(self) -> bool:
        if self.backend is None:
            return False
        try:
            return self.backend.is_loaded() or self.backend.load_model()
        except Exception:
            return False

    def generate(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        return self.complete(request)

    def begin_task(self, task_id: str) -> None:
        if self.backend and hasattr(self.backend, "begin_task"):
            self.backend.begin_task(task_id)

    def end_task(self, task_id: str) -> None:
        if self.backend and hasattr(self.backend, "end_task"):
            self.backend.end_task(task_id)

    def get_idle_metrics(self) -> Dict[str, Any]:
        if self.backend and hasattr(self.backend, "get_idle_metrics"):
            return self.backend.get_idle_metrics()
        return {}

    def get_telemetry(self) -> ModelRuntimeTelemetry:
        vm = psutil.virtual_memory()
        ctx_len = 4096
        if self.backend and hasattr(self.backend, "context_length"):
            ctx_len = self.backend.context_length
        is_loaded = bool(self.backend and self.backend.is_loaded())
        return ModelRuntimeTelemetry(
            active_provider="LFM25CognitiveProvider",
            model_name=self.provider_name,
            is_simulated=False,
            total_ram_gb=round(vm.total / (1024 ** 3), 2),
            available_ram_gb=round(vm.available / (1024 ** 3), 2),
            ram_percent=round(vm.percent, 1),
            context_length=ctx_len,
            load_state="RESIDENT" if is_loaded else "UNLOADED",
        )

    def complete(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()
        if not self.backend:
            return ModelCompletionResponse(
                text="",
                error="LFM25NativeBackend is not initialized",
                provider_type=ModelProviderType.LFM2_5_COGNITIVE_CORE,
                is_simulated=False,
            )

        if getattr(request, "task_id", None):
            self.begin_task(request.task_id)

        full_prompt = self.backend.format_chatml_prompt(
            messages=request.messages,
            system_prompt=request.system_prompt,
        )

        task_tier = getattr(request, "task_tier", "MEDIUM")
        res = self.backend.generate(
            prompt=full_prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            task_tier=task_tier,
        )

        parsed_json = res.parsed_json
        if not parsed_json and (request.json_schema or "{" in res.text or "[" in res.text):
            try:
                from .runtime.lfm25_backend import StructuredOutputParser
                parsed_json = StructuredOutputParser.extract_balanced_json(res.text)
            except Exception:
                pass

        latency = (time.perf_counter() - t0) * 1000.0
        return ModelCompletionResponse(
            text=res.text,
            parsed_json=parsed_json,
            tokens_prompt=res.prompt_tokens,
            tokens_completion=res.tokens_generated,
            latency_ms=latency,
            provider_type=ModelProviderType.LFM2_5_COGNITIVE_CORE,
            model_name="LiquidAI/LFM2.5-8B-A1B-Q4_K_M",
            is_simulated=False,
            error=res.error,
        )

    def unload(self) -> None:
        if self.backend:
            self.backend.unload_model()


class Llama32MoETestProvider(BaseModelProvider):
    """
    Archived test provider bridging historical Llama-3.2-3B-MoE-4Expert experiments.
    """

    def __init__(self, backend: Optional[Any] = None) -> None:
        if backend is not None:
            self.backend = backend
        else:
            try:
                from .archived_moe.real_moe_backend import LlamaCppMoEBackend
                self.backend = LlamaCppMoEBackend()
            except ImportError:
                self.backend = None

    def get_provider_type(self) -> ModelProviderType:
        return ModelProviderType.LLAMA_3_2_MOE_TEST_PROVIDER

    def is_available(self) -> bool:
        if self.backend is None:
            return False
        try:
            return self.backend.load_model()
        except Exception:
            return False

    def complete(self, request: ModelCompletionRequest) -> ModelCompletionResponse:
        t0 = time.perf_counter()
        if not self.backend:
            return ModelCompletionResponse(
                text="",
                error="LlamaCppMoEBackend is archived/uninitialized",
                provider_type=ModelProviderType.LLAMA_3_2_MOE_TEST_PROVIDER,
                is_simulated=False,
            )

        prompt_parts: List[str] = []
        if request.system_prompt:
            prompt_parts.append(f"<|system|>\n{request.system_prompt}")
        for msg in request.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            prompt_parts.append(f"<|{role}|>\n{content}")
        prompt_parts.append("<|assistant|>\n")
        full_prompt = "\n".join(prompt_parts)

        res = self.backend.generate(
            prompt=full_prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )

        parsed_json: Optional[Dict[str, Any]] = None
        if request.json_schema or "{" in res.text:
            try:
                raw_json = res.text.strip()
                if "```json" in raw_json:
                    raw_json = raw_json.split("```json")[1].split("```")[0].strip()
                elif "```" in raw_json:
                    raw_json = raw_json.split("```")[1].split("```")[0].strip()
                parsed_json = json.loads(raw_json)
            except Exception:
                pass

        latency = (time.perf_counter() - t0) * 1000.0
        return ModelCompletionResponse(
            text=res.text,
            parsed_json=parsed_json,
            tokens_prompt=res.prompt_tokens,
            tokens_completion=res.tokens_generated,
            latency_ms=latency,
            provider_type=ModelProviderType.LLAMA_3_2_MOE_TEST_PROVIDER,
            model_name="Llama-3.2-3B-MoE-4Expert",
            is_simulated=False,
            error=res.error,
        )

    def unload(self) -> None:
        if self.backend:
            self.backend.unload_model()


# Global Provider Registry
_ACTIVE_PROVIDER: Optional[BaseModelProvider] = None


def _is_local_endpoint(base_url: Optional[str]) -> bool:
    if not base_url:
        return False
    host = urllib.parse.urlparse(base_url).hostname or ""
    return host.lower() in {"localhost", "127.0.0.1", "::1"}


def _environment_provider_configuration(selected_provider: Optional[str] = None) -> Optional[Dict[str, Optional[str]]]:
    """Return one explicit live provider configuration from supported env vars."""
    from core.llm.providers import PROVIDERS, detect_provider

    # WISE_* is an intentional override for a custom gateway or a named
    # catalogue provider.  Never assume it speaks OpenAI if its URL says
    # Anthropic/Gemini.
    wise_key = os.environ.get("WISE_LLM_API_KEY")
    wise_base = os.environ.get("WISE_LLM_BASE_URL")
    if wise_key or wise_base:
        detected = detect_provider(wise_key or "", wise_base)
        configuration = {
            "provider_id": os.environ.get("WISE_LLM_PROVIDER") or detected.id,
            "api_key": wise_key,
            "base_url": wise_base or detected.base_url,
            "model_name": os.environ.get("WISE_LLM_MODEL") or detected.default_model,
        }
        if selected_provider is None or configuration["provider_id"] == selected_provider:
            return configuration

    for info in PROVIDERS:
        if selected_provider is not None and info.id != selected_provider:
            continue
        key = os.environ.get(info.api_key_env) if info.api_key_env else None
        if info.id == "gemini":
            key = key or os.environ.get("GOOGLE_API_KEY")
        if key:
            return {
                "provider_id": info.id,
                "api_key": key,
                "base_url": info.base_url,
                "model_name": info.default_model,
            }
    return None


def _selected_provider_id() -> Optional[str]:
    """Read the explicit provider choice made in the desktop model picker.

    ``LocalModelManager`` owns this small registry.  Reading just its public
    selection field here avoids importing that manager (and its dependency
    graph) into the canonical inference path.
    """
    try:
        from core.paths import CONFIG_DIR
        registry = CONFIG_DIR / "model_registry.json"
        if registry.exists():
            data = json.loads(registry.read_text(encoding="utf-8"))
            selected = str(data.get("active_provider_id") or "").strip()
            # An old registry may say "local" while containing no selected
            # local model.  Treat that as an unconfigured legacy state, not as
            # an instruction to silently route a user away from a real choice.
            if selected == "local" and not data.get("active_model_id"):
                return None
            return selected or None
    except Exception:
        pass
    return None


def get_model_provider(
    preferred_type: Optional[ModelProviderType] = None,
    allow_simulation: Optional[bool] = None,
) -> BaseModelProvider:
    """
    Returns the active canonical model provider obeying the strict hierarchy:
    1. Preferred explicit provider (if configured and available)
    2. Local LFM2.5 GGUF cognitive core (if model file exists and host RAM passes safety gate)
    3. Configured external cloud endpoint from persistent KeyStore
    4. Configured external cloud endpoint from environment variables
    5. SimulatedTestProvider ONLY if explicitly requested, in test suites, or in dev mode
    6. UnavailableModelProvider with explicit status (NEVER silent simulation in production)
    """
    global _ACTIVE_PROVIDER
    if _ACTIVE_PROVIDER is not None:
        if allow_simulation is False and isinstance(_ACTIVE_PROVIDER, SimulatedTestProvider):
            return UnavailableModelProvider("A simulated test provider is active; production execution is unavailable")
        return _ACTIVE_PROVIDER

    if preferred_type == ModelProviderType.SIMULATED_TEST:
        _ACTIVE_PROVIDER = SimulatedTestProvider()
        return _ACTIVE_PROVIDER

    if preferred_type == ModelProviderType.OXCODER_9B_TEST_PROVIDER:
        _ACTIVE_PROVIDER = OxCoder9BTestProvider()
        return _ACTIVE_PROVIDER

    selected_provider = _selected_provider_id()

    # 1. Local LFM2.5 Cognitive Core (Protected by host RAM safety gate)
    if (
        (preferred_type == ModelProviderType.LFM2_5_COGNITIVE_CORE and selected_provider in (None, "local"))
        or (preferred_type is None and selected_provider in (None, "local"))
    ):
        try:
            from .runtime.lfm25_model_locator import LFM25ModelLocator
            model_file = LFM25ModelLocator.locate_model()
            if model_file:
                lfm_prov = LFM25CognitiveProvider()
                if lfm_prov.is_available():
                    _ACTIVE_PROVIDER = lfm_prov
                    return _ACTIVE_PROVIDER
        except Exception:
            pass

    # 2. KeyStore persistent configurations (e.g. from UI Settings)
    try:
        from core.llm.keystore import KeyStore
        ks = KeyStore()
        keys = [
            k for k in ks.list(reveal=True)
            if k.get("enabled", True)
            and (k.get("api_key") or _is_local_endpoint(k.get("base_url")))
            and (selected_provider is None or k.get("provider") == selected_provider)
        ]
        if keys:
            keys.sort(key=lambda x: x.get("priority", 0), reverse=True)
            key = keys[0]
            external_prov = create_external_model_provider(
                provider_id=key.get("provider"),
                api_key=key.get("api_key"),
                base_url=key.get("base_url"),
                model_name=key.get("model"),
            )
            if external_prov.is_available():
                _ACTIVE_PROVIDER = external_prov
                return _ACTIVE_PROVIDER
    except Exception:
        pass

    # 3. Environment variables (Cloud OpenAI-compatible, Groq, DeepSeek, etc.)
    env_config = _environment_provider_configuration(selected_provider)
    if env_config and (selected_provider is None or env_config.get("provider_id") == selected_provider):
        external_prov = create_external_model_provider(**env_config)
        if external_prov.is_available():
            _ACTIVE_PROVIDER = external_prov
            return _ACTIVE_PROVIDER

    # 4. Simulation mode: STRICTLY guarded, never silent in production
    if allow_simulation is False:
        is_sim_allowed = False
    else:
        is_sim_allowed = bool(
            allow_simulation
            or (os.environ.get("WISE_DEV_MODE") == "1")
            or (os.environ.get("WISE_SIMULATION_MODE") == "1")
            or (os.environ.get("ENV") == "test")
            or ("pytest" in sys.modules)
            or os.environ.get("PYTEST_CURRENT_TEST")
        )
    if is_sim_allowed:
        _ACTIVE_PROVIDER = SimulatedTestProvider()
        return _ACTIVE_PROVIDER

    # 5. Explicit Unavailable State
    _ACTIVE_PROVIDER = UnavailableModelProvider(
        f"The selected provider '{selected_provider}' is not configured or available; no substitute was selected."
        if selected_provider else "No live model provider is configured and available. Configure a model in Settings; production never uses simulation."
    )
    return _ACTIVE_PROVIDER


def get_production_model_provider() -> BaseModelProvider:
    """Returns model provider strictly without simulation fallback."""
    return get_model_provider(allow_simulation=False)


def set_active_model_provider(provider: BaseModelProvider) -> None:
    """Sets or overrides the active model provider for test fixtures or runtime configuration."""
    global _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = provider


def reset_model_provider() -> None:
    """Release the current runtime and force fresh provider selection.

    Local providers may own several gigabytes of CPU/GPU memory.  Simply
    dropping the Python reference leaves that memory allocated until garbage
    collection (and sometimes longer inside the native runtime), so settings
    changes now explicitly unload providers that expose an unload hook.
    """
    global _ACTIVE_PROVIDER
    previous = _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = None
    if previous is None:
        return
    unload = getattr(previous, "unload", None)
    if not callable(unload):
        return
    try:
        unload()
    except Exception as exc:  # pragma: no cover - cleanup must not block reconfiguration
        LOG.warning("Could not unload previous model provider cleanly: %s", exc)


def get_model_runtime_telemetry(allow_simulation: Optional[bool] = None) -> ModelRuntimeTelemetry:
    """Returns standardized runtime telemetry from the active model provider."""
    prov = _ACTIVE_PROVIDER if _ACTIVE_PROVIDER is not None else get_model_provider(allow_simulation=allow_simulation)
    if hasattr(prov, "get_telemetry"):
        return prov.get_telemetry()
    vm = psutil.virtual_memory()
    return ModelRuntimeTelemetry(
        active_provider=type(prov).__name__,
        model_name="unknown",
        is_simulated=getattr(prov, "is_simulated", False),
        total_ram_gb=round(vm.total / (1024 ** 3), 2),
        available_ram_gb=round(vm.available / (1024 ** 3), 2),
        ram_percent=round(vm.percent, 1),
        context_length=2048,
        load_state="UNAVAILABLE",
    )
