"""Actual table alternatives must not be flattened into interchangeable limits."""
import pytest

from core.mcp_document_evidence import extract_mcp_documents, validate_scoped_limit_answer

URL = "https://docs.publisher.example/limits"
GOAL = "Report one documented limit with its plan and invocation context."
TABLE = """| Limit | Starter | Professional |
| --- | --- | --- |
| CPU time per HTTP request | 10 ms | 5 min (default: 30 seconds) |
| CPU time per scheduled trigger | 10 ms | 30 seconds (< 1 hour interval) 15 min (>= 1 hour interval) |
"""


def records():
    return extract_mcp_documents("mcp.docs.search", {"success": True, "output": {
        "structuredContent": {"results": [{"url": URL, "text": TABLE}]}}})


@pytest.mark.parametrize("claim", [
    "Professional CPU time per scheduled trigger is 30 seconds (< 1 hour interval).",
    "Professional CPU time per scheduled trigger is 15 minutes (>= 1 hour interval).",
    "Starter CPU time per scheduled trigger is 10 milliseconds.",
    "Professional CPU time per HTTP request is 5 minutes, with a default of 30 seconds.",
])
def test_literal_alternative_or_default_keeps_its_actual_scope(claim):
    assert not validate_scoped_limit_answer(f"{claim} [Source]({URL})", records(), GOAL)


@pytest.mark.parametrize("claim", [
    "Professional CPU time per scheduled trigger is 15 minutes (< 1 hour interval).",
    "Professional CPU time per scheduled trigger is 30 seconds (>= 1 hour interval).",
    "Professional CPU time per scheduled trigger is 15 minutes.",
    "Professional CPU time per HTTP request has a default of 5 minutes.",
])
def test_wrong_alternative_or_default_is_not_supported(claim):
    assert validate_scoped_limit_answer(f"{claim} [Source]({URL})", records(), GOAL)


def test_non_limit_goal_does_not_require_a_numeric_scope():
    assert not validate_scoped_limit_answer("Documentation titles only.", records(), "List documentation titles")


def test_later_excerpt_same_source_does_not_erase_observed_scope():
    evidence = records()
    evidence.append({**evidence[0], "page_text": "Other actual excerpt from the same document."})
    claim = f"Professional CPU time per HTTP request is 5 minutes. [Source]({URL})"
    assert not validate_scoped_limit_answer(claim, evidence, GOAL)


def test_malformed_non_text_evidence_cannot_support_a_limit():
    evidence = [None, {"url": URL, "evidence_kind": "page_excerpt", "page_text": {"unexpected": "shape"}}]
    claim = f"Professional CPU time per HTTP request is 5 minutes. [Source]({URL})"
    assert validate_scoped_limit_answer(claim, evidence, GOAL)
