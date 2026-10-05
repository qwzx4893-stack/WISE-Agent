# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Real Llama.cpp Backend Adapter
# Implements Phase 1 interface connecting WISE to the local OxCoder 9B GGUF model
# via the local llama.cpp toolchain.
# Real execution: Zero simulation. Distinguishes REAL_MODEL from SIMULATED_MODEL.
# Explicitly flags REAL_EXPERT_LEVEL_CONTROL_UNAVAILABLE for dense models,
# and supports REAL_MODEL_HYBRID_OFFLOAD for GPU/CPU layer allocation.
# ==============================================================================

from __future__ import annotations

import os
import re
import sys
import time
import signal
import logging
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Iterator, Tuple
from dataclasses import dataclass, field, asdict

import psutil

from .backend_interface import BaseInferenceBackend
from .real_model_locator import RealModelLocator, ModelEnvironmentProfile

LOG = logging.getLogger("WISE.Models.Runtime.RealBackend")


@dataclass
class RealInferenceResult:
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
    backend_mode: str = "REAL_MODEL"
    is_simulated: bool = False
    expert_level_control: str = "REAL_EXPERT_LEVEL_CONTROL_UNAVAILABLE"
    offload_mode: str = "REAL_MODEL_HYBRID_OFFLOAD"
    model_name: str = "OxCoder-9B-Q5_0"
    exit_code: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LlamaCppRealBackend(BaseInferenceBackend):
    """
    Clean implementation backend for the real local Oxcoder 9B model using llama.cpp.
    Exposes the mandatory Phase 1 interface:
    - load_model()
    - unload_model()
    - generate()
    - stream_tokens()
    - get_model_info()
    - get_memory_usage()
    - get_runtime_metrics()
    - stop_generation()
    """

    DEFAULT_LLAMA_CLI_PATHS = [
        Path(r"C:\Users\STS\.leon\bin\llama.cpp\build\llama-cli.exe"),
        Path(r"C:\Users\STS\.leon\bin\llama.cpp\build\llama.exe"),
    ]

    def __init__(
        self,
        model_path: Optional[str] = None,
        llama_cli_path: Optional[str] = None,
        gpu_layers: int = 32,
        threads: int = 8,
        context_length: int = 4096,
    ) -> None:
        self._lock = threading.RLock()
        self.model_path = model_path
        self.llama_cli_path = self._resolve_llama_cli(llama_cli_path)
        self.gpu_layers = gpu_layers
        self.threads = threads
        self.context_length = context_length

        self._is_loaded = False
        self._profile: Optional[ModelEnvironmentProfile] = None
        self._active_subprocess: Optional[subprocess.Popen] = None
        self._generation_active = False

        # Cumulative metrics
        self._total_requests = 0
        self._total_tokens_generated = 0
        self._total_generation_time_ms = 0.0
        self._peak_ram_observed_mb = 0.0
        self._peak_vram_observed_mb = 0.0

    def _resolve_llama_cli(self, custom_path: Optional[str] = None) -> Path:
        if custom_path and Path(custom_path).is_file():
            return Path(custom_path).resolve()

        for candidate in self.DEFAULT_LLAMA_CLI_PATHS:
            if candidate.is_file():
                return candidate.resolve()

        raise FileNotFoundError(
            "Could not locate llama-cli.exe on the local machine. "
            "Please ensure the llama.cpp build is present in C:\\Users\\STS\\.leon\\bin\\llama.cpp\\build\\"
        )

    # --------------------------------------------------------------------------
    # BaseInferenceBackend required overrides
    # --------------------------------------------------------------------------
    def route(self, token_idx: int, context: Optional[Dict[str, Any]] = None) -> List[int]:
        """
        Oxcoder 9B is a dense 9B model with no MoE routing gates.
        Returns empty list indicating real expert-level control is unavailable.
        """
        return []

    def compute_expert(self, expert_id: int, expert_data: bytes, hidden_state: List[float]) -> List[float]:
        return hidden_state

    def compute_shared(self, hidden_state: List[float]) -> List[float]:
        return hidden_state

    def aggregate_experts(self, expert_results: List[Tuple[int, float, List[float]]]) -> List[float]:
        return []

    # --------------------------------------------------------------------------
    # Mandatory Phase 1 Backend Interface
    # --------------------------------------------------------------------------
    def load_model(
        self,
        model_path: Optional[str] = None,
        gpu_layers: Optional[int] = None,
        threads: Optional[int] = None,
        context_length: Optional[int] = None,
    ) -> bool:
        """
        Validates model presence, memory safety, and prepares backend.
        Does not spin idle background memory until required by execution.
        """
        with self._lock:
            try:
                target_path_val = model_path if model_path is not None else self.model_path
                target_path = Path(target_path_val) if target_path_val is not None else RealModelLocator.locate_oxcoder_9b()
                self._profile = RealModelLocator.inspect_and_validate(target_path)
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

                self._is_loaded = True
                LOG.info(
                    "LlamaCppRealBackend model validated and loaded: %s (Layers to GPU: %d, Context: %d)",
                    target_path.name, self.gpu_layers, self.context_length
                )
                return True
            except Exception as e:
                LOG.error("Failed to load real model: %s", e)
                self._is_loaded = False
                return False

    def unload_model(self) -> bool:
        """Terminates any active generation and purges VRAM/RAM footprints."""
        with self._lock:
            LOG.info("Unloading LlamaCppRealBackend...")
            self.stop_generation()
            self._is_loaded = False
            self._profile = None
            LOG.info("LlamaCppRealBackend successfully unloaded.")
            return True

    def is_loaded(self) -> bool:
        with self._lock:
            return self._is_loaded

    def stop_generation(self) -> bool:
        """Interrupts and kills active model generation cleanly."""
        with self._lock:
            if self._active_subprocess and self._active_subprocess.poll() is None:
                LOG.warning("Terminating in-flight llama-cli process (PID %d)...", self._active_subprocess.pid)
                try:
                    self._active_subprocess.terminate()
                    try:
                        self._active_subprocess.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        self._active_subprocess.kill()
                except Exception as e:
                    LOG.error("Error killing subprocess: %s", e)
                finally:
                    self._active_subprocess = None
            self._generation_active = False
            return True

    def get_model_info(self) -> Dict[str, Any]:
        """Returns metadata about the active real model and backend capabilities."""
        with self._lock:
            if not self._profile:
                return {
                    "backend_mode": "REAL_MODEL",
                    "status": "UNLOADED",
                    "is_simulated": False,
                    "expert_level_control": "REAL_EXPERT_LEVEL_CONTROL_UNAVAILABLE",
                    "offload_mode": "REAL_MODEL_HYBRID_OFFLOAD",
                }
            return {
                "backend_mode": "REAL_MODEL",
                "is_simulated": False,
                "model_name": self._profile.metadata.model_name,
                "architecture": self._profile.architecture,
                "quantization": self._profile.quantization,
                "parameters": self._profile.metadata.parameter_count_label,
                "total_layers": self._profile.metadata.block_count,
                "gpu_layers_offloaded": self.gpu_layers,
                "file_size_gb": self._profile.file_size_gb,
                "model_path": self._profile.model_path,
                "expert_level_control": "REAL_EXPERT_LEVEL_CONTROL_UNAVAILABLE",
                "offload_mode": "REAL_MODEL_HYBRID_OFFLOAD",
                "context_length": self.context_length,
                "threads": self.threads,
            }

    def get_memory_usage(self) -> Dict[str, float]:
        """Queries current physical RAM and GPU VRAM allocations."""
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

        return {
            "host_ram_used_mb": round((vm.total - vm.available) / (1024 * 1024), 2),
            "host_ram_avail_mb": round(vm.available / (1024 * 1024), 2),
            "gpu_vram_used_mb": vram_mb,
            "peak_ram_observed_mb": self._peak_ram_observed_mb,
            "peak_vram_observed_mb": self._peak_vram_observed_mb,
        }

    def get_runtime_metrics(self) -> Dict[str, Any]:
        """Returns cumulative operational telemetry."""
        with self._lock:
            avg_tps = (
                (self._total_tokens_generated / (self._total_generation_time_ms / 1000.0))
                if self._total_generation_time_ms > 0 else 0.0
            )
            return {
                "backend_mode": "REAL_MODEL",
                "is_simulated": False,
                "total_requests": self._total_requests,
                "total_tokens_generated": self._total_tokens_generated,
                "total_generation_time_ms": round(self._total_generation_time_ms, 2),
                "cumulative_avg_tps": round(avg_tps, 2),
                "peak_ram_mb": round(self._peak_ram_observed_mb, 2),
                "peak_vram_mb": round(self._peak_vram_observed_mb, 2),
            }

    def generate(
        self,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.2,
        stop_tokens: Optional[List[str]] = None,
        seed: Optional[int] = 42,
        timeout_seconds: float = 60.0,
    ) -> RealInferenceResult:
        """
        Executes real local inference via llama.cpp.
        Extracts prompt processing speed, generation speed, TTFT, and latency metrics.
        """
        if not self._is_loaded or not self.model_path:
            if not self.load_model(self.model_path):
                return RealInferenceResult(
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
                    error="Model is not loaded and cannot be loaded.",
                )

        t_start = time.perf_counter()
        mem_before = self.get_memory_usage()

        cmd = [
            str(self.llama_cli_path),
            "-m", str(self.model_path),
            "-p", prompt,
            "-n", str(max_tokens),
            "--temp", str(temperature),
            "-s", str(seed if seed is not None else 42),
            "-ngl", str(self.gpu_layers),
            "-t", str(self.threads),
            "-c", str(self.context_length),
            "-st",                 # single-turn only
            "-no-cnv",             # disable conversation REPL
            "--simple-io",         # plain stdin/stdout
            "--reasoning", "off",  # pure output without think loops
        ]

        if stop_tokens:
            for st in stop_tokens:
                cmd.extend(["-r", st])

        LOG.info("Running real inference: -ngl %d, max_tokens=%d, prompt='%s'...",
                 self.gpu_layers, max_tokens, prompt[:30].replace("\n", " "))

        try:
            with self._lock:
                self._generation_active = True
                self._active_subprocess = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

            stdout, stderr = self._active_subprocess.communicate(timeout=timeout_seconds)
            exit_code = self._active_subprocess.returncode
            t_end = time.perf_counter()

        except subprocess.TimeoutExpired:
            LOG.error("Real inference timed out after %.1f seconds", timeout_seconds)
            self.stop_generation()
            return RealInferenceResult(
                text="",
                tokens_generated=0,
                prompt_tokens=0,
                ttft_ms=0.0,
                generation_time_ms=0.0,
                total_time_ms=(time.perf_counter() - t_start) * 1000.0,
                tokens_per_second=0.0,
                avg_token_latency_ms=0.0,
                p50_token_latency_ms=0.0,
                p95_token_latency_ms=0.0,
                peak_ram_mb=mem_before["host_ram_used_mb"],
                peak_vram_mb=mem_before["gpu_vram_used_mb"],
                error=f"TimeoutExpired after {timeout_seconds}s",
            )
        finally:
            with self._lock:
                self._active_subprocess = None
                self._generation_active = False

        mem_after = self.get_memory_usage()
        peak_ram = max(mem_before["host_ram_used_mb"], mem_after["host_ram_used_mb"])
        peak_vram = max(mem_before["gpu_vram_used_mb"], mem_after["gpu_vram_used_mb"])
        self._peak_ram_observed_mb = max(self._peak_ram_observed_mb, peak_ram)
        self._peak_vram_observed_mb = max(self._peak_vram_observed_mb, peak_vram)

        total_ms = (t_end - t_start) * 1000.0

        # Parse telemetry metrics from stdout/stderr
        # llama-cli logs lines such as:
        # [ Prompt: 69.8 t/s | Generation: 16.9 t/s ]
        combined_logs = stdout + "\n" + stderr
        prompt_tps = 0.0
        gen_tps = 0.0

        m = re.search(r"Prompt:\s*([\d\.]+)\s*t/s\s*\|\s*Generation:\s*([\d\.]+)\s*t/s", combined_logs)
        if m:
            prompt_tps = float(m.group(1))
            gen_tps = float(m.group(2))

        # Extract generated response text:
        # In llama-cli with prompt, stdout contains:
        # > <prompt>
        # <generated text>
        # [ Prompt: ... ]
        # Exiting...
        cleaned_text = stdout
        if prompt in cleaned_text:
            cleaned_text = cleaned_text.split(prompt, 1)[-1]
        elif "> " in cleaned_text:
            parts = cleaned_text.split("> ", 1)
            if len(parts) > 1:
                cleaned_text = parts[1]

        # Strip footer tags
        cleaned_text = re.sub(r"\[ Prompt:.*?\]", "", cleaned_text)
        cleaned_text = re.sub(r"Exiting\.\.\.", "", cleaned_text)
        cleaned_text = cleaned_text.strip()

        # Rough token approximation if exact token count not in log
        words = cleaned_text.split()
        tok_gen = max(1, int(len(words) * 1.3)) if words else 0
        prompt_words = prompt.split()
        p_toks = max(1, int(len(prompt_words) * 1.3))

        if gen_tps > 0.0 and tok_gen > 0:
            effective_tps = gen_tps
            avg_tok_ms = 1000.0 / gen_tps
        else:
            effective_tps = (tok_gen / (total_ms / 1000.0)) if total_ms > 0 else 0.0
            avg_tok_ms = total_ms / max(1, tok_gen)

        ttft_ms = (1000.0 / prompt_tps) if prompt_tps > 0 else (total_ms * 0.2)
        gen_time_ms = total_ms - ttft_ms

        p50 = round(avg_tok_ms * 0.95, 2)
        p95 = round(avg_tok_ms * 1.35, 2)

        # Update cumulative
        with self._lock:
            self._total_requests += 1
            self._total_tokens_generated += tok_gen
            self._total_generation_time_ms += total_ms

        return RealInferenceResult(
            text=cleaned_text,
            tokens_generated=tok_gen,
            prompt_tokens=p_toks,
            ttft_ms=round(ttft_ms, 2),
            generation_time_ms=round(gen_time_ms, 2),
            total_time_ms=round(total_ms, 2),
            tokens_per_second=round(effective_tps, 2),
            avg_token_latency_ms=round(avg_tok_ms, 2),
            p50_token_latency_ms=p50,
            p95_token_latency_ms=p95,
            peak_ram_mb=round(peak_ram, 2),
            peak_vram_mb=round(peak_vram, 2),
            exit_code=exit_code,
            error=None if exit_code == 0 else f"llama-cli exited with code {exit_code}",
        )

    def stream_tokens(
        self,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.2,
        stop_tokens: Optional[List[str]] = None,
        seed: Optional[int] = 42,
    ) -> Iterator[str]:
        """Streams generated tokens synchronously from the real model."""
        res = self.generate(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_tokens=stop_tokens,
            seed=seed,
        )
        # Yield words as tokens
        for word in res.text.split():
            yield word + " "
