"""Bounded MCP document observations and conservative scoped-limit checks.

Only explicit URL/text records become evidence. An MCP observation is not a
native HTTP fetch, source completeness guarantee, or instruction authority.
Scope checks cover observed table associations, not general semantic truth.
"""
from __future__ import annotations
import html
import ipaddress
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit


def _document_url(value):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r"[\s<>\x00-\x1f]", value): return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password: return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535: return None
        host = parsed.hostname.lower().rstrip(".")
        try:
            if not ipaddress.ip_address(host).is_global: return None
        except ValueError:
            if "." not in host or host.endswith((".localhost", ".local", ".internal")) or host == "localhost": return None
        return parsed._replace(fragment="").geturl().rstrip("/")
    except ValueError:
        return None


def extract_mcp_documents(tool, result):
    """Read known document envelopes only; never crawl arbitrary JSON/URLs."""
    if not isinstance(tool, str) or not tool.startswith("mcp.") or not isinstance(result, dict) or result.get("success") is not True: return []
    payload = result.get("output")
    if isinstance(payload, str):
        if len(payload) > 200000: return []
        try: payload = json.loads(payload)
        except ValueError: return []
    if not isinstance(payload, dict) or payload.get("isError"): return []
    records = []
    structured = payload.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("results"), list):
        records.extend(structured["results"][:24])
    content = payload.get("content")
    if isinstance(content, list):
        for block in content[:24]:
            if not isinstance(block, dict) or block.get("type") != "text": continue
            text = block.get("text")
            if not isinstance(text, str) or len(text) > 200000: continue
            # This exact envelope contains Markdown, including bare '< 1 hour',
            # so it is not XML. Match associated fields without DTD/entity parsing.
            pattern = r"<result>\s*<url>([^<>]+)</url>\s*<title>([^<>]*)</title>\s*<text>(.*?)</text>\s*</result>"
            for match in list(re.finditer(pattern, text, re.S))[:24]:
                records.append({"url": html.unescape(match[1]), "title": html.unescape(match[2]), "text": match[3]})
    evidence, seen, chars = [], set(), 0
    retrieved_at = datetime.now(timezone.utc).isoformat()
    for record in records[:48]:
        if not isinstance(record, dict): continue
        url, text = _document_url(record.get("url")), record.get("text")
        if not url or not isinstance(text, str) or not text.strip(): continue
        text = text.strip()
        key = (url, text)
        if key in seen: continue
        seen.add(key)
        if len(evidence) >= 24 or chars >= 120000: break
        excerpt = text[:min(12000, 120000 - chars)]
        chars += len(excerpt)
        evidence.append({"url": url, "resolved_url": url, "title": str(record.get("title", ""))[:300],
            "source": urlsplit(url).hostname, "snippet": excerpt[:1600], "page_text": excerpt,
            "evidence_kind": "mcp_document_excerpt", "retrieved_at": retrieved_at,
            "excerpt_truncated": len(excerpt) != len(text), "mcp_tool": tool,
            "mcp_call_id": str(result.get("call_id", ""))[:200],
            "observation_scope": "Actual MCP document record; destination not independently fetched by native HTTP"})
    return evidence


def requires_scoped_limit(goal):
    return bool(isinstance(goal, str) and re.search(r"\blimits?\b|حد(?:ود)?", goal, re.I)
                and re.search(r"\b(?:plan|tier|context|invocation)\b|خطة|سياق|استدعاء", goal, re.I))


_NUMBER_UNIT = re.compile(r"(?<![\w.])([0-9][0-9,_]*(?:\.[0-9]+)?)\s*"
    r"(milliseconds?|msecs?|ms|seconds?|secs?|s|minutes?|mins?|hours?|hrs?|"
    r"bytes?|kb|mb|gb|requests?|invocations?|calls?)(?![\w])", re.I)


