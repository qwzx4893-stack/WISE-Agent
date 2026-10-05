# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Asynchronous Transfer Manager
# Architecture: Non-blocking transfer engine across memory tiers:
# NVMe -> RAM, RAM -> VRAM, VRAM -> RAM, RAM -> COLD
# Tracks exact transfer latencies, bytes, throughput, and error recovery.
# ==============================================================================

from __future__ import annotations

import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Any, List, Optional, Callable, Tuple
from dataclasses import dataclass, field, asdict

from .memory_tiers import MemoryTier
from .expert_manager import ExpertManager, ExpertState
from .model_storage import ModelStorage
from .expert_cache import VRAMExpertCache, RAMExpertCache

LOG = logging.getLogger("WISE.Models.Runtime.TransferManager")


@dataclass
class TransferRecord:
    expert_id: int
    source_tier: MemoryTier
    destination_tier: MemoryTier
    bytes_transferred: int
    start_time: float
    end_time: float
    latency_ms: float
    throughput_mb_s: float
    success: bool
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "source": self.source_tier.value,
            "dest": self.destination_tier.value,
            "bytes": self.bytes_transferred,
            "latency_ms": round(self.latency_ms, 2),
            "throughput_mb_s": round(self.throughput_mb_s, 2),
            "success": self.success,
            "error": self.error,
        }


