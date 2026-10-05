# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Backend Abstraction & Simulation Backend
# Architecture: Provider-independent backend interface for MoE inference.
# Implements SimulationBackend: Deterministic synthetic MoE model with real disk I/O,
# memory-mapped slicing, real tensor operations, and reference resident mode.
# ==============================================================================

from __future__ import annotations

import os
import math
import struct
import logging
import threading
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .model_storage import ModelStorage, ModelStorageMetadata, ExpertStorageLocation

LOG = logging.getLogger("WISE.Models.Runtime.Backend")


class RoutingPattern(str, Enum):
    LOCALITY_HEAVY = "LOCALITY_HEAVY"
    CLUSTERED = "CLUSTERED"
    RANDOM = "RANDOM"


class BaseInferenceBackend(ABC):
    """Abstract interface for all MoE inference compute engines."""

    @abstractmethod
    def route(self, token_idx: int, context: Optional[Dict[str, Any]] = None) -> List[int]:
        """Returns the list of active expert IDs selected for the current token."""
        pass

    @abstractmethod
    def compute_expert(self, expert_id: int, expert_data: bytes, hidden_state: List[float]) -> List[float]:
        """Executes expert feed-forward compute using the supplied weights and hidden state."""
        pass

    @abstractmethod
    def compute_shared(self, hidden_state: List[float]) -> List[float]:
        """Computes attention/shared non-expert weights."""
        pass

    @abstractmethod
    def aggregate_experts(self, expert_results: List[Tuple[int, float, List[float]]]) -> List[float]:
        """Blends active expert output vectors according to router softmax weights."""
        pass


