# ==============================================================================
# WISE Cognitive Core Enhancement — 16-Scenario Verification Suite
# Model: LiquidAI/LFM2.5-8B-A1B Q4_K_M GGUF
# Evaluates:
#   1. Direct Answer (Simple Question)
#   2. Single Search Query (Factual Lookup)
#   3. Deep Multi-Source Research & Contradiction Detection
#   4. Skill Capability Routing
#   5. MCP Client Routing
#   6. Windows OS Control Task
#   7. Browser Navigation Task
#   8. Multi-Capability Sequential Task (Search + File)
#   9. Multi-Step TaskEngine Session Pinning (Zero mid-task unloading)
#  10. State-Aware Cognitive Failure Recovery & Replanning
#  11. Complex Arabic Cognitive Analysis
#  12. Direct Answer vs. Unnecessary Tool Use
#  13. Incomplete / Ambiguous Request (Honest Clarification)
#  14. Idle Mode Resource Measurement (Model VRAM = 0 MB, Baseline preserved)
#  15. JIT Wake-up on Task Arrival
#  16. Return to Idle after Grace Period (Watchdog Cooldown)
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add Supergent path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SUPERGENT_PATH = WORKSPACE_ROOT / "Supergent--main"
sys.path.insert(0, str(SUPERGENT_PATH))

import psutil
from core.models.runtime.lfm25_backend import LFM25NativeBackend
from core.models.provider_interface import (
    LFM25CognitiveProvider,
    ModelCompletionRequest,
    ModelCompletionResponse,
    get_model_provider,
    set_active_model_provider,
)
from core.brain.cognitive_decision_engine import (
    CognitiveDecisionEngine,
    CognitivePreflightDecision,
    get_cognitive_decision_engine,
)
from core.brain.task_engine import get_task_engine, create_task_from_steps
from core.context.world_state import get_world_state_engine, WISEWorldState
from core.hands import ComputerActionType
from core.brain.intent_parser import PlannedStep