def _quantities(text):
    factors = {"ms": ("time", ".001"), "s": ("time", "1"), "min": ("time", "60"), "hr": ("time", "3600")}
    quantities = set()
    for value, unit in _NUMBER_UNIT.findall(text):
        unit = unit.lower()
        if unit.startswith(("millisecond", "msec")): unit = "ms"
        elif unit.startswith(("second", "sec")): unit = "s"
        elif unit.startswith(("minute", "min")): unit = "min"
        elif unit.startswith(("hour", "hr")): unit = "hr"
        dimension, factor = factors.get(unit, (unit.rstrip("s"), "1"))
        quantities.add((dimension, Decimal(value.replace(",", "").replace("_", "")) * Decimal(factor)))
    return quantities


def _plain(text):
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text.replace("*", "").replace("`", "")).strip().casefold()


def _scoped_table_rows(text):
    if not isinstance(text, str): return
    lines = text.splitlines()
    def cells(line): return [cell.strip() for cell in line.strip().strip("|").split("|")]
    for index in range(len(lines) - 2):
        if "|" not in lines[index] or "|" not in lines[index + 1]: continue
        headers, separator = cells(lines[index]), cells(lines[index + 1])
        if len(headers) < 3 or len(headers) != len(separator) or not all(re.fullmatch(r"[:\-\s]+", cell) for cell in separator): continue
        for line in lines[index + 2:]:
            if "|" not in line: break
            row = cells(line)
            if len(row) != len(headers): break
            context = re.search(r"\bper\s+(.+)", _plain(row[0]))
            if not context: continue
            for label, value in zip(headers[1:], row[1:]):
                default = re.search(r"\bdefault\s*:\s*([^)]*)", value, re.I)
                # Conditional values in one cell are alternatives, not a bag of
                # numbers interchangeable across their interval qualifiers.
                # Only this explicit number/parenthesis shape is interpreted.
                variants = list(re.finditer(r"([^()]+)\(([^()]*)\)", value))
                conditional = [match for match in variants if re.search(r"(?:<=|>=|<|>)\s*\d", match[2])]
                if conditional:
                    if len(conditional) != len(variants) or value[variants[-1].end():].strip():
                        continue  # Mixed or ambiguous scope remains unverified.
                    values = [(match[1], [_plain(match[2])]) for match in conditional]
                else:
                    values = [(value, [])]
                for literal, conditions in values:
                    quantities = _quantities(literal)
                    if not quantities: continue
                    yield {"plan": _plain(label), "context": context[1].strip(), "quantities": quantities,
                           "default_quantities": _quantities(default[1]) if default else set(),
                           "conditions": conditions, "measure": row[0], "observed_value": literal.strip()}


def scoped_limit_observations(evidence, goal):
    """Small exact association index, not inferred facts or a model summary.

    Large MCP search responses can hide a table row behind range previews.
    Give the agent the same bounded rows the validator can actually inspect,
    including canonical labels and citation, instead of making it reconstruct
    plan/value relationships from thousands of unrelated document characters.
    """
    if not requires_scoped_limit(goal): return []
    records, seen, chars = [], set(), 0
    for item in evidence:
        if not isinstance(item, dict) or item.get("evidence_kind") not in {
                "page_excerpt", "mcp_document_excerpt", "source_api_data"}: continue
        url = _document_url(item.get("resolved_url") or item.get("url"))
        if not url: continue
        for row in _scoped_table_rows(item.get("page_text", "")):
            if any(len(row[key]) > 500 for key in ("plan", "context", "measure", "observed_value")): continue
            if len(row["conditions"]) > 4 or any(len(condition) > 160 for condition in row["conditions"]): continue
            record = {key: row[key] for key in ("plan", "context", "measure", "observed_value", "conditions")}
            record["source_url"] = url
            signature = json.dumps(record, sort_keys=True)
            if signature in seen: continue
            seen.add(signature)
            # Bound the whole JSON packet too, not only each record. Never
            # truncate a value/URL into an apparently complete association.
            size = len(json.dumps(record, ensure_ascii=False)) + 2
            if chars + size > 4000: continue
            chars += size; records.append(record)
            if len(records) == 24: return records
    return records


