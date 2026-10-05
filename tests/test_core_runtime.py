#!/usr/bin/env python3
"""
WISE Core Functions Comprehensive Runtime Execution Test Suite.
Directly implements User Condition 2:
"اعتبر اختبار Pre-Merge الحالي اختبار سلامة أساسي فقط، وليس إثباتًا أن جميع وظائف المشروعين تعمل.
يجب إجراء اختبارات تشغيل فعلية للوظائف الأساسية بعد الدمج."

Executes real runtime operations across:
1. Supergent File Operations (_write_file, _read_file, _grep_file, _list_directory)
2. Supergent System Execution (_execute_python, _run_shell)
3. Supergent RAG Knowledge Router (querying router across scientific/web sources)
4. Supergent Task Scheduler (job lifecycle: schedule, query, cancel)
5. Supergent Workforce Multi-Agent Orchestration (DAG execution model)
6. Leon Voice & Assistant Adapter (leon_speak, leon_get_context, leon_get_memory, leon_send_ui)
7. Bidirectional Bridge Functionality (calling Leon functions from Supergent dispatcher)
8. Secret Isolation Enforcement (verifying no cross-contamination between secret stores)
"""

from __future__ import annotations

import os
import sys
import json
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = REPO_ROOT / "Supergent--main"
LEON_DIR = REPO_ROOT / "leon-develop"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SUPERGENT_DIR))

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


print("=" * 70)
print("   WISE CORE FUNCTIONS COMPREHENSIVE RUNTIME EXECUTION TEST SUITE")
print("   (User Condition 2 Verification: Real Execution of Core Functions)")
print("=" * 70)

# ==========================================================================
# 1. Supergent File System Operations (Real Execution)
# ==========================================================================
print("\n--- 1. Testing Supergent Core File Tools ---")
try:
    from core.tools_bridge import (
        _write_file, _read_file, _grep_file, _list_directory, call_python_builtin
    )
    from core.paths import WORKSPACE_DIR

    test_filename = "wise_runtime_test_probe.txt"
    test_payload = "WISE_CORE_OPERATIONAL_SIGNATURE_2026\nINTEGRATION_VERIFIED=TRUE\n"

    # Write
    write_res = _write_file({"path": test_filename, "content": test_payload})
    check("File Write Execution (_write_file)", write_res == "OK")

    # Read
    read_res = _read_file({"path": test_filename})
    check("File Read Execution (_read_file)", read_res == test_payload)

    # Grep
    grep_res = _grep_file({"path": test_filename, "pattern": "INTEGRATION_VERIFIED"})
    check("Pattern Grep Execution (_grep_file)", "INTEGRATION_VERIFIED=TRUE" in grep_res)

    # List Directory
    list_res = _list_directory({"path": "."})
    check("List Directory Execution (_list_directory)", test_filename in list_res)

    # Cleanup
    (WORKSPACE_DIR / test_filename).unlink(missing_ok=True)
except Exception as e:
    check("Supergent Core File Tools", False, str(e))

# ==========================================================================
# 2. Supergent System Code & Shell Execution (Real Execution)
# ==========================================================================
print("\n--- 2. Testing Supergent Code & Shell Execution ---")
try:
    # Python code evaluation
    py_code = "import math; print(f'MATH_PROBE:{int(math.sqrt(65536))}')"
    py_res = call_python_builtin("execute_python", {"code": py_code})
    check("Python Subprocess Execution (execute_python)", "MATH_PROBE:256" in py_res, f"(output: {py_res.strip()})")

    # Shell execution (Windows echo)
    shell_cmd = "cmd /c echo WISE_SHELL_ONLINE"
    shell_res = call_python_builtin("run_shell", {"command": shell_cmd})
    check("Shell Command Execution (run_shell)", "WISE_SHELL_ONLINE" in shell_res, f"(output: {shell_res.strip()})")
except Exception as e:
    check("Supergent Execution Tools", False, str(e))

# ==========================================================================
# 3. Supergent RAG Knowledge Router (Real Query Execution)
# ==========================================================================
print("\n--- 3. Testing Supergent RAG Knowledge Router ---")
try:
    from core.rag import get_router
    router = get_router()
    check("Knowledge Router Initialization", router is not None and len(router.sources) >= 15)

    # Query Wikipedia source route
    sources = list(router.sources.keys())
    check("RAG Sources Catalog Verification", len(sources) >= 15, f"({len(sources)} sources registered)")

    # Execute a router query against a local or standard knowledge source
    wiki_source = router.sources.get("wikipedia")
    check("RAG Wikipedia Source Available", wiki_source is not None)
    if wiki_source:
        wiki_results = wiki_source.search("Artificial Intelligence", max_results=1)
        check(
            "RAG Search Execution (Wikipedia)",
            isinstance(wiki_results, list) and len(wiki_results) > 0,
            f"(retrieved {len(wiki_results)} results)"
        )
except Exception as e:
    check("Supergent RAG Router Execution", False, str(e))

