"""
WISE Resource Manager (WISEResourceManager).
Monitors real hardware metrics (CPU, RAM, GPU, Battery) and governs worker lifecycles.
Enforces zero/low-compute idle by suspending heavy workers and clearing memory when entering IDLE.
"""

from __future__ import annotations

import gc
import os
import sys
import time
import logging
import threading
import subprocess
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, asdict

import psutil

from core.state.state_machine import WiseState, WiseStateMachine, get_state_machine

LOG = logging.getLogger("wise.resource_manager")


class WorkerStatus(str, Enum):
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    STOPPED = "STOPPED"


class WorkerPriority(str, Enum):
    LOW_BACKGROUND = "LOW_BACKGROUND"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL_ON_DEMAND = "CRITICAL_ON_DEMAND"


@dataclass
class HardwareMetrics:
    cpu_percent: float
    cpu_name: str
    ram_total_mb: float
    ram_used_mb: float
    ram_percent: float
    gpu_available: bool
    gpu_name: str
    gpu_memory_used_mb: float
    gpu_memory_total_mb: float
    gpu_utilization_percent: float
    power_plugged: bool
    battery_percent: Optional[float]
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ManagedWorker:
    """Represents a subsystem worker managed by WISEResourceManager."""

    def __init__(
        self,
        name: str,
        start_fn: Optional[Callable[[], Any]] = None,
        suspend_fn: Optional[Callable[[], Any]] = None,
        resume_fn: Optional[Callable[[], Any]] = None,
        stop_fn: Optional[Callable[[], Any]] = None,
        health_check_fn: Optional[Callable[[], bool]] = None,
        is_essential_in_idle: bool = False,
        priority: WorkerPriority = WorkerPriority.NORMAL,
    ):
        self.name = name
        self.start_fn = start_fn
        self.suspend_fn = suspend_fn
        self.resume_fn = resume_fn
        self.stop_fn = stop_fn
        self.health_check_fn = health_check_fn
        self.is_essential_in_idle = is_essential_in_idle
        self.priority = priority
        self.status = WorkerStatus.STOPPED
        self.last_activated_time: float = 0.0

    def is_healthy(self) -> bool:
        """Run an optional observable health probe without guessing failures."""
        if self.health_check_fn is None:
            return True
        try:
            return bool(self.health_check_fn())
        except Exception as exc:
            LOG.warning("Health check for worker '%s' failed: %s", self.name, exc)
            return False

    def start(self) -> bool:
        if self.status == WorkerStatus.RUNNING:
            return True
        try:
            if self.start_fn:
                self.start_fn()
            self.status = WorkerStatus.RUNNING
            self.last_activated_time = time.time()
            LOG.info("Worker '%s' started successfully.", self.name)
            return True
        except Exception as e:
            LOG.error("Failed to start worker '%s': %s", self.name, e)
            return False

    def suspend(self) -> bool:
        if self.status == WorkerStatus.SUSPENDED or self.status == WorkerStatus.STOPPED:
            return True
        try:
            if self.suspend_fn:
                self.suspend_fn()
            self.status = WorkerStatus.SUSPENDED
            LOG.info("Worker '%s' suspended to conserve resources.", self.name)
            return True
        except Exception as e:
            LOG.error("Failed to suspend worker '%s': %s", self.name, e)
            return False

    def resume(self) -> bool:
        if self.status == WorkerStatus.RUNNING:
            return True
        try:
            if self.resume_fn:
                self.resume_fn()
            elif self.start_fn:
                self.start_fn()
            self.status = WorkerStatus.RUNNING
            self.last_activated_time = time.time()
            LOG.info("Worker '%s' resumed.", self.name)
            return True
        except Exception as e:
            LOG.error("Failed to resume worker '%s': %s", self.name, e)
            return False

    def stop(self) -> bool:
        if self.status == WorkerStatus.STOPPED:
            return True
        try:
            if self.stop_fn:
                self.stop_fn()
            self.status = WorkerStatus.STOPPED
            LOG.info("Worker '%s' stopped cleanly.", self.name)
            return True
        except Exception as e:
            LOG.error("Failed to stop worker '%s': %s", self.name, e)
            return False


