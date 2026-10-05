# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Memory Tier Manager
# Architecture: 3-tier physical memory hierarchy:
# HOT: VRAM (GPU computation, hottest active experts)
# WARM: RAM (Warm staging, recently evicted VRAM experts, prefetched experts)
# COLD: NVMe (Memory-mapped source of truth, complete inactive expert storage)
# Hardware-independent: Probes host telemetry dynamically without hardcoding.
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import shutil
import logging
import threading
from enum import Enum
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict

import psutil

LOG = logging.getLogger("WISE.Models.Runtime.MemoryTiers")


class MemoryTier(str, Enum):
    HOT_VRAM = "HOT_VRAM"
    WARM_RAM = "WARM_RAM"
    COLD_NVME = "COLD_NVME"


@dataclass
class TierCapacity:
    tier: MemoryTier
    total_bytes: int
    allocated_bytes: int = 0
    reserved_headroom_bytes: int = 0
    resident_count: int = 0
    total_allocations: int = 0
    total_deallocations: int = 0
    peak_allocated_bytes: int = 0

    @property
    def total_mb(self) -> float:
        return round(self.total_bytes / (1024 * 1024), 2)

    @property
    def allocated_mb(self) -> float:
        return round(self.allocated_bytes / (1024 * 1024), 2)

    @property
    def reserved_headroom_mb(self) -> float:
        return round(self.reserved_headroom_bytes / (1024 * 1024), 2)

    @property
    def usable_bytes(self) -> int:
        """Usable budget strictly excluding safety headroom."""
        return max(0, self.total_bytes - self.reserved_headroom_bytes)

    @property
    def usable_mb(self) -> float:
        return round(self.usable_bytes / (1024 * 1024), 2)

    @property
    def available_bytes(self) -> int:
        """Free bytes remaining within the usable budget."""
        return max(0, self.usable_bytes - self.allocated_bytes)

    @property
    def available_mb(self) -> float:
        return round(self.available_bytes / (1024 * 1024), 2)

    @property
    def utilization_pct(self) -> float:
        if self.usable_bytes <= 0:
            return 0.0
        return round((self.allocated_bytes / self.usable_bytes) * 100.0, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.value,
            "total_mb": self.total_mb,
            "allocated_mb": self.allocated_mb,
            "reserved_headroom_mb": self.reserved_headroom_mb,
            "usable_mb": self.usable_mb,
            "available_mb": self.available_mb,
            "utilization_pct": self.utilization_pct,
            "resident_count": self.resident_count,
            "total_allocations": self.total_allocations,
            "total_deallocations": self.total_deallocations,
            "peak_allocated_mb": round(self.peak_allocated_bytes / (1024 * 1024), 2),
        }