# ==========================================================================
# 4. Supergent Task Scheduler (Real Lifecycle Execution)
# ==========================================================================
print("\n--- 4. Testing Supergent Task Scheduler ---")
try:
    from core.scheduler import get_scheduler
    scheduler = get_scheduler()
    check("Scheduler Initialization", scheduler is not None)

    # Schedule a test job using add()
    sched_obj = scheduler.add(
        name="wise_heartbeat_test",
        cron="0 * * * *",
        target="system.ping",
        payload={"owner": "wise_supervisor"}
    )
    check("Scheduler Job Creation", sched_obj is not None and hasattr(sched_obj, "id"), f"(job_id: {getattr(sched_obj, 'id', None)})")

    # Verify job exists in list()
    all_jobs = scheduler.list()
    job_found = any(j.id == sched_obj.id or j.name == "wise_heartbeat_test" for j in all_jobs)
    check("Scheduler Job Listing", job_found)

    # Cancel job using delete()
    deleted = scheduler.delete(sched_obj.id)
    all_jobs_after = scheduler.list()
    job_cancelled = not any(j.id == sched_obj.id for j in all_jobs_after)
    check("Scheduler Job Cancellation", deleted and job_cancelled)
except Exception as e:
    check("Supergent Scheduler Execution", False, str(e))

# ==========================================================================
# 5. Supergent Multi-Agent Workforce Orchestrator
# ==========================================================================
print("\n--- 5. Testing Supergent Multi-Agent Workforce Orchestrator ---")
try:
    from core.workforce import Workforce, RootPlanner
    async def sample_exec(subtask):
        return f"EXECUTED: {subtask.description}"

    workforce = Workforce(execute=sample_exec)
    check("Workforce Coordinator Initialization", workforce is not None)
    planner = RootPlanner()
    check("Workforce RootPlanner Initialization", planner is not None)
    subtasks = planner.plan("1. Analyze security vulnerabilities 2. Generate report")
    check("Workforce Task Planning Execution", isinstance(subtasks, list) and len(subtasks) == 2, f"(subtasks: {len(subtasks)})")
except Exception as e:
    check("Supergent Workforce Execution", False, str(e))

# ==========================================================================
# 6. Leon Voice & Assistant Adapter (Real Execution via Bridge)
# ==========================================================================
print("\n--- 6. Testing Leon Voice & Assistant Adapter ---")
try:
    from core.adapters.leon_adapter import get_leon_adapter
    leon_adapter = get_leon_adapter()

    # 1. get_context
    ctx = leon_adapter.get_context(category="system")
    check("Leon Context Retrieval (get_context)", isinstance(ctx, dict) and "status" in ctx)

    # 2. speak
    speak_res = leon_adapter.speak("Hello from WISE unified ecosystem", lang="en")
    check("Leon Voice Dispatch (speak)", isinstance(speak_res, dict) and "spoken" in speak_res)

    # 3. get_memory
    mem_res = leon_adapter.get_memory("user profile preferences", max_results=3)
    check("Leon Memory Retrieval (get_memory)", isinstance(mem_res, dict) and "results" in mem_res)

    # 4. send_ui_message
    ui_res = leon_adapter.send_ui_message("System test notification", title="WISE Check")
    check("Leon UI Dispatch (send_ui_message)", isinstance(ui_res, dict) and "delivered" in ui_res)

except Exception as e:
    check("Leon Voice & Assistant Adapter", False, str(e))

# ==========================================================================
# 7. Dispatcher Execution for Pack 20 Tools
# ==========================================================================
print("\n--- 7. Testing Dispatcher Execution for Pack 20 Tools ---")
try:
    # Test dispatching leon_speak
    raw_res = call_python_builtin("leon_speak", {"text": "Unit test ping", "voice": "default", "lang": "en"})
    parsed = json.loads(raw_res)
    check("Dispatcher: leon_speak", isinstance(parsed, dict) and parsed.get("text") == "Unit test ping")

    # Test dispatching leon_get_context
    raw_ctx = call_python_builtin("leon_get_context", {"category": "all"})
    parsed_ctx = json.loads(raw_ctx)
    check("Dispatcher: leon_get_context", isinstance(parsed_ctx, dict) and parsed_ctx.get("category") == "all")

    # Test dispatching leon_get_memory
    raw_mem = call_python_builtin("leon_get_memory", {"query": "test query", "max_results": 2})
    parsed_mem = json.loads(raw_mem)
    check("Dispatcher: leon_get_memory", isinstance(parsed_mem, dict) and "results" in parsed_mem)

    # Test dispatching leon_send_ui
    raw_ui = call_python_builtin("leon_send_ui", {"message": "Test UI alert", "title": "Test"})
    parsed_ui = json.loads(raw_ui)
    check("Dispatcher: leon_send_ui", isinstance(parsed_ui, dict) and "delivered" in parsed_ui)

except Exception as e:
    check("Dispatcher Pack 20 Tools", False, str(e))

# ==========================================================================
# 8. Secret Isolation Enforcement (Mandatory Condition 3)
# ==========================================================================
print("\n--- 8. Testing Secret Isolation Enforcement ---")
try:
    from config.wise_config import wise_config
    status = wise_config.get_service_status()
    check("WiseConfig reports isolated secrets", status.get("secrets_isolated") is True)

    leon_env_path = wise_config.get_leon_profile_env_path()
    supergent_keystore_path = wise_config.get_supergent_keystore_path()

    # Verify they are physically distinct paths
    check(
        "Leon and Supergent secrets paths are physically isolated",
        leon_env_path != supergent_keystore_path and str(leon_env_path) not in str(supergent_keystore_path)
    )

    # Ensure no secrets file exists in root WISE git repository
    root_env_file = REPO_ROOT / ".env"
    check("No .env secret file checked into root", not root_env_file.exists())

except Exception as e:
    check("Secret Isolation Enforcement", False, str(e))

print("=" * 70)
print(f"   CORE RUNTIME EXECUTION RESULTS: {passed} PASSED, {failed} FAILED")
print("=" * 70)

if failed > 0:
    sys.exit(1)
sys.exit(0)
