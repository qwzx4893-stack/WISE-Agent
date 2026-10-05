"""Universal LLM wrapper that speaks the three real transports.

We deliberately do NOT pull in ``litellm`` because it's a 100MB+ dep
that conflicts with phone-friendly deployment. Instead we implement
the three native request shapes ourselves:

- ``openai_compat`` — uses the ``openai`` SDK (already a hard dep).
- ``anthropic`` — uses the ``anthropic`` SDK if installed; falls back to
  raw ``httpx`` POST against ``/v1/messages`` otherwise.
- ``gemini`` — raw ``httpx`` POST against
  ``/v1beta/models/<model>:generateContent``. The ``google-genai`` SDK
  is not required.

Public surface:
- :class:`UniversalLLM` — single backend, one ``__call__`` returning
  a string.
- :class:`LLMRouter` — multi-backend dispatcher with fallback / vote /
  ensemble modes (drop-in for ``TeamModel``).
- :func:`build_router_from_keystore(keystore)` — convenience.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .providers import ProviderInfo, detect_provider, get_provider

try:
    from core.security.transient_vault import sanitize_text
except Exception:  # pragma: no cover - the router must stay usable standalone
    def sanitize_text(text: str) -> str:
        return text


# ----------------------------------------------------------------------
# Single-backend client
# ----------------------------------------------------------------------
class UniversalLLM:
    """One LLM backend.

    Examples:
        UniversalLLM(api_key="sk-…", model="gpt-4o-mini")
        UniversalLLM(api_key="sk-ant-…", model="claude-3-5-sonnet-20241022")
        UniversalLLM(api_key="AIza…", model="gemini-2.0-flash")
        UniversalLLM(base_url="http://localhost:11434/v1", model="llama3")
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        provider: Optional[str] = None,
        name: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        timeout: int = 120,
    ):
        info = (
            get_provider(provider)
            if provider else detect_provider(api_key, base_url)
        )
        self.provider: ProviderInfo = info or detect_provider(api_key, base_url)
        self.api_key = api_key
        self.model = model or self.provider.default_model or "gpt-4o-mini"
        self.base_url = base_url or self.provider.base_url
        self.name = name or f"{self.provider.id}:{self.model}"
        self.model_name = self.model  # for tools that expect this attribute
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout = int(timeout)

        self._openai_client = None
        self._anthropic_client = None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def __call__(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        messages = self._with_user_instructions(messages)
        transport = self.provider.transport
        if transport == "anthropic":
            return self._call_anthropic(messages, **kwargs)
        if transport == "gemini":
            return self._call_gemini(messages, **kwargs)
        return self._call_openai_compat(messages, **kwargs)

    # ------------------------------------------------------------------
    # Streaming dispatch (Phase 8 Part 2)
    # ------------------------------------------------------------------
    def stream(self, messages: List[Dict[str, str]],
               **kwargs: Any) -> Iterable[str]:
        """Yield text deltas as the provider emits them.

        Each yielded string is the *delta* for the current chunk (not
        the cumulative text). Callers that want the full response can
        simply ``"".join(llm.stream(messages))``. Providers that can't
        stream (or whose SDKs we don't have here) fall back to a
        single-chunk yield with the complete response — callers can
        rely on the iterator protocol unconditionally.
        """
        messages = self._with_user_instructions(messages)
        transport = self.provider.transport
        try:
            if transport == "anthropic":
                yield from self._stream_anthropic(messages, **kwargs)
                return
            if transport == "gemini":
                yield from self._stream_gemini(messages, **kwargs)
                return
            yield from self._stream_openai_compat(messages, **kwargs)
        except Exception as e:  # noqa: BLE001
            # A broken stream must not spend an unrequested second completion
            # or duplicate already-delivered text. Preserve provider selection.
            raise RuntimeError("Selected provider stream failed; retry explicitly") from e

    @staticmethod
    def _with_user_instructions(messages):
        from core.user_instructions import apply_user_instructions
        output = [dict(message) for message in messages]
        for message in output:
            if message.get("role") == "system":
                message["content"] = apply_user_instructions(str(message.get("content", "")))
                break
        else:
            prompt = apply_user_instructions("")
            if prompt:
                output.insert(0, {"role": "system", "content": prompt})
        return output

    # ------------------------------------------------------------------
    # OpenAI-compatible
    # ------------------------------------------------------------------
    def _ensure_openai(self):
        if self._openai_client is not None:
            return self._openai_client
        from openai import OpenAI
        self._openai_client = OpenAI(
            api_key=self.api_key or "not-needed",
            base_url=self.base_url,
        )
        return self._openai_client

    def _call_openai_compat(self, messages, **kw) -> str:
        client = self._ensure_openai()
        temperature = kw.get("temperature", self.temperature)
        max_tokens = kw.get("max_tokens", self.max_tokens)
        timeout = kw.get("timeout", self.timeout)
        stop = kw.get("stop")
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stop=stop,
                timeout=timeout if timeout and timeout > 0 else None,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            raise RuntimeError(f"openai_compat[{self.name}]: {e}") from e

    def _stream_openai_compat(self, messages, **kw) -> Iterable[str]:
        client = self._ensure_openai()
        temperature = kw.get("temperature", self.temperature)
        max_tokens = kw.get("max_tokens", self.max_tokens)
        timeout = kw.get("timeout", self.timeout)
        stop = kw.get("stop")
        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            timeout=timeout if timeout and timeout > 0 else None,
            stream=True,
        )
        for chunk in resp:
            try:
                delta = chunk.choices[0].delta.content
            except Exception:  # noqa: BLE001
                delta = None
            if delta:
                yield delta

    # ------------------------------------------------------------------
    # Anthropic
    # ------------------------------------------------------------------
    @staticmethod
    def _split_anthropic(messages: List[Dict[str, str]]) -> tuple[str, List[Dict[str, Any]]]:
        system_parts: List[str] = []
        chat: List[Dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                chat.append({"role": role, "content": content})
            else:
                # Treat unknown roles as user input.
                chat.append({"role": "user", "content": content})
        return "\n\n".join(system_parts), chat

    def _ensure_anthropic(self):
        if self._anthropic_client is not None:
            return self._anthropic_client
        try:
            import anthropic  # type: ignore
        except ImportError:
            return None
        self._anthropic_client = anthropic.Anthropic(
            api_key=self.api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
            base_url=self.base_url or None,
            timeout=self.timeout,
        )
        return self._anthropic_client

    def _call_anthropic(self, messages, **kw) -> str:
        system, chat = self._split_anthropic(messages)
        max_tokens = kw.get("max_tokens", self.max_tokens)
        temperature = kw.get("temperature", self.temperature)

        client = self._ensure_anthropic()
        if client is not None:
            try:
                resp = client.messages.create(
                    model=self.model,
                    system=system or None,
                    messages=chat,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                # ``content`` is a list of blocks.
                pieces = []
                for block in resp.content:
                    text = getattr(block, "text", None) or block.get("text", "") if isinstance(block, dict) else ""
                    if text:
                        pieces.append(text)
                return "\n".join(pieces).strip()
            except Exception as e:
                raise RuntimeError(f"anthropic[{self.name}]: {e}") from e

        # Fallback: raw HTTP via httpx
        return self._call_anthropic_http(system, chat, max_tokens, temperature)

    def _call_anthropic_http(self, system: str, chat: List[Dict[str, Any]],
                             max_tokens: int, temperature: float) -> str:
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError("anthropic transport requires `anthropic` SDK or `httpx`") from e
        url = (self.base_url or "https://api.anthropic.com/v1") + "/messages"
        payload = {
            "model": self.model,
            "messages": chat,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as cli:
            r = cli.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"anthropic[{self.name}] HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
        pieces = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(pieces).strip()

    def _stream_anthropic(self, messages, **kw) -> Iterable[str]:
        system, chat = self._split_anthropic(messages)
        max_tokens = kw.get("max_tokens", self.max_tokens)
        temperature = kw.get("temperature", self.temperature)
        client = self._ensure_anthropic()
        if client is None:
            # Fallback to non-streaming when the SDK is absent.
            yield self._call_anthropic_http(
                system, chat, max_tokens, temperature)
            return
        with client.messages.stream(
            model=self.model,
            system=system or None,
            messages=chat,
            max_tokens=max_tokens,
            temperature=temperature,
        ) as stream:
            for delta in stream.text_stream:
                if delta:
                    yield delta

    # ------------------------------------------------------------------
    # Google Gemini
    # ------------------------------------------------------------------
    @staticmethod
    def _to_gemini_contents(messages: List[Dict[str, str]]) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        system_parts: List[str] = []
        contents: List[Dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(content)
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": content}]})
        sys_block = (
            {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
        )
        return sys_block, contents

    def _call_gemini(self, messages, **kw) -> str:
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError("gemini transport requires `httpx`") from e
        max_tokens = kw.get("max_tokens", self.max_tokens)
        temperature = kw.get("temperature", self.temperature)

        sys_block, contents = self._to_gemini_contents(messages)
        url = (
            (self.base_url or "https://generativelanguage.googleapis.com/v1beta")
            + f"/models/{self.model}:generateContent"
        )
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if sys_block is not None:
            payload["systemInstruction"] = sys_block
        params = {"key": self.api_key} if self.api_key else {}
        headers = {"content-type": "application/json"}
        with httpx.Client(timeout=self.timeout) as cli:
            r = cli.post(url, params=params, headers=headers, json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"gemini[{self.name}] HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "\n".join(p.get("text", "") for p in parts if "text" in p).strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"gemini[{self.name}] unexpected response: {data}") from e

    def _stream_gemini(self, messages, **kw) -> Iterable[str]:
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError(
                "gemini transport requires `httpx`") from e
        max_tokens = kw.get("max_tokens", self.max_tokens)
        temperature = kw.get("temperature", self.temperature)
        sys_block, contents = self._to_gemini_contents(messages)
        url = (
            (self.base_url
             or "https://generativelanguage.googleapis.com/v1beta")
            + f"/models/{self.model}:streamGenerateContent"
        )
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if sys_block is not None:
            payload["systemInstruction"] = sys_block
        params = {"alt": "sse"}
        if self.api_key:
            params["key"] = self.api_key
        headers = {"content-type": "application/json"}
        with httpx.Client(timeout=self.timeout) as cli:
            with cli.stream("POST", url, params=params,
                             headers=headers, json=payload) as r:
                if r.status_code >= 400:
                    body = r.read().decode(errors="replace")[:200]
                    raise RuntimeError(
                        f"gemini[{self.name}] HTTP "
                        f"{r.status_code}: {body}")
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        data = json.loads(raw)
                        parts = (data.get("candidates") or [{}])[0]\
                            .get("content", {}).get("parts", [])
                        for p in parts:
                            t = p.get("text")
                            if t:
                                yield t
                    except Exception:  # noqa: BLE001
                        continue


# ----------------------------------------------------------------------
# Multi-backend router
# ----------------------------------------------------------------------
@dataclass
class _Backend:
    llm: UniversalLLM
    priority: int = 0
    enabled: bool = True
    last_error: Optional[str] = None
    last_success_ts: float = 0.0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0


class LLMRouter:
    """Multi-provider router with fallback / ensemble / vote modes.

    Drop-in replacement for ``TeamModel``: same ``__call__(messages)``
    surface, same modes (``single`` / ``fallback`` / ``ensemble`` /
    ``vote``), and exposes ``clients`` + ``active_client`` for compat.
    """

    def __init__(self, mode: str = "single", timeout: int = 120,
                 active_name: Optional[str] = None,
                 failure_threshold: int = 3,
                 circuit_cooldown_seconds: float = 30.0):
        self.mode = mode
        self.timeout = timeout
        self.active_name = active_name
        # A completion request can have reached the provider even if its
        # response timed out.  We therefore do not retry it automatically:
        # retrying can double-charge or duplicate an action.  Instead a
        # bounded circuit breaker moves new requests to an independent
        # fallback until the provider has had time to recover.
        self.failure_threshold = max(1, int(failure_threshold))
        self.circuit_cooldown_seconds = max(1.0, float(circuit_cooldown_seconds))
        self.backends: List[_Backend] = []

    # Compatibility shims for the old TeamModel surface ----------------
    @property
    def clients(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": b.llm.name,
                "model_name": b.llm.model,
                "base_url": b.llm.base_url,
                "temperature": b.llm.temperature,
                "max_tokens": b.llm.max_tokens,
                "client": b.llm,
            }
            for b in self.backends if b.enabled
        ]

    @property
    def active_client(self) -> Optional[Dict[str, Any]]:
        if not self.backends:
            return None
        if self.active_name:
            for b in self.backends:
                if b.llm.name == self.active_name and b.enabled:
                    return {
                        "name": b.llm.name,
                        "model_name": b.llm.model,
                        "base_url": b.llm.base_url,
                        "temperature": b.llm.temperature,
                        "max_tokens": b.llm.max_tokens,
                        "client": b.llm,
                    }
        return self.clients[0] if self.clients else None

    @property
    def model_name(self) -> str:
        ac = self.active_client
        return ac["model_name"] if ac else "gpt-4o-mini"

    # ------------------------------------------------------------------
    def add(self, llm: UniversalLLM, *, priority: int = 0,
            enabled: bool = True) -> None:
        self.backends.append(_Backend(llm=llm, priority=priority, enabled=enabled))
        self.backends.sort(key=lambda b: -b.priority)

    def remove(self, name: str) -> None:
        self.backends = [b for b in self.backends if b.llm.name != name]

    # ------------------------------------------------------------------
    # Availability and failure accounting
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_error(exc: Exception | str) -> str:
        return sanitize_text(str(exc))[:500]

    def _circuit_is_open(self, backend: _Backend, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return backend.circuit_open_until > now

    def _mark_success(self, backend: _Backend) -> None:
        backend.last_success_ts = time.time()
        backend.last_error = None
        backend.consecutive_failures = 0
        backend.circuit_open_until = 0.0

    def _mark_failure(self, backend: _Backend, exc: Exception | str) -> None:
        backend.last_error = self._safe_error(exc)
        backend.consecutive_failures += 1
        if backend.consecutive_failures >= self.failure_threshold:
            backend.circuit_open_until = time.time() + self.circuit_cooldown_seconds

    def _eligible(self, *, single: bool = False) -> Iterable[_Backend]:
        now = time.time()
        for backend in self.backends:
            if not backend.enabled:
                continue
            if single and self.active_name and backend.llm.name != self.active_name:
                continue
            if self._circuit_is_open(backend, now):
                continue
            yield backend

    def _selected_backend(self) -> Optional[_Backend]:
        """Return the user's selected backend without substituting another."""
        for backend in self.backends:
            if not backend.enabled:
                continue
            if self.active_name and backend.llm.name != self.active_name:
                continue
            return backend
        return None

    def _unavailable_message(self, *, single: bool = False) -> str:
        if single:
            selected = self._selected_backend()
            if selected and self._circuit_is_open(selected):
                return "❌ المزوّد أو النموذج المختار متوقف مؤقتاً بعد أخطاء متكررة."
        candidates = [b for b in self.backends if b.enabled and (
            not single or not self.active_name or b.llm.name == self.active_name
        )]
        if candidates and all(self._circuit_is_open(b) for b in candidates):
            return "❌ جميع المزودين المطابقين متوقفة مؤقتاً بعد أخطاء متكررة."
        return "❌ لا توجد backend نشطة."

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def __call__(self, messages: List[Dict[str, str]], **kw: Any) -> str:
        if not self.backends:
            return "❌ لا توجد نماذج مفعلة. استخدم /admin/keys لإضافة مفتاح."
        mode = kw.pop("mode", None) or self.mode
        if mode == "single":
            return self._single(messages, **kw)
        if mode == "vote":
            return self._vote(messages, **kw)
        if mode == "ensemble":
            return self._ensemble(messages, **kw)
        # Provider selection is explicit.  A failed completion is reported to
        # the caller; it must never silently spend a request on another
        # provider or model selected by the user.
        return self._single(messages, **kw)

    def stream(self, messages: List[Dict[str, str]],
               **kw: Any) -> Iterable[str]:
        """Delegate streaming to the first enabled backend.

        ``vote`` / ``ensemble`` modes aggregate multiple full
        responses so they can't be streamed; we transparently fall
        back to the non-streaming ``__call__`` and yield the complete
        answer as a single chunk.
        """
        if not self.backends:
            yield ("❌ لا توجد نماذج مفعلة. "
                   "استخدم /admin/keys لإضافة مفتاح.")
            return
        mode = kw.pop("mode", None) or self.mode
        if mode in ("vote", "ensemble"):
            yield self(messages, mode=mode, **kw)
            return
        backend = self._selected_backend()
        if backend is None or self._circuit_is_open(backend):
            yield self._unavailable_message(single=True)
            return
        try:
            yielded = False
            for delta in backend.llm.stream(messages, **kw):
                yielded = True
                yield delta
            if not yielded:
                raise RuntimeError("provider returned an empty stream")
            self._mark_success(backend)
        except Exception as e:  # noqa: BLE001
            self._mark_failure(backend, e)
            yield f"❌ {backend.llm.name}: {self._safe_error(e)}"

    def _single(self, messages, **kw) -> str:
        backend = self._selected_backend()
        if backend is None or self._circuit_is_open(backend):
            return self._unavailable_message(single=True)
        try:
            out = backend.llm(messages, **kw)
            self._mark_success(backend)
            return out
        except Exception as e:
            self._mark_failure(backend, e)
            return f"❌ {backend.llm.name}: {self._safe_error(e)}"

    def _fallback(self, messages, **kw) -> str:
        errors: List[str] = []
        for b in self._eligible():
            try:
                out = b.llm(messages, **kw)
                self._mark_success(b)
                return out
            except Exception as e:
                self._mark_failure(b, e)
                errors.append(f"{b.llm.name}: {self._safe_error(e)}")
        if not errors:
            return self._unavailable_message()
        return "❌ فشلت جميع النماذج: " + " | ".join(errors)

    def _ensemble(self, messages, **kw) -> str:
        responses: List[str] = []
        eligible = list(self._eligible())
        if not eligible:
            return self._unavailable_message()
        with ThreadPoolExecutor(max_workers=max(1, len(eligible))) as pool:
            futures = {
                pool.submit(b.llm, messages, **kw): b for b in eligible
            }
            for fut in as_completed(futures):
                b = futures[fut]
                try:
                    responses.append(f"### {b.llm.name}:\n{fut.result()}")
                    self._mark_success(b)
                except Exception as e:
                    self._mark_failure(b, e)
                    responses.append(f"### {b.llm.name}: ❌ {self._safe_error(e)}")
        return "\n\n---\n\n".join(responses) if responses else "❌ لا ردود."

    def _vote(self, messages, **kw) -> str:
        replies: List[str] = []
        with ThreadPoolExecutor(max_workers=max(1, len(self.backends))) as pool:
            futures = {
                pool.submit(b.llm, messages, **kw): b
                for b in self._eligible()
            }
            for fut in as_completed(futures):
                try:
                    replies.append(fut.result())
                    self._mark_success(futures[fut])
                except Exception as e:
                    self._mark_failure(futures[fut], e)
                    continue
        if not replies:
            return "❌ لا ردود."
        keys = [r[:200].strip() for r in replies]
        winner_key, _ = Counter(keys).most_common(1)[0]
        for r in replies:
            if r[:200].strip() == winner_key:
                return r
        return replies[0]

    # ------------------------------------------------------------------
    def status(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": b.llm.name,
                "provider": b.llm.provider.id,
                "model": b.llm.model,
                "base_url": b.llm.base_url,
                "enabled": b.enabled,
                "priority": b.priority,
                "last_error": b.last_error,
                "last_success_ts": b.last_success_ts,
                "consecutive_failures": b.consecutive_failures,
                "circuit_open": self._circuit_is_open(b),
                "circuit_open_until": b.circuit_open_until or None,
            }
            for b in self.backends
        ]


# ----------------------------------------------------------------------
# Factories
# ----------------------------------------------------------------------
def build_router_from_keystore(keystore: Any, *,
                               mode: str = "single",
                               timeout: int = 120) -> LLMRouter:
    """Build a router from a :class:`KeyStore` plus environment.

    1. Pull every entry from the keystore (with revealed plaintext key).
    2. For each, instantiate :class:`UniversalLLM` via the resolved
       provider transport.
    3. Also auto-include any ``*_API_KEY`` env var listed on the
       provider catalogue, so users who already exported their keys
       don't have to re-add them.
    """
    router = LLMRouter(mode=mode, timeout=timeout)

    seen: set[str] = set()
    for entry in keystore.list(reveal=True):
        if not entry.get("enabled", True):
            continue
        api_key = entry.get("api_key", "")
        provider_id = entry.get("provider")
        try:
            llm = UniversalLLM(
                api_key=api_key,
                model=entry.get("model"),
                base_url=entry.get("base_url"),
                provider=provider_id,
                name=entry.get("name"),
                timeout=timeout,
            )
            router.add(llm, priority=int(entry.get("priority", 0) or 0))
            seen.add(provider_id)
        except Exception as e:
            print(f"⚠️ تعذّر تحميل المفتاح {entry.get('name')}: {e}")

    # Pick up ENV-only keys for providers we didn't already see.
    from .providers import PROVIDERS
    for p in PROVIDERS:
        if not p.api_key_env or p.id in seen:
            continue
        env_key = os.environ.get(p.api_key_env)
        if not env_key:
            continue
        try:
            llm = UniversalLLM(
                api_key=env_key, provider=p.id, timeout=timeout,
                name=f"env:{p.id}",
            )
            router.add(llm, priority=-1)  # env keys are lowest priority
        except Exception as e:
            print(f"⚠️ تعذّر تهيئة {p.id} من البيئة: {e}")
    return router


__all__ = [
    "UniversalLLM",
    "LLMRouter",
    "build_router_from_keystore",
]
