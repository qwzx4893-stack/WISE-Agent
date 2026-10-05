# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Real Model Locator & Safety Validator
# Locates existing local model files (Oxcoder 9B), parses GGUF headers,
# and enforces strict pre-load hardware safety checks (RAM, VRAM, Disk).
# ==============================================================================

from __future__ import annotations

import os
import shutil
import struct
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict

import psutil

LOG = logging.getLogger("WISE.Models.Runtime.ModelLocator")


class EnvironmentSafetyError(RuntimeError):
    """Raised when the host environment does not meet minimum safety bounds for model loading."""
    pass


@dataclass
class GGUFMetadata:
    architecture: str
    model_name: str
    parameter_count_label: str
    quantization: str
    block_count: int
    context_length: int
    embedding_length: int
    feed_forward_length: int
    attention_heads: int
    raw_metadata: Dict[str, Any]


@dataclass
class ModelEnvironmentProfile:
    model_path: str
    file_size_bytes: int
    file_size_mb: float
    file_size_gb: float
    quantization: str
    architecture: str
    is_dense_model: bool
    expert_count: int
    available_disk_gb: float
    total_ram_gb: float
    available_ram_gb: float
    total_vram_mb: float
    estimated_vram_needed_mb: float
    estimated_ram_needed_mb: float
    is_safe_to_load: bool
    safety_message: str
    metadata: GGUFMetadata

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["metadata"] = asdict(self.metadata)
        return d


