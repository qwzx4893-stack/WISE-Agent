"""Task coverage and complementary discovery across tools, skills and sources.

No LLM requests, no execution permissions, no 'use everything' quota. The graph
records intended coverage separately from observed successful calls.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from core.capability_network import CapabilityNetwork, tokenize

_PHASES = {
    "web_search": (r"\b(?:search|browse)\b[^\n]{0,100}\b(?:internet|web|online)\b|ابحث[^\n]{0,80}(?:الانترنت|الإنترنت|الويب)", {"research"}),
    "web_fetch": (r"\b(?:fetch|retrieve)\b[^\n]{0,70}\b(?:page|website|source)\b|اقرأ[^\n]{0,70}(?:صفحة|موقع)", {"research"}),
    "research": (r"\b(?:research|search|source|latest|compare|cve|osint)\b|ابحث|بحث|مصادر|أحدث|استخبارات", {"research","open_data","osint","exposure_monitoring","social_research","threat_intelligence","infrastructure"}),
    "inspect": (r"\b(?:read|inspect|import|analy[sz]e|audit|review)\b|اقرأ|افحص|استورد|حلل|راجع", {"files","forensics","media","vision","reverse_engineering","knowledge"}),
    "modify": (r"\b(?:edit|fix|repair|create|build|save|write)\b|عدل|عدّل|اصلح|أصلح|أنشئ|احفظ", {"files"}),
    "security": (r"\b(?:security|secrets|scan|vulnerab\w*|cve|stix|yara)\b|أمن|امن|أسرار|اسرار|ثغر", {"security","exposure_monitoring","threat_intelligence"}),
    "verify": (r"\b(?:verify|validate|test|check|audit|review)\b|تحقق|تأكد|اختبر|راجع", {"files","testing","security"}),
}

@dataclass
class UsagePlan:
    phases: list
    tools: list
    skills: list
    unavailable: list

    def to_dict(self):
        return {"schema":"wise.usage-network.v1","phases":self.phases,
            "recommended_tools":[item.to_dict() for item in self.tools],
            "recommended_skills":[item.to_dict() for item in self.skills],"unavailable":self.unavailable,
            "dependencies":[{"before":"inspect","after":"modify"},{"before":"modify","after":"verify"},{"before":"research","after":"cross_check"}],
            "strategy":"Use complementary capabilities for the actual goal. Do not call irrelevant tools to increase a count. Discover/replan when evidence is insufficient; verify final state."}

class UsageNetwork:
    def plan(self, goal, *, capabilities, skills, limit=12):
        from core.capability_catalog import group_tool
        phases=[name for name,(pattern,_) in _PHASES.items() if re.search(pattern,goal,re.I)]
        ranked=CapabilityNetwork().plan(goal,limit=max(1,len(capabilities)),skill_limit=6,capabilities=capabilities,skills=skills)
        descriptors={cap.id:cap for cap in capabilities}
        selected=[]
        # Preserve explicit tools, then cover distinct task phases instead of
        # letting eight similar web sources occupy the entire initial manifest.
        exact=[item for item in ranked.capabilities if item.score>=.9]
        selected.extend(exact[:max(1,limit//2)])
        for phase in phases:
            categories=_PHASES[phase][1]
            choices=[item for item in ranked.capabilities if group_tool(descriptors[item.id]) in categories]
            for item in choices[:2]:
                if item.id not in {x.id for x in selected}: selected.append(item)
        for item in ranked.capabilities:
            if len(selected)>=limit: break
            if item.id not in {x.id for x in selected}: selected.append(item)
        tokens=tokenize(goal)
        missing=[{"id":cap.id,"reason":getattr(cap,"missing_reason",None) or getattr(cap,"runtime_status","UNAVAILABLE")} for cap in capabilities
            if not cap.availability and (cap.id.rsplit(".",1)[-1].casefold() in goal.casefold()
                or len(tokens.intersection(tokenize(cap.name)))>=2)][:10]
        return UsagePlan(phases,selected[:limit],ranked.skills,missing)

    @staticmethod
    def coverage(plan, calls, loaded_skills, capabilities):
        from core.capability_catalog import group_tool
        descriptors={cap.id:cap for cap in capabilities}
        successful={call["tool"] for call in calls if call.get("success") and not call.get("duplicate_prevented")}
        last_write=max((i for i,call in enumerate(calls) if call.get("success") and call.get("tool")=="native.write_file"),default=-1)
        observed={}
        for phase in plan.phases:
            matches=[]
            for identifier in successful:
                cap=descriptors.get(identifier)
                if not cap: continue
                category=group_tool(cap)
                if phase=="web_search": relevant=identifier=="native.web_search"
                elif phase=="web_fetch": relevant=identifier=="native.web_fetch"
                elif phase=="modify": relevant=identifier in {"native.write_file"}
                elif phase=="verify": relevant=identifier in {"native.read_file","native.security_scan","intelligence.bandit","intelligence.detect-secrets"} and any(
                    i>last_write and call.get("tool")==identifier and call.get("success") and not call.get("duplicate_prevented") for i,call in enumerate(calls))
                elif phase=="inspect": relevant=identifier in {"native.read_file","native.list_directory"} or identifier.startswith("intelligence.")
                elif phase=="research": relevant=identifier.startswith(("source.","rag.")) or identifier in {"native.web_search","native.web_fetch"} or category=="research"
                else: relevant=identifier in {"native.security_scan","intelligence.bandit","intelligence.detect-secrets","intelligence.yara","intelligence.stix-taxii"} or identifier.startswith("source.") and category in _PHASES["security"][1]
                if relevant: matches.append(identifier)
            observed[phase]=sorted(matches)
        return {"phases":observed,"unobserved_phases":[name for name,items in observed.items() if not items],
            "distinct_successful_tools":len(successful),"skills_actually_read":sorted(loaded_skills),
            "note":"Call coverage is not proof of correctness, complete source coverage or the optimal tool choice"}
