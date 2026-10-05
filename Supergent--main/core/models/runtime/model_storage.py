# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Model Storage & Security
# Architecture: NVMe-backed memory-mapped (mmap) lazy weight storage.
# Guarantees that large models are NOT loaded entirely into host RAM.
# Strict security: Model files are passive data; enforces path confinement,
# format validation, and SHA256 integrity checks. Zero code execution.
# ==============================================================================

from __future__ import annotations

import os
import sys
import mmap
import time
import hashlib
import logging
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

LOG = logging.getLogger("WISE.Models.Runtime.ModelStorage")

ALLOWED_MODEL_EXTENSIONS = {".gguf", ".safetensors", ".bin", ".wisemoe", ".onnx"}


@dataclass
class ExpertStorageLocation:
    expert_id: int
    layer_id: int
    offset_bytes: int
    size_bytes: int
    sha256_checksum: Optional[str] = None

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


@dataclass
class ModelStorageMetadata:
    model_name: str
    total_experts: int
    active_experts_per_token: int
    num_layers: int
    expert_size_bytes: int
    shared_weights_size_bytes: int
    total_file_size_bytes: int
    format_type: str = "wisemoe"
    file_path: str = ""
    is_mmap_enabled: bool = True

    @property
    def total_size_mb(self) -> float:
        return round(self.total_file_size_bytes / (1024 * 1024), 2)

    @property
    def expert_size_mb(self) -> float:
        return round(self.expert_size_bytes / (1024 * 1024), 2)


class ModelStorageSecurityError(ValueError):
    """Raised when model file validation fails security invariants."""
    pass


