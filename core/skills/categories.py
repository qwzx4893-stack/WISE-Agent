"""
Heuristic categorization for skills.

The repo carries 1800+ skills with very different conventions. This module
infers a reasonable category from:

1. ``metadata.json`` (if it sets ``category``)
2. The skill's path/name (e.g. ``security-audit/...`` → security)
3. Keywords in the description / excerpt

Categories are intentionally broad so the LLM can pick high level groups
when listing skills.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable

# Order matters: more specific keywords first.
_CATEGORY_RULES: list[tuple[str, Iterable[str]]] = [
    ("security", (
        "security", "pentest", "vuln", "exploit", "attack", "fuzz",
        "burp", "metasploit", "trufflehog", "gitleaks", "semgrep",
        "snyk", "audit", "redteam", "red-team", "ctf",
    )),
    ("web3", (
        "web3", "blockchain", "solidity", "ethereum", "evm", "defi",
        "foundry", "slither", "mythril", "ethers", "etherscan",
        "tokenomics", "wallet", "uniswap", "aave",
    )),
    ("data", (
        "data-scientist", "polars", "pandas", "scikit", "spark",
        "snowflake", "dbt", "etl", "warehouse", "lakehouse",
        "embedding", "vector-database", "rag",
    )),
    ("devops", (
        "docker", "kubernetes", "terraform", "ansible", "helm",
        "ci-cd", "github-actions", "deployment", "incident", "observability",
        "prometheus", "grafana", "k6", "loadtest",
    )),
    ("frontend", (
        "react", "next", "svelte", "vue", "tailwind", "shadcn",
        "astro", "frontend", "ui", "design-system", "flutter",
        "react-native", "expo", "maestro", "webtoapp",
    )),
    ("backend", (
        "fastapi", "nestjs", "hono", "django", "flask", "express",
        "graphql", "microservice", "monolith", "api-design",
    )),
    ("database", (
        "postgres", "mysql", "redis", "prisma", "drizzle", "supabase",
        "mongo", "cassandra", "neon", "sqlite",
    )),
    ("automation", (
        "automation", "n8n", "zapier", "temporal", "bullmq", "inngest",
        "workflow", "scheduler", "stripe", "twilio", "slack",
        "discord", "telegram", "notion", "jira", "hubspot",
        "google-sheets",
    )),
    ("seo", (
        "seo", "content-marketer", "social-content", "content-creator",
        "podcast", "programmatic-seo",
    )),
    ("agents", (
        "agent", "orchestrator", "multi-agent", "crewai", "langgraph",
        "openmanus", "openclaw", "claw-code", "vexor", "goose",
        "agenticseek", "godel", "dispatching-parallel",
    )),
    ("knowledge", (
        "knowledge", "rag-engineer", "search", "firecrawl", "exa",
        "tavily", "searxng", "wiki", "openalex",
    )),
    ("media", (
        "elevenlabs", "stability", "fal", "videodb", "image", "audio", "video",
    )),
    ("llm", (
        "litellm", "langfuse", "openai", "prompt-engineer",
        "llm-evaluation",
    )),
    ("testing", (
        "pytest", "test-automator", "hypothesis", "playwright",
        "cypress", "k6", "load-testing",
    )),
    ("business", (
        "startup", "product-manager", "sales-automator", "legal-advisor",
        "launch-strategy", "competitive-landscape", "pricing-strategy",
        "revops", "go-to-market",
    )),
    ("infra", (
        "aws", "azure", "gcp", "cloudflare", "serverless", "lambda",
    )),
    ("memory", (
        "memory-systems", "context-manager", "context-compression",
        "vector", "mem0", "memubot",
    )),
    ("browser", (
        "browser-use", "computer-use", "stagehand", "deep-research",
    )),
]

_FALLBACK = "general"


def infer_category(name: str, description: str = "", path: Path | str = "",
                   metadata: Dict | None = None) -> str:
    """Pick a single category for a skill. Always returns a non-empty string."""
    if metadata and isinstance(metadata, dict):
        cat = metadata.get("category")
        if isinstance(cat, str) and cat.strip():
            return cat.strip().lower()

    haystack = " ".join([
        name or "",
        str(path) if path else "",
        description or "",
    ]).lower()

    for category, keywords in _CATEGORY_RULES:
        for kw in keywords:
            if kw in haystack:
                return category

    return _FALLBACK


def categorize_index(index: Dict[str, Dict]) -> Dict[str, list[str]]:
    """Build a ``{category: [skill_name, ...]}`` mapping from a skill index."""
    buckets: Dict[str, list[str]] = {}
    for name, info in index.items():
        cat = info.get("category") or infer_category(
            name=name,
            description=info.get("description", ""),
            path=info.get("path", ""),
            metadata=info.get("metadata"),
        )
        buckets.setdefault(cat, []).append(name)
    return {k: sorted(v) for k, v in sorted(buckets.items())}


__all__ = ["infer_category", "categorize_index"]
