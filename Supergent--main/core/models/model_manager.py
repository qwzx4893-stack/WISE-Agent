"""
WISE Model Manager (WISEModelManager).
Orchestrates the lifecycle of the embedded native model runtime.
Integrates directly with WISEResourceManager to enforce zero VRAM usage in IDLE,
instantaneous on-demand awakening, and GPU layer offload governance for NVIDIA RTX 4060.
Zero user-facing model selectors: WISE acts as a single, cohesive intelligent entity.
"""

from __future__ import annotations

import os
import sys
import time
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.models.model_runtime import (
    BaseModelRuntime,
    EmbeddedLocalRuntime,
    RuntimeBackend,
    QuantizationType,
    InferenceConfig,
    InferenceResult,
)
from core.resource.resource_manager import get_resource_manager, ManagedWorker, WorkerPriority, WorkerStatus
from core.state.state_machine import get_state_machine, WiseState

LOG = logging.getLogger("wise.model_manager")


class WISEModelManager:
    """Internal governor for the native model runtime and VRAM memory lifecycle."""

    def __init__(self, models_dir: Optional[Path] = None):
        self._lock = threading.RLock()
        
        # Locate local models directory
        if models_dir is None:
            base_dir = Path(__file__).resolve().parent.parent.parent
            self.models_dir = base_dir / "models"
        else:
            self.models_dir = Path(models_dir).resolve()

        self.models_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize native runtime
        self._runtime: BaseModelRuntime = EmbeddedLocalRuntime(backend=RuntimeBackend.EMBEDDED_LLAMA)
        self._current_model_path: Optional[str] = None
        self._is_active = False

        # Register as a ManagedWorker with WISEResourceManager
        self._register_with_resource_manager()

    def _register_with_resource_manager(self) -> None:
        """Wires the model manager into the central resource governor."""
        try:
            rm = get_resource_manager()
            worker = ManagedWorker(
                name="native_model_runtime",
                priority=WorkerPriority.CRITICAL_ON_DEMAND,
                is_essential_in_idle=False,  # Unloaded or suspended in IDLE
                start_fn=self.awaken,
                suspend_fn=self.sleep,
                resume_fn=self.awaken,
                stop_fn=self.unload,
                health_check_fn=self._runtime.is_loaded,
            )
            rm.register_worker(worker)
            LOG.info("Registered 'native_model_runtime' in WISEResourceManager.")
        except Exception as e:
            LOG.error("Failed to register with WISEResourceManager: %s", e)

    def scan_available_models(self) -> List[Dict[str, Any]]:
        """Discovers local GGUF, ONNX, and safetensors models in the local models directory."""
        with self._lock:
            discovered = []
            if not self.models_dir.exists():
                return discovered

            for f in self.models_dir.iterdir():
                if f.is_file() and f.suffix.lower() in (".gguf", ".onnx", ".bin", ".safetensors"):
                    discovered.append({
                        "filename": f.name,
                        "path": str(f.resolve()),
                        "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                        "format": f.suffix.lower().replace(".", ""),
                    })
            return discovered

    def awaken(self) -> bool:
        """Loads or resumes the native model runtime when needed for reasoning."""
        with self._lock:
            if self._runtime.is_loaded():
                self._is_active = True
                return True

            t0 = time.perf_counter()
            available = self.scan_available_models()
            target_path = available[0]["path"] if available else str(self.models_dir / "default_brain.gguf")
            
            # Load with GPU layers offloaded to RTX 4060
            success = self._runtime.load_model(
                target_path,
                config=InferenceConfig(gpu_layers_offload=-1),
            )
            if success:
                self._current_model_path = target_path
                self._is_active = True
                LOG.info("Native Model Runtime AWAKENED in %.2f ms", (time.perf_counter() - t0) * 1000)
            return success

    def sleep(self) -> bool:
        """Enforces zero VRAM footprint when WISE enters IDLE."""
        with self._lock:
            if not self._runtime.is_loaded():
                self._is_active = False
                return True

            LOG.info("Entering IDLE: Suspending Native Model Runtime and releasing VRAM...")
            self._runtime.flush_kv_cache()
            self._runtime.unload_model()
            self._is_active = False
            return True

    def unload(self) -> bool:
        return self.sleep()

    def generate(self, prompt: str, config: Optional[InferenceConfig] = None) -> InferenceResult:
        """Executes inference using the embedded brain."""
        with self._lock:
            if not self._runtime.is_loaded():
                self.awaken()
            return self._runtime.generate(prompt, config)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            mem = self._runtime.get_memory_footprint()
            return {
                "is_loaded": self._runtime.is_loaded(),
                "is_active": self._is_active,
                "current_model": self._current_model_path,
                "vram_used_mb": mem.get("vram_mb", 0.0),
                "ram_used_mb": mem.get("ram_mb", 0.0),
                "available_models_count": len(self.scan_available_models()),
            }


# Singleton
_GLOBAL_MODEL_MGR: Optional[WISEModelManager] = None
_MM_LOCK = threading.Lock()


def get_model_manager() -> WISEModelManager:
    global _GLOBAL_MODEL_MGR
    if _GLOBAL_MODEL_MGR is None:
        with _MM_LOCK:
            if _GLOBAL_MODEL_MGR is None:
                _GLOBAL_MODEL_MGR = WISEModelManager()
    return _GLOBAL_MODEL_MGR
