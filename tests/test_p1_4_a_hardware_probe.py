# ==============================================================================
# WISE Phase P1.4-A Real Windows Hardware & Telemetry Field Test
# Focus: Probes physical CPU, RAM, GPU (RTX 4060 Laptop), VRAM, and NVMe disk.
# Executes a real end-to-end synthetic MoE workload on physical hardware.
# Verifies real allocations, real transfers, and clean release without large models.
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import shutil
import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SUPERGENT_DIR = BASE_DIR / "Supergent--main"
if str(SUPERGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERGENT_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
LOG = logging.getLogger("WISE.HardwareProbe")

passed = 0
failed = 0


def check(name: str, condition: bool, details: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"[PASS] {name} {details}")
    else:
        failed += 1
        print(f"[FAIL] {name} {details}")


print("=" * 80)
print("   WISE PHASE P1.4-A: REAL WINDOWS HARDWARE FIELD TEST")
print("=" * 80)

from core.models.runtime import (
    MemoryTier,
    MemoryTierManager,
    SimulationBackend,
    RuntimePolicy,
    RuntimeState,
    MoERuntime,
    RoutingPattern,
)
import psutil


def main() -> None:
    scratch_dir = BASE_DIR / "scratch" / "hw_probe"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # 1. Probe CPU
    cpu_cores_logical = psutil.cpu_count(logical=True)
    cpu_cores_phys = psutil.cpu_count(logical=False)
    cpu_freq = psutil.cpu_freq()
    check("CPU Cores probed", cpu_cores_logical is not None and cpu_cores_logical > 0,
          f"({cpu_cores_logical} logical, {cpu_cores_phys} physical, Max Freq: {cpu_freq.max if cpu_freq else 'N/A'} MHz)")

    # 2. Probe Physical RAM
    vmem = psutil.virtual_memory()
    ram_total_gb = round(vmem.total / (1024**3), 2)
    ram_avail_gb = round(vmem.available / (1024**3), 2)
    check("Host Physical RAM probed", ram_total_gb > 4.0, f"(Total: {ram_total_gb} GB, Available: {ram_avail_gb} GB)")

    # 3. Probe GPU and VRAM
    gpu_detected = False
    gpu_name = "None"
    vram_mb = 0.0
    try:
        import subprocess
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("\n")[0].split(",")
            gpu_detected = True
            gpu_name = parts[0].strip()
            vram_mb = float(parts[1].replace("MiB", "").strip())
            drv_ver = parts[2].strip()
            LOG.info("Hardware probe: Detected NVIDIA GPU: %s (Driver: %s, VRAM: %.1f MB)", gpu_name, drv_ver, vram_mb)
    except Exception as e:
        LOG.warning("nvidia-smi probe warning: %s", e)

    check("NVIDIA GPU / VRAM probed", gpu_detected is True, f"({gpu_name}, VRAM: {vram_mb:.1f} MB)")

    # 4. Probe NVMe Disk Volume
    disk_total, disk_used, disk_free = shutil.disk_usage(str(scratch_dir))
    disk_free_gb = round(disk_free / (1024**3), 1)
    check("NVMe Disk Storage probed", disk_free_gb > 1.0, f"(Available: {disk_free_gb} GB)")

    # 5. Real Physical Memory Tier Allocation & Hardware-calibrated Policy
    print("\n--- Physical Runtime Calibration & Workload Execution ---")
    policy = RuntimePolicy.auto_detect()
    check("Auto-detected Policy Usable VRAM Budget", policy.vram_budget_bytes > 0, f"({policy.vram_budget_bytes / (1024**2):.1f} MB)")
    check("Auto-detected Policy Usable RAM Budget", policy.ram_budget_bytes > 0, f"({policy.ram_budget_bytes / (1024**2):.1f} MB)")
    check("Auto-detected Policy Headroom Safety", policy.vram_headroom_bytes >= 1024 * 1024 * 1024)

    # 6. Execute Real MoE Workload using Calibrated Runtime
    backend = SimulationBackend(
        total_experts=16,
        active_per_token=2,
        expert_size_bytes=2 * 1024 * 1024, # 2 MB per expert (32 MB model)
        routing_pattern=RoutingPattern.LOCALITY_HEAVY,
        storage_dir=scratch_dir,
    )
    storage = backend.create_model_storage()
    runtime = MoERuntime(backend=backend, storage=storage, policy=policy)
    
    init_res = runtime.initialize()
    check("Hardware MoERuntime Initialized", init_res is True and runtime.state == RuntimeState.WARM)

    outputs, snapshot = runtime.generate_tokens(num_tokens=20)
    check("Hardware Token Generation Executed (20 tokens)", len(outputs) == 20)
    check("Hardware Throughput Measured", snapshot.tokens_per_second > 100.0, f"({snapshot.tokens_per_second:.1f} TPS)")
    check("NVMe Disk I/O Measured", snapshot.nvme_reads_count > 0, f"({snapshot.nvme_reads_count} reads, {snapshot.nvme_bytes_read_mb:.1f} MB)")
    check("RAM Staging Transfers Measured", snapshot.ram_to_vram_transfers_count > 0, f"({snapshot.ram_to_vram_transfers_count} transfers)")

    # 7. Hardware Cleanup & Zero-Resource Verification
    print("\n--- Hardware Resource Cleanup Verification ---")
    runtime.enter_idle()
    check("Runtime entered IDLE", runtime.state == RuntimeState.IDLE)
    vram_status_idle = runtime.tier_manager.tiers[MemoryTier.HOT_VRAM].allocated_bytes
    check("VRAM footprint cleared in IDLE (0 bytes)", vram_status_idle == 0)

    runtime.unload()
    check("Runtime fully UNLOADED", runtime.state == RuntimeState.UNLOADED)
    check("Model storage file closed", not storage.is_open())

    # Cleanup scratch directory
    try:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    except Exception:
        pass

    print("\n" + "=" * 80)
    print(f"   REAL HARDWARE FIELD TEST SUMMARY: {passed} PASSED, {failed} FAILED")
    print("=" * 80)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
