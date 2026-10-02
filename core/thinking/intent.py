"""Chat-driven intent classifier — surface admin tools for config commands.

When a user types e.g. *"add this OpenAI key sk-..."* during a chat,
the agent should be able to apply the change directly instead of
asking the user to navigate to Settings. This module classifies the
incoming user message into one of a small set of **configuration
intents** and returns the matching admin tool name plus a best-effort
extraction of arguments.

The classifier is intentionally cheap and deterministic — regex +
keyword matching — so it works offline, in tests, and on tiny devices
where a model round-trip would be wasteful. Where the regex is
ambiguous we prefer to return ``None`` and let the regular ReAct loop
do its job.

Public surface:

* :class:`IntentMatch` — dataclass with ``tool``, ``args``,
  ``confidence``, ``reason``.
* :func:`classify(text)` — main entry; returns ``Optional[IntentMatch]``.
* :func:`format_intent_hint(match)` — produces a tiny human-readable
  hint the engine prepends to the tool list when surfacing the
  matched tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
_API_KEY_PATTERN = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{16,}"
    r"|sk-ant-[A-Za-z0-9_\-]{16,}"
    r"|gsk_[A-Za-z0-9_\-]{16,}"
    r"|AIza[A-Za-z0-9_\-]{16,})",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(
    r"\b(Bearer\s+[A-Za-z0-9_\-\.]{20,}|hf_[A-Za-z0-9]{16,}|"
    r"xoxb-[A-Za-z0-9-]{20,}|ghp_[A-Za-z0-9]{20,})",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"\b(https?://[^\s'\"<>]+)")
_APPRISE_URL_PATTERN = re.compile(
    r"\b((?:tgram|discord|slack|mailto|pover|gotify|ntfy|"
    r"json|xml|matrix|teams|rocket|signal|gchat|webex)s?://[^\s'\"<>]+)",
    re.IGNORECASE,
)
_CRON_PATTERN = re.compile(
    r"(?:every\s+day\s+at\s+\d{1,2}(?::\d{2})?|"
    r"every\s+\d+\s+(?:minute|hour|day)s?|"
    r"\b\*?[\d\*\-\/,]+\s+\*?[\d\*\-\/,]+\s+\*?[\d\*\-\/,]+\s+"
    r"\*?[\d\*\-\/,]+\s+\*?[\d\*\-\/,]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class IntentMatch:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "args": dict(self.args),
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _has_word(text: str, *words: str) -> bool:
    low = text.lower()
    return any(w in low for w in words)


def _name_token(text: str, default: str = "") -> str:
    m = re.search(r"\b(?:named|name|call(?:ed)?|alias)\s+"
                  r"['\"]?([A-Za-z0-9_\-]{2,40})['\"]?",
                  text, re.IGNORECASE)
    if m:
        return m.group(1)
    return default


def _natural_to_cron(text: str) -> Optional[str]:
    low = text.lower()
    m = re.search(r"every\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?", low)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2) or 0)
        return f"{mm} {h} * * *"
    m = re.search(r"every\s+(\d+)\s+minute", low)
    if m:
        return f"*/{int(m.group(1))} * * * *"
    m = re.search(r"every\s+(\d+)\s+hour", low)
    if m:
        return f"0 */{int(m.group(1))} * * *"
    m = re.search(r"every\s+hour\b", low)
    if m:
        return "0 * * * *"
    if re.match(r"^\s*[\d\*\-\/,]+(\s+[\d\*\-\/,]+){4}\s*$", text.strip()):
        return text.strip()
    return None


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
def classify(text: str) -> Optional[IntentMatch]:
    """Return the best-matching admin tool + args for ``text`` (or ``None``)."""
    if not isinstance(text, str) or not text.strip():
        return None

    low = text.lower()

    # --- API keys ----------------------------------------------------------
    key_match = _API_KEY_PATTERN.search(text) or _BEARER_PATTERN.search(text)
    if key_match and _has_word(low, "key", "api", "token", "credential",
                                  "save", "store", "add", "register",
                                  "use this", "مفتاح", "أضف"):
        api_key = key_match.group(0)
        provider = None
        if api_key.startswith("sk-ant-"):
            provider = "anthropic"
        elif api_key.startswith("sk-"):
            provider = "openai"
        elif api_key.startswith("gsk_"):
            provider = "groq"
        elif api_key.startswith("AIza"):
            provider = "google"
        elif api_key.startswith("hf_"):
            provider = "huggingface"
        name = _name_token(text) or (provider or "key")
        return IntentMatch(
            tool="admin.add_api_key",
            args={"name": name, "api_key": api_key,
                   **({"provider": provider} if provider else {})},
            confidence=0.85,
            reason="detected an api-key shaped token + add/register verb",
        )

    if _has_word(low, "remove", "delete", "drop") and \
            _has_word(low, "key", "api"):
        name = _name_token(text)
        if name:
            return IntentMatch(
                tool="admin.remove_api_key",
                args={"name": name},
                confidence=0.65,
                reason="delete-verb + 'key' + named target",
            )

    # --- MCP servers -------------------------------------------------------
    if _has_word(low, "mcp"):
        if _has_word(low, "remove", "delete", "drop"):
            n = _name_token(text)
            if n:
                return IntentMatch(
                    tool="admin.remove_mcp_server",
                    args={"name": n},
                    confidence=0.7,
                    reason="mcp + delete-verb + named target")
        if _has_word(low, "add", "register", "connect", "link", "use"):
            url_m = _URL_PATTERN.search(text)
            n = _name_token(text) or "mcp-server"
            if url_m:
                return IntentMatch(
                    tool="admin.add_mcp_server",
                    args={"name": n, "kind": "http",
                           "url": url_m.group(0)},
                    confidence=0.85,
                    reason="mcp + add-verb + http URL")

    # --- Channels ----------------------------------------------------------
    apprise_m = _APPRISE_URL_PATTERN.search(text)
    if apprise_m and _has_word(low, "channel", "notification", "notify",
                                  "send to", "alert"):
        scheme = apprise_m.group(0).split("://", 1)[0]
        n = _name_token(text) or scheme
        return IntentMatch(
            tool="admin.add_channel",
            args={"name": n, "url": apprise_m.group(0)},
            confidence=0.85,
            reason="notification verb + apprise-shaped URL",
        )

    # --- Mode switching ----------------------------------------------------
    if _has_word(low, "switch", "enable", "turn on", "use"):
        if "pro mode" in low or "pro-mode" in low:
            return IntentMatch(
                tool="admin.set_mode",
                args={"mode": "pro"},
                confidence=0.8,
                reason="explicit 'pro mode' phrase")
        if "lite mode" in low or "lite-mode" in low:
            return IntentMatch(
                tool="admin.set_mode",
                args={"mode": "lite"},
                confidence=0.8,
                reason="explicit 'lite mode' phrase")

    # --- Scheduling (checked before self-test so "every day at 9 run a
    # self-test" routes to scheduler, not the immediate run) ---------------
    cron = _natural_to_cron(text)
    if cron and _has_word(low, "schedule", "every", "cron",
                            "remind", "trigger"):
        n = _name_token(text) or "scheduled-task"
        target = "self_test" if "self-test" in low or "self test" in low \
                 else "agent.run"
        return IntentMatch(
            tool="admin.schedule_task",
            args={"name": n, "cron": cron, "target": target,
                   "payload": {"text": text}},
            confidence=0.7,
            reason="cron-able expression + schedule verb")

    # --- Self-test / repair / health ---------------------------------------
    if _has_word(low, "self-test", "self test", "system check",
                  "health check", "diagnostics", "diagnose"):
        return IntentMatch(
            tool="admin.run_self_test",
            args={},
            confidence=0.75,
            reason="self-test phrase")

    if _has_word(low, "repair", "fix the config", "reset config"):
        target = "config"
        if "sandbox" in low:
            target = "sandbox"
        elif "tools" in low:
            target = "tools"
        elif "channel" in low:
            target = "channels"
        return IntentMatch(
            tool="admin.repair",
            args={"target": target},
            confidence=0.7,
            reason="repair verb + target keyword")

    # --- Resource limits ---------------------------------------------------
    rm = re.search(r"(?:cpu|memory|ram|timeout|disk|parallel)"
                   r"[a-z\s\-:=]{0,32}?(\d+)\s*"
                   r"(seconds?|secs?|s|mb|gb|minutes?|mins?)?",
                   low)
    if rm and _has_word(low, "set", "raise", "lower", "limit", "cap",
                          "increase", "decrease"):
        n = int(rm.group(1))
        unit = (rm.group(2) or "").lower()
        field = "cpu_seconds"
        if "memory" in low or "ram" in low:
            field = "memory_mb"
            if unit == "gb":
                n *= 1024
        elif "disk" in low:
            field = "disk_mb"
            if unit == "gb":
                n *= 1024
        elif "timeout" in low:
            field = "network_timeout_s"
        elif "parallel" in low:
            field = "parallel_tools"
        else:
            field = "cpu_seconds"
            if unit and unit.startswith("min"):
                n *= 60
        return IntentMatch(
            tool="admin.set_resource_limits",
            args={field: n},
            confidence=0.65,
            reason=f"resource verb + numeric '{n}' for {field}")

    return None


def format_intent_hint(match: IntentMatch) -> str:
    """Render a short hint the ThinkingEngine can prepend to tool docs."""
    parts = [
        f"Suggested tool: {match.tool}",
        f"Reason: {match.reason}",
    ]
    if match.args:
        keys = ", ".join(sorted(match.args.keys()))
        parts.append(f"Pre-extracted args: {{{keys}}}")
    return " | ".join(parts)


__all__ = [
    "IntentMatch",
    "classify",
    "format_intent_hint",
]
