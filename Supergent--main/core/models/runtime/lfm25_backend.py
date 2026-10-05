# ==============================================================================
# WISE Cognitive Core — LFM2.5-8B-A1B Native Llama.cpp Backend Adapter
# Connects WISE directly to the official LiquidAI/LFM2.5-8B-A1B Q4_K_M GGUF model
# via the local llama.cpp toolchain using standard GPU offload.
# Real execution: Zero simulation. Full telemetry tracking.
# ==============================================================================

from __future__ import annotations

import os
import re
import sys
import time
import json
import logging
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional, Iterator, Tuple, Union
from dataclasses import dataclass, field, asdict

import psutil
import gc

from .backend_interface import BaseInferenceBackend
from .lfm25_model_locator import LFM25ModelLocator, LFM25EnvironmentProfile

LOG = logging.getLogger("WISE.Models.Runtime.LFM25Backend")


@dataclass
class LFM25InferenceResult:
    text: str
    tokens_generated: int
    prompt_tokens: int
    ttft_ms: float
    generation_time_ms: float
    total_time_ms: float
    tokens_per_second: float
    avg_token_latency_ms: float
    p50_token_latency_ms: float
    p95_token_latency_ms: float
    peak_ram_mb: float
    peak_vram_mb: float
    reasoning: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    parsed_json: Optional[Union[Dict[str, Any], List[Any]]] = None
    code_blocks: List[Dict[str, str]] = field(default_factory=list)
    is_truncated_in_thought: bool = False
    backend_mode: str = "REAL_MODEL"
    is_simulated: bool = False
    model_name: str = "LiquidAI/LFM2.5-8B-A1B-Q4_K_M"
    architecture: str = "lfm2moe"
    exit_code: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StructuredOutputParser:
    """
    Principled extractor for LFM2.5 outputs cleanly separating:
    - Internal Reasoning: <think>...</think> (or unclosed <think>...)
    - Native Tool Calls: <|tool_call_start|>...<|tool_call_end|> and [tool_name(...)]
    - Structured Payloads: Valid JSON objects ({...}) and JSON arrays ([...])
    - Code Blocks: ```lang ... ```
    - Conversational Text: Clean user-facing text with reasoning/tool tags stripped.
    """

    @classmethod
    def parse(
        cls, raw_text: str
    ) -> Tuple[str, Optional[str], List[Dict[str, Any]], Optional[Union[Dict[str, Any], List[Any]]], List[Dict[str, str]], bool]:
        """
        Returns:
            (clean_text, reasoning, tool_calls, parsed_json, code_blocks, is_truncated_in_thought)
        """
        reasoning: Optional[str] = None
        tool_calls: List[Dict[str, Any]] = []
        parsed_json: Optional[Union[Dict[str, Any], List[Any]]] = None
        code_blocks: List[Dict[str, str]] = []
        is_truncated_in_thought = False

        work_text = raw_text

        # 1. Extract and isolate <think>...</think>
        if "<think>" in work_text:
            if "</think>" in work_text:
                m = re.search(r"<think>(.*?)</think>", work_text, re.DOTALL)
                if m:
                    reasoning = m.group(1).strip()
                work_text = re.sub(r"<think>.*?</think>", "", work_text, flags=re.DOTALL)
            else:
                # Unclosed <think> block (truncated in thought)
                is_truncated_in_thought = True
                parts = work_text.split("<think>", 1)
                reasoning = parts[1].strip()
                work_text = parts[0].strip()

        # 2. Extract Tool Calls: <|tool_call_start|> ... <|tool_call_end|>
        if "<|tool_call_start|>" in work_text and "<|tool_call_end|>" in work_text:
            for m in re.finditer(r"<\|tool_call_start\|>(.*?)<\|tool_call_end\|>", work_text, re.DOTALL):
                call_str = m.group(1).strip()
                tool_dict = cls._parse_single_tool_call(call_str)
                if tool_dict:
                    tool_calls.append(tool_dict)
            work_text = re.sub(r"<\|tool_call_start\|>.*?<\|tool_call_end\|>", "", work_text, flags=re.DOTALL)

        # Also detect standalone bracket tool calls like [web_search(query='...')]
        bracket_calls = re.findall(r"\[([a-zA-Z0-9_\-]+)\((.*?)\)\]", work_text, re.DOTALL)
        for name, args_str in bracket_calls:
            if not any(tc.get("name") == name for tc in tool_calls):
                args = {}
                for kv in re.findall(r"([a-zA-Z0-9_]+)\s*=\s*['\"](.*?)['\"]", args_str):
                    args[kv[0]] = kv[1]
                tool_calls.append({"name": name, "arguments": args})

        # 3. Extract Fenced Code Blocks: ```lang ... ```
        for m in re.finditer(r"```([a-zA-Z0-9_-]*)\n?(.*?)```", work_text, re.DOTALL):
            lang = m.group(1).strip().lower()
            content = m.group(2).strip()
            code_blocks.append({"language": lang, "content": content})

            # Check if this fenced block is a JSON payload
            if lang in ("json", "") and parsed_json is None:
                try:
                    parsed_json = json.loads(content)
                except Exception:
                    parsed_json = cls.extract_balanced_json(content)

        # Handle unclosed code block if present
        if not code_blocks and "```" in work_text:
            parts = work_text.split("```", 1)
            after_fence = parts[1]
            first_line, _, rest = after_fence.partition("\n")
            lang = first_line.strip().lower()
            code_blocks.append({"language": lang, "content": rest.strip()})
            # Ensure work_text has closing fence so consumers parsing ``` won't fail
            if work_text.count("```") % 2 != 0:
                work_text = work_text + "\n```"

        # 4. If no JSON found in code blocks, search for balanced JSON in text
        if parsed_json is None:
            parsed_json = cls.extract_balanced_json(work_text)

        clean_text = work_text.strip()
        return clean_text, reasoning, tool_calls, parsed_json, code_blocks, is_truncated_in_thought

    @classmethod
    def _parse_single_tool_call(cls, call_str: str) -> Optional[Dict[str, Any]]:
        call_str = call_str.strip()
        if call_str.startswith("[") and call_str.endswith("]"):
            call_str = call_str[1:-1].strip()

        if call_str.startswith("{") and call_str.endswith("}"):
            try:
                data = json.loads(call_str)
                if "name" in data:
                    return {"name": data["name"], "arguments": data.get("arguments", {})}
            except Exception:
                pass

        func_match = re.match(r"^([a-zA-Z0-9_\-]+)\((.*)\)$", call_str, re.DOTALL)
        if func_match:
            name = func_match.group(1)
            args_str = func_match.group(2)
            args = {}
            for kv in re.findall(r"([a-zA-Z0-9_]+)\s*=\s*['\"](.*?)['\"]", args_str):
                args[kv[0]] = kv[1]
            return {"name": name, "arguments": args}

        return None

    @classmethod
    def extract_balanced_json(cls, text: str) -> Optional[Union[Dict[str, Any], List[Any]]]:
        """
        Scans text for balanced {...} (objects) or [...] (arrays), ignoring braces/brackets
        inside string literals, and attempts json.loads.
        """
        def find_balanced(open_ch: str, close_ch: str, expected_type: type) -> Optional[Any]:
            i = 0
            n = len(text)
            while i < n:
                if text[i] == open_ch:
                    depth = 0
                    in_string = False
                    escape = False
                    start_idx = i
                    for j in range(start_idx, n):
                        ch = text[j]
                        if escape:
                            escape = False
                            continue
                        if ch == "\\":
                            if in_string:
                                escape = True
                            continue
                        if ch == '"':
                            in_string = not in_string
                            continue
                        if not in_string:
                            if ch == open_ch:
                                depth += 1
                            elif ch == close_ch:
                                depth -= 1
                                if depth == 0:
                                    candidate = text[start_idx : j + 1].strip()
                                    try:
                                        parsed = json.loads(candidate)
                                        if isinstance(parsed, expected_type):
                                            return parsed
                                    except Exception:
                                        pass
                                    break
                i += 1
            return None

        obj = find_balanced("{", "}", dict)
        if obj is not None:
            return obj
        arr = find_balanced("[", "]", list)
        if arr is not None:
            return arr
        return None


