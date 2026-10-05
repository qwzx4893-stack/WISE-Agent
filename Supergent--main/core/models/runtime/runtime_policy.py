# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Runtime Policies
# Architecture: Governs physical resource budgets, safety headroom margins,
# cache eviction algorithms, concurrency limits, and prefetch behavior.
# Hardware-independent: Dynamically computes optimal configurations.
# ==============================================================================

from __future__ import annotations

import os
import psutil
import logging
from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass, field, asdict

LOG = logging.getLogger("WISE.Models.Runtime.Policy")


class EvictionPolicyType(str, Enum):
    LRU = "LRU"
    LFU = "LFU"
    FREQUENCY_AWARE = "FREQUENCY_AWARE"
    PREDICTION_AWARE = "PREDICTION_AWARE"
    HYBRID = "HYBRID"


@dataclass
class RuntimePolicy:
    """
    Configuration parameters governing MoE memory tiers, caches, and transfers.
    Dynamically auto-configured based on detected host specifications.
    """
    # Eviction policy
    eviction_policy: EvictionPolicyType = EvictionPolicyType.HYBRID

    # VRAM settings (Bytes)
    vram_budget_bytes: int = 4 * 1024 * 1024 * 1024       # 4 GB default usable
    vram_headroom_bytes: int = 1024 * 1024 * 1024         # 1 GB reserved for CUDA/KV/DWM

    # RAM settings (Bytes)
    ram_budget_bytes: int = 6 * 1024 * 1024 * 1024        # 6 GB default usable
    ram_headroom_bytes: int = 1024 * 1024 * 1024          # 1 GB reserved for OS/WISE

    # Transfer concurrency
    max_concurrent_transfers: int = 2

    # Prefetch settings
    enable_prefetch: bool = True
    prefetch_depth: int = 2
    prefetch_confidence_threshold: float = 0.30
    prefetch_to_vram: bool = True

    # Storage settings
    use_mmap: bool = True

    # Idle settings
    unload_vram_on_idle: bool = True
    idle_timeout_seconds: float = 30.0

    @classmethod
    def auto_detect(cls) -> RuntimePolicy:
        """
        Dynamically calculates safe, optimal budgets according to host specifications.
        Supports GPUs from 4GB to 24GB+, CPU-only machines, and varying RAM sizes.
        """
        # 1. Probe physical RAM
        try:
            ram_total = psutil.virtual_memory().total
        except Exception:
            ram_total = 16 * 1024 * 1024 * 1024

        # 2. Probe physical GPU VRAM
        vram_total = 0
        try:
            import subprocess
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and res.stdout.strip():
                first_line = res.stdout.strip().split("\n")[0]
                vram_total = int(float(first_line.strip()) * 1024 * 1024)
        except Exception:
            pass

        if vram_total == 0:
            try:
                import torch
                if torch.cuda.is_available():
                    vram_total = int(torch.cuda.get_device_properties(0).total_memory)
            except Exception:
                pass

        # 3. Calculate balanced budgets:
        if vram_total > 0:
            # Leave 20% or minimum 1.2 GB headroom for CUDA context, KV cache, and Windows
            vram_headroom = max(1024 * 1024 * 1024, int(vram_total * 0.20))
            vram_budget = max(512 * 1024 * 1024, vram_total - vram_headroom)
        else:
            # Emulated VRAM budget for CPU testing
            vram_budget = 1024 * 1024 * 1024
            vram_headroom = 256 * 1024 * 1024

        # RAM: Reserve 4GB for Windows OS & other apps, take 50% of remainder
        ram_headroom = max(1024 * 1024 * 1024, int(ram_total * 0.15))
        ram_budget = max(1024 * 1024 * 1024, int((ram_total - ram_headroom) * 0.45))

        LOG.info(
            "Auto-calibrated RuntimePolicy: VRAM Usable=%.1f MB (Headroom=%.1f MB), "
            "RAM Usable=%.1f MB (Headroom=%.1f MB)",
            vram_budget / (1024 * 1024),
            vram_headroom / (1024 * 1024),
            ram_budget / (1024 * 1024),
            ram_headroom / (1024 * 1024),
        )

        return cls(
            vram_budget_bytes=vram_budget,
            vram_headroom_bytes=vram_headroom,
            ram_budget_bytes=ram_budget,
            ram_headroom_bytes=ram_headroom,
            eviction_policy=EvictionPolicyType.HYBRID,
            enable_prefetch=True,
            prefetch_depth=2,
            prefetch_confidence_threshold=0.30,
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["eviction_policy"] = self.eviction_policy.value
        d["vram_budget_mb"] = round(self.vram_budget_bytes / (1024 * 1024), 2)
        d["vram_headroom_mb"] = round(self.vram_headroom_bytes / (1024 * 1024), 2)
        d["ram_budget_mb"] = round(self.ram_budget_bytes / (1024 * 1024), 2)
        d["ram_headroom_mb"] = round(self.ram_headroom_bytes / (1024 * 1024), 2)
        return d
