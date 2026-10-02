"""TeamModel: orchestrate multiple OpenAI-compatible LLMs.

Improvements over the previous version:

- Skip a model entry when the API key is missing **and** the base URL
  isn't local (avoids accidentally sending requests with a placeholder
  key like ``"not-needed"``).
- Mask any secret-like value before logging.
- Per-model ``temperature`` honoured from ``team_config.json``.
- Single-call retries (1 retry with short backoff) on transient errors.
- New ``vote`` mode that picks the most common response among models.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI


def _mask_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "…" + value[-3:]


def _is_local_base(base_url: Optional[str]) -> bool:
    if not base_url:
        return False
    return any(h in base_url for h in ("localhost", "127.0.0.1", "0.0.0.0"))


class TeamModel:
    """Manage a team of cloud-hosted models."""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            from .paths import CONFIG_DIR

            self.config_path = CONFIG_DIR / "team_config.json"
        else:
            self.config_path = Path(config_path).expanduser()

        self.config: Dict[str, Any] = {}
        self.clients: List[Dict[str, Any]] = []
        self.active_client: Optional[Dict[str, Any]] = None
        self._load_and_init()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _load_and_init(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except Exception as e:
            print(f"⚠️ فشل تحميل إعدادات الفريق: {e}. تشغيل بدون نماذج.")
            self.config = {
                "mode": "single",
                "active_model": "",
                "timeout": 120,
                "models": [],
            }
            return
        self._init_clients()

    def _init_clients(self) -> None:
        self.clients = []
        for m in self.config.get("models", []):
            if not m.get("enabled", True):
                continue
            api_key = m.get("api_key")
            base_url = m.get("base_url")
            if not api_key and not _is_local_base(base_url):
                print(
                    f"⚠️ تم تجاهل النموذج {m.get('name', m.get('model_name'))} "
                    "(لا يوجد api_key)."
                )
                continue
            try:
                client = OpenAI(
                    api_key=api_key or "not-needed",
                    base_url=base_url,
                )
                self.clients.append({
                    "client": client,
                    "model_name": m["model_name"],
                    "name": m.get("name", m["model_name"]),
                    "api_key": api_key,
                    "base_url": base_url,
                    "temperature": float(m.get("temperature", 0.7)),
                    "max_tokens": int(m.get("max_tokens", 1024)),
                })
                print(
                    "✅ تم ربط النموذج: "
                    f"{m.get('name', m['model_name'])}"
                    f" (key={_mask_secret(api_key)})"
                )
            except Exception as e:
                print(f"⚠️ فشل تهيئة النموذج {m.get('model_name')}: {e}")

        mode = self.config.get("mode", "single")
        if mode == "single":
            active_name = self.config.get("active_model", "")
            if active_name:
                for c in self.clients:
                    if c["name"] == active_name:
                        self.active_client = c
                        break
            if self.active_client is None and self.clients:
                self.active_client = self.clients[0]
            if self.active_client:
                print(f"✅ النموذج النشط: {self.active_client['name']}")
            else:
                print("⚠️ لا توجد نماذج مفعلة للوضع الفردي.")
        else:
            print(f"✅ تم تحميل {len(self.clients)} نموذج. الوضع: {mode}")

    # ------------------------------------------------------------------
    # Public dispatch
    # ------------------------------------------------------------------
    def __call__(self, messages: List[Dict], **kwargs) -> str:
        if not self.clients:
            return "❌ لا توجد نماذج مفعلة. استخدم 'admin add-model' لإضافة نموذج."

        mode = self.config.get("mode", "single")
        timeout = kwargs.get("timeout", self.config.get("timeout", 120))
        temperature = kwargs.get("temperature")
        max_tokens = kwargs.get("max_tokens")
        stop = kwargs.get("stop")

        if mode == "single":
            if self.active_client is None:
                return "❌ لم يتم تعيين نموذج نشط. استخدم 'admin set-active'."
            return self._call_with_retry(
                self.active_client, messages, temperature, max_tokens,
                timeout, stop, retries=1,
            )
        if mode == "ensemble":
            return self._ensemble(messages, temperature, max_tokens, timeout, stop)
        if mode == "vote":
            return self._vote(messages, temperature, max_tokens, timeout, stop)
        if mode == "fallback":
            return self._fallback(messages, temperature, max_tokens, timeout, stop)
        return self._first(messages, temperature, max_tokens, timeout, stop)

    # ------------------------------------------------------------------
    # Streaming (Phase 8 Part 2)
    # ------------------------------------------------------------------
    def stream(self, messages: List[Dict], **kwargs):
        """Yield text deltas from the active client.

        Uses OpenAI-compatible ``stream=True``. On error, falls back
        to a single full-response chunk so callers can always consume
        the iterator protocol.
        """
        if not self.clients:
            yield ("❌ لا توجد نماذج مفعلة. "
                   "استخدم 'admin add-model' لإضافة نموذج.")
            return
        mode = self.config.get("mode", "single")
        c = self.active_client if mode == "single" else None
        if c is None:
            c = self.clients[0]
        temperature = kwargs.get("temperature")
        max_tokens = kwargs.get("max_tokens")
        stop = kwargs.get("stop")
        timeout = kwargs.get("timeout", self.config.get("timeout", 120))
        temp = (temperature if temperature is not None
                else c.get("temperature", 0.7))
        max_t = (max_tokens if max_tokens is not None
                 else c.get("max_tokens", 1024))
        try:
            resp = c["client"].chat.completions.create(
                model=c["model_name"],
                messages=messages,
                temperature=temp,
                max_tokens=max_t,
                stop=stop,
                timeout=timeout if timeout and timeout > 0 else None,
                stream=True,
            )
            yielded = False
            for chunk in resp:
                try:
                    delta = chunk.choices[0].delta.content
                except Exception:  # noqa: BLE001
                    delta = None
                if delta:
                    yielded = True
                    yield delta
            if not yielded:
                yield ""
        except Exception as e:  # noqa: BLE001
            # Degrade to a non-streaming call so the user still gets
            # an answer.
            yield self(messages, **kwargs) or f"[stream error: {e}]"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _call_client(self, c, messages, temperature, max_tokens, stop, timeout):
        temp = temperature if temperature is not None else c.get("temperature", 0.7)
        max_t = max_tokens if max_tokens is not None else c.get("max_tokens", 1024)
        response = c["client"].chat.completions.create(
            model=c["model_name"],
            messages=messages,
            temperature=temp,
            max_tokens=max_t,
            stop=stop,
            timeout=timeout if timeout and timeout > 0 else None,
        )
        return response.choices[0].message.content.strip()

    def _call_with_retry(self, c, messages, temperature, max_tokens, timeout,
                         stop, retries: int = 1) -> str:
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                return self._call_client(
                    c, messages, temperature, max_tokens, stop, timeout
                )
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.4 * (2 ** attempt))
        return f"❌ خطأ في النموذج {c['name']}: {last_err}"

    def _first(self, messages, temperature, max_tokens, timeout, stop) -> str:
        for c in self.clients:
            res = self._call_with_retry(
                c, messages, temperature, max_tokens, timeout, stop, retries=0,
            )
            if not res.startswith("❌"):
                return res
        return "❌ فشلت جميع النماذج."

    def _fallback(self, messages, temperature, max_tokens, timeout, stop) -> str:
        errors: List[str] = []
        for c in self.clients:
            res = self._call_with_retry(
                c, messages, temperature, max_tokens, timeout, stop, retries=1,
            )
            if not res.startswith("❌"):
                return res
            errors.append(res)
        return "❌ فشلت جميع النماذج: " + "; ".join(errors)

    def _ensemble(self, messages, temperature, max_tokens, timeout, stop) -> str:
        responses: List[str] = []
        with ThreadPoolExecutor(max_workers=max(1, len(self.clients))) as pool:
            futures = {
                pool.submit(
                    self._call_client, c, messages, temperature, max_tokens,
                    stop, timeout,
                ): c["name"]
                for c in self.clients
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    responses.append(f"### {name}:\n{fut.result()}")
                except Exception as e:
                    responses.append(f"### {name}: ❌ {e}")
        return "\n\n---\n\n".join(responses) if responses else "❌ لم يتم الحصول على أي رد."

    def _vote(self, messages, temperature, max_tokens, timeout, stop) -> str:
        replies: List[str] = []
        with ThreadPoolExecutor(max_workers=max(1, len(self.clients))) as pool:
            futures = [
                pool.submit(
                    self._call_client, c, messages, temperature, max_tokens,
                    stop, timeout,
                )
                for c in self.clients
            ]
            for fut in as_completed(futures):
                try:
                    replies.append(fut.result())
                except Exception:
                    continue
        if not replies:
            return "❌ لم يتم الحصول على أي رد."
        # Vote on the first 200 chars to avoid noise.
        keys = [r[:200].strip() for r in replies]
        winner_key, _ = Counter(keys).most_common(1)[0]
        for r in replies:
            if r[:200].strip() == winner_key:
                return r
        return replies[0]