class MemoryTierManager:
    """
    Governor for physical and budgeted memory tiers (VRAM, RAM, NVMe).
    Enforces that memory tiers are NOT treated as a flat pool.
    Monitors hardware dynamically and guarantees safety headroom.
    """

    def __init__(
        self,
        vram_budget_bytes: Optional[int] = None,
        ram_budget_bytes: Optional[int] = None,
        nvme_storage_dir: Optional[Path] = None,
        vram_headroom_bytes: Optional[int] = None,
        ram_headroom_bytes: Optional[int] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.nvme_storage_dir = Path(nvme_storage_dir or Path.cwd()).resolve()

        # Probe physical host capabilities
        physical_ram, physical_vram, physical_disk = self._probe_physical_hardware()

        # Establish configured or dynamically calibrated budgets
        # VRAM Budget:
        if vram_budget_bytes is not None:
            vram_total = vram_budget_bytes
        elif physical_vram > 0:
            # Reserve 20% or min 1GB for OS/CUDA context, balance as usable
            vram_total = int(physical_vram * 0.75)
        else:
            # Fallback for CPU-only environments: emulated VRAM partition (1 GB)
            vram_total = 1024 * 1024 * 1024

        # Headroom:
        vram_headroom = (
            vram_headroom_bytes
            if vram_headroom_bytes is not None
            else int(vram_total * 0.15)
        )

        # RAM Budget:
        if ram_budget_bytes is not None:
            ram_total = ram_budget_bytes
        else:
            # Safe default: 50% of available physical RAM
            ram_total = int(physical_ram * 0.50)

        # RAM Headroom:
        ram_headroom = (
            ram_headroom_bytes
            if ram_headroom_bytes is not None
            else int(ram_total * 0.10)
        )

        # Headroom safety clamp: headroom must never consume the entire budget
        if vram_headroom >= vram_total:
            vram_headroom = max(0, int(vram_total * 0.15))
        if ram_headroom >= ram_total:
            ram_headroom = max(0, int(ram_total * 0.15))

        # NVMe Budget (Disk space on target storage volume):
        nvme_total = physical_disk

        self.tiers: Dict[MemoryTier, TierCapacity] = {
            MemoryTier.HOT_VRAM: TierCapacity(
                tier=MemoryTier.HOT_VRAM,
                total_bytes=vram_total,
                reserved_headroom_bytes=vram_headroom,
            ),
            MemoryTier.WARM_RAM: TierCapacity(
                tier=MemoryTier.WARM_RAM,
                total_bytes=ram_total,
                reserved_headroom_bytes=ram_headroom,
            ),
            MemoryTier.COLD_NVME: TierCapacity(
                tier=MemoryTier.COLD_NVME,
                total_bytes=nvme_total,
                reserved_headroom_bytes=0,
            ),
        }

        # Track active allocations by ID/tag
        self._allocations: Dict[str, Tuple[MemoryTier, int]] = {}

        LOG.info(
            "MemoryTierManager initialized: VRAM Usable=%.1f MB (Headroom=%.1f MB), "
            "RAM Usable=%.1f MB (Headroom=%.1f MB), NVMe Total=%.1f MB",
            self.tiers[MemoryTier.HOT_VRAM].usable_mb,
            self.tiers[MemoryTier.HOT_VRAM].reserved_headroom_mb,
            self.tiers[MemoryTier.WARM_RAM].usable_mb,
            self.tiers[MemoryTier.WARM_RAM].reserved_headroom_mb,
            self.tiers[MemoryTier.COLD_NVME].total_mb,
        )

    def _probe_physical_hardware(self) -> Tuple[int, int, int]:
        """Probes physical RAM, GPU VRAM, and NVMe disk space."""
        try:
            vmem = psutil.virtual_memory()
            physical_ram = int(vmem.total)
        except Exception as e:
            LOG.warning("Failed to probe physical RAM: %s; using default 16GB", e)
            physical_ram = 16 * 1024 * 1024 * 1024

        physical_vram = 0
        try:
            # Check nvidia-smi dynamically
            import subprocess
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and res.stdout.strip():
                first_gpu = res.stdout.strip().split("\n")[0]
                physical_vram = int(float(first_gpu.strip()) * 1024 * 1024)
        except Exception:
            pass

        # Check PyTorch CUDA if nvidia-smi was unavailable
        if physical_vram == 0:
            try:
                import torch
                if torch.cuda.is_available():
                    props = torch.cuda.get_device_properties(0)
                    physical_vram = int(props.total_memory)
            except Exception:
                pass

        try:
            drive_path = str(self.nvme_storage_dir)
            total, used, free = shutil.disk_usage(drive_path)
            physical_disk = int(total)
        except Exception as e:
            LOG.warning("Failed to probe disk space: %s; using default 256GB", e)
            physical_disk = 256 * 1024 * 1024 * 1024

        return physical_ram, physical_vram, physical_disk

    def can_allocate(self, tier: MemoryTier, size_bytes: int) -> bool:
        """Verifies if the tier has enough remaining usable capacity without violating headroom."""
        with self._lock:
            cap = self.tiers[tier]
            return cap.available_bytes >= size_bytes

    def allocate(self, tier: MemoryTier, size_bytes: int, tag: str) -> bool:
        """
        Allocates memory in the designated tier.
        Returns True on success, False if budget exceeded.
        """
        with self._lock:
            cap = self.tiers[tier]
            if cap.available_bytes < size_bytes:
                LOG.warning(
                    "Allocation failed in %s: Requested %d bytes (%.2f MB), Available %d bytes (%.2f MB)",
                    tier.value,
                    size_bytes,
                    size_bytes / (1024 * 1024),
                    cap.available_bytes,
                    cap.available_mb,
                )
                return False

            cap.allocated_bytes += size_bytes
            cap.resident_count += 1
            cap.total_allocations += 1
            if cap.allocated_bytes > cap.peak_allocated_bytes:
                cap.peak_allocated_bytes = cap.allocated_bytes

            self._allocations[tag] = (tier, size_bytes)
            LOG.debug(
                "Allocated %d bytes in %s for '%s' (Tier Total: %.1f/%.1f MB)",
                size_bytes,
                tier.value,
                tag,
                cap.allocated_mb,
                cap.usable_mb,
            )
            return True

    def release(self, tag: str) -> bool:
        """Releases allocated memory by tag."""
        with self._lock:
            if tag not in self._allocations:
                return False

            tier, size_bytes = self._allocations.pop(tag)
            cap = self.tiers[tier]
            cap.allocated_bytes = max(0, cap.allocated_bytes - size_bytes)
            cap.resident_count = max(0, cap.resident_count - 1)
            cap.total_deallocations += 1

            LOG.debug(
                "Released %d bytes in %s for '%s' (Tier Remaining: %.1f/%.1f MB)",
                size_bytes,
                tier.value,
                tag,
                cap.allocated_mb,
                cap.usable_mb,
            )
            return True

    def release_direct(self, tier: MemoryTier, size_bytes: int, tag: Optional[str] = None) -> None:
        """Directly decrements allocated bytes for a tier."""
        with self._lock:
            if tag and tag in self._allocations:
                self._allocations.pop(tag)
            cap = self.tiers[tier]
            cap.allocated_bytes = max(0, cap.allocated_bytes - size_bytes)
            cap.resident_count = max(0, cap.resident_count - 1)
            cap.total_deallocations += 1

    def get_tier(self, tier: MemoryTier) -> TierCapacity:
        with self._lock:
            return self.tiers[tier]

    def reset_metrics(self) -> None:
        """Resets peak and cumulative counters while preserving allocations."""
        with self._lock:
            for cap in self.tiers.values():
                cap.peak_allocated_bytes = cap.allocated_bytes
                cap.total_allocations = cap.resident_count
                cap.total_deallocations = 0

    def purge_all(self) -> None:
        """Clears all allocations across all tiers (used on unload/idle)."""
        with self._lock:
            for cap in self.tiers.values():
                cap.allocated_bytes = 0
                cap.resident_count = 0
            self._allocations.clear()
            LOG.info("Purged all allocations across all memory tiers.")

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "vram": self.tiers[MemoryTier.HOT_VRAM].to_dict(),
                "ram": self.tiers[MemoryTier.WARM_RAM].to_dict(),
                "nvme": self.tiers[MemoryTier.COLD_NVME].to_dict(),
                "active_allocations_count": len(self._allocations),
            }