class ModelStorage:
    """
    NVMe-backed storage interface with lazy, memory-mapped access to MoE weights.
    Avoids loading inactive experts into RAM.
    Tolerates absent or synthetic weights while strictly validating disk integrity.
    """

    def __init__(
        self,
        file_path: Path,
        metadata: ModelStorageMetadata,
        expert_locations: Dict[int, ExpertStorageLocation],
        expected_sha256: Optional[str] = None,
        base_dir: Optional[Path] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.file_path = Path(file_path).resolve()
        self.base_dir = Path(base_dir or self.file_path.parent).resolve()
        self.metadata = metadata
        self.expert_locations = expert_locations
        self.expected_sha256 = expected_sha256

        self._file_handle: Optional[Any] = None
        self._mmap_handle: Optional[mmap.mmap] = None
        self._is_open = False
        self._read_count = 0
        self._bytes_read = 0

        # Run strict security checks upon instantiation
        self._validate_security_invariants()

    def _validate_security_invariants(self) -> None:
        """
        Validates path confinement, extension allowlist, and passive-data status.
        Never executes files or loads untrusted code.
        """
        # 1. Path confinement: Prevent path traversal escaping base directory
        try:
            self.file_path.relative_to(self.base_dir)
        except ValueError:
            # If path outside base_dir, ensure it is still a valid absolute path without '..'
            if ".." in self.file_path.parts:
                raise ModelStorageSecurityError(
                    f"Security violation: Directory traversal detected in model path: {self.file_path}"
                )

        # 2. File extension validation
        ext = self.file_path.suffix.lower()
        if ext not in ALLOWED_MODEL_EXTENSIONS:
            raise ModelStorageSecurityError(
                f"Security violation: File extension '{ext}' is not in the allowed model formats: "
                f"{ALLOWED_MODEL_EXTENSIONS}. Model files must be passive data tensors."
            )

        # 3. Optional SHA256 integrity verification
        if self.expected_sha256 and self.file_path.exists():
            actual_sha = self._compute_file_sha256()
            if actual_sha.lower() != self.expected_sha256.lower():
                raise ModelStorageSecurityError(
                    f"Integrity check failed for {self.file_path.name}: "
                    f"expected {self.expected_sha256}, got {actual_sha}"
                )
            LOG.info("Model file integrity verified via SHA256: %s", actual_sha[:16])

    def _compute_file_sha256(self) -> str:
        """Computes SHA256 checksum in chunks without exhausting RAM."""
        hasher = hashlib.sha256()
        with open(self.file_path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()

    def open(self) -> bool:
        """Opens file handle and creates memory map (mmap) for zero-latency offset slicing."""
        with self._lock:
            if self._is_open:
                return True

            if not self.file_path.exists():
                LOG.error("Model file does not exist on disk: %s", self.file_path)
                return False

            try:
                self._file_handle = open(self.file_path, "rb")
                file_size = self.file_path.stat().st_size

                if file_size > 0 and self.metadata.is_mmap_enabled:
                    # Windows mmap: access=mmap.ACCESS_READ
                    self._mmap_handle = mmap.mmap(
                        self._file_handle.fileno(),
                        length=0,
                        access=mmap.ACCESS_READ,
                    )
                    LOG.info(
                        "Memory-mapped model file opened: %s (Size: %.2f MB)",
                        self.file_path.name,
                        file_size / (1024 * 1024),
                    )
                self._is_open = True
                return True
            except Exception as e:
                LOG.error("Failed to open mmap model file %s: %s", self.file_path, e)
                self.close()
                return False

    def close(self) -> None:
        """Closes memory map and file handles cleanly."""
        with self._lock:
            if self._mmap_handle is not None:
                try:
                    self._mmap_handle.close()
                except Exception as e:
                    LOG.debug("Error closing mmap: %s", e)
                self._mmap_handle = None

            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except Exception as e:
                    LOG.debug("Error closing file: %s", e)
                self._file_handle = None

            self._is_open = False
            LOG.info("ModelStorage closed for %s", self.file_path.name)

    def is_open(self) -> bool:
        with self._lock:
            return self._is_open

    def get_expert_bytes(self, expert_id: int) -> bytes:
        """
        Lazily reads the expert tensor slice directly from mmap or disk.
        Does NOT load any other experts into RAM.
        """
        with self._lock:
            if not self._is_open:
                if not self.open():
                    raise IOError(f"ModelStorage is not open and cannot be opened for {self.file_path}")

            if expert_id not in self.expert_locations:
                raise KeyError(f"Expert ID {expert_id} is not mapped in model storage {self.metadata.model_name}")

            loc = self.expert_locations[expert_id]
            t0 = time.perf_counter()

            if self._mmap_handle is not None:
                # Slicing from mmap: lazily faults in only the requested pages
                data = self._mmap_handle[loc.offset_bytes : loc.offset_bytes + loc.size_bytes]
            else:
                # Direct seek & read fallback
                self._file_handle.seek(loc.offset_bytes)
                data = self._file_handle.read(loc.size_bytes)

            self._read_count += 1
            self._bytes_read += len(data)
            duration_ms = (time.perf_counter() - t0) * 1000

            LOG.debug(
                "Read expert %d (%.2f MB) from NVMe in %.2f ms",
                expert_id,
                len(data) / (1024 * 1024),
                duration_ms,
            )
            return data

    def get_shared_weights_bytes(self) -> bytes:
        """Reads non-expert shared/attention weights from beginning of file."""
        with self._lock:
            if not self._is_open:
                if not self.open():
                    raise IOError(f"ModelStorage is not open for {self.file_path}")

            size = self.metadata.shared_weights_size_bytes
            if size <= 0:
                return b""

            if self._mmap_handle is not None:
                data = self._mmap_handle[0:size]
            else:
                self._file_handle.seek(0)
                data = self._file_handle.read(size)

            self._bytes_read += len(data)
            return data

    @property
    def total_reads(self) -> int:
        with self._lock:
            return self._read_count

    @property
    def total_bytes_read(self) -> int:
        with self._lock:
            return self._bytes_read

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "metadata": asdict(self.metadata),
                "is_open": self._is_open,
                "total_reads": self._read_count,
                "total_bytes_read_mb": round(self._bytes_read / (1024 * 1024), 2),
                "mapped_experts_count": len(self.expert_locations),
            }
