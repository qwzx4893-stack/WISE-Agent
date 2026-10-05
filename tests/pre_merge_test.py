#!/usr/bin/env python3
"""
WISE Pre-Merge Baseline Verification Script.
Validates the baseline health, module imports, and tool/skill inventories
of both Leon AI and Supergent before any modifications.
"""

import os
import sys
import json
import glob
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = REPO_ROOT / "Supergent--main"
LEON_DIR = REPO_ROOT / "leon-develop"

passed = 0
failed = 0

def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        print(f"[PASS] {name} {detail}")
        passed += 1
    else:
        print(f"[FAIL] {name} {detail}")
        failed += 1

print("=" * 60)
print("  WISE PRE-MERGE BASELINE HEALTH AUDIT")
print("=" * 60)

# 1. Check Directories
check("Supergent directory exists", SUPERGENT_DIR.is_dir(), str(SUPERGENT_DIR))
check("Leon directory exists", LEON_DIR.is_dir(), str(LEON_DIR))

# 2. Audit Supergent Core Modules
sys.path.insert(0, str(SUPERGENT_DIR))

try:
    import agent_core
    check("Supergent agent_core import", True, f"({len(agent_core.tool_registry.tools)} kernel tools loaded)")
except Exception as e:
    check("Supergent agent_core import", False, str(e))

try:
    from core.paths import SKILLS_DIR, TOOLS_PACKS_DIR
    check("Supergent paths resolution", SKILLS_DIR.exists() and TOOLS_PACKS_DIR.exists())
except Exception as e:
    check("Supergent paths resolution", False, str(e))

try:
    from core.channels.unified import _available_schemes, list_channels
    schemes = _available_schemes()
    check("Supergent Apprise channel schemes", len(schemes) > 0, f"({len(schemes)} schemes available)")
except Exception as e:
    check("Supergent Apprise channel schemes", False, str(e))

try:
    from core.rag import get_router
    router = get_router()
    sources = list(router.sources.keys())
    check("Supergent RAG KnowledgeRouter", len(sources) > 0, f"(sources: {sources})")
except Exception as e:
    check("Supergent RAG KnowledgeRouter", False, str(e))

try:
    from core.scheduler import Scheduler
    scheduler = Scheduler()
    check("Supergent Scheduler initialized", scheduler is not None)
except Exception as e:
    check("Supergent Scheduler initialized", False, str(e))

try:
    from core.skills.indexer import SkillIndexer
    indexer = SkillIndexer()
    check("Supergent SkillIndexer initialized", len(indexer._index) > 0, f"({len(indexer._index)} skills indexed)")
except Exception as e:
    check("Supergent SkillIndexer initialized", False, str(e))

# 3. Audit Leon Assets & Tools
leon_tools = list(LEON_DIR.glob("tools/**/tool.json"))
check("Leon tools inventory", len(leon_tools) == 26, f"found {len(leon_tools)} tools")

valid_tool_functions = 0
for t in leon_tools:
    try:
        data = json.loads(t.read_text(encoding="utf-8"))
        valid_tool_functions += len(data.get("functions", {}))
    except Exception as e:
        check(f"Leon tool schema valid: {t.name}", False, str(e))

check("Leon callable functions total", valid_tool_functions == 110, f"(found {valid_tool_functions} functions)")

leon_native_skills = [p for p in (LEON_DIR / "skills" / "native").iterdir() if p.is_dir()]
check("Leon native skills count", len(leon_native_skills) == 27, f"(found {len(leon_native_skills)} native skills)")

leon_agent_skills = [p for p in (LEON_DIR / "skills" / "agent").iterdir() if p.is_dir()]
check("Leon agent skills count", len(leon_agent_skills) == 2, f"(found {len(leon_agent_skills)} agent skills)")

print("=" * 60)
print(f"Audit Summary: {passed} PASSED, {failed} FAILED")
print("=" * 60)

if failed > 0:
    sys.exit(1)
sys.exit(0)