class AsyncTransferManager:
    """
    Coordinates asynchronous, non-blocking weight transfers between NVMe, RAM, and VRAM.
    Maintains detailed transfer latency logs and protects the main cognitive loop.
    """

    def __init__(
        self,
        storage: ModelStorage,
        expert_manager: ExpertManager,
        vram_cache: VRAMExpertCache,
        ram_cache: RAMExpertCache,
        max_workers: int = 2,
    ) -> None:
        self._lock = threading.RLock()
        self.storage = storage
        self.expert_manager = expert_manager
        self.vram_cache = vram_cache
        self.ram_cache = ram_cache

        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="MoETransfer")
        self._in_flight: Dict[int, Future] = {}
        self._history: List[TransferRecord] = []
        self._total_bytes_transferred = 0
        self._is_active = True

    def transfer_nvme_to_ram(self, expert_id: int, async_mode: bool = False) -> Future | Any:
        """
        Loads expert weights from NVMe storage into RAM cache.
        If async_mode is True, returns a concurrent.futures.Future.
        """
        task_key = ("nvme_to_ram", expert_id)
        with self._lock:
            if not self._is_active:
                raise RuntimeError("TransferManager has been shut down.")

            if task_key in self._in_flight:
                fut = self._in_flight[task_key]
            else:
                self.expert_manager.transition_state(expert_id, ExpertState.LOADING_TO_RAM)

                def _do_transfer() -> Any:
                    t0 = time.perf_counter()
                    start_wall = time.time()
                    try:
                        # 1. Read slice from storage
                        data = self.storage.get_expert_bytes(expert_id)
                        n_bytes = len(data)

                        # 2. Put into RAM cache
                        self.ram_cache.put(expert_id, data)

                        duration = max(0.0001, time.perf_counter() - t0)
                        latency_ms = duration * 1000.0
                        mb_s = (n_bytes / (1024 * 1024)) / duration

                        rec = TransferRecord(
                            expert_id=expert_id,
                            source_tier=MemoryTier.COLD_NVME,
                            destination_tier=MemoryTier.WARM_RAM,
                            bytes_transferred=n_bytes,
                            start_time=start_wall,
                            end_time=time.time(),
                            latency_ms=latency_ms,
                            throughput_mb_s=mb_s,
                            success=True,
                        )
                        with self._lock:
                            self._history.append(rec)
                            self._total_bytes_transferred += n_bytes
                            self._in_flight.pop(task_key, None)

                        self.expert_manager.record_latencies(expert_id, load_latency_ms=latency_ms)
                        LOG.debug("NVMe -> RAM: Expert %d (%.2f MB) loaded in %.2f ms (%.1f MB/s)",
                                  expert_id, n_bytes / (1024 * 1024), latency_ms, mb_s)
                        return data
                    except Exception as e:
                        duration = max(0.0001, time.perf_counter() - t0)
                        rec = TransferRecord(
                            expert_id=expert_id,
                            source_tier=MemoryTier.COLD_NVME,
                            destination_tier=MemoryTier.WARM_RAM,
                            bytes_transferred=0,
                            start_time=start_wall,
                            end_time=time.time(),
                            latency_ms=duration * 1000.0,
                            throughput_mb_s=0.0,
                            success=False,
                            error=str(e),
                        )
                        with self._lock:
                            self._history.append(rec)
                            self._in_flight.pop(task_key, None)

                        self.expert_manager.transition_state(
                            expert_id,
                            new_state=ExpertState.ERROR,
                            error_msg=str(e),
                        )
                        LOG.error("Failed NVMe -> RAM transfer for Expert %d: %s", expert_id, e)
                        raise

                fut = self.executor.submit(_do_transfer)
                self._in_flight[task_key] = fut

        return fut if async_mode else fut.result()

    def transfer_ram_to_vram(
        self,
        expert_id: int,
        async_mode: bool = False,
        protected_ids: Optional[Set[int]] = None,
    ) -> Future | Any:
        """
        Promotes an expert from RAM cache into VRAM cache.
        If evicted from VRAM, demographic pairs are placed into RAM cache.
        """
        task_key = ("ram_to_vram", expert_id)
        with self._lock:
            if not self._is_active:
                raise RuntimeError("TransferManager has been shut down.")

            if task_key in self._in_flight:
                fut = self._in_flight[task_key]
            else:
                self.expert_manager.transition_state(expert_id, ExpertState.LOADING_TO_VRAM)

                def _do_transfer() -> Any:
                    t0 = time.perf_counter()
                    start_wall = time.time()
                    try:
                        # 1. Fetch and remove data from RAM cache (or NVMe if missed)
                        data = self.ram_cache.remove(expert_id)
                        if data is None:
                            # Fallback: stream through RAM
                            data = self.storage.get_expert_bytes(expert_id)

                        n_bytes = len(data) if hasattr(data, "__len__") else 1024 * 1024

                        # 2. Insert into VRAM cache, getting any demoted VRAM items
                        evicted_from_vram = self.vram_cache.put(expert_id, data, protected_ids=protected_ids)

                        # 3. Demoted items stay warm in RAM cache
                        for ev_id, ev_data in evicted_from_vram:
                            if not self.ram_cache.contains(ev_id):
                                try:
                                    self.ram_cache.put(ev_id, ev_data, protected_ids=protected_ids)
                                except MemoryError:
                                    # If RAM full, expert stays COLD
                                    pass

                        duration = max(0.0001, time.perf_counter() - t0)
                        latency_ms = duration * 1000.0
                        mb_s = (n_bytes / (1024 * 1024)) / duration

                        rec = TransferRecord(
                            expert_id=expert_id,
                            source_tier=MemoryTier.WARM_RAM,
                            destination_tier=MemoryTier.HOT_VRAM,
                            bytes_transferred=n_bytes,
                            start_time=start_wall,
                            end_time=time.time(),
                            latency_ms=latency_ms,
                            throughput_mb_s=mb_s,
                            success=True,
                        )
                        with self._lock:
                            self._history.append(rec)
                            self._total_bytes_transferred += n_bytes
                            self._in_flight.pop(task_key, None)

                        self.expert_manager.record_latencies(expert_id, transfer_latency_ms=latency_ms)
                        LOG.debug("RAM -> VRAM: Expert %d promoted in %.2f ms (%.1f MB/s)",
                                  expert_id, latency_ms, mb_s)
                        return data
                    except Exception as e:
                        duration = max(0.0001, time.perf_counter() - t0)
                        rec = TransferRecord(
                            expert_id=expert_id,
                            source_tier=MemoryTier.WARM_RAM,
                            destination_tier=MemoryTier.HOT_VRAM,
                            bytes_transferred=0,
                            start_time=start_wall,
                            end_time=time.time(),
                            latency_ms=duration * 1000.0,
                            throughput_mb_s=0.0,
                            success=False,
                            error=str(e),
                        )
                        with self._lock:
                            self._history.append(rec)
                            self._in_flight.pop(task_key, None)

                        self.expert_manager.transition_state(
                            expert_id,
                            new_state=ExpertState.ERROR,
                            error_msg=str(e),
                        )
                        LOG.error("Failed RAM -> VRAM transfer for Expert %d: %s", expert_id, e)
                        raise

                fut = self.executor.submit(_do_transfer)
                self._in_flight[task_key] = fut

        return fut if async_mode else fut.result()

    def cancel_task(self, expert_id: int) -> bool:
        """Cancels an in-flight transfer task if it has not already started execution."""
        with self._lock:
            cancelled_any = False
            for key in [("nvme_to_ram", expert_id), ("ram_to_vram", expert_id)]:
                fut = self._in_flight.get(key)
                if fut and not fut.done():
                    if fut.cancel():
                        self._in_flight.pop(key, None)
                        cancelled_any = True
            return cancelled_any

    def is_in_flight(self, expert_id: int) -> bool:
        with self._lock:
            for key in [("nvme_to_ram", expert_id), ("ram_to_vram", expert_id)]:
                fut = self._in_flight.get(key)
                if fut is not None and not fut.done():
                    return True
            return False

    def wait_all(self, timeout: Optional[float] = None) -> None:
        """Blocks until all active in-flight transfers complete."""
        with self._lock:
            futs = list(self._in_flight.values())
        for f in futs:
            try:
                f.result(timeout=timeout)
            except Exception:
                pass

    def shutdown(self, wait: bool = True) -> None:
        """Shuts down worker thread pool cleanly."""
        with self._lock:
            self._is_active = False
            for exp_id, fut in list(self._in_flight.items()):
                fut.cancel()
            self._in_flight.clear()

        self.executor.shutdown(wait=wait, cancel_futures=True)
        LOG.info("AsyncTransferManager shut down cleanly.")

    @property
    def total_bytes_transferred(self) -> int:
        with self._lock:
            return self._total_bytes_transferred

    @property
    def transfer_history(self) -> List[TransferRecord]:
        with self._lock:
            return list(self._history)

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            total_transfers = len(self._history)
            successful = sum(1 for r in self._history if r.success)
            failed = total_transfers - successful
            avg_latency = (
                sum(r.latency_ms for r in self._history) / total_transfers
                if total_transfers > 0
                else 0.0
            )
            return {
                "total_transfers": total_transfers,
                "successful_transfers": successful,
                "failed_transfers": failed,
                "total_bytes_mb": round(self._total_bytes_transferred / (1024 * 1024), 2),
                "average_latency_ms": round(avg_latency, 2),
                "in_flight_count": len(self._in_flight),
            }