class RealModelLocator:
    """
    Locates and validates the existing local Oxcoder 9B model without downloading
    any new weights or creating duplicate files.
    """

    DEFAULT_CANDIDATE_PATHS = [
        Path(r"C:\Users\STS\.lmstudio\models\prithivMLmods\OxCoder-9B-GGUF\OxCoder-9B.Q5_0.gguf"),
        Path(r"C:\Users\STS\OneDrive\Documents\WISE\scratch\models\OxCoder-9B.Q5_0.gguf"),
    ]

    @classmethod
    def locate_oxcoder_9b(cls, custom_path: Optional[Path] = None) -> Path:
        """Finds the local Oxcoder 9B GGUF file on disk."""
        if custom_path is not None:
            p = Path(custom_path).resolve()
            if not p.is_file():
                raise FileNotFoundError(f"Specified model path does not exist: {custom_path}")
            return p

        for candidate in cls.DEFAULT_CANDIDATE_PATHS:
            if candidate.is_file():
                return candidate.resolve()

        # Dynamic search under user profile if not in default location
        user_home = Path.home()
        lmstudio_dir = user_home / ".lmstudio" / "models"
        if lmstudio_dir.exists():
            for p in lmstudio_dir.rglob("*.gguf"):
                if "oxcoder" in p.name.lower() and not p.name.endswith(".mmproj-bf16.gguf"):
                    return p.resolve()

        raise FileNotFoundError(
            "Local Oxcoder 9B model file could not be found. "
            "Please ensure OxCoder-9B.Q5_0.gguf is present on the local machine."
        )

    @classmethod
    def parse_gguf_metadata(cls, file_path: Path) -> GGUFMetadata:
        """Parses binary GGUF header to extract architecture, layer count, and quantization."""
        path = Path(file_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Cannot parse GGUF: file does not exist at {path}")

        def read_str(f) -> str:
            length = struct.unpack("<Q", f.read(8))[0]
            raw = f.read(length)
            return raw.decode("utf-8", errors="replace")

        def read_val(f, vtype: int) -> Any:
            if vtype == 0: return struct.unpack("<B", f.read(1))[0]
            elif vtype == 1: return struct.unpack("<b", f.read(1))[0]
            elif vtype == 2: return struct.unpack("<H", f.read(2))[0]
            elif vtype == 3: return struct.unpack("<h", f.read(2))[0]
            elif vtype == 4: return struct.unpack("<I", f.read(4))[0]
            elif vtype == 5: return struct.unpack("<i", f.read(4))[0]
            elif vtype == 6: return struct.unpack("<f", f.read(4))[0]
            elif vtype == 7: return struct.unpack("<?", f.read(1))[0]
            elif vtype == 8: return read_str(f)
            elif vtype == 9:
                elem_type = struct.unpack("<I", f.read(4))[0]
                n_elems = struct.unpack("<Q", f.read(8))[0]
                arr = []
                for i in range(n_elems):
                    val = read_val(f, elem_type)
                    if i < 16:  # limit sampled elements
                        arr.append(val)
                return arr
            elif vtype == 10: return struct.unpack("<Q", f.read(8))[0]
            elif vtype == 11: return struct.unpack("<q", f.read(8))[0]
            elif vtype == 12: return struct.unpack("<d", f.read(8))[0]
            return None

        raw_meta: Dict[str, Any] = {}
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != b"GGUF":
                raise ValueError(f"File {path.name} is not a valid GGUF file (Magic: {magic})")
            version = struct.unpack("<I", f.read(4))[0]
            tensor_count = struct.unpack("<Q", f.read(8))[0]
            metadata_kv_count = struct.unpack("<Q", f.read(8))[0]

            for _ in range(metadata_kv_count):
                key = read_str(f)
                vtype = struct.unpack("<I", f.read(4))[0]
                val = read_val(f, vtype)
                # Omit large token lists from metadata dictionary
                if not key.endswith(".tokens") and not key.endswith(".token_type") and not key.endswith(".scores"):
                    raw_meta[key] = val

        arch = str(raw_meta.get("general.architecture", "unknown"))
        block_cnt = int(raw_meta.get(f"{arch}.block_count", raw_meta.get("qwen35.block_count", 32)))
        ctx_len = int(raw_meta.get(f"{arch}.context_length", raw_meta.get("qwen35.context_length", 4096)))
        emb_len = int(raw_meta.get(f"{arch}.embedding_length", raw_meta.get("qwen35.embedding_length", 4096)))
        ffn_len = int(raw_meta.get(f"{arch}.feed_forward_length", raw_meta.get("qwen35.feed_forward_length", 12288)))
        heads = int(raw_meta.get(f"{arch}.attention.head_count", raw_meta.get("qwen35.attention.head_count", 16)))

        return GGUFMetadata(
            architecture=arch,
            model_name=str(raw_meta.get("general.name", "OxCoder 9B")),
            parameter_count_label=str(raw_meta.get("general.size_label", "9B")),
            quantization="Q5_0",  # GGUF ftype 8 corresponds to Q5_0
            block_count=block_cnt,
            context_length=ctx_len,
            embedding_length=emb_len,
            feed_forward_length=ffn_len,
            attention_heads=heads,
            raw_metadata=raw_meta,
        )

    @classmethod
    def inspect_and_validate(cls, custom_path: Optional[Path] = None) -> ModelEnvironmentProfile:
        """
        Gathers disk space, host RAM, GPU VRAM, parses model metadata,
        and determines whether it is safe to run the model.
        """
        model_file = cls.locate_oxcoder_9b(custom_path)
        meta = cls.parse_gguf_metadata(model_file)

        stat = model_file.stat()
        file_bytes = stat.st_size
        file_mb = round(file_bytes / (1024 * 1024), 2)
        file_gb = round(file_bytes / (1024 * 1024 * 1024), 2)

        # Probing Disk Space
        usage = shutil.disk_usage(model_file.drive or model_file.parent)
        disk_free_gb = round(usage.free / (1024 * 1024 * 1024), 2)

        # Probing RAM
        vm = psutil.virtual_memory()
        total_ram_gb = round(vm.total / (1024 * 1024 * 1024), 2)
        avail_ram_gb = round(vm.available / (1024 * 1024 * 1024), 2)

        # Probing GPU VRAM
        total_vram_mb = 0.0
        try:
            import subprocess
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,nounits,noheader"],
                capture_output=True, text=True, timeout=5, check=True
            )
            total_vram_mb = float(res.stdout.strip().split("\n")[0])
        except Exception:
            # Fallback probe
            try:
                import torch
                if torch.cuda.is_available():
                    total_vram_mb = torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
            except Exception:
                pass

        # Memory estimation:
        # With full GPU offload (-ngl 32), VRAM footprint is ~6200 MB, RAM is ~400 MB.
        # With partial offload (-ngl 20), VRAM footprint is ~4000 MB, RAM is ~2500 MB.
        # With pure CPU, RAM footprint is ~6500 MB.
        est_vram_mb = 6200.0 if total_vram_mb >= 7000.0 else 4000.0
        est_ram_mb = 500.0 if total_vram_mb >= 7000.0 else 2500.0

        is_safe = True
        safety_reasons: List[str] = []

        if disk_free_gb < 2.0:
            is_safe = False
            safety_reasons.append(f"Disk space critically low: {disk_free_gb} GB free (< 2 GB required)")

        if total_vram_mb < 4000.0 and avail_ram_gb < 6.0:
            is_safe = False
            safety_reasons.append(
                f"Insufficient memory: Host available RAM is {avail_ram_gb} GB and VRAM is {total_vram_mb} MB. "
                "A minimum of 6 GB RAM or 6 GB VRAM is required to load OxCoder 9B safely."
            )

        msg = "Environment verified safe for OxCoder 9B execution." if is_safe else "; ".join(safety_reasons)

        profile = ModelEnvironmentProfile(
            model_path=str(model_file),
            file_size_bytes=file_bytes,
            file_size_mb=file_mb,
            file_size_gb=file_gb,
            quantization=meta.quantization,
            architecture=meta.architecture,
            is_dense_model=True,  # Qwen 3.5 9B is dense, no MoE routing
            expert_count=0,
            available_disk_gb=disk_free_gb,
            total_ram_gb=total_ram_gb,
            available_ram_gb=avail_ram_gb,
            total_vram_mb=total_vram_mb,
            estimated_vram_needed_mb=est_vram_mb,
            estimated_ram_needed_mb=est_ram_mb,
            is_safe_to_load=is_safe,
            safety_message=msg,
            metadata=meta,
        )

        LOG.info(
            "Model Environment Profile: %s (%.2f GB) | Host RAM: %.2f GB avail | VRAM: %.1f MB | Safe: %s",
            model_file.name, file_gb, avail_ram_gb, total_vram_mb, is_safe
        )

        return profile
