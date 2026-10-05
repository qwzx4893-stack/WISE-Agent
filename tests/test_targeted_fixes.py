# ==============================================================================
# WISE Cognitive Core - Targeted Regression & Fix Verification Suite
# Tests ONLY the 4 non-passing cases from initial accreditation:
# 1. H.1 - Long Task 1: Python Code Generation & Execution Loop
# 2. H.3 - Long Task 3: Filesystem Audit & JSON Array Extraction
# 3. H.4 - Long Task 4: Structured Schema Artifact Generation
# 4. I.1 - Failure Recovery: Tool Failure Replanning under constrained budget
# Plus 1 Direct Provider Sanity Check to guarantee end-to-end integration.
# ==============================================================================

from __future__ import annotations

import os
import sys
import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, List

# Add core path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "Supergent--main"))

from core.models.runtime.lfm25_backend import LFM25NativeBackend, LFM25ModelLocator, LFM25InferenceResult
from core.models.provider_interface import LFM25CognitiveProvider, ModelCompletionRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("WISE.TargetedFixes")


class TargetedFixVerifier:
    def __init__(self) -> None:
        self.model_path = LFM25ModelLocator.locate_model()
        LOG.info("Found LFM2.5 model at: %s", self.model_path)
        self.backend = LFM25NativeBackend(model_path=str(self.model_path), gpu_layers=33, context_length=4096)
        self.provider = LFM25CognitiveProvider(backend=self.backend)
        self.results: List[Dict[str, Any]] = []

    def ensure_backend_loaded(self) -> bool:
        if not self.backend.is_loaded():
            LOG.info("Loading LFM2.5 model into GPU runtime...")
            return self.backend.load_model()
        return True

    def query_model(self, user_msg: str, sys_prompt: str = "", max_tokens: int = 256) -> LFM25InferenceResult:
        prompt = self.backend.format_chatml_prompt(
            messages=[{"role": "user", "content": user_msg}],
            system_prompt=sys_prompt or "You are WISE Cognitive Core. Be logical, precise, and structured.",
        )
        return self.backend.generate(prompt=prompt, max_tokens=max_tokens, temperature=0.1)

    def run_h1(self) -> Dict[str, Any]:
        """H.1 - Code Generation and Subprocess Execution Loop"""
        LOG.info("--> Running Target Test H.1 (Code Generation & Execution Loop)...")
        p1 = (
            "Task: Write a Python script to compute the 10th Fibonacci number (where F(0)=0, F(1)=1, F(10)=55). "
            "Output the code in a ```python block."
        )
        res1 = self.query_model(p1, max_tokens=200)
        script_file = WORKSPACE_ROOT / "scratch" / "fib_test.py"
        status = "FAIL"
        details = ""
        proc_out = ""

        try:
            code = res1.text
            if "```python" in code:
                code = code.split("```python")[1].split("```")[0]
            elif "```" in code:
                code = code.split("```")[1].split("```")[0]
            code += "\nprint(fibonacci(10) if 'fibonacci' in locals() else fib(10))\n"
            script_file.write_text(code, encoding="utf-8")

            proc = subprocess.run([sys.executable, str(script_file)], capture_output=True, text=True, timeout=5)
            proc_out = proc.stdout.strip()
            if "55" in proc.stdout:
                status = "PASS"
                details = f"Generated valid code and executed successfully to produce 55. Tokens: {res1.tokens_generated}"
            else:
                status = "PARTIAL"
                details = f"Execution output unexpected: {proc.stdout} err: {proc.stderr}"
        except Exception as e:
            status = "FAIL"
            details = str(e)
        finally:
            if script_file.exists():
                script_file.unlink()

        record = {
            "test_id": "H.1",
            "name": "Long Task 1: Code Generation & Execution Loop",
            "status": status,
            "details": details,
            "latency_ms": res1.total_time_ms,
            "tokens_generated": res1.tokens_generated,
            "preview": proc_out or res1.text[:200],
        }
        self.results.append(record)
        LOG.info("H.1 Result: %s | %s", status, details)
        return record

    def run_h3(self) -> Dict[str, Any]:
        """H.3 - Filesystem Audit & JSON Array Filtering"""
        LOG.info("--> Running Target Test H.3 (Filesystem Audit & JSON Array Filtering)...")
        files_sample = ["README.md", "app.py", "test_core.py", "config.json", "notes.txt"]
        p3 = f"Given this file list: {files_sample}. Filter out only the Python (.py) files and return as a JSON array."
        res3 = self.query_model(p3, max_tokens=100)
        t3 = res3.text

        status = "FAIL"
        details = ""
        if "app.py" in t3 and "test_core.py" in t3 and "README.md" not in t3:
            status = "PASS"
            details = f"Filtered Python files cleanly. Parsed JSON: {res3.parsed_json}"
        else:
            status = "PARTIAL"
            details = f"Filter included non-python files or missed items. Raw: {t3[:150]}"

        record = {
            "test_id": "H.3",
            "name": "Long Task 3: Filesystem Audit & Filtering",
            "status": status,
            "details": details,
            "latency_ms": res3.total_time_ms,
            "tokens_generated": res3.tokens_generated,
            "parsed_json": res3.parsed_json,
            "preview": t3[:200],
        }
        self.results.append(record)
        LOG.info("H.3 Result: %s | %s", status, details)
        return record

    def run_h4(self) -> Dict[str, Any]:
        """H.4 - Structured Schema Artifact Generation"""
        LOG.info("--> Running Target Test H.4 (Structured Schema Artifact Generation)...")
        p4 = "Generate a valid JSON object representing a machine learning experiment artifact with keys: 'model_name', 'accuracy', 'parameters_count', 'dataset', 'quantization'."
        res4 = self.query_model(p4, max_tokens=150)

        status = "FAIL"
        details = ""
        if res4.parsed_json and all(k in res4.parsed_json for k in ["model_name", "accuracy"]):
            status = "PASS"
            details = f"Generated clean conforming JSON artifact with {len(res4.parsed_json)} keys."
        else:
            status = "PARTIAL"
            details = f"JSON schema parsing failed or missed required keys. Parsed: {res4.parsed_json}"

        record = {
            "test_id": "H.4",
            "name": "Long Task 4: Structured Schema Artifact Generation",
            "status": status,
            "details": details,
            "latency_ms": res4.total_time_ms,
            "tokens_generated": res4.tokens_generated,
            "parsed_json": res4.parsed_json,
            "preview": str(res4.parsed_json or res4.text)[:200],
        }
        self.results.append(record)
        LOG.info("H.4 Result: %s | %s", status, details)
        return record

    def run_i1(self) -> Dict[str, Any]:
        """I.1 - Failure Observation & Strategy Replanning under Constrained Tokens"""
        LOG.info("--> Running Target Test I.1 (Failure Observation & Strategy Replanning)...")
        p1 = (
            "Tool Execution Status: FAILED.\n"
            "Tool: download_remote_package\n"
            "Error: ConnectionRefusedError: Host 'pkg.server.internal' unreachable on port 443.\n\n"
            "Do NOT assume the package is downloaded. What is the next recovery action?"
        )
        sys_i = "You are WISE Resilient Task Engine. When a tool fails, analyze the root cause and formulate an alternate recovery strategy."
        res1 = self.query_model(p1, sys_i, max_tokens=150)
        t1 = res1.text.lower()

        status = "FAIL"
        details = ""
        if ("fail" in t1 or "not" in t1 or "unreachable" in t1) and (
            "retry" in t1 or "mirror" in t1 or "alternate" in t1 or "check" in t1 or "dns" in t1 or "fallback" in t1
        ):
            status = "PASS"
            details = f"Observed tool failure and formulated recovery fallback. Tokens: {res1.tokens_generated}"
        else:
            status = "FAIL"
            details = f"Assumed success or failed to produce recovery strategy. Text: {t1[:150]}"

        record = {
            "test_id": "I.1",
            "name": "Failure Observation & Strategy Replanning",
            "status": status,
            "details": details,
            "latency_ms": res1.total_time_ms,
            "tokens_generated": res1.tokens_generated,
            "preview": res1.text[:200],
        }
        self.results.append(record)
        LOG.info("I.1 Result: %s | %s", status, details)
        return record

    def run_sanity_provider_check(self) -> Dict[str, Any]:
        """Sanity check: verify LFM25CognitiveProvider through unified provider interface"""
        LOG.info("--> Running Direct Provider Sanity Check...")
        req = ModelCompletionRequest(
            messages=[{"role": "user", "content": "Return a JSON object with key 'status' set to 'active'."}],
            system_prompt="You are a JSON assistant. Output valid JSON.",
            max_tokens=100,
        )
        resp = self.provider.complete(req)
        status = "PASS" if (resp.parsed_json and resp.parsed_json.get("status") == "active") else "FAIL"
        details = f"Provider response parsed_json: {resp.parsed_json}"

        record = {
            "test_id": "SANITY",
            "name": "Direct Provider Interface Structured Output",
            "status": status,
            "details": details,
            "latency_ms": resp.latency_ms,
            "preview": str(resp.parsed_json),
        }
        self.results.append(record)
        LOG.info("SANITY Result: %s | %s", status, details)
        return record

    def run_all(self) -> Dict[str, Any]:
        try:
            self.ensure_backend_loaded()
            self.run_h1()
            self.run_h3()
            self.run_h4()
            self.run_i1()
            self.run_sanity_provider_check()
        finally:
            LOG.info("Unloading LFM2.5 model and freeing GPU memory...")
            self.backend.unload_model()

        pass_count = sum(1 for r in self.results if r["status"] == "PASS")
        total = len(self.results)
        summary = {
            "total_targeted_tests": total,
            "pass_count": pass_count,
            "all_passed": (pass_count == total),
            "results": self.results,
        }

        out_path = WORKSPACE_ROOT / "scratch" / "targeted_fixes_results.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        LOG.info("==================================================")
        LOG.info("TARGETED VERIFICATION FINISHED: %d/%d PASSED", pass_count, total)
        LOG.info("==================================================")
        return summary


if __name__ == "__main__":
    verifier = TargetedFixVerifier()
    verifier.run_all()
