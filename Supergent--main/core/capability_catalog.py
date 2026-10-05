"""Bounded, grouped discovery. Metadata is not proof a service is installed."""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path
from core.capability_network import tokenize


def eligible_skills(index):
    from core.skills.lifecycle import LIFECYCLE_FILE
    if not LIFECYCLE_FILE.exists():
        return index
    try:
        states = json.loads(LIFECYCLE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # Corrupt eligibility data is not permission to load all skills.
    if not isinstance(states, dict) or any(not isinstance(value, dict) for value in states.values()):
        return {}
    return {name:info for name,info in index.items() if states.get(name, {}).get("state", "active") == "active"}


def group_tool(cap):
    if getattr(cap, "category", ""):
        return cap.category
    name = cap.id.lower()
    for group, words in (("files", ("file", "directory", "git", "patch")),
                         ("research", ("search", "research", "web_fetch")),
                         ("browser", ("browser",)), ("security", ("security", "scan")),
                         ("media", ("speak", "voice", "image", "video")),
                         ("system", ("computer", "shell", "execute")), ("memory", ("memory", "remember"))):
        if any(word in name for word in words): return group
    return "mcp" if name.startswith("mcp.") else "general"


def _page(rows, *, query="", category="", offset=0, limit=10):
    groups = dict(sorted(Counter(row["category"] for row in rows).items()))
    tokens = tokenize(query)
    filtered = []
    for row in rows:
        if category and row["category"] != category: continue
        text = " ".join(str(row.get(key, "")) for key in ("id", "name", "description", "category"))
        if query and query.casefold() not in text.casefold() and not tokens.intersection(tokenize(text)): continue
        filtered.append(row)
    filtered.sort(key=lambda row: row["name"].casefold())
    offset, limit = max(0, int(offset)), max(1, min(20, int(limit)))
    return {"groups":groups, "total":len(filtered), "items":filtered[offset:offset+limit],
        "next_offset":offset+limit if offset+limit<len(filtered) else None}


def browse_skills(**args):
    from core.skills.indexer import SkillIndexer
    index = eligible_skills(SkillIndexer().get_index())
    rows = [{"id":name, "name":name, "category":info.get("category", "general"),
             "description":str(info.get("description", ""))[:500], "load_with":"read_skill"} for name,info in index.items()]
    return _page(rows, **args)


def browse_tools(**args):
    from core.capability_router import get_capability_router
    rows = [{"id":cap.id, "name":cap.name, "category":group_tool(cap),
             "description":cap.description[:500], "available":cap.availability,
             "risk":cap.risk_level, "status":cap.runtime_status} for cap in get_capability_router().list_capabilities()]
    return _page(rows, **args)


def browse_resources(**args):
    from core.intelligence.inventory import inventory
    return _page(inventory(), **args)