def single_limit_answer_candidates(evidence, goal):
    """Verbatim source-row choices for an explicitly single-limit report.

    Not a general answer synthesizer or a semantic completeness certificate.
    The caller additionally checks the task is research-only. Names/values
    come solely from observed rows; no product limits or aliases are guessed.
    """
    if not requires_scoped_limit(goal): return []
    request = re.search(r"\b(?:one|single)\s+((?:[a-z][a-z0-9_-]*\s+){0,4})limit\b", goal, re.I)
    if not request or set(request[1].casefold().split()) & {
            "of", "way", "method", "technique", "among", "to", "for", "their"}: return []
    rows = scoped_limit_observations(evidence, goal)
    # Prefer rows whose measured property appears in the exact request. Keep
    # all ties; the model selects one, not the first remembered vendor number.
    words = set(re.findall(r"\b[a-z][a-z0-9_]+\b", goal.casefold()))
    excluded = {"per", "limit", "limits", "request", "requests", "documented", "context"}
    scores = [len((set(re.findall(r"\b[a-z][a-z0-9_]+\b", row["measure"].casefold())) - excluded) & words)
              for row in rows]
    best = max(scores, default=0)
    if not best: return []  # No observed measured property was associated with this topic.
    answers = []
    for row, score in zip(rows, scores):
        if score != best: continue
        conditions = ("; " + "; ".join(row["conditions"])) if row["conditions"] else ""
        answer = (f"Observed source row: {row['plan']} — {row['measure']}: "
                  f"{row['observed_value']}{conditions}. [Source]({row['source_url']})")
        if answer not in answers: answers.append(answer)
    return answers


def validate_scoped_limit_answer(answer, evidence, goal):
    """Retain actual table plan/context/value pairings for an explicit request.

Other prose formats remain unverified rather than inventing source scope.
"""
    if not requires_scoped_limit(goal): return []
    by_url = {}
    for item in evidence:
        if not isinstance(item, dict): continue
        if item.get("evidence_kind") not in {"page_excerpt", "mcp_document_excerpt", "source_api_data"}: continue
        for key in ("url", "resolved_url"):
            url = _document_url(item.get(key))
            if url and item not in by_url.setdefault(url, []): by_url[url].append(item)
    from core.web_research import _answer_urls
    errors, checked = [], 0
    for paragraph in re.split(r"\n\s*\n|\n(?=\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s))", answer):
        prose = re.sub(r"https?://[^\s<>\]\)]+", "", paragraph)
        # The threshold defining an interval is not itself the execution limit.
        limit_prose = re.sub(r"(?:<=|>=|<|>)\s*\d+\s*\w+(?:\s+interval)?", "", prose)
        quantities = _quantities(limit_prose)
        if not quantities: continue
        checked += 1
        rows = [row for url in _answer_urls(paragraph) if url in by_url
                for item in by_url[url] for row in _scoped_table_rows(item.get("page_text", ""))]
        plain = _plain(prose)
        matched = [row for row in rows if row["plan"] and row["plan"] in plain and row["context"] in plain]
        if not matched:
            errors.append("A documented numeric limit is missing its adjacent observed plan and invocation context")
        elif not any(quantities <= row["quantities"]
                     and (not quantities & row["default_quantities"] or "default" in plain)
                     and ("default" not in plain or not row["default_quantities"]
                          or bool(quantities & row["default_quantities"]))
                     and all(condition in plain for condition in row["conditions"]) for row in matched):
            errors.append("The numeric limit or its qualifier is not supported for the cited plan and invocation context")
    if not checked: errors.append("The requested documented limit with plan and invocation context was not verified")
    return list(dict.fromkeys(errors))