class WISEResourceManager:
    """Intelligent Resource Governor for the WISE ecosystem."""

    def __init__(self, state_machine: Optional[WiseStateMachine] = None):
        self.state_machine = state_machine or get_state_machine()
        self.workers: Dict[str, ManagedWorker] = {}
        self._lock = threading.RLock()
        self._cpu_name: str = "Unknown CPU"
        self._gpu_detected: Optional[bool] = None
        self._gpu_name: str = "Unknown"

        # Register hook on state machine transitions
        self.state_machine.add_listener(self._on_state_transition)
        self._probe_cpu_hardware()
        self._probe_gpu_hardware()

    def _probe_cpu_hardware(self) -> None:
        """Queries the exact physical CPU brand string from Windows registry."""
        if sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                ) as key:
                    self._cpu_name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
                    LOG.info("Hardware probe: CPU detected: %s", self._cpu_name)
                    return
            except Exception:
                pass
        import platform
        self._cpu_name = platform.processor() or "Unknown CPU"

    def _probe_gpu_hardware(self) -> None:
        """Probes for NVIDIA GPU presence without blocking."""
        try:
            # Check nvidia-smi
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(",")
                self._gpu_detected = True
                self._gpu_name = parts[0].strip()
                LOG.info("Hardware probe: NVIDIA GPU detected: %s", self._gpu_name)
                return
        except Exception:
            pass
        self._gpu_detected = False
        self._gpu_name = "None/Integrated"

    def register_worker(self, worker: ManagedWorker) -> None:
        with self._lock:
            self.workers[worker.name] = worker
            LOG.info("Registered managed worker: %s (essential: %s)", worker.name, worker.is_essential_in_idle)

    def get_hardware_metrics(self) -> HardwareMetrics:
        """Fetches instantaneous physical telemetry for CPU, RAM, GPU, and Battery."""
        cpu = psutil.cpu_percent(interval=None)
        vmem = psutil.virtual_memory()
        
        # Battery probe
        battery = psutil.sensors_battery()
        plugged = True if battery is None else battery.power_plugged
        bat_percent = None if battery is None else battery.percent

        # GPU probe
        gpu_mem_used = 0.0
        gpu_mem_total = 0.0
        gpu_util = 0.0

        if self._gpu_detected:
            try:
                res = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=memory.used,memory.total,utilization.gpu",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                )
                if res.returncode == 0 and res.stdout.strip():
                    vals = [float(x.strip()) for x in res.stdout.strip().split(",")]
                    if len(vals) >= 3:
                        gpu_mem_used = vals[0]
                        gpu_mem_total = vals[1]
                        gpu_util = vals[2]
            except Exception:
                pass

        return HardwareMetrics(
            cpu_percent=cpu,
            cpu_name=self._cpu_name,
            ram_total_mb=round(vmem.total / (1024 * 1024), 1),
            ram_used_mb=round(vmem.used / (1024 * 1024), 1),
            ram_percent=vmem.percent,
            gpu_available=bool(self._gpu_detected),
            gpu_name=self._gpu_name,
            gpu_memory_used_mb=gpu_mem_used,
            gpu_memory_total_mb=gpu_mem_total,
            gpu_utilization_percent=gpu_util,
            power_plugged=plugged,
            battery_percent=bat_percent,
            timestamp=time.time(),
        )

    def _on_state_transition(self, old_state: WiseState, new_state: WiseState, reason: str) -> None:
        """Enforces low-idle policies automatically upon state changes."""
        profile = self.state_machine.current_profile

        if profile.is_low_power:
            # Transitioning to IDLE or low-power state
            LOG.info("Entering low-power state (%s). Suspending non-essential workers...", new_state.value)
            with self._lock:
                for worker in self.workers.values():
                    if not worker.is_essential_in_idle:
                        worker.suspend()

            # Trigger garbage collection
            gc.collect()

            # Empty CUDA cache if torch is loaded in process
            try:
                import sys
                if "torch" in sys.modules:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                        LOG.debug("CUDA memory cache cleared on idle transition.")
            except Exception as e:
                LOG.debug("CUDA empty_cache skip: %s", e)

        elif new_state in (WiseState.AWAKENING, WiseState.OBSERVING, WiseState.REASONING, WiseState.EXECUTING):
            # Transitioning to active state: wake relevant workers on-demand
            LOG.info("Entering active state (%s). Activating required workers...", new_state.value)
            with self._lock:
                if new_state == WiseState.OBSERVING and "vision" in self.workers:
                    self.workers["vision"].resume()
                elif new_state == WiseState.REASONING and "reasoning" in self.workers:
                    self.workers["reasoning"].resume()
                elif new_state == WiseState.EXECUTING:
                    if "computer_use" in self.workers:
                        self.workers["computer_use"].resume()

    def activate_worker(self, name: str) -> bool:
        """Demand-driven activation for a specific worker."""
        with self._lock:
            worker = self.workers.get(name)
            if worker:
                return worker.resume()
            LOG.warning("Worker '%s' not registered.", name)
            return False

    def suspend_worker(self, name: str) -> bool:
        with self._lock:
            worker = self.workers.get(name)
            if worker:
                return worker.suspend()
            return False

    def is_battery_constrained(self) -> bool:
        """Returns True if the device is running on battery and charge is below 30%."""
        metrics = self.get_hardware_metrics()
        if not metrics.power_plugged and metrics.battery_percent is not None:
            return metrics.battery_percent < 30.0
        return False


# Singleton instance
_GLOBAL_RESOURCE_MANAGER: Optional[WISEResourceManager] = None
_RM_LOCK = threading.Lock()


def get_resource_manager() -> WISEResourceManager:
    global _GLOBAL_RESOURCE_MANAGER
    if _GLOBAL_RESOURCE_MANAGER is None:
        with _RM_LOCK:
            if _GLOBAL_RESOURCE_MANAGER is None:
                _GLOBAL_RESOURCE_MANAGER = WISEResourceManager()
    return _GLOBAL_RESOURCE_MANAGER
