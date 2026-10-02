"""Quality-preserving, deterministic chat-context compression.

This module saves prompt tokens without another paid LLM call. System
policies, the original user goal, recent turns, and consent/security decisions
are never silently discarded. Older turns become a compact fact ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .token_counter import TokenCounter


NOISE_PREFIXES = ("لم يتم العثور",)
NUDGE_HINTS = ("التنسيق غير صحيح",)
_SUMMARY_PREFIX = "[ملخص حقائق السياق السابق]"
_CRITICAL_WORDS = (
    "security", "safe", "safety", "consent", "confirm", "approval",
    "permission", "secret", "password", "token", "api key", "uac",
    "أمان", "سلامة", "موافقة", "إذن", "كلمة مرور", "مفتاح", "سر",
    "لا تحذف", "لا ترسل", "لا تنفذ", "ممنوع",
)


@dataclass
class CompressionReport:
    before_tokens: int = 0
    after_tokens: int = 0
    dropped: int = 0
    summarised: int = 0
    truncated: int = 0
    deduplicated: int = 0
    retained_facts: int = 0

    def saved(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)


@dataclass
class CompressionResult:
    messages: List[Dict[str, str]]
    report: CompressionReport = field(default_factory=CompressionReport)


class ContextCompressor:
    """Compress a chat while retaining the information that drives actions."""

    def __init__(self, model_name: str = "gpt-4o-mini", max_tokens: int = 8000,
                 keep_recent_turns: int = 6,
                 summariser: Optional[Callable[[List[Dict[str, str]]], str]] = None):
        self.counter = TokenCounter(model_name)
        self.max_tokens = max(64, int(max_tokens))
        self.keep_recent_turns = max(1, keep_recent_turns)
        # For explicitly supplied local/offline summarisers. The built-in path
        # remains deterministic and incurs zero LLM tokens.
        self.summariser = summariser

    def compress(self, messages: List[Dict[str, str]], *,
                 preserve_system: bool = True) -> List[Dict[str, str]]:
        return self.compress_with_report(messages, preserve_system=preserve_system).messages

    def __call__(self, messages: List[Dict[str, str]],
                 preserve_system: bool = True) -> List[Dict[str, str]]:
        return self.compress(messages, preserve_system=preserve_system)

    def compress_with_report(self, messages: List[Dict[str, str]], *,
                             preserve_system: bool = True) -> CompressionResult:
        report = CompressionReport(before_tokens=self.counter.count_messages(messages))
        cleaned = self._deduplicate(self._drop_noise(messages, report), report)
        cleaned = self._coalesce_observations(cleaned)
        if self._fits(cleaned):
            return self._result(cleaned, report)

        # The old implementation windowed first, then tried to summarise the
        # already-discarded history. Summarise the complete clean history first.
        summarised = self._summarise_older(cleaned, preserve_system, report)
        if self._fits(summarised):
            return self._result(summarised, report)

        windowed = self._sliding_window(summarised, preserve_system)
        if self._fits(windowed):
            return self._result(windowed, report)
        return self._result(self._hard_truncate(windowed, report), report)

    def _result(self, messages: List[Dict[str, str]], report: CompressionReport) -> CompressionResult:
        report.after_tokens = self.counter.count_messages(messages)
        return CompressionResult(messages, report)

    def _drop_noise(self, msgs: Sequence[Dict[str, str]], report: CompressionReport) -> List[Dict[str, str]]:
        kept: List[Dict[str, str]] = []
        for message in msgs:
            content = (message.get("content") or "").strip()
            if not content:
                report.dropped += 1
                continue
            # Observations are evidence, not noise. Do not discard their payload.
            if (any(content.startswith(prefix) for prefix in NOISE_PREFIXES)
                    or any(hint in content for hint in NUDGE_HINTS)):
                report.dropped += 1
                continue
            kept.append(dict(message))
        return kept

    @staticmethod
    def _deduplicate(msgs: Sequence[Dict[str, str]], report: CompressionReport) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        seen = set()
        for message in msgs:
            key = (message.get("role", ""), (message.get("content") or "").strip())
            # Repeated user text can be an explicit instruction; repeated tool
            # and assistant outputs are retried data and can be collapsed.
            if message.get("role") != "user" and key in seen:
                report.deduplicated += 1
                continue
            seen.add(key)
            out.append(message)
        return out

    @staticmethod
    def _coalesce_observations(msgs: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for message in msgs:
            if (out and out[-1].get("role") == message.get("role")
                    and message.get("content", "").startswith("Observation:")
                    and out[-1].get("content", "").startswith("Observation:")):
                out[-1] = {**out[-1], "content": out[-1]["content"] + "\n" + message["content"]}
            else:
                out.append(message)
        return out

    @staticmethod
    def _split(msgs: Sequence[Dict[str, str]], preserve_system: bool):
        if not preserve_system:
            return [], list(msgs)
        return ([m for m in msgs if m.get("role") == "system"],
                [m for m in msgs if m.get("role") != "system"])

    def _sliding_window(self, msgs: Sequence[Dict[str, str]], preserve_system: bool) -> List[Dict[str, str]]:
        system, others = self._split(msgs, preserve_system)
        first_user = next((m for m in others if m.get("role") == "user"), None)
        recent = others[-self.keep_recent_turns:]
        kept: List[Dict[str, str]] = []
        if first_user is not None and first_user not in recent:
            kept.append(first_user)
        kept.extend(m for m in others if m.get("content", "").startswith(_SUMMARY_PREFIX)
                    and m not in recent)
        kept.extend(recent)
        return system + kept

    def _summarise_older(self, msgs: Sequence[Dict[str, str]], preserve_system: bool,
                         report: CompressionReport) -> List[Dict[str, str]]:
        system, others = self._split(msgs, preserve_system)
        if len(others) <= self.keep_recent_turns + 1:
            return list(msgs)
        first_user = next((m for m in others if m.get("role") == "user"), None)
        recent = others[-self.keep_recent_turns:]
        head = list(others[:-self.keep_recent_turns])
        if first_user is not None and first_user in head:
            head.remove(first_user)
        summary = self._make_summary(head)
        result: List[Dict[str, str]] = list(system)
        if first_user is not None and first_user not in recent:
            result.append(first_user)
        if summary:
            # A ledger contains user/tool text, not new policy. Promoting it to
            # system would also promote remote prompt injection during compaction.
            result.append({"role": "assistant", "content": _SUMMARY_PREFIX + "\n" + summary})
            report.summarised += len(head)
            report.retained_facts += len([line for line in summary.splitlines() if line.startswith("- ")])
        result.extend(recent)
        return result

    def _make_summary(self, head: Sequence[Dict[str, str]]) -> str:
        model_summary = ""
        if self.summariser is not None:
            try:
                model_summary = (self.summariser(list(head)) or "").strip()
            except Exception:
                model_summary = ""
        facts = [(self._is_critical_message(message), self._fact_line(message)) for message in head]
        parts: List[str] = []
        if model_summary:
            parts.append("ملخص محلي مقدم: " + self._clip(model_summary, 420))
        # Consent decisions and tool evidence are the first facts preserved
        # when an unusually tiny context budget forces further trimming.
        parts.extend(fact for _, fact in sorted(facts, key=lambda item: not item[0]) if fact)
        return "\n".join(parts[:40])

    @staticmethod
    def _is_critical_message(message: Dict[str, str]) -> bool:
        content = (message.get("content") or "").lower()
        return message.get("role") == "tool" or any(word in content for word in _CRITICAL_WORDS)

    def _fact_line(self, message: Dict[str, str]) -> str:
        content = re.sub(r"\s+", " ", (message.get("content") or "").strip())
        if not content:
            return ""
        lower = content.lower()
        limit = 420 if any(word in lower for word in _CRITICAL_WORDS) else 220
        return f"- [{message.get('role', 'unknown')}] {self._clip(content, limit)}"

    @staticmethod
    def _clip(content: str, limit: int) -> str:
        if len(content) <= limit:
            return content
        head = max(1, int(limit * 0.72))
        tail = max(1, int(limit * 0.20))
        return content[:head].rstrip() + " … " + content[-tail:].lstrip()

    def _hard_truncate(self, msgs: Sequence[Dict[str, str]], report: CompressionReport) -> List[Dict[str, str]]:
        out = list(msgs)
        while not self._fits(out):
            index = self._largest_mutable_index(out)
            if index < 0:
                break  # Only immutable system policy remains; never corrupt it.
            content = out[index].get("content", "")
            if len(content) <= 160:
                out.pop(index)
                report.dropped += 1
                continue
            if content.startswith(_SUMMARY_PREFIX):
                shortened = self._shrink_summary(content)
                if shortened != content:
                    out[index] = {**out[index], "content": shortened}
                    report.truncated += 1
                    continue
            out[index] = {**out[index], "content": self._clip(content, max(120, int(len(content) * 0.58)))
                          + "\n…[اختصار سياق آمن]…"}
            report.truncated += 1
        return out

    @staticmethod
    def _shrink_summary(content: str) -> str:
        """Remove the least valuable ledger line, never a fact by position."""
        lines = content.splitlines()
        if len(lines) <= 2:
            return content
        header, facts = lines[0], lines[1:]
        for index in range(len(facts) - 1, -1, -1):
            lower = facts[index].lower()
            if not any(word in lower for word in _CRITICAL_WORDS) and "[tool]" not in lower:
                return "\n".join([header] + facts[:index] + facts[index + 1:])
        # Only evidence/consent lines remain. At this point preserve their
        # beginnings and endings rather than deleting an arbitrary condition.
        return "\n".join([header] + [ContextCompressor._clip(line, 180) for line in facts])

    def _largest_mutable_index(self, msgs: Sequence[Dict[str, str]]) -> int:
        candidates = []
        for index, message in enumerate(msgs):
            content = message.get("content", "")
            # Original system prompts are policies. Only the generated ledger
            # may shrink further.
            if message.get("role") == "system" and not content.startswith(_SUMMARY_PREFIX):
                continue
            # Keep the fact ledger until ordinary, replaceable execution
            # chatter has been reduced. A user instruction is never a
            # hard-truncation candidate.
            if message.get("role") == "user":
                continue
            tier = 2 if content.startswith(_SUMMARY_PREFIX) else 1
            candidates.append((tier, self.counter.count(content), index))
        if not candidates:
            return -1
        lowest_tier = min(item[0] for item in candidates)
        return max(item for item in candidates if item[0] == lowest_tier)[2]

    def _fits(self, msgs: Sequence[Dict[str, str]]) -> bool:
        return self.counter.count_messages(msgs) <= self.max_tokens


__all__ = ["ContextCompressor", "CompressionReport", "CompressionResult"]
