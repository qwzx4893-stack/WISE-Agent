# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Expert Prefetcher
# Architecture: Background asynchronous prefetcher for high-probability MoE experts.
# Hides memory transfer latency behind GPU execution of current token.
# Tracks prefetch hits, misses, waste, and transfer latencies.
# ==============================================================================

from __future__ import annotations

import time
import queue
import logging
import threading
from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from .memory_tiers import MemoryTier
from .expert_manager import ExpertManager, ExpertState
from .expert_cache import VRAMExpertCache, RAMExpertCache
from .transfer_manager import AsyncTransferManager

LOG = logging.getLogger("WISE.Models.Runtime.ExpertPrefetch")


@dataclass
class PrefetchRequest:
    expert_id: int
    priority: int = 10  # Lower number = higher priority
    target_tier: MemoryTier = MemoryTier.HOT_VRAM
    confidence: float = 1.0
    requested_at: float = field(default_factory=time.time)


class ExpertPrefetcher:
    """
    Asynchronous prefetcher coordinating background loading of anticipated experts.
    Hides NVMe and RAM transfer latencies without blocking inference token generation.
    """

    def __init__(
        self,
        expert_manager: ExpertManager,
        vram_cache: VRAMExpertCache,
        ram_cache: RAMExpertCache,
        transfer_manager: AsyncTransferManager,
        max_queue_size: int = 32,
    ) -> None:
        self._lock = threading.RLock()
        self.expert_manager = expert_manager
        self.vram_cache = vram_cache
        self.ram_cache = ram_cache
        self.transfer_manager = transfer_manager

        # Priority Queue: stores (priority, sequence, PrefetchRequest)
        self._queue: queue.PriorityQueue[Tuple[int, int, PrefetchRequest]] = queue.PriorityQueue(maxsize=max_queue_size)
        self._seq = 0
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # Tracking metrics
        self._prefetched_ids: Set[int] = set()       # All experts ever prefetched
        self._prefetched_hits: Set[int] = set()      # Prefetched and subsequently accessed
        self._prefetched_waste: Set[int] = set()     # Prefetched but evicted or unused
        self._total_prefetches = 0
        self._total_prefetched_bytes = 0

    def start(self) -> None:
        """Starts the background prefetch worker loop."""
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._prefetch_loop,
                name="MoEPrefetchWorker",
                daemon=True,
            )
            self._worker_thread.start()
            LOG.info("ExpertPrefetcher background worker started.")

    def stop(self) -> None:
        """Stops the prefetch worker and clears pending queue."""
        with self._lock:
            self._stop_event.set()
            # Drain queue
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)
        LOG.info("ExpertPrefetcher worker stopped.")

    def prefetch(
        self,
        expert_ids: List[int],
        priority: int = 10,
        target_tier: MemoryTier = MemoryTier.HOT_VRAM,
        confidence: float = 1.0,
        protected_ids: Optional[Set[int]] = None,
    ) -> int:
        """
        Enqueues candidate expert IDs for asynchronous prefetching.
        Deduplicates against already resident or in-flight experts.
        Returns number of enqueued requests.
        """
        enqueued = 0
        with self._lock:
            for exp_id in expert_ids:
                # 1. Skip if already resident in target tier
                if target_tier == MemoryTier.HOT_VRAM and self.vram_cache.contains(exp_id):
                    continue
                if target_tier == MemoryTier.WARM_RAM and self.ram_cache.contains(exp_id):
                    continue

                # 2. Skip if already actively transferring
                if self.transfer_manager.is_in_flight(exp_id):
                    continue

                req = PrefetchRequest(
                    expert_id=exp_id,
                    priority=priority,
                    target_tier=target_tier,
                    confidence=confidence,
                )
                try:
                    self._seq += 1
                    self._queue.put_nowait((priority, self._seq, req))
                    enqueued += 1
                    self.expert_manager.set_predicted(exp_id, is_predicted=True, confidence=confidence)
                except queue.Full:
                    LOG.debug("Prefetch queue full; dropping low priority prefetch for Expert %d", exp_id)
                    break

        return enqueued

    def record_expert_used(self, expert_id: int) -> None:
        """Notifies the prefetcher that an expert was actively used by the router."""
        with self._lock:
            if expert_id in self._prefetched_ids:
                self._prefetched_hits.add(expert_id)

    def record_expert_evicted(self, expert_id: int) -> None:
        """Notifies prefetcher that an expert was evicted."""
        with self._lock:
            if expert_id in self._prefetched_ids and expert_id not in self._prefetched_hits:
                self._prefetched_waste.add(expert_id)

    def cancel_all_pending(self) -> int:
        """Flushes all queued prefetch tasks."""
        cancelled = 0
        with self._lock:
            while not self._queue.empty():
                try:
                    _, _, req = self._queue.get_nowait()
                    self.transfer_manager.cancel_task(req.expert_id)
                    cancelled += 1
                except queue.Empty:
                    break
        return cancelled

    def _prefetch_loop(self) -> None:
        """Internal worker executing non-blocking prefetch tasks from the queue."""
        while not self._stop_event.is_set():
            try:
                # Wait up to 50ms for a prefetch request
                priority, seq, req = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue

            exp_id = req.expert_id
            try:
                # If target is VRAM:
                if req.target_tier == MemoryTier.HOT_VRAM:
                    # 1. Warm in RAM first if not present
                    if not self.ram_cache.contains(exp_id) and not self.vram_cache.contains(exp_id):
                        self.transfer_manager.transfer_nvme_to_ram(exp_id, async_mode=False)

                    # 2. Promote to VRAM
                    if not self.vram_cache.contains(exp_id):
                        self.transfer_manager.transfer_ram_to_vram(exp_id, async_mode=False)

                elif req.target_tier == MemoryTier.WARM_RAM:
                    # Only warm in RAM
                    if not self.ram_cache.contains(exp_id) and not self.vram_cache.contains(exp_id):
                        self.transfer_manager.transfer_nvme_to_ram(exp_id, async_mode=False)

                desc = self.expert_manager.get_descriptor(exp_id)
                n_bytes = desc.size_bytes if desc else 0

                with self._lock:
                    self._prefetched_ids.add(exp_id)
                    self._total_prefetches += 1
                    self._total_prefetched_bytes += n_bytes

                LOG.debug("Prefetched Expert %d to %s (Confidence: %.2f)", exp_id, req.target_tier.value, req.confidence)
            except Exception as e:
                LOG.warning("Background prefetch failed for Expert %d: %s", exp_id, e)
            finally:
                self._queue.task_done()

    @property
    def hit_rate(self) -> float:
        with self._lock:
            total = len(self._prefetched_ids)
            return round((len(self._prefetched_hits) / total) * 100.0, 2) if total > 0 else 0.0

    @property
    def waste_rate(self) -> float:
        with self._lock:
            total = len(self._prefetched_ids)
            return round((len(self._prefetched_waste) / total) * 100.0, 2) if total > 0 else 0.0

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_prefetches": self._total_prefetches,
                "prefetched_count": len(self._prefetched_ids),
                "hits_count": len(self._prefetched_hits),
                "waste_count": len(self._prefetched_waste),
                "hit_rate_pct": self.hit_rate,
                "waste_rate_pct": self.waste_rate,
                "total_prefetched_mb": round(self._total_prefetched_bytes / (1024 * 1024), 2),
                "pending_queue_size": self._queue.qsize(),
            }