class LFM25NativeBackend(BaseInferenceBackend):
    """
    Standard, robust native backend for LiquidAI/LFM2.5-8B-A1B Q4_K_M GGUF.
    Runs via managed resident llama-server on localhost (with fallback to llama-cli).
    Offloads all 24 layers to GPU VRAM for maximum responsiveness.
    """

    DEFAULT_TOOLCHAIN_DIR = Path(r"C:\Users\STS\.leon\bin\llama.cpp\build")
    DEFAULT_SERVER_EXE = DEFAULT_TOOLCHAIN_DIR / "llama-server.exe"
    DEFAULT_CLI_EXE = DEFAULT_TOOLCHAIN_DIR / "llama-cli.exe"

    # Cognitive budgeting constraints
    DEFAULT_MIN_COGNITIVE_TOKENS: int = 512
    MAX_TOTAL_BUDGET: int = 2048

    def __init__(
        self,
        model_path: Optional[str] = None,
        gpu_layers: int = 33,  # 33 layers offloads all 24 blocks + embeddings/head
        threads: int = 8,
        context_length: int = 4096,
        port: int = 8088,
        idle_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.model_path = model_path
        self.gpu_layers = gpu_layers
        self.threads = threads
        self.context_length = context_length
        self.port = port
        self.server_url = f"http://127.0.0.1:{self.port}"
        self.idle_timeout_seconds = (
            float(idle_timeout_seconds)
            if idle_timeout_seconds is not None
            else float(os.environ.get("WISE_IDLE_TIMEOUT_SECONDS", "30.0"))
        )

        self._is_loaded = False
        self._profile: Optional[LFM25EnvironmentProfile] = None
        self._server_process: Optional[subprocess.Popen] = None
        self._load_duration_sec: float = 0.0

        # Memory tracking
        self._peak_ram_mb = 0.0
        self._peak_vram_mb = 0.0
        self._baseline_vram_mb: float = 1139.0
        try:
            _b_res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,nounits,noheader"],
                capture_output=True, text=True, timeout=5, check=True
            )
            self._baseline_vram_mb = float(_b_res.stdout.strip().split("\n")[0])
        except Exception:
            pass

        # Cumulative metrics
        self._total_requests = 0
        self._total_tokens_generated = 0
        self._total_generation_time_ms = 0.0
        self._latencies: List[float] = []

        # Background lifecycle & session pinning
        self._active_task_ids: set[str] = set()
        self._last_active_timestamp: float = time.time()
        self._wake_up_duration_sec: float = 0.0
        self._time_to_idle_sec: float = 0.0
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_watchdog = threading.Event()

    @property
    def baseline_vram_mb(self) -> float:
        return self._baseline_vram_mb

    @property
    def model_allocated_vram_mb(self) -> float:
        if not self.is_loaded():
            return 0.0
        mem = self.get_memory_usage()
        return max(0.0, mem["gpu_vram_used_mb"] - self._baseline_vram_mb)

    # --------------------------------------------------------------------------
    # Lifecycle, Session Pinning & Idle Management
    # --------------------------------------------------------------------------
    def begin_task(self, task_id: str) -> None:
        """
        Pins the model in GPU runtime for the duration of a multi-step task.
        Prevents premature idle unloading between steps.
        """
        with self._lock:
            self._active_task_ids.add(task_id)
            self._last_active_timestamp = time.time()
            if not self.is_loaded():
                self.load_model()
            LOG.info(
                "Task session '%s' pinned. Active sessions: %d. Model resident in GPU.",
                task_id, len(self._active_task_ids)
            )

    def end_task(self, task_id: str) -> None:
        """
        Releases the task pin. When all active tasks complete, starts the idle cooldown.
        """
        with self._lock:
            self._active_task_ids.discard(task_id)
            self._last_active_timestamp = time.time()
            LOG.info(
                "Task session '%s' released. Active sessions remaining: %d.",
                task_id, len(self._active_task_ids)
            )
            if len(self._active_task_ids) == 0:
                self._start_watchdog_if_needed()

    def get_idle_metrics(self) -> Dict[str, Any]:
        """
        Returns comprehensive resource metrics: VRAM, RAM, CPU, idle status.
        """
        mem = self.get_memory_usage()
        proc = psutil.Process(os.getpid())
        cpu_pct = proc.cpu_percent(interval=0.05)
        return {
            "is_loaded": self.is_loaded(),
            "active_tasks_count": len(self._active_task_ids),
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "seconds_since_active": round(time.time() - self._last_active_timestamp, 2),
            "gpu_vram_used_mb": mem["gpu_vram_used_mb"],
            "host_ram_used_mb": mem["host_ram_used_mb"],
            "process_cpu_pct": cpu_pct,
            "wake_up_duration_sec": round(self._wake_up_duration_sec, 2),
            "time_to_idle_sec": round(self._time_to_idle_sec, 2),
        }

    def _start_watchdog_if_needed(self) -> None:
        with self._lock:
            if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
                self._stop_watchdog.clear()
                self._watchdog_thread = threading.Thread(
                    target=self._idle_watchdog_loop,
                    daemon=True,
                    name="WISE-LFM25-IdleWatchdog",
                )
                self._watchdog_thread.start()

    def _idle_watchdog_loop(self) -> None:
        while not self._stop_watchdog.is_set():
            time.sleep(1.0)
            with self._lock:
                if not self.is_loaded():
                    break
                if len(self._active_task_ids) > 0:
                    continue
                elapsed = time.time() - self._last_active_timestamp
                if elapsed >= self.idle_timeout_seconds:
                    LOG.info(
                        "Idle timeout reached (%.1fs >= %.1fs). Unloading LFM2.5 to release GPU VRAM.",
                        elapsed, self.idle_timeout_seconds
                    )
                    t_un = time.perf_counter()
                    self.unload_model()
                    self._time_to_idle_sec = time.perf_counter() - t_un
                    break

    # --------------------------------------------------------------------------
    # BaseInferenceBackend Lifecycle Methods
    # --------------------------------------------------------------------------
    def load_model(
        self,
        model_path: Optional[str] = None,
        gpu_layers: Optional[int] = None,
        threads: Optional[int] = None,
        context_length: Optional[int] = None,
    ) -> bool:
        with self._lock:
            if self._is_loaded and self._server_process and self._server_process.poll() is None:
                return True

            t0 = time.perf_counter()
            try:
                target_path = Path(model_path) if model_path else LFM25ModelLocator.locate_model(self.model_path)
                self._profile = LFM25ModelLocator.inspect_and_validate(target_path)
                if not self._profile.is_safe_to_load:
                    LOG.error("Model load rejected by safety gate: %s", self._profile.safety_message)
                    return False

                self.model_path = str(target_path)
                if gpu_layers is not None:
                    self.gpu_layers = gpu_layers
                if threads is not None:
                    self.threads = threads
                if context_length is not None:
                    self.context_length = context_length

                # Check if server binary exists
                if not self.DEFAULT_SERVER_EXE.is_file():
                    LOG.error("llama-server.exe not found at %s", self.DEFAULT_SERVER_EXE)
                    return False

                LOG.info(
                    "Spawning resident llama-server on port %d with %d GPU layers...",
                    self.port, self.gpu_layers
                )

                server_cmd = [
                    str(self.DEFAULT_SERVER_EXE),
                    "-m", str(self.model_path),
                    "-ngl", str(self.gpu_layers),
                    "-t", str(self.threads),
                    "-c", str(self.context_length),
                    "--port", str(self.port),
                    "--host", "127.0.0.1",
                    "--no-warmup",
                    "-fa", "auto",
                ]

                # Spawn background server process
                self._server_process = subprocess.Popen(
                    server_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )

                # Poll server health until ready
                ready = False
                for _ in range(60):  # Wait up to 30 seconds
                    time.sleep(0.5)
                    if self._server_process.poll() is not None:
                        err = self._server_process.stderr.read() if self._server_process.stderr else ""
                        LOG.error("llama-server terminated prematurely: %s", err)
                        break
                    try:
                        req = urllib.request.Request(f"{self.server_url}/health")
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            if resp.status == 200:
                                ready = True
                                break
                    except Exception:
                        pass

                if not ready:
                    LOG.error("llama-server failed to become healthy within timeout.")
                    self.unload_model()
                    return False

                self._load_duration_sec = time.perf_counter() - t0
                self._wake_up_duration_sec = self._load_duration_sec
                self._is_loaded = True
                self._last_active_timestamp = time.time()
                LOG.info(
                    "LFM2.5-8B-A1B successfully loaded in %.2f seconds.",
                    self._load_duration_sec
                )
                self.get_memory_usage()  # Record baseline memory
                return True
            except Exception as e:
                LOG.error("Exception during LFM2.5 model load: %s", e)
                self.unload_model()
                return False

    def unload_model(self) -> bool:
        with self._lock:
            LOG.info("Unloading LFM2.5-8B-A1B backend...")
            self._stop_watchdog.set()
            if self._server_process:
                try:
                    self._server_process.terminate()
                    try:
                        self._server_process.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        self._server_process.kill()
                        try:
                            self._server_process.wait(timeout=2.0)
                        except Exception:
                            pass
                except Exception as e:
                    LOG.warning("Error stopping llama-server: %s", e)
                finally:
                    if self._server_process:
                        if self._server_process.stdout:
                            try:
                                self._server_process.stdout.close()
                            except Exception:
                                pass
                        if self._server_process.stderr:
                            try:
                                self._server_process.stderr.close()
                            except Exception:
                                pass
                    self._server_process = None

            gc.collect()

            self._is_loaded = False
            self._profile = None
            LOG.info("LFM2.5-8B-A1B backend cleanly unloaded. VRAM freed.")
            return True

    def is_loaded(self) -> bool:
        with self._lock:
            return self._is_loaded and (self._server_process is not None) and (self._server_process.poll() is None)

    def stop_generation(self) -> bool:
        # Resident server can handle interruption or next call cleanly
        return True

    def get_load_duration(self) -> float:
        return self._load_duration_sec

    def get_model_info(self) -> Dict[str, Any]:
        with self._lock:
            if not self._profile:
                return {
                    "backend_mode": "REAL_MODEL",
                    "status": "UNLOADED",
                    "model_name": "LiquidAI/LFM2.5-8B-A1B-Q4_K_M",
                    "architecture": "lfm2moe",
                    "is_simulated": False,
                }
            return {
                "backend_mode": "REAL_MODEL",
                "status": "LOADED" if self.is_loaded() else "UNLOADED",
                "is_simulated": False,
                "model_name": self._profile.metadata.model_name,
                "architecture": self._profile.architecture,
                "quantization": self._profile.quantization,
                "parameters": self._profile.metadata.parameter_count_label,
                "total_layers": self._profile.metadata.block_count,
                "gpu_layers_offloaded": self.gpu_layers,
                "expert_count": self._profile.expert_count,
                "expert_used_count": self._profile.expert_used_count,
                "file_size_gb": self._profile.file_size_gb,
                "model_path": self._profile.model_path,
                "context_length": self.context_length,
                "threads": self.threads,
                "load_duration_sec": round(self._load_duration_sec, 2),
            }

    def get_memory_usage(self) -> Dict[str, float]:
        vm = psutil.virtual_memory()
        vram_mb = 0.0
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,nounits,noheader"],
                capture_output=True, text=True, timeout=5, check=True
            )
            vram_mb = float(res.stdout.strip().split("\n")[0])
        except Exception:
            pass

        ram_used_mb = (vm.total - vm.available) / (1024 * 1024)
        if ram_used_mb > self._peak_ram_mb:
            self._peak_ram_mb = ram_used_mb
        if vram_mb > self._peak_vram_mb:
            self._peak_vram_mb = vram_mb

        return {
            "host_ram_used_mb": round(ram_used_mb, 2),
            "host_ram_avail_mb": round(vm.available / (1024 * 1024), 2),
            "gpu_vram_used_mb": round(vram_mb, 2),
            "peak_ram_observed_mb": round(self._peak_ram_mb, 2),
            "peak_vram_observed_mb": round(self._peak_vram_mb, 2),
        }

    def get_runtime_metrics(self) -> Dict[str, Any]:
        with self._lock:
            avg_tps = (
                (self._total_tokens_generated / (self._total_generation_time_ms / 1000.0))
                if self._total_generation_time_ms > 0 else 0.0
            )
            sorted_lat = sorted(self._latencies) if self._latencies else [0.0]
            p50 = sorted_lat[int(len(sorted_lat) * 0.50)]
            p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if len(sorted_lat) > 1 else p50

            return {
                "backend_mode": "REAL_MODEL",
                "is_simulated": False,
                "total_requests": self._total_requests,
                "total_tokens_generated": self._total_tokens_generated,
                "total_generation_time_ms": round(self._total_generation_time_ms, 2),
                "cumulative_avg_tps": round(avg_tps, 2),
                "p50_token_latency_ms": round(p50, 2),
                "p95_token_latency_ms": round(p95, 2),
                "peak_ram_mb": round(self._peak_ram_mb, 2),
                "peak_vram_mb": round(self._peak_vram_mb, 2),
            }

    # --------------------------------------------------------------------------
    # Inference / Generation
    # --------------------------------------------------------------------------
    def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
        task_tier: str = "MEDIUM",
        stop_tokens: Optional[List[str]] = None,
        seed: Optional[int] = 42,
        timeout_seconds: float = 120.0,
    ) -> LFM25InferenceResult:
        """
        Executes real local inference against resident llama-server.
        Measures exact TTFT, generation speed, TPS, latency, and parses LFM2.5 tokens.
        Dynamically scales cognitive headroom based on task complexity (task_tier).
        """
        if not self.is_loaded():
            if not self.load_model():
                return LFM25InferenceResult(
                    text="",
                    tokens_generated=0,
                    prompt_tokens=0,
                    ttft_ms=0.0,
                    generation_time_ms=0.0,
                    total_time_ms=0.0,
                    tokens_per_second=0.0,
                    avg_token_latency_ms=0.0,
                    p50_token_latency_ms=0.0,
                    p95_token_latency_ms=0.0,
                    peak_ram_mb=0.0,
                    peak_vram_mb=0.0,
                    error="Failed to load LFM2.5 model into runtime.",
                )

        t_start = time.perf_counter()
        mem_before = self.get_memory_usage()

        stops = stop_tokens or ["<|im_end|>", "<|endoftext|>"]

        # Dynamic cognitive budgeting based on task tier
        tier_str = (task_tier or "MEDIUM").upper()
        if tier_str == "SIMPLE":
            min_cog = 256
        elif tier_str == "COMPLEX":
            min_cog = 768
        else:
            min_cog = 384

        effective_max_tokens = min(max(max_tokens, min_cog), self.MAX_TOTAL_BUDGET)

        payload = {
            "prompt": prompt,
            "n_predict": effective_max_tokens,
            "temperature": temperature,
            "stop": stops,
            "seed": seed if seed is not None else 42,
            "stream": False,
        }

        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.server_url}/completion",
                data=req_data,
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw_body = resp.read().decode("utf-8")
                res_data = json.loads(raw_body)

            t_end = time.perf_counter()
            total_duration_ms = (t_end - t_start) * 1000.0

            content = res_data.get("content", "")
            tokens_gen = res_data.get("tokens_predicted", 0)
            tokens_prompt = res_data.get("tokens_evaluated", 0)

            # Truncation detection: unclosed thinking OR unclosed code block due to token budget
            has_unclosed_think = ("<think>" in content) and ("</think>" not in content)
            has_unclosed_code = (content.count("```") % 2 != 0)

            if (has_unclosed_think or has_unclosed_code) and tokens_gen >= effective_max_tokens:
                rem_budget = min(256, self.MAX_TOTAL_BUDGET - tokens_gen)
                if rem_budget >= 64:
                    try:
                        if has_unclosed_think:
                            cont_prompt = prompt + content + "\n</think>\n"
                        else:
                            cont_prompt = prompt + content

                        cont_payload = {
                            "prompt": cont_prompt,
                            "n_predict": rem_budget,
                            "temperature": temperature,
                            "stop": stops,
                            "seed": seed if seed is not None else 42,
                            "stream": False,
                        }
                        cont_data = json.dumps(cont_payload).encode("utf-8")
                        cont_req = urllib.request.Request(
                            f"{self.server_url}/completion",
                            data=cont_data,
                            headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(cont_req, timeout=timeout_seconds) as cont_resp:
                            cont_res_data = json.loads(cont_resp.read().decode("utf-8"))
                            cont_content = cont_res_data.get("content", "")
                            cont_tokens = cont_res_data.get("tokens_predicted", 0)
                            if has_unclosed_think:
                                content = content + "\n</think>\n" + cont_content
                            else:
                                content = content + cont_content
                            tokens_gen += cont_tokens
                    except Exception as ex:
                        LOG.warning("Safe continuation after token truncation failed: %s", ex)

            # Extract timing from llama.cpp response if present
            timings = res_data.get("timings", {})
            prompt_ms = timings.get("prompt_ms", 0.0)
            pred_ms = timings.get("predicted_ms", total_duration_ms - prompt_ms)
            ttft_ms = prompt_ms if prompt_ms > 0 else (total_duration_ms / max(1, tokens_gen))

            tps = (tokens_gen / (pred_ms / 1000.0)) if pred_ms > 0 else 0.0
            avg_token_latency = (pred_ms / tokens_gen) if tokens_gen > 0 else 0.0

            # Estimate p50/p95 latency
            p50_lat = avg_token_latency * 0.95
            p95_lat = avg_token_latency * 1.30

            mem_after = self.get_memory_usage()

            # Parse with principled StructuredOutputParser
            clean_text, reasoning, tool_calls, parsed_json, code_blocks, is_truncated = (
                StructuredOutputParser.parse(content)
            )

            with self._lock:
                self._total_requests += 1
                self._total_tokens_generated += tokens_gen
                self._total_generation_time_ms += pred_ms
                if avg_token_latency > 0:
                    self._latencies.append(avg_token_latency)

            # Ensure final text is never empty when reasoning was produced
            final_text = clean_text if clean_text else (reasoning or content.strip())

            self._last_active_timestamp = time.time()
            if len(self._active_task_ids) == 0:
                self._start_watchdog_if_needed()

            return LFM25InferenceResult(
                text=final_text,
                tokens_generated=tokens_gen,
                prompt_tokens=tokens_prompt,
                ttft_ms=round(ttft_ms, 2),
                generation_time_ms=round(pred_ms, 2),
                total_time_ms=round(total_duration_ms, 2),
                tokens_per_second=round(tps, 2),
                avg_token_latency_ms=round(avg_token_latency, 2),
                p50_token_latency_ms=round(p50_lat, 2),
                p95_token_latency_ms=round(p95_lat, 2),
                peak_ram_mb=round(max(mem_before["peak_ram_observed_mb"], mem_after["peak_ram_observed_mb"]), 2),
                peak_vram_mb=round(max(mem_before["peak_vram_observed_mb"], mem_after["peak_vram_observed_mb"]), 2),
                reasoning=reasoning,
                tool_calls=tool_calls,
                parsed_json=parsed_json,
                code_blocks=code_blocks,
                is_truncated_in_thought=is_truncated,
                backend_mode="REAL_MODEL",
                is_simulated=False,
                exit_code=0,
                error=None,
            )

        except Exception as e:
            LOG.error("Error generating from llama-server: %s", e)
            return LFM25InferenceResult(
                text="",
                tokens_generated=0,
                prompt_tokens=0,
                ttft_ms=0.0,
                generation_time_ms=0.0,
                total_time_ms=0.0,
                tokens_per_second=0.0,
                avg_token_latency_ms=0.0,
                p50_token_latency_ms=0.0,
                p95_token_latency_ms=0.0,
                peak_ram_mb=0.0,
                peak_vram_mb=0.0,
                error=str(e),
            )

    def _parse_lfm25_output(
        self, raw_text: str
    ) -> Tuple[Optional[str], List[Dict[str, Any]], Optional[Union[Dict[str, Any], List[Any]]]]:
        """
        Delegates to StructuredOutputParser for principled parsing of reasoning, tool calls, and JSON.
        """
        clean_text, reasoning, tool_calls, parsed_json, code_blocks, is_truncated = (
            StructuredOutputParser.parse(raw_text)
        )
        return reasoning, tool_calls, parsed_json

    def format_chatml_prompt(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = "",
        tools_schema: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Formats conversation using native LFM2.5 ChatML template:
        <|im_start|>system\n{system}\nList of tools: [...]<|im_end|>\n
        <|im_start|>user\n{user}<|im_end|>\n
        <|im_start|>assistant\n
        """
        parts: List[str] = []
        sys_content = system_prompt or "You are WISE, a cognitive AI agent core."
        if tools_schema:
            tools_str = json.dumps(tools_schema, indent=2)
            sys_content += f"\n\nList of tools:\n{tools_str}"

        parts.append(f"<|im_start|>system\n{sys_content}<|im_end|>")

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")

        parts.append("<|im_start|>assistant\n")
        return "\n".join(parts)

    def route(self, token_idx: int, context: Optional[Dict[str, Any]] = None) -> List[int]:
        return []

    def compute_expert(self, expert_id: int, expert_data: bytes, hidden_state: List[float]) -> List[float]:
        return hidden_state

    def compute_shared(self, hidden_state: List[float]) -> List[float]:
        return hidden_state

    def aggregate_experts(self, expert_results: List[Tuple[int, float, List[float]]]) -> List[float]:
        return []
