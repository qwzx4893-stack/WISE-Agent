"""Explainable capability, skill, and MCP selection for WISE.

The runtime already has a capability registry, a tool ranker, and a skill
index. This module joins them into one deterministic selection plan. It is
advisory only: security gates remain the execution authority.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from core.capability_router import CapabilityDescriptor, get_capability_router
from core.contracts import CapabilitySource


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u0621-\u064A]{2,}", re.UNICODE)
_STOP = frozenset({"the", "and", "for", "with", "from", "this", "that", "into", "about", "في", "من", "على", "الى", "إلى", "هذا", "هذه", "اريد", "أريد", "ممكن", "قم", "لي", "عن", "مع", "ثم", "او", "أو"})
_SIGNALS = {
    "research": ("بحث", "ابحث", "مصدر", "مراجع", "خبر", "أحدث", "research", "latest", "compare"),
    "browser": ("متصفح", "موقع", "ويب", "browser", "website", "login", "صفحة"),
    "files": ("ملف", "مجلد", "كود", "مشروع", "file", "folder", "code", "repo"),
    "computer": ("افتح", "اكتب", "اضغط", "برنامج", "شاشة", "حاسوب", "computer", "desktop"),
    "memory": ("تذكر", "تذكّر", "ذاكرة", "remember", "memory", "سابق"),
    "mcp": ("mcp", "قاعدة", "database", "slack", "github", "notion", "drive"),
    "security": ("أمن", "امني", "أمني", "ثغر", "أسرار", "اسرار", "سرية", "security", "secrets", "vulnerability", "scan"),
    "osint": ("osint", "استخبارات", "تحقيق", "threat", "cve", "cisa", "epss", "stix"),
}
_RISK_PENALTY = {"LOW": 0.0, "MEDIUM": 0.03, "HIGH": 0.07, "CRITICAL": 0.12}


def tokenize(text: str) -> Set[str]:
    return {item.lower() for item in _TOKEN_RE.findall(text or "") if len(item) > 1 and item.lower() not in _STOP}


def _overlap(left: Set[str], right: Set[str]) -> float:
    return (len(left & right) / len(left | right)) if left and right else 0.0


@dataclass
class NetworkCandidate:
    id: str
    name: str
    source: str
    score: float
    reason: str
    risk_level: str = "LOW"
    available: bool = True
    description: str = ""
    path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "source": self.source, "score": round(self.score, 4), "reason": self.reason, "risk_level": self.risk_level, "available": self.available, "description": self.description, "path": self.path}


@dataclass
class CapabilityPlan:
    task: str
    capabilities: List[NetworkCandidate] = field(default_factory=list)
    skills: List[NetworkCandidate] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"task": self.task, "signals": self.signals, "capabilities": [item.to_dict() for item in self.capabilities], "skills": [item.to_dict() for item in self.skills]}

    def to_prompt(self) -> str:
        lines = ["## شبكة اختيار القدرات لهذه المهمة"]
        if self.capabilities:
            lines.append("القدرات الموصى بها بالترتيب (اختر الأدنى خطراً الذي يحقق الهدف):")
            lines.extend(f"- `{item.name}` [{item.source}] — {item.reason}." for item in self.capabilities)
        if self.skills:
            lines.append("مهارات إجرائية مفيدة؛ ابحث/حمّل المهارة قبل اتباع تفاصيلها عند الحاجة:")
            lines.extend(f"- `{item.name}` — {item.reason}." for item in self.skills)
        if not self.capabilities and not self.skills:
            lines.append("لا توجد مطابقة عالية الثقة؛ استخدم search_tools أو search_skills قبل التنفيذ.")
        lines.append("هذه توصية فقط: تحقق من التوفر ولا تتجاوز بوابة الأمان أو طلب الموافقة.")
        return "\n".join(lines)


class CapabilityNetwork:
    """Build a small task graph across native tools, MCPs, and skills."""

    def __init__(self, router: Any = None, indexer: Any = None) -> None:
        self._router = router
        self._indexer = indexer

    @property
    def router(self):
        return self._router or get_capability_router()

    def plan(self, task: str, *, limit: int = 5, skill_limit: int = 3, capabilities: Optional[Sequence[CapabilityDescriptor]] = None, skills: Optional[Dict[str, Dict[str, Any]]] = None) -> CapabilityPlan:
        tokens = tokenize(task)
        task_lower = (task or "").lower()
        signals = [name for name, words in _SIGNALS.items() if any(word.lower() in task_lower for word in words)]
        caps = list(capabilities) if capabilities is not None else self.router.list_capabilities()
        ranked = [self._score_capability(tokens, signals, cap) for cap in caps]
        # A zero-score item is merely available, not relevant. Returning it
        # would bias the agent toward an arbitrary tool name instead of the
        # explicit search/discovery path.
        ranked = [item for item in ranked if item.available and item.score > 0.02]
        ranked.sort(key=lambda item: (-item.score, item.risk_level, item.name))
        skill_index = skills if skills is not None else self._skill_index()
        ranked_skills = [self._score_skill(tokens, signals, name, info) for name, info in skill_index.items()]
        ranked_skills = [item for item in ranked_skills if item.score > 0.04]
        ranked_skills.sort(key=lambda item: (-item.score, item.name))
        return CapabilityPlan(task=task, capabilities=ranked[:max(1, int(limit))], skills=ranked_skills[:max(0, int(skill_limit))], signals=signals)

    def _skill_index(self) -> Dict[str, Dict[str, Any]]:
        if self._indexer is not None:
            return self._indexer.get_index()
        try:
            from core.skills.indexer import SkillIndexer
            from core.capability_catalog import eligible_skills
            return eligible_skills(SkillIndexer().get_index())
        except Exception:
            return {}

    def _score_capability(self, task_tokens: Set[str], signals: List[str], cap: CapabilityDescriptor) -> NetworkCandidate:
        source_obj = getattr(cap,"source", CapabilitySource.MCP if cap.id.startswith("mcp.") else CapabilitySource.NATIVE)
        source = source_obj.value if hasattr(source_obj, "value") else str(source_obj)
        name=getattr(cap,"name",cap.id.rsplit(".",1)[-1])
        text = " ".join((name, cap.description, cap.id, source))
        score = _overlap(task_tokens, tokenize(text))
        low = text.lower()
        if "research" in signals and source == CapabilitySource.RAG.value: score += 0.35
        if "memory" in signals and source == CapabilitySource.MEMORY.value: score += 0.35
        if "computer" in signals and source == CapabilitySource.COMPUTER_USE.value: score += 0.35
        if "mcp" in signals and source == CapabilitySource.MCP.value: score += 0.30
        if "browser" in signals and ("browser" in low or "web" in low): score += 0.25
        if "files" in signals and any(word in low for word in ("file", "directory", "workspace", "git")): score += 0.25
        if "security" in signals and any(word in low for word in ("security", "defensive", "secrets", "scan")): score += 0.25
        if "osint" in signals and getattr(cap,"category", "") in {"osint","threat_intelligence","exposure_monitoring","infrastructure"}: score += .20
        affinities={"intelligence.duckdb": {"csv","duckdb"},"intelligence.bandit":{"python","bandit"},
            "intelligence.detect-secrets":{"secrets"},"intelligence.yara":{"yara"},"intelligence.stix-taxii":{"stix","taxii"},
            "source.cisa-known-exploited-vulnerabilities":{"cisa","kev"},"source.first-epss":{"epss"},"source.nvd":{"nvd"}}
        if task_tokens.intersection(affinities.get(cap.id,set())): score=max(score,.95)
        # Explicit upstream identity outranks vague category matches. Hyphen/
        # underscore names are normalized; no matching arbitrary substrings.
        aliases={cap.id.rsplit(".",1)[-1].replace("-"," ").replace("_"," "),name}
        for alias in aliases:
            parts=tokenize(alias)
            if parts and parts.issubset(task_tokens): score=max(score,1.0); break
        risk = (getattr(cap,"risk_level","LOW") or "LOW").upper()
        score = max(0.0, score - _RISK_PENALTY.get(risk, 0.08))
        reason = "matched intent: " + ", ".join(signals) if signals else "lexical task match"
        return NetworkCandidate(id=cap.id, name=name, source=source, score=score, reason=reason, risk_level=risk, available=cap.availability, description=cap.description)

    @staticmethod
    def _score_skill(task_tokens: Set[str], signals: List[str], name: str, info: Dict[str, Any]) -> NetworkCandidate:
        text = " ".join((name, str(info.get("description", "")), str(info.get("excerpt", "")), str(info.get("category", ""))))
        score = _overlap(task_tokens, tokenize(text))
        category = str(info.get("category", "")).lower()
        if "research" in signals and category in {"knowledge", "agents"}: score += 0.12
        if "browser" in signals and category == "browser": score += 0.18
        if "files" in signals and category in {"backend", "testing", "devops"}: score += 0.10
        if "security" in signals and category=="security": score+=.18
        if tokenize(name).issubset(task_tokens): score=max(score,.95)
        return NetworkCandidate(id=f"skill.{name}", name=name, source="SKILLNET", score=score, reason="matched procedural workflow", description=str(info.get("description", "")), path=str(info.get("path", "")) or None)


_NETWORK: Optional[CapabilityNetwork] = None
_LOCK = threading.Lock()


def get_capability_network() -> CapabilityNetwork:
    global _NETWORK
    if _NETWORK is None:
        with _LOCK:
            if _NETWORK is None:
                _NETWORK = CapabilityNetwork()
    return _NETWORK


__all__ = ["CapabilityNetwork", "CapabilityPlan", "NetworkCandidate", "get_capability_network", "tokenize"]
