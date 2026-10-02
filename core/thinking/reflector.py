"""Self-critique between iterations.

The :class:`Reflector` is invoked AFTER an Observation lands and BEFORE
the next Thought. It returns a :class:`Reflection` with:

- ``advice``: short text the engine appends to the next system reminder.
- ``decision``: ``continue`` | ``change_tactic`` | ``stop_with_partial``
  | ``ask_user``.
- ``confidence`` ∈ [0, 1].

Two reflection paths:

1. **Heuristic** (always runs, free): looks at the scratchpad and the
   most recent observation. Flags loops, repeated errors, contradictory
   facts, and successful task completion signals. This is enough on
   its own for ~80% of runs.
2. **LLM critique** (optional): if a model is provided AND the
   heuristic is uncertain, we ask the model for a short critique.
   We never let the LLM critique drive >1 extra call per iteration to
   stay within the token budget.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Optional

from .scratchpad import Scratchpad, ThoughtStep


@dataclass
class Reflection:
    decision: str = "continue"   # continue | change_tactic | stop_with_partial | ask_user
    advice: str = ""
    confidence: float = 0.5
    source: str = "heuristic"    # heuristic | llm | merged


# --------------------------------------------------------------------------
# Heuristic indicators
# --------------------------------------------------------------------------
_SUCCESS_TOKENS = (
    "Final Answer:", "تم بنجاح", "completed", "اكتملت", "done.", "✅",
)
_HARD_ERROR_TOKENS = (
    "❌ خطأ في النموذج", "rate limit", "context length", "maximum context",
    "401 Unauthorized", "AuthenticationError", "ConnectionRefused",
    "name not resolved", "no internet",
)
_AMBIGUOUS_TOKENS = (
    "متعدد الاحتمالات", "ambiguous", "غير واضح", "could mean", "could refer",
)


# --------------------------------------------------------------------------
class Reflector:
    """Heuristic + optional-LLM self-critique."""

    def __init__(self, model: Any = None, *, enable_llm: bool = True,
                 token_counter: Any = None, max_critique_tokens: int = 200):
        self.model = model
        self.enable_llm = enable_llm and model is not None
        self.counter = token_counter
        self.max_critique_tokens = max_critique_tokens
        self._llm_calls = 0  # cap inside one engine run via .reset_budget()

    def reset_budget(self) -> None:
        self._llm_calls = 0

    # ------------------------------------------------------------------
    def reflect(self, pad: Scratchpad, *, max_llm_calls: int = 2) -> Reflection:
        h = self._heuristic(pad)
        if h.confidence >= 0.75 or not self.enable_llm:
            return h
        if self._llm_calls >= max_llm_calls:
            return h
        try:
            llm = self._llm_critique(pad)
        except Exception:
            llm = None
        if llm is None:
            return h
        self._llm_calls += 1
        return self._merge(h, llm)

    # ------------------------------------------------------------------
    # Heuristic
    # ------------------------------------------------------------------
    def _heuristic(self, pad: Scratchpad) -> Reflection:
        if not pad.steps:
            return Reflection(decision="continue", confidence=0.4)

        last = pad.steps[-1]
        obs = (last.observation or "").strip()

        # 1) Hard infra failure → stop with partial.
        for tok in _HARD_ERROR_TOKENS:
            if tok in obs:
                return Reflection(
                    decision="stop_with_partial",
                    advice=f"خطأ بنية تحتية: {tok}. أنهِ بأفضل إجابة ممكنة.",
                    confidence=0.95,
                    source="heuristic",
                )

        # 2) Repeated identical action with no progress → change tactic.
        if last.action and pad.repeated_action(last.action, last.args, within=3):
            return Reflection(
                decision="change_tactic",
                advice=(
                    f"كرّرتَ نفس الأداة {last.action} بنفس المعطيات. "
                    "غيّر المعطيات أو جرّب أداة مختلفة، أو أنهِ بـ Final Answer."
                ),
                confidence=0.85,
                source="heuristic",
            )

        # 3) Multiple consecutive failures → change tactic / stop.
        consec = pad.consecutive_failures()
        if consec >= 3:
            return Reflection(
                decision="stop_with_partial",
                advice=(
                    f"{consec} محاولات فاشلة متتالية. أنهِ بـ Final Answer "
                    "موضحاً ما توصّلت إليه وما تعذّر."
                ),
                confidence=0.9,
                source="heuristic",
            )
        if consec == 2:
            return Reflection(
                decision="change_tactic",
                advice=(
                    "محاولتان فاشلتان متتاليتان. غيّر المنهج: قسّم المهمة، "
                    "أو ابحث عن أداة بديلة قبل المحاولة الثالثة."
                ),
                confidence=0.78,
                source="heuristic",
            )

        # 4) Strong success signal → continue toward Final Answer.
        if any(tok in obs for tok in _SUCCESS_TOKENS):
            return Reflection(
                decision="continue",
                advice="نتيجة واضحة. لخّص في Final Answer.",
                confidence=0.85,
                source="heuristic",
            )

        # 5) Ambiguous → ask the model to disambiguate via plan, not user.
        if any(tok in obs for tok in _AMBIGUOUS_TOKENS):
            return Reflection(
                decision="change_tactic",
                advice="النتيجة غامضة. اختر أحد الفروع وقدّم سبب الاختيار.",
                confidence=0.7,
                source="heuristic",
            )

        # 6) Default: continue with low confidence.
        return Reflection(decision="continue", confidence=0.5)

    # ------------------------------------------------------------------
    # LLM critique
    # ------------------------------------------------------------------
    def _llm_critique(self, pad: Scratchpad) -> Optional[Reflection]:
        if self.model is None:
            return None
        last = pad.steps[-1] if pad.steps else None
        if not last:
            return None

        snippet = pad.render(max_steps=3, include_facts=False)
        prompt = (
            "أنت مراقب جودة لوكيل ذكي. اقرأ آخر الخطوات وأجب بـJSON واحد فقط:\n"
            '{"decision":"continue|change_tactic|stop_with_partial",'
            '"advice":"<≤25 كلمة>","confidence":0..1}\n\n'
            f"المهمة: {pad.task}\n\n{snippet}"
        )
        try:
            response = self.model([
                {"role": "system", "content": "أعد JSON واحد فقط دون أي شرح."},
                {"role": "user", "content": prompt},
            ])
        except Exception:
            return None
        m = re.search(r"\{[\s\S]*\}", response or "")
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        decision = str(data.get("decision") or "continue")
        if decision not in {"continue", "change_tactic",
                            "stop_with_partial", "ask_user"}:
            decision = "continue"
        advice = str(data.get("advice") or "")[:200]
        try:
            confidence = float(data.get("confidence") or 0.5)
        except (TypeError, ValueError):
            confidence = 0.5
        return Reflection(
            decision=decision, advice=advice,
            confidence=max(0.0, min(1.0, confidence)),
            source="llm",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _merge(h: Reflection, l: Reflection) -> Reflection:
        # Prefer the more conservative decision.
        order = {"continue": 0, "change_tactic": 1,
                 "stop_with_partial": 2, "ask_user": 3}
        chosen = h if order.get(h.decision, 0) >= order.get(l.decision, 0) else l
        return Reflection(
            decision=chosen.decision,
            advice=(h.advice + (" / " + l.advice if l.advice else "")).strip(),
            confidence=max(h.confidence, l.confidence),
            source="merged",
        )


__all__ = ["Reflector", "Reflection"]
