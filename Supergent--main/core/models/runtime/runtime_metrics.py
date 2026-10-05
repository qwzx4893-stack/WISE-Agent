# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Runtime Metrics Collector
# Architecture: High-fidelity telemetry engine measuring tokens/sec, TTFT,
# percentiles (p50, p95, p99), multi-tier cache hit rates, prefetch efficiency,
# transfer overheads, and machine-readable exports.
# ==============================================================================

from __future__ import annotations

import json
import time
import logging
import threading
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict

LOG = logging.getLogger("WISE.Models.Runtime.Metrics")


@dataclass
class MoETelemetrySnapshot:
    timestamp: float
    total_tokens: int
    time_to_first_token_ms: float
    avg_token_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    tokens_per_second: float
    vram_cache_hit_rate_pct: float
    ram_cache_hit_rate_pct: float
    nvme_reads_count: int
    nvme_bytes_read_mb: float
    ram_to_vram_transfers_count: int
    total_bytes_transferred_mb: float
    vram_evictions_count: int
    ram_evictions_count: int
    prefetch_hit_rate_pct: float
    prefetch_waste_pct: float
    peak_vram_mb: float
    peak_ram_mb: float
    current_vram_mb: float
    current_ram_mb: float
    cpu_utilization_pct: float
    gpu_utilization_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RuntimeMetricsCollector:
    """
    Thread-safe metrics aggregator for MoE inference runtime.
    Gathers per-token latency samples, calculates statistical distributions,
    and produces machine-readable benchmarks.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._token_latencies: List[float] = []
        self._ttft_ms: float = 0.0
        self._generation_start_time: float = 0.0
        self._generation_end_time: float = 0.0
        self._tokens_count = 0

        # Transfer & Cache counters
        self.nvme_reads = 0
        self.nvme_bytes = 0
        self.ram_to_vram_transfers = 0
        self.total_transferred_bytes = 0
        self.vram_evictions = 0
        self.ram_evictions = 0

    def start_generation(self) -> None:
        """Marks beginning of prompt processing/generation."""
        with self._lock:
            self._generation_start_time = time.perf_counter()
            self._token_latencies.clear()
            self._tokens_count = 0
            self._ttft_ms = 0.0

    def record_first_token(self) -> None:
        """Records Time-To-First-Token (TTFT)."""
        with self._lock:
            if self._generation_start_time > 0 and self._ttft_ms <= 0:
                self._ttft_ms = (time.perf_counter() - self._generation_start_time) * 1000.0

    def record_token_latency(self, latency_ms: float) -> None:
        """Appends individual token generation duration in ms."""
        with self._lock:
            self._token_latencies.append(latency_ms)
            self._tokens_count += 1
            if self._tokens_count == 1 and self._ttft_ms <= 0:
                self._ttft_ms = latency_ms

    def end_generation(self) -> None:
        with self._lock:
            self._generation_end_time = time.perf_counter()

    def get_percentile(self, percentile: float) -> float:
        """Computes statistical percentile (e.g. 50.0, 95.0, 99.0) from sample latencies."""
        with self._lock:
            if not self._token_latencies:
                return 0.0
            sorted_lat = sorted(self._token_latencies)
            idx = int((percentile / 100.0) * len(sorted_lat))
            idx = min(len(sorted_lat) - 1, max(0, idx))
            return sorted_lat[idx]

    def build_snapshot(
        self,
        vram_hit_rate: float = 0.0,
        ram_hit_rate: float = 0.0,
        prefetch_hit_rate: float = 0.0,
        prefetch_waste: float = 0.0,
        peak_vram_mb: float = 0.0,
        peak_ram_mb: float = 0.0,
        current_vram_mb: float = 0.0,
        current_ram_mb: float = 0.0,
        cpu_pct: float = 0.0,
        gpu_pct: float = 0.0,
    ) -> MoETelemetrySnapshot:
        with self._lock:
            total_tokens = len(self._token_latencies)
            avg_lat = sum(self._token_latencies) / total_tokens if total_tokens > 0 else 0.0

            total_duration = (
                self._generation_end_time - self._generation_start_time
                if self._generation_end_time > self._generation_start_time
                else (sum(self._token_latencies) / 1000.0 if total_tokens > 0 else 0.001)
            )
            tps = total_tokens / total_duration if total_duration > 0 else 0.0

            return MoETelemetrySnapshot(
                timestamp=time.time(),
                total_tokens=total_tokens,
                time_to_first_token_ms=round(self._ttft_ms, 2),
                avg_token_latency_ms=round(avg_lat, 2),
                p50_latency_ms=round(self.get_percentile(50.0), 2),
                p95_latency_ms=round(self.get_percentile(95.0), 2),
                p99_latency_ms=round(self.get_percentile(99.0), 2),
                tokens_per_second=round(tps, 2),
                vram_cache_hit_rate_pct=round(vram_hit_rate, 2),
                ram_cache_hit_rate_pct=round(ram_hit_rate, 2),
                nvme_reads_count=self.nvme_reads,
                nvme_bytes_read_mb=round(self.nvme_bytes / (1024 * 1024), 2),
                ram_to_vram_transfers_count=self.ram_to_vram_transfers,
                total_bytes_transferred_mb=round(self.total_transferred_bytes / (1024 * 1024), 2),
                vram_evictions_count=self.vram_evictions,
                ram_evictions_count=self.ram_evictions,
                prefetch_hit_rate_pct=round(prefetch_hit_rate, 2),
                prefetch_waste_pct=round(prefetch_waste, 2),
                peak_vram_mb=round(peak_vram_mb, 2),
                peak_ram_mb=round(peak_ram_mb, 2),
                current_vram_mb=round(current_vram_mb, 2),
                current_ram_mb=round(current_ram_mb, 2),
                cpu_utilization_pct=round(cpu_pct, 2),
                gpu_utilization_pct=round(gpu_pct, 2),
            )

    def export_json(self, snapshot: MoETelemetrySnapshot) -> str:
        return json.dumps(snapshot.to_dict(), indent=2)

    def reset(self) -> None:
        with self._lock:
            self._token_latencies.clear()
            self._ttft_ms = 0.0
            self._generation_start_time = 0.0
            self._generation_end_time = 0.0
            self._tokens_count = 0
            self.nvme_reads = 0
            self.nvme_bytes = 0
            self.ram_to_vram_transfers = 0
            self.total_transferred_bytes = 0
            self.vram_evictions = 0
            self.ram_evictions = 0