class SimulationBackend(BaseInferenceBackend):
    """
    Deterministic synthetic MoE backend reproducing the exact memory and compute
    characteristics of a large sparse MoE without requiring 30GB+ downloads.
    Performs real file creation, real mmap disk reads, real buffer allocations,
    and real floating-point mathematical transformations.
    """

    def __init__(
        self,
        total_experts: int = 32,
        active_per_token: int = 2,
        expert_size_bytes: int = 16 * 1024 * 1024,  # 16 MB per expert (32 * 16MB = 512 MB total)
        hidden_dim: int = 64,
        routing_pattern: RoutingPattern = RoutingPattern.LOCALITY_HEAVY,
        storage_dir: Optional[Path] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.total_experts = total_experts
        self.active_per_token = active_per_token
        self.expert_size_bytes = expert_size_bytes
        self.hidden_dim = hidden_dim
        self.routing_pattern = routing_pattern
        self.storage_dir = Path(storage_dir or Path.cwd() / "scratch" / "sim_moe").resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.model_file = self.storage_dir / "synthetic_moe_model.wisemoe"
        self._shared_size_bytes = 2 * 1024 * 1024  # 2 MB shared weights

        # Pre-generate synthetic model file on disk if missing
        self._ensure_synthetic_file_on_disk()

        # In-memory resident weights for REFERENCE MODE
        self._reference_weights: Dict[int, bytes] = {}
        self._load_reference_weights()

    def _ensure_synthetic_file_on_disk(self) -> None:
        """Writes deterministic synthetic weights to a real binary file on disk."""
        with self._lock:
            expected_total = self._shared_size_bytes + (self.total_experts * self.expert_size_bytes)
            if self.model_file.exists() and self.model_file.stat().st_size == expected_total:
                return

            LOG.info("Creating synthetic MoE weight file on disk: %s (%.2f MB)...",
                     self.model_file.name, expected_total / (1024 * 1024))

            chunk_size = 64 * 1024  # 64 KB block
            pattern_block = bytes([(i % 251) for i in range(chunk_size)])

            with open(self.model_file, "wb") as f:
                # 1. Write shared weights
                written = 0
                while written < self._shared_size_bytes:
                    to_write = min(chunk_size, self._shared_size_bytes - written)
                    f.write(pattern_block[:to_write])
                    written += to_write

                # 2. Write deterministic expert weights
                for exp_id in range(self.total_experts):
                    # Deterministic seed per expert
                    exp_seed = ((exp_id * 37) + 11) % 251
                    exp_chunk = bytes([((i + exp_seed) % 251) for i in range(chunk_size)])
                    exp_written = 0
                    while exp_written < self.expert_size_bytes:
                        to_write = min(chunk_size, self.expert_size_bytes - exp_written)
                        f.write(exp_chunk[:to_write])
                        exp_written += to_write

            LOG.info("Synthetic MoE weight file created successfully (Size: %.2f MB)",
                     self.model_file.stat().st_size / (1024 * 1024))

    def _load_reference_weights(self) -> None:
        """Loads all expert weights into RAM for the ground-truth Reference Mode."""
        with open(self.model_file, "rb") as f:
            f.seek(self._shared_size_bytes)
            for exp_id in range(self.total_experts):
                data = f.read(self.expert_size_bytes)
                self._reference_weights[exp_id] = data

    def create_model_storage(self) -> ModelStorage:
        """Instantiates a production ModelStorage bound to this synthetic model."""
        locations: Dict[int, ExpertStorageLocation] = {}
        offset = self._shared_size_bytes

        for exp_id in range(self.total_experts):
            locations[exp_id] = ExpertStorageLocation(
                expert_id=exp_id,
                layer_id=exp_id // 8,
                offset_bytes=offset,
                size_bytes=self.expert_size_bytes,
            )
            offset += self.expert_size_bytes

        meta = ModelStorageMetadata(
            model_name="Synthetic-MoE-Benchmark-v1",
            total_experts=self.total_experts,
            active_experts_per_token=self.active_per_token,
            num_layers=max(1, self.total_experts // 8),
            expert_size_bytes=self.expert_size_bytes,
            shared_weights_size_bytes=self._shared_size_bytes,
            total_file_size_bytes=offset,
            format_type="wisemoe",
            file_path=str(self.model_file),
            is_mmap_enabled=True,
        )

        return ModelStorage(
            file_path=self.model_file,
            metadata=meta,
            expert_locations=locations,
            base_dir=self.storage_dir,
        )

    def route(self, token_idx: int, context: Optional[Dict[str, Any]] = None) -> List[int]:
        """
        Deterministic router generating expert activations based on selected pattern:
        - LOCALITY_HEAVY: High temporal re-use with periodic drift.
        - CLUSTERED: Experts grouped in clusters of 8, switching every 10 tokens.
        - RANDOM: Pseudo-random deterministic sequence.
        """
        if self.routing_pattern == RoutingPattern.LOCALITY_HEAVY:
            # 80% chance of staying in current active pair, 20% shift
            primary = (token_idx // 3) % self.total_experts
            secondary = (primary + 1) % self.total_experts
            if self.active_per_token == 3:
                tertiary = (primary + 2) % self.total_experts
                return [primary, secondary, tertiary]
            return [primary, secondary]

        elif self.routing_pattern == RoutingPattern.CLUSTERED:
            cluster_idx = (token_idx // 8) % (self.total_experts // 4)
            base = cluster_idx * 4
            return [(base + (token_idx % 4)) % self.total_experts, (base + ((token_idx + 1) % 4)) % self.total_experts]

        else:
            # Deterministic pseudo-random pattern
            h1 = (token_idx * 17 + 5) % self.total_experts
            h2 = (token_idx * 31 + 13) % self.total_experts
            if h1 == h2:
                h2 = (h2 + 1) % self.total_experts
            return [h1, h2]

    def compute_shared(self, hidden_state: List[float]) -> List[float]:
        """Applies non-expert normalization and linear transformation."""
        dim = len(hidden_state)
        # Layer norm simulation: zero mean, unit variance
        mean = sum(hidden_state) / max(1, dim)
        var = sum((x - mean) ** 2 for x in hidden_state) / max(1, dim)
        std = math.sqrt(var + 1e-5)
        return [(x - mean) / std for x in hidden_state]

    def compute_expert(self, expert_id: int, expert_data: bytes, hidden_state: List[float]) -> List[float]:
        """
        Executes real mathematical computation:
        Unpacks expert weight slice as float array, performs matrix-vector dot product.
        Deterministic: Result is purely a function of expert weights and hidden state.
        """
        dim = len(hidden_state)
        # Extract first (dim * 4) bytes as float32 parameters
        n_floats = min(dim, len(expert_data) // 4)
        if n_floats <= 0:
            return list(hidden_state)

        # Unpack deterministic floats from expert slice
        unpacked_weights = struct.unpack(f"{n_floats}f", expert_data[: n_floats * 4])

        output: List[float] = []
        for i in range(dim):
            w = unpacked_weights[i % n_floats]
            x = hidden_state[i]
            # SwiGLU / GELU activation simulation: x * w * sigmoid(x)
            val = x * w * (1.0 / (1.0 + math.exp(-max(-10.0, min(10.0, x)))))
            output.append(round(val, 6))

        return output

    def aggregate_experts(self, expert_results: List[Tuple[int, float, List[float]]]) -> List[float]:
        """Combines expert vectors weighted by softmax router probabilities."""
        if not expert_results:
            return [0.0] * self.hidden_dim

        dim = len(expert_results[0][2])
        blended = [0.0] * dim

        # Normalize weights
        total_w = sum(w for _, w, _ in expert_results) or 1.0
        for exp_id, weight, vec in expert_results:
            norm_w = weight / total_w
            for i in range(dim):
                blended[i] += norm_w * vec[i]

        return blended

    def compute_reference_step(self, token_idx: int, hidden_state: List[float]) -> Tuple[List[int], List[float]]:
        """
        Ground-truth reference computation:
        Executes the step using permanently resident reference weights.
        """
        active_ids = self.route(token_idx)
        shared_out = self.compute_shared(hidden_state)

        results: List[Tuple[int, float, List[float]]] = []
        for idx, exp_id in enumerate(active_ids):
            data = self._reference_weights[exp_id]
            exp_out = self.compute_expert(exp_id, data, shared_out)
            weight = 0.6 if idx == 0 else 0.4
            results.append((exp_id, weight, exp_out))

        final_out = self.aggregate_experts(results)
        return active_ids, final_out
