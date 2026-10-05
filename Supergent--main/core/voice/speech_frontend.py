"""Language-aware preparation for text that is sent to a speech model.

The displayed assistant response must never be rewritten.  This module creates
an internal, speakable form instead: it expands the small set of technical
tokens WISE commonly says, turns numbers into words, and gives an optional
Arabic diacritizer a clean Arabic-only span to process.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable


_ARABIC_LETTER = re.compile(r"[\u0621-\u064A\u066E-\u066F\u0671-\u06D3\u06FA-\u06FC\u06FF]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")
_ARABIC_RUN = re.compile(r"[\u0621-\u064A\u066E-\u066F\u0671-\u06D3\u06FA-\u06FC\u06FF\u064B-\u065F\u0670\u0640\s]+")
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")
_NUMBER = re.compile(r"(?<![A-Za-z\u0621-\u06FF])(-?\d+(?:[.,]\d+)?)(?![A-Za-z\u0621-\u06FF])")

_ARABIC_TECHNICAL = {
    "API": "إيه بي آي",
    "AI": "إيه آي",
    "CPU": "سي بي يو",
    "GPU": "جي بي يو",
    "RAM": "رام",
    "VRAM": "في رام",
    "JSON": "جيسون",
    "URL": "يو آر إل",
    "HTTP": "إتش تي تي بي",
    "HTTPS": "إتش تي تي بي إس",
    "MCP": "إم سي بي",
    "LLM": "إل إل إم",
    "RVC": "آر في سي",
    "TTS": "تي تي إس",
    "STT": "إس تي تي",
    "WISE": "وايز",
    "OPENROUTER": "أوبن راوتر",
    "WINDOWS": "ويندوز",
}

_ENGLISH_ACRONYMS = {
    "API", "AI", "CPU", "GPU", "RAM", "VRAM", "JSON", "URL", "HTTP", "HTTPS",
    "MCP", "LLM", "RVC", "TTS", "STT",
}


def detect_speech_language(text: str, requested_language: str = "AR") -> str:
    """Use the script actually present in a response, not a stale UI setting."""
    if _ARABIC_LETTER.search(text):
        return "AR"
    if _LATIN_LETTER.search(text):
        return "EN"
    return requested_language.upper() if requested_language.upper() in {"AR", "EN"} else "AR"


def _number_words(value: str, language: str) -> str:
    """Spell numeric input, with a safe per-digit fallback if a package fails."""
    decimal = "." if "." in value else ("," if "," in value else "")
    try:
        from num2words import num2words

        if decimal:
            whole, fraction = value.replace(",", ".").split(".", 1)
            point = "point" if language == "EN" else "فاصلة"
            fraction_words = " ".join(num2words(int(char), lang="en" if language == "EN" else "ar") for char in fraction)
            return f"{num2words(int(whole), lang='en' if language == 'EN' else 'ar')} {point} {fraction_words}"
        return str(num2words(int(value), lang="en" if language == "EN" else "ar"))
    except Exception:
        digits = "zero one two three four five six seven eight nine".split() if language == "EN" else (
            "صفر واحد اثنان ثلاثة أربعة خمسة ستة سبعة ثمانية تسعة".split()
        )
        return " ".join(digits[int(char)] for char in value if char.isdigit())


def _expand_numbers(text: str, language: str) -> str:
    return _NUMBER.sub(lambda match: _number_words(match.group(1), language), text)


def _replace_tokens(text: str, replacements: dict[str, str]) -> str:
    for token, spoken in replacements.items():
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", spoken, text, flags=re.IGNORECASE)
    return text


def _prepare_english(text: str) -> str:
    text = _replace_tokens(text, {token: " ".join(token) for token in _ENGLISH_ACRONYMS})
    text = re.sub(r"(?<![A-Za-z])WISE(?![A-Za-z])", "Wise", text, flags=re.IGNORECASE)
    return _expand_numbers(text, "EN")


def _prepare_arabic(text: str, diacritize: Callable[[str], str] | None) -> str:
    text = _replace_tokens(text, _ARABIC_TECHNICAL)
    text = _expand_numbers(text, "AR")
    text = text.replace("?", "؟").replace(";", "؛")
    if diacritize:
        def apply(match: re.Match[str]) -> str:
            value = match.group(0)
            leading = value[: len(value) - len(value.lstrip())]
            trailing = value[len(value.rstrip()):]
            # CATT predicts its own diacritics. Feeding a partially vocalized
            # conversational string to it can duplicate or corrupt marks.
            bare = _ARABIC_DIACRITICS.sub("", value.strip())
            # A statistical diacritizer needs contextual words. Do not let it
            # guess a greeting, a name, or a one-word fragment between commas.
            # IndexTTS already handles these common short forms well.
            letters = _ARABIC_LETTER.findall(bare)
            if len(letters) < 18:
                return value
            spoken = diacritize(bare) if _ARABIC_LETTER.search(bare) else bare
            return f"{leading}{spoken}{trailing}"

        text = _ARABIC_RUN.sub(apply, text)
    # Preserve a deliberate product pronunciation even if a generic tashkeel
    # model assigns a different reading to this invented name.
    text = re.sub(r"و(?:[\u064B-\u065F])*ا(?:[\u064B-\u065F])*ي(?:[\u064B-\u065F])*ز", "وَايْز", text)
    return text


def prepare_spoken_text(
    text: str,
    requested_language: str = "AR",
    *,
    diacritize_arabic: Callable[[str], str] | None = None,
) -> tuple[str, str]:
    """Return ``(spoken_text, language)`` without altering user-visible text."""
    text = unicodedata.normalize("NFKC", text or "").strip()
    language = detect_speech_language(text, requested_language)
    if language == "AR":
        return _prepare_arabic(text, diacritize_arabic), language
    if language == "EN":
        return _prepare_english(text), language
    return text, language
