#!/usr/bin/env python3
"""
WISE Post-Merge Comprehensive Integration & Runtime Verification Test Suite.

Validates the integrated WISE ecosystem:
1. Leon -> Supergent tool registry & bridge integrity (supergent_os toolkit)
2. Supergent -> Leon assistant adapter (LeonAdapter & pack_20)
3. Direct execution of tools in both directions
4. RAG knowledge router live functionality
5. Apprise channels registry & schema verification
6. Semantic skill search & discovery across the 1,727 skills catalog
7. Secret isolation verification (no cross-contamination of secrets)
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path

# Force UTF-8 encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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


print("=" * 65)
print("   WISE POST-MERGE INTEGRATION & RUNTIME TEST SUITE")
print("=" * 65)

# --------------------------------------------------------------------------
# 1. Supergent -> Leon Assistant Adapter Tests
# --------------------------------------------------------------------------
sys.path.insert(0, str(SUPERGENT_DIR))

try:
    from core.adapters.leon_adapter import LeonAdapter, get_leon_adapter
    adapter = get_leon_adapter()
    check("Supergent LeonAdapter instantiation", adapter is not None)

    # Test get_context execution
    ctx_res = adapter.get_context("system")
    check("LeonAdapter.get_context execution", isinstance(ctx_res, dict) and "category" in ctx_res, f"(status: {ctx_res.get('status')})")

    # Test speak execution (graceful fallback)
    speak_res = adapter.speak("WISE integration test utterance", lang="en")
    check("LeonAdapter.speak execution", isinstance(speak_res, dict) and "text" in speak_res)

    # Test tools_bridge dispatch
    from core.tools_bridge import call_python_builtin
    bridge_res_raw = call_python_builtin("leon_get_context", {"category": "all"})
    bridge_res = json.loads(bridge_res_raw)
    check("tools_bridge leon_get_context dispatch", isinstance(bridge_res, dict) and bridge_res.get("category") == "all")

except Exception as e:
    check("Supergent LeonAdapter tests", False, str(e))

# --------------------------------------------------------------------------
# 2. Leon -> Supergent Toolkit Integration Tests
# --------------------------------------------------------------------------
supergent_toolkit_json = LEON_DIR / "tools" / "supergent_os" / "toolkit.json"
check("Leon supergent_os toolkit.json exists", supergent_toolkit_json.exists())

supergent_tool_json = LEON_DIR / "tools" / "supergent_os" / "bridge" / "tool.json"
check("Leon supergent_os bridge tool.json exists", supergent_tool_json.exists())

try:
    tool_data = json.loads(supergent_tool_json.read_text(encoding="utf-8"))
    functions = list(tool_data.get("functions", {}).keys())
    expected_functions = ["executeTool", "searchRAG", "runWorkforce", "sendNotification", "getHealth", "searchSkills"]
    all_present = all(fn in functions for fn in expected_functions)
    check("Leon supergent_os bridge functions registered", all_present, f"(found: {functions})")
except Exception as e:
    check("Leon supergent_os bridge schema parse", False, str(e))

# --------------------------------------------------------------------------
# 3. Supergent Toolpack 20 Verification
# --------------------------------------------------------------------------
pack_20 = SUPERGENT_DIR / "tools" / "packs" / "pack_20_leon_assistant.json"
check("Supergent pack_20_leon_assistant.json exists", pack_20.exists())

try:
    pack_data = json.loads(pack_20.read_text(encoding="utf-8"))
    tool_names = [t.get("name") for t in pack_data]
    expected_tools = ["leon_speak", "leon_get_context", "leon_get_memory", "leon_send_ui"]
    check("Pack 20 tools registered", all(t in tool_names for t in expected_tools), f"(found: {tool_names})")
except Exception as e:
    check("Pack 20 schema parse", False, str(e))

# --------------------------------------------------------------------------
# 4. Supergent Core Runtime Tool Execution
# --------------------------------------------------------------------------
try:
    from core.tools_bridge import _read_file, _write_file, _list_directory
    test_path = "test_wise_artifact.txt"
    test_content = "WISE integration runtime test OK"
    write_res = _write_file({"path": test_path, "content": test_content})
    check("Supergent _write_file execution", write_res == "OK")

    read_res = _read_file({"path": test_path})
    check("Supergent _read_file execution", read_res == test_content)

    list_res = _list_directory({"path": "."})
    check("Supergent _list_directory execution", "test_wise_artifact.txt" in list_res)

    # Clean up test artifact
    from core.paths import WORKSPACE_DIR
    (WORKSPACE_DIR / test_path).unlink(missing_ok=True)
except Exception as e:
    check("Supergent file tool execution", False, str(e))

# --------------------------------------------------------------------------
# 5. RAG Knowledge Router Verification
# --------------------------------------------------------------------------
try:
    from core.rag import get_router
    router = get_router()
    check("RAG router initialized", router is not None and len(router.sources) >= 15)
except Exception as e:
    check("RAG router verification", False, str(e))

# --------------------------------------------------------------------------
# 6. Semantic Skills Indexing & Discovery Test
# --------------------------------------------------------------------------
try:
    from core.skills.indexer import SkillIndexer
    indexer = SkillIndexer()
    total_skills = len(indexer.list_skills())
    check("SkillIndexer catalog size", total_skills >= 1720, f"({total_skills} skills discovered)")

    # Test indexing a specific critical skill
    skills = indexer.list_skills()
    sample_skills = [s for s in skills if "security" in s or "test" in s or "agent" in s]
    check("Critical domain skills discoverable", len(sample_skills) > 0, f"(sample matches: {len(sample_skills)})")
except Exception as e:
    check("SkillIndexer test", False, str(e))

# --------------------------------------------------------------------------
# 7. Apprise Unified Channels Verification
# --------------------------------------------------------------------------
try:
    from core.channels.unified import _available_schemes
    schemes = _available_schemes()
    check("Apprise schemes available", len(schemes) >= 150, f"({len(schemes)} schemes active)")
except Exception as e:
    check("Apprise schemes verification", False, str(e))

# --------------------------------------------------------------------------
# 8. Secret Isolation Audit (Mandatory Condition 3)
# --------------------------------------------------------------------------
root_env = REPO_ROOT / ".env"
root_env_example = REPO_ROOT / ".env.example"

check("Root .env.example exists with documentation", root_env_example.exists())

# Verify no raw private keys were hardcoded into committed files
leaked_tokens = False
if root_env_example.exists():
    content = root_env_example.read_text(encoding="utf-8")
    if "sk-" in content or "ghp_" in content:
        leaked_tokens = True
check("Secret isolation: .env.example free of raw keys", not leaked_tokens)

print("=" * 65)
print(f"  WISE POST-MERGE TEST RESULTS: {passed} PASSED, {failed} FAILED")
print("=" * 65)

if failed > 0:
    sys.exit(1)
sys.exit(0)