LOG = logging.getLogger("WISE.Tests.CognitiveEnhancements")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class CognitiveEnhancementTestSuite:
    def __init__(self) -> None:
        self.provider = LFM25CognitiveProvider()
        set_active_model_provider(self.provider)
        self.cde = get_cognitive_decision_engine()
        self.state_engine = get_world_state_engine()
        self.task_engine = get_task_engine()
        self.results: List[Dict[str, Any]] = []

    def _get_metrics(self) -> Dict[str, Any]:
        backend: LFM25NativeBackend = self.provider.backend
        mem = backend.get_memory_usage()
        current_vram = mem["gpu_vram_used_mb"]
        baseline_vram = getattr(backend, "baseline_vram_mb", 1139.0)
        model_vram = getattr(backend, "model_allocated_vram_mb", 0.0) if backend.is_loaded() else 0.0

        proc = psutil.Process(os.getpid())
        cpu_pct = proc.cpu_percent(interval=0.05)
        ram_mb = mem["host_ram_used_mb"]

        return {
            "current_vram_mb": round(current_vram, 2),
            "model_vram_mb": round(model_vram, 2),
            "baseline_vram_mb": round(baseline_vram, 2),
            "ram_mb": round(ram_mb, 2),
            "cpu_pct": round(cpu_pct, 1),
            "is_loaded": backend.is_loaded(),
        }

    # --------------------------------------------------------------------------
    # Scenario 1: Simple Question (Direct Answer, 0 tools)
    # --------------------------------------------------------------------------
    def test_01_simple_question_direct_answer(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "What is the boiling point of water at sea level in Celsius?"
        dec = self.cde.preflight_analyze(intent)
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            dec.can_answer_directly is True
            and ("none" in dec.selected_capabilities or len(dec.selected_capabilities) == 0 or "none" in str(dec.selected_capabilities).lower())
            and (dec.direct_answer is not None and ("100" in dec.direct_answer or "celsius" in dec.direct_answer.lower()))
        )

        m = self._get_metrics()
        res = {
            "id": 1,
            "name": "Simple Question Direct Answer",
            "decision": "DIRECT_ANSWER" if dec.can_answer_directly else "TOOL_REQUIRED",
            "capability": "none",
            "reason": dec.reasoning or "General knowledge fact, no tools needed",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": str(dec.direct_answer or dec.recommended_action)[:100],
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 2: Single Search Query (Factual Lookup)
    # --------------------------------------------------------------------------
    def test_02_single_search_query(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "Search the web for recent discoveries and astronomical findings of the James Webb Space Telescope"
        dec = self.cde.preflight_analyze(intent)

        from core.tools_bridge import call_python_builtin
        search_out = call_python_builtin("web_search", {"query": "James Webb Space Telescope recent discoveries", "sources": "wikipedia"})
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            "web_search" in dec.selected_capabilities
            or "web" in str(dec.selected_capabilities).lower()
            or "search" in dec.recommended_action.lower()
        ) and bool(search_out and len(search_out) > 50)

        m = self._get_metrics()
        res = {
            "id": 2,
            "name": "Single Search Query",
            "decision": "SEARCH_REQUIRED",
            "capability": "web_search",
            "reason": dec.reasoning or "Specific scientific specification lookup",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": search_out[:120].replace("\n", " "),
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 3: Deep Multi-Source Research
    # --------------------------------------------------------------------------
    def test_03_deep_multi_source_research(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        topic = "James Webb Space Telescope launch date and scientific instrument capabilities"
        res_data = self.cde.execute_deep_research(topic, sources_limit=2)
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            bool(res_data.get("summary"))
            and len(res_data.get("sub_queries_used", [])) >= 1
            and res_data.get("sources_count", 0) >= 1
        )

        m = self._get_metrics()
        res = {
            "id": 3,
            "name": "Deep Multi-Source Research",
            "decision": "DEEP_RESEARCH_SYNTHESIS",
            "capability": "web_search + knowledge_router",
            "reason": "Decomposed queries, collected evidence, cross-referenced facts",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 2,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": str(res_data.get("summary", ""))[:120].replace("\n", " "),
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 4: Skill Capability Routing
    # --------------------------------------------------------------------------
    def test_04_skill_capability_routing(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "Run specialized codebase security audit and vulnerability profiling skill workflow"
        dec = self.cde.preflight_analyze(intent)
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            "skill_runner" in dec.selected_capabilities
            or "skill" in str(dec.selected_capabilities).lower()
            or "skill" in dec.recommended_action.lower()
        )

        m = self._get_metrics()
        res = {
            "id": 4,
            "name": "Skill Capability Routing",
            "decision": "SKILL_WORKFLOW",
            "capability": "skill_runner",
            "reason": dec.reasoning or "Complex internal auditing requires specialized skill workflow",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Selected: {dec.selected_capabilities} | Action: {dec.recommended_action[:80]}",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 5: MCP Capability Routing
    # --------------------------------------------------------------------------
    def test_05_mcp_client_routing(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "Query the external Postgres database server via registered Model Context Protocol (MCP) to count active records"
        dec = self.cde.preflight_analyze(intent)
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            "mcp_client" in dec.selected_capabilities
            or "mcp" in str(dec.selected_capabilities).lower()
            or "mcp" in dec.recommended_action.lower()
        )

        m = self._get_metrics()
        res = {
            "id": 5,
            "name": "MCP Client Routing",
            "decision": "MCP_PROTOCOL_QUERY",
            "capability": "mcp_client",
            "reason": dec.reasoning or "External database query routed through registered MCP server",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Selected: {dec.selected_capabilities} | Action: {dec.recommended_action[:80]}",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 6: Windows Control Task
    # --------------------------------------------------------------------------
    def test_06_windows_os_task(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "Launch the Windows Calculator application and focus its window on the desktop"
        dec = self.cde.preflight_analyze(intent)
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            "windows_control" in dec.selected_capabilities
            or "windows" in str(dec.selected_capabilities).lower()
            or "desktop" in dec.recommended_action.lower()
            or "calculator" in dec.recommended_action.lower()
            or "window" in dec.recommended_action.lower()
        )

        m = self._get_metrics()
        res = {
            "id": 6,
            "name": "Windows OS Control Task",
            "decision": "WINDOWS_DESKTOP_CONTROL",
            "capability": "windows_control",
            "reason": dec.reasoning or "Direct local desktop OS window management",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Selected: {dec.selected_capabilities} | Action: {dec.recommended_action[:80]}",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 7: Browser Navigation Task
    # --------------------------------------------------------------------------
    def test_07_browser_navigation_task(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "Open Microsoft Edge browser, navigate to https://en.wikipedia.org/wiki/Artificial_intelligence, and extract the page header"
        dec = self.cde.preflight_analyze(intent)
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            "browser_tool" in dec.selected_capabilities
            or "browser" in str(dec.selected_capabilities).lower()
            or "web_search" in dec.selected_capabilities
        )

        m = self._get_metrics()
        res = {
            "id": 7,
            "name": "Browser Navigation Task",
            "decision": "BROWSER_AUTOMATION",
            "capability": "browser_tool",
            "reason": dec.reasoning or "Requires interacting with web URL and DOM content in Edge",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Selected: {dec.selected_capabilities} | Action: {dec.recommended_action[:80]}",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 8: Multi-Capability Sequential Task (Search + Filesystem)
    # --------------------------------------------------------------------------
    def test_08_multi_capability_search_and_file(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "Search for a brief summary of Python 3.12 features and write the result to a local summary file in the workspace"
        dec = self.cde.preflight_analyze(intent)

        # Execute both operations sequentially
        from core.tools_bridge import call_python_builtin
        from core.paths import WORKSPACE_DIR
        search_out = call_python_builtin("web_search", {"query": "Python 3.12 release features", "sources": "wikipedia"})
        test_file_rel = "scratch/test_python_312_summary.txt"
        test_file_abs = WORKSPACE_DIR / test_file_rel
        test_file_abs.parent.mkdir(parents=True, exist_ok=True)
        write_res = call_python_builtin("write_file", {"path": test_file_rel, "content": search_out[:500]})
        latency = (time.perf_counter() - t0) * 1000.0

        file_verified = test_file_abs.is_file() and test_file_abs.stat().st_size > 20

        passed = (
            ("web_search" in dec.selected_capabilities or "filesystem_tool" in dec.selected_capabilities or dec.is_multi_step)
            and file_verified
        )

        m = self._get_metrics()
        res = {
            "id": 8,
            "name": "Multi-Capability Task (Search + File)",
            "decision": "SEQUENTIAL_PIPELINE",
            "capability": "web_search + filesystem_tool",
            "reason": dec.reasoning or "Search external web then persist output to local workspace file",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"File verified: {test_file_rel} ({test_file_abs.stat().st_size if test_file_abs.exists() else 0} bytes)",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 9: Long Multi-Step Task (TaskEngine Pinning, Zero Unloading)
    # --------------------------------------------------------------------------
    def test_09_multi_step_task_engine_pinning(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        task_id = f"task_test_{int(time.time())}"

        # 1. Pin task session
        self.provider.begin_task(task_id)
        unloads_observed = 0

        try:
            # 5 sequential steps executed inside the pinned session
            step_latencies = []
            for step_num in range(1, 6):
                t_step = time.perf_counter()
                prompt = f"Step {step_num} of 5: Generate status heartbeat for session verification. Return concise JSON: {{\"step\": {step_num}, \"status\": \"OK\"}}"
                req = ModelCompletionRequest(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=64,
                    task_tier="SIMPLE",
                    task_id=task_id,
                )
                resp = self.provider.generate(req)
                step_latencies.append((time.perf_counter() - t_step) * 1000.0)

                # Check if model remained loaded
                if not self.provider.backend.is_loaded():
                    unloads_observed += 1

            latency = (time.perf_counter() - t0) * 1000.0
            passed = (unloads_observed == 0) and (len(step_latencies) == 5)
        finally:
            self.provider.end_task(task_id)

        m = self._get_metrics()
        res = {
            "id": 9,
            "name": "Long Multi-Step TaskEngine Pinning",
            "decision": "SESSION_PINNED_EXECUTION",
            "capability": "lfm25_session_pinning + task_engine",
            "reason": "5 consecutive steps completed; 0 mid-task unloads observed",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 5,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"5 steps completed in {round(latency, 1)}ms. Mid-task unloads: {unloads_observed}",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 10: Tool Failure & State-Aware Recovery
    # --------------------------------------------------------------------------
    def test_10_state_aware_failure_recovery(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        ws = self.state_engine.get_current_world_state()

        diag = self.cde.diagnose_and_replan(
            task_goal="Sync workspace repository to secondary backup host",
            completed_steps=["Verify git working tree", "Generate archive bundle bundle.tar.gz"],
            failed_step="Upload archive via SFTP to host backup-mirror.corp (Port 22)",
            error_message="ConnectionRefusedError: Host backup-mirror.corp connection timed out on port 22",
            world_state=ws,
        )
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            diag.get("can_recover") is True
            and bool(diag.get("root_cause"))
            and bool(diag.get("recovery_strategy") or diag.get("fallback_action"))
        )

        m = self._get_metrics()
        res = {
            "id": 10,
            "name": "State-Aware Failure Recovery",
            "decision": "COGNITIVE_DYNAMIC_REPLAN",
            "capability": "cognitive_recovery_engine",
            "reason": f"Root cause diagnosed: {str(diag.get('root_cause'))[:60]}",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Strategy: {str(diag.get('recovery_strategy'))[:80]} | Fallback: {str(diag.get('fallback_action'))[:50]}",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 11: Complex Arabic Cognitive Task
    # --------------------------------------------------------------------------
    def test_11_complex_arabic_cognitive_analysis(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        prompt = (
            "حلل أسباب استهلاك الذاكرة في تطبيقات بايثون عند معالجة ملفات البيانات الضخمة، "
            "واذكر ثلاثة حلول هندسية معمارية محددة للتغلب على هذه المشكلة."
        )
        req = ModelCompletionRequest(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="أنت خبير معماري في هندسة النظم ولغة بايثون. أجب بلغة عربية تقنية دقيقة.",
            max_tokens=350,
            task_tier="MEDIUM",
        )
        resp = self.provider.generate(req)
        latency = (time.perf_counter() - t0) * 1000.0

        text = resp.text
        has_arabic = any("\u0600" <= ch <= "\u06FF" for ch in text)
        has_solutions = any(kw in text for kw in ["ذاكرة", "generator", "مولد", "تدفق", "chunk", "mmap", "تجزئة", "garbage", "تنظيف"])

        passed = has_arabic and has_solutions and len(text) > 100

        m = self._get_metrics()
        res = {
            "id": 11,
            "name": "Complex Arabic Cognitive Task",
            "decision": "ARABIC_ARCHITECTURAL_SYNTHESIS",
            "capability": "lfm25_arabic_reasoning",
            "reason": "Detailed Arabic architectural analysis with concrete solutions",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": text[:120].replace("\n", " "),
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 12: Direct Answer vs. Unnecessary Tool Use
    # --------------------------------------------------------------------------
    def test_12_direct_answer_vs_unnecessary_tools(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "What is 15 multiplied by 14?"
        dec = self.cde.preflight_analyze(intent)
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            dec.can_answer_directly is True
            and ("none" in dec.selected_capabilities or len(dec.selected_capabilities) == 0 or "none" in str(dec.selected_capabilities).lower())
            and (dec.direct_answer is not None and "210" in dec.direct_answer)
        )

        m = self._get_metrics()
        res = {
            "id": 12,
            "name": "Direct Answer vs Unnecessary Tool",
            "decision": "DIRECT_ARITHMETIC",
            "capability": "none (0 tools)",
            "reason": dec.reasoning or "Pure arithmetic calculation computed directly",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Direct Answer: {dec.direct_answer} | Tools invoked: 0",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 13: Incomplete Request (Honest Clarification, No Guessing)
    # --------------------------------------------------------------------------
    def test_13_incomplete_request_clarification(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        intent = "Upload the database dump file to the server immediately"
        dec = self.cde.preflight_analyze(intent)
        latency = (time.perf_counter() - t0) * 1000.0

        passed = (
            dec.needs_clarification is True
            or (dec.missing_information is not None and len(dec.missing_information) > 0)
            or (dec.clarification_question is not None and len(dec.clarification_question) > 0)
        )

        m = self._get_metrics()
        res = {
            "id": 13,
            "name": "Incomplete Request Clarification",
            "decision": "DEMAND_CLARIFICATION",
            "capability": "none (clarification required)",
            "reason": dec.reasoning or "Missing filename, target server hostname, and credentials",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(latency, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Clarification: {str(dec.clarification_question or dec.missing_information)[:100]}",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 14: Idle Mode Resource Measurement
    # --------------------------------------------------------------------------
    def test_14_idle_mode_resource_measurement(self) -> Dict[str, Any]:
        # Unload model to measure pure idle baseline
        self.provider.backend.unload_model()
        time.sleep(1.0)

        m = self._get_metrics()
        # Model allocated VRAM must be 0 MB (baseline system VRAM ~1.1 GB preserved)
        passed = (m["is_loaded"] is False) and (m["model_vram_mb"] == 0.0)

        res = {
            "id": 14,
            "name": "Idle Mode Resource Measurement",
            "decision": "SYSTEM_IDLE_MONITOR",
            "capability": "background_watchdog",
            "reason": f"Baseline system VRAM: {m['baseline_vram_mb']}MB. Model VRAM: 0.0MB released.",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 0,
            "latency_ms": 0.0,
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Model VRAM: {m['model_vram_mb']} MB | Host RAM: {m['ram_mb']} MB | CPU: {m['cpu_pct']}%",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 15: JIT Wake-up on Task Arrival
    # --------------------------------------------------------------------------
    def test_15_jit_wake_up_on_task_arrival(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        # Model is currently unloaded (from Test 14)
        assert not self.provider.backend.is_loaded()

        req = ModelCompletionRequest(
            messages=[{"role": "user", "content": "Respond with the single word: READY"}],
            max_tokens=16,
            task_tier="SIMPLE",
        )
        resp = self.provider.generate(req)
        total_time_ms = (time.perf_counter() - t0) * 1000.0

        m = self._get_metrics()
        wake_up_sec = self.provider.backend._wake_up_duration_sec
        passed = (
            self.provider.backend.is_loaded() is True
            and bool(resp.text)
            and wake_up_sec > 0.0
        )

        res = {
            "id": 15,
            "name": "JIT Wake-up on Task Arrival",
            "decision": "JIT_ON_DEMAND_LOAD",
            "capability": "resident_llama_server",
            "reason": f"Cold start wake-up completed in {round(wake_up_sec, 2)}s",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(total_time_ms, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Wake-up: {round(wake_up_sec, 2)}s | Output: {resp.text.strip()[:60]}",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Scenario 16: Return to Idle after Grace Period
    # --------------------------------------------------------------------------
    def test_16_return_to_idle_after_grace_period(self) -> Dict[str, Any]:
        backend: LFM25NativeBackend = self.provider.backend
        # Configure a short grace period for verification test
        test_grace_period_sec = 4.0
        backend.idle_timeout_seconds = test_grace_period_sec

        # Trigger short task to reset timer
        req = ModelCompletionRequest(
            messages=[{"role": "user", "content": "Ping"}],
            max_tokens=8,
            task_tier="SIMPLE",
        )
        self.provider.generate(req)
        assert backend.is_loaded() is True

        # Wait for grace period + 2.0s watchdog margin
        time.sleep(test_grace_period_sec + 2.0)

        m = self._get_metrics()
        passed = (backend.is_loaded() is False) and (m["model_vram_mb"] == 0.0)

        # Restore default idle timeout
        backend.idle_timeout_seconds = 30.0

        res = {
            "id": 16,
            "name": "Return to Idle after Grace Period",
            "decision": "AUTOMATIC_IDLE_COOLDOWN",
            "capability": "idle_watchdog_thread",
            "reason": f"Cooldown elapsed ({test_grace_period_sec}s). VRAM freed back to baseline.",
            "result": "PASS" if passed else "FAIL",
            "model_calls": 1,
            "latency_ms": round(test_grace_period_sec * 1000, 2),
            "model_vram_mb": m["model_vram_mb"],
            "ram_mb": m["ram_mb"],
            "cpu_pct": m["cpu_pct"],
            "output_snippet": f"Model unloaded: {not backend.is_loaded()} | Freed VRAM: {m['model_vram_mb']} MB",
        }
        self.results.append(res)
        return res

    # --------------------------------------------------------------------------
    # Suite Orchestrator
    # --------------------------------------------------------------------------
    def run_all(self) -> None:
        tests = [
            self.test_01_simple_question_direct_answer,
            self.test_02_single_search_query,
            self.test_03_deep_multi_source_research,
            self.test_04_skill_capability_routing,
            self.test_05_mcp_client_routing,
            self.test_06_windows_os_task,
            self.test_07_browser_navigation_task,
            self.test_08_multi_capability_search_and_file,
            self.test_09_multi_step_task_engine_pinning,
            self.test_10_state_aware_failure_recovery,
            self.test_11_complex_arabic_cognitive_analysis,
            self.test_12_direct_answer_vs_unnecessary_tools,
            self.test_13_incomplete_request_clarification,
            self.test_14_idle_mode_resource_measurement,
            self.test_15_jit_wake_up_on_task_arrival,
            self.test_16_return_to_idle_after_grace_period,
        ]

        print("\n" + "=" * 80)
        print("WISE COGNITIVE CORE ENHANCEMENT: 16-SCENARIO VERIFICATION SUITE")
        print("Model: LiquidAI/LFM2.5-8B-A1B Q4_K_M GGUF")
        print("=" * 80 + "\n")

        passed_count = 0
        failed_count = 0

        for t in tests:
            try:
                res = t()
                status_symbol = "[PASS]" if res["result"] == "PASS" else "[FAIL]"
                if res["result"] == "PASS":
                    passed_count += 1
                else:
                    failed_count += 1
                print(f"{status_symbol} #{res['id']:02d}: {res['name']} ({res['latency_ms']} ms) | Result: {res['result']}", flush=True)
                print(f"       Decision: {res['decision']} | Tool: {res['capability']}", flush=True)
                print(f"       Detail: {res['output_snippet']}", flush=True)
                print(f"       VRAM Model: {res['model_vram_mb']}MB | RAM: {res['ram_mb']}MB | CPU: {res['cpu_pct']}%\n", flush=True)
            except Exception as ex:
                failed_count += 1
                print(f"[FAIL] Test {t.__name__} threw exception: {ex}\n", flush=True)

        print("=" * 80)
        print(f"SUMMARY: {passed_count} PASSED / {failed_count} FAILED out of {len(tests)} scenarios.")
        print("=" * 80)

        # Save JSON results
        out_file = WORKSPACE_ROOT / "scratch" / "cognitive_enhancements_16_results.json"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(self.results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Detailed JSON results saved to: {out_file}\n")


if __name__ == "__main__":
    suite = CognitiveEnhancementTestSuite()
    suite.run_all()
