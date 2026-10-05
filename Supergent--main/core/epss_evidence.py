"""Narrow EPSS interpretation contract, not a general semantic truth checker."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit


def interpretation_contract():
    """Static adapter semantics; never mislabel this as a live definition fetch.

    FIRST's primary definition was reviewed on 2026-10-04. Raw API records,
    their score dates and the raw HTTP digest remain separate and unmodified.
    """
    return {
        "kind": "adapter_contract_not_live_definition_fetch",
        "definition_reference": "https://www.first.org/epss/",
        "definition_reviewed_on": "2026-10-04",
        "forecast_window_days": 30,
        "forecast_event": "A published CVE is exploited in the wild in the next 30 days",
        "score_unit": "Model-estimated probability from 0 to 1; not the percentile",
        "percentile_unit": "Relative ranking of the score, not exploitation probability",
        "limitations": "Not a guarantee, proof of compromise, per-device attack probability, or severity score. "
                       "Separate known exploitation (for example KEV) from this forecast. "
                       "Preserve the observed score and score date; qualify probability interpretations "
                       "with the 30-day in-the-wild model estimate and uncertainty.",
    }


def validate_epss_interpretation(answer, evidence):
    """Catch the observed unqualified-probability failure in EN/AR prose.

    Activated only by actual FIRST API evidence, not a keyword in the user's
    request or an untrusted third-party page. Plain score/date tables remain
    allowed. This bounded lexical check cannot prove all semantic correctness.
    """
    if not isinstance(answer, str):
        return []
    observed = False
    for item in evidence:
        if item.get("evidence_kind") != "source_api_data":
            continue
        parsed = urlsplit(str(item.get("url", "")))
        if (parsed.scheme != "https" or parsed.hostname != "api.first.org"
                or parsed.username or parsed.password or parsed.path.rstrip("/") != "/data/v1/epss"):
            continue
        try:
            payload = json.loads(item.get("page_text", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("resource_id") == "first-epss" and payload.get("records"):
            observed = True
            break
    if not observed:
        return []
    errors = []
    section = False
    blocks = []
    for paragraph in re.split(r"\n\s*\n|\n(?=\s*#{1,6}\s)", answer):
        if re.search(r"(?m)^\s*#{1,6}\s", paragraph):
            section = (bool(re.search(r"\b(?:EPSS|FIRST)\b", paragraph, re.I))
                       and not re.search(r"\b(?:CISA|KEV)\b", paragraph, re.I))
        if not section and not re.search(r"\bEPSS\b", paragraph, re.I):
            continue
        if re.search(r"\b(?:CISA|KEV)\b", paragraph, re.I) and not re.search(r"\bEPSS\b", paragraph, re.I):
            continue
        # Headings describe a section, not an affirmative probability claim.
        paragraph = re.sub(r"(?m)^\s*#{1,6}\s[^\n]*", "", paragraph).strip()
        if paragraph:
            blocks.append(paragraph)
    def qualified(paragraph):
        window = re.search(r"\b(?:(?:next|within|over|following)\s+(?:the\s+)?30\s+days|30[- ]day(?:s)?(?:\s+(?:forecast|window))?)\b"
                           r"|(?:30|٣٠)\s*(?:يوم|يوماً|يوما)", paragraph, re.I)
        wild = re.search(r"\bin\s+the\s+wild\b|\breal[- ]world\s+exploitation\b|في\s+(?:الواقع|العالم\s+الحقيقي|البرية)", paragraph, re.I)
        estimate = re.search(r"\b(?:model[- ]estimated|model\s+(?:estimate|forecast)|estimate\w*|predict\w*|forecast\w*)\b"
                             r"|تقدير|توقع|متوقع", paragraph, re.I)
        uncertainty = re.search(r"\b(?:not\s+(?:a\s+)?(?:guarantee|certainty|proof)|uncertain\w*|no\s+guarantee)\b"
                                r"|ليس\s+(?:ضمان|مضمون|مؤكد|دليل)|لا\s+يضمن|غير\s+مؤكد", paragraph, re.I)
        return bool(window and wild and estimate and uncertainty)
    # Explicit EPSS notes may qualify a restatement for the same single CVE.
    # Never assemble unrelated facts, or pool notes across distinct subjects.
    single_subject = len({cve.upper() for cve in re.findall(r"\bCVE-\d{4}-\d{4,}\b", answer, re.I)}) <= 1
    shared_definition = single_subject and any(
        re.search(r"\bEPSS\b", block, re.I) and qualified(block) for block in blocks)
    for paragraph in blocks:
        # Warnings are not affirmative predictions. Strip only narrow warning
        # sentences, not arbitrary paragraphs containing the word 'not'.
        sentences = re.split(r"(?<=[.!?؛])\s+|\n", paragraph)
        statements = []
        for sentence in sentences:
            warning = re.search(
                r"^\s*(?:EPSS\s+(?:is|scores?\s+(?:is|are))\s+not\s+(?:a\s+)?(?:guarantee|proof|certainty)"
                r"|do\s+not\s+(?:describe|interpret|treat|call)|لا\s+(?:تصف|تعتبر|تفسر)|EPSS\s+ليس\s+(?:ضمان|دليل))", sentence, re.I)
            if not warning:
                statements.append(sentence)
        paragraph = " ".join(statements)
        explanation = re.search(
            r"\b(?:indicat\w*|suggest\w*|predict\w*|estimat\w*|means|probability|likelihood|chance|potential|certainty|certain)\b"
            r"|يشير|تقدير|يتوقع|متوقع|توقع|احتمال\s+(?:الاستغلال|استغلال|الهجوم)|مضمون|مؤكد", paragraph, re.I)
        if not explanation:
            continue
        if not (qualified(paragraph) or shared_definition):
            errors.append("EPSS probability interpretation must be qualified as an uncertain model estimate "
                          "of CVE exploitation in the wild in the next 30 days, not a guarantee or device-compromise probability")
        for sentence in statements:
            certainty = re.search(r"\b(?:near[- ]certain|guaranteed|will\s+definitely\s+be\s+(?:exploited|compromised))\b"
                                  r"|شبه\s+مؤكد|سيتم\s+(?:اختراق|استغلال).*حتماً", sentence, re.I)
            # 'Probability that ... will be exploited' describes a forecast,
            # but an unqualified affirmative 'will' is a certainty claim.
            affirmative_will = (re.search(r"\bwill\s+be\s+(?:exploited|compromised)\b", sentence, re.I)
                                and not re.search(r"\b(?:probability|chance|likelihood|may|might|could)\b", sentence, re.I))
            negated = re.search(r"\b(?:not\s+(?:guaranteed|near[- ]certain)|not\s+(?:a\s+)?(?:guarantee|certainty))\b"
                                r"|ليس\s+(?:مؤكد|مضمون|ضمان)|غير\s+مؤكد", sentence, re.I)
            if (certainty or affirmative_will) and not negated:
                errors.append("Do not turn an EPSS estimate into certain or near-certain exploitation/compromise")
            historical = re.search(r"\b(?:happened|occurred|exploited)\s+(?:yesterday|last\s+\w+)|\b(?:past|last)\s+\d+\s+days\b", sentence, re.I)
            windows = re.findall(r"\b(?:next|within|over|following)\s+(?:the\s+)?(\d+)\s+days\b|\b(\d+)[- ]day\s+(?:forecast|window)\b", sentence, re.I)
            wrong_window = any(int(a or b) != 30 for a, b in windows)
            if historical or wrong_window:
                errors.append("EPSS forecasts the next 30 days, not historical events or another forecast horizon")
            host_forecast = re.search(r"\b(?:chance|probability)\b.{0,80}\b(?:your|this|my)\s+(?:laptop|device|host|computer)\b"
                                      r"|احتمال.{0,60}(?:جهازك|اختراق\s+هذا\s+الجهاز)", sentence, re.I)
            if host_forecast and not re.search(r"\b(?:not|never|no)\b|ليس|لا\s", sentence, re.I):
                errors.append("EPSS does not estimate compromise probability for an individual device")
    return list(dict.fromkeys(errors))
