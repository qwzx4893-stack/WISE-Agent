# ==============================================================================
# WISE MoE / Hybrid Inference Runtime — Real MoE Model Locator & Binary Inspector
# Locates Llama-3.2-3B-MoE-4Expert.Q4_K_M.gguf, performs deep GGUF binary inspection
# of router and stacked expert tensors, and enforces pre-load hardware safety.
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

LOG = logging.getLogger("WISE.Models.Runtime.RealMoELocator")


@dataclass
class MoEGGUFMetadata:
    architecture: str
    model_name: str
    size_label: str
    quantization: str
    block_count: int
    expert_count: int
    expert_used_count: int
    context_length: int
    embedding_length: int
    feed_forward_length: int
    attention_heads: int
    attention_heads_kv: int
    router_tensor_name_pattern: str
    expert_tensor_names: List[str]
    raw_metadata: Dict[str, Any]


@dataclass
class MoEModelEnvironmentProfile:
    model_path: str
    file_size_bytes: int
    file_size_mb: float
    file_size_gb: float
    model_is_moe: bool
    expert_count: int
    expert_used_count: int
    router_present: bool
    expert_tensors_present: bool
    total_layers: int
    quantization: str
    architecture: str
    available_disk_gb: float
    total_ram_gb: float
    available_ram_gb: float
    total_vram_mb: float
    free_vram_mb: float
    is_safe_to_load: bool
    safety_message: str
    metadata: MoEGGUFMetadata

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["metadata"] = asdict(self.metadata)
        return d


class RealMoELocator:
    """
    Locates and validates the physical Llama-3.2-3B-MoE-4Expert GGUF model on disk.
    Performs deep inspection of GGUF v3 metadata and tensor definitions to verify
    genuine MoE architecture.
    """

    DEFAULT_CANDIDATE_PATHS = [
        Path(r"c:\Users\STS\OneDrive\Documents\WISE\scratch\models\Llama-3.2-3B-MoE-4Expert.Q4_K_M.gguf"),
        Path(r"C:\Users\STS\.lmstudio\models\Fu01978\Llama-3.2-3B-MoE-4Expert-Q4_K_M-GGUF\Llama-3.2-3B-MoE-4Expert.Q4_K_M.gguf"),
    ]

    @classmethod
    def locate_moe_model(cls, custom_path: Optional[Path] = None) -> Path:
        """Finds the local Llama-3.2-3B-MoE-4Expert GGUF file on disk."""
        if custom_path is not None:
            p = Path(custom_path).resolve()
            if not p.is_file():
                raise FileNotFoundError(f"Specified MoE model path does not exist: {custom_path}")
            return p

        for candidate in cls.DEFAULT_CANDIDATE_PATHS:
            if candidate.is_file():
                return candidate.resolve()

        # Dynamic search in scratch/models
        scratch_models = Path(r"c:\Users\STS\OneDrive\Documents\WISE\scratch\models")
        if scratch_models.exists():
            for p in scratch_models.glob("*.gguf"):
                if "moe" in p.name.lower():
                    return p.resolve()

        raise FileNotFoundError(
            "Local Llama-3.2-3B-MoE-4Expert model file could not be found. "
            "Please ensure Llama-3.2-3B-MoE-4Expert.Q4_K_M.gguf is downloaded in scratch/models/."
        )

    @classmethod
    def parse_gguf_metadata(cls, file_path: Path) -> Tuple[MoEGGUFMetadata, Dict[str, Any]]:
        """Parses binary GGUF header to extract MoE architecture, layer count, and tensor info."""
        path = Path(file_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Cannot parse GGUF: file does not exist at {path}")

        def read_str(f) -> str:
            length = struct.unpack("<Q", f.read(8))[0]
            raw = f.read(length)
            return raw.decode("utf-8", errors="replace")

        def skip_val(f, vt: int) -> None:
            if vt in (0, 1, 7): f.seek(1, os.SEEK_CUR)
            elif vt in (2, 3): f.seek(2, os.SEEK_CUR)
            elif vt in (4, 5, 6): f.seek(4, os.SEEK_CUR)
            elif vt in (10, 11, 12): f.seek(8, os.SEEK_CUR)
            elif vt == 8:
                l = struct.unpack("<Q", f.read(8))[0]
                f.seek(l, os.SEEK_CUR)
            elif vt == 9:
                et = struct.unpack("<I", f.read(4))[0]
                n = struct.unpack("<Q", f.read(8))[0]
                if et in (0, 1, 7): f.seek(n * 1, os.SEEK_CUR)
                elif et in (2, 3): f.seek(n * 2, os.SEEK_CUR)
                elif et in (4, 5, 6): f.seek(n * 4, os.SEEK_CUR)
                elif et in (10, 11, 12): f.seek(n * 8, os.SEEK_CUR)
                elif et == 8:
                    for _ in range(n):
                        l = struct.unpack("<Q", f.read(8))[0]
                        f.seek(l, os.SEEK_CUR)

        def read_val(f, vt: int) -> Any:
            if vt == 0: return struct.unpack("<B", f.read(1))[0]
            elif vt == 1: return struct.unpack("<b", f.read(1))[0]
            elif vt == 2: return struct.unpack("<H", f.read(2))[0]
            elif vt == 3: return struct.unpack("<h", f.read(2))[0]
            elif vt == 4: return struct.unpack("<I", f.read(4))[0]
            elif vt == 5: return struct.unpack("<i", f.read(4))[0]
            elif vt == 6: return struct.unpack("<f", f.read(4))[0]
            elif vt == 7: return struct.unpack("<?", f.read(1))[0]
            elif vt == 8: return read_str(f)
            elif vt == 9:
                et = struct.unpack("<I", f.read(4))[0]
                n = struct.unpack("<Q", f.read(8))[0]
                if n > 20:
                    # Skip large arrays (tokens, scores)
                    if et in (0, 1, 7): f.seek(n * 1, os.SEEK_CUR)
                    elif et in (2, 3): f.seek(n * 2, os.SEEK_CUR)
                    elif et in (4, 5, 6): f.seek(n * 4, os.SEEK_CUR)
                    elif et in (10, 11, 12): f.seek(n * 8, os.SEEK_CUR)
                    elif et == 8:
                        for _ in range(n):
                            l = struct.unpack("<Q", f.read(8))[0]
                            f.seek(l, os.SEEK_CUR)
                    return f"<array of {n} items>"
                else:
                    return [read_val(f, et) for _ in range(n)]
            elif vt == 10: return struct.unpack("<Q", f.read(8))[0]
            elif vt == 11: return struct.unpack("<q", f.read(8))[0]
            elif vt == 12: return struct.unpack("<d", f.read(8))[0]
            return None

        raw_meta: Dict[str, Any] = {}
        tensors: Dict[str, Dict[str, Any]] = {}

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
                if any(key.endswith(x) for x in [".tokens", ".token_type", ".scores", ".merges"]):
                    skip_val(f, vtype)
                else:
                    raw_meta[key] = read_val(f, vtype)

            # Parse tensor information table
            for _ in range(tensor_count):
                tname = read_str(f)
                ndims = struct.unpack("<I", f.read(4))[0]
                dims = [struct.unpack("<Q", f.read(8))[0] for _ in range(ndims)]
                ttype = struct.unpack("<I", f.read(4))[0]
                offset = struct.unpack("<Q", f.read(8))[0]
                tensors[tname] = {"dims": dims, "type": ttype, "offset": offset}

        arch = str(raw_meta.get("general.architecture", "llama"))
        block_cnt = int(raw_meta.get(f"{arch}.block_count", 28))
        exp_cnt = int(raw_meta.get(f"{arch}.expert_count", 4))
        exp_used = int(raw_meta.get(f"{arch}.expert_used_count", 2))
        ctx_len = int(raw_meta.get(f"{arch}.context_length", 131072))
        emb_len = int(raw_meta.get(f"{arch}.embedding_length", 3072))
        ffn_len = int(raw_meta.get(f"{arch}.feed_forward_length", 8192))
        heads = int(raw_meta.get(f"{arch}.attention.head_count", 24))
        heads_kv = int(raw_meta.get(f"{arch}.attention.head_count_kv", 8))

        # Discover expert tensors in model
        expert_tensors = [t for t in tensors if "exp" in t or "gate_inp" in t]

        meta = MoEGGUFMetadata(
            architecture=arch,
            model_name=str(raw_meta.get("general.name", "Llama-3.2-3B-MoE-4Expert")),
            size_label=str(raw_meta.get("general.size_label", "4x3.2B")),
            quantization="Q4_K_M",
            block_count=block_cnt,
            expert_count=exp_cnt,
            expert_used_count=exp_used,
            context_length=ctx_len,
            embedding_length=emb_len,
            feed_forward_length=ffn_len,
            attention_heads=heads,
            attention_heads_kv=heads_kv,
            router_tensor_name_pattern="blk.{i}.ffn_gate_inp.weight",
            expert_tensor_names=expert_tensors,
            raw_metadata=raw_meta,
        )

        return meta, tensors

    @classmethod
    def inspect_and_validate(cls, custom_path: Optional[Path] = None) -> MoEModelEnvironmentProfile:
        """
        Validates the physical MoE GGUF file, checks router & expert tensors,
        probes hardware resources (RAM, VRAM, Disk), and outputs an environment profile.
        """
        model_file = cls.locate_moe_model(custom_path)
        meta, tensors = cls.parse_gguf_metadata(model_file)

        stat = model_file.stat()
        file_bytes = stat.st_size
        file_mb = round(file_bytes / (1024 * 1024), 2)
        file_gb = round(file_bytes / (1024 * 1024 * 1024), 2)

        # 1. Probing MoE Tensors & Router
        router_present = any("gate_inp" in t for t in tensors)
        expert_tensors_present = any("exps" in t for t in tensors)
        model_is_moe = (meta.expert_count > 1) and router_present and expert_tensors_present

        # 2. Probing Disk Space
        usage = shutil.disk_usage(model_file.drive or model_file.parent)
        disk_free_gb = round(usage.free / (1024 * 1024 * 1024), 2)

        # 3. Probing RAM
        vm = psutil.virtual_memory()
        total_ram_gb = round(vm.total / (1024 * 1024 * 1024), 2)
        avail_ram_gb = round(vm.available / (1024 * 1024 * 1024), 2)

        # 4. Probing GPU VRAM
        total_vram_mb = 0.0
        free_vram_mb = 0.0
        try:
            import subprocess
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,nounits,noheader"],
                capture_output=True, text=True, timeout=5, check=True
            )
            parts = res.stdout.strip().split(",")
            total_vram_mb = float(parts[0].strip())
            free_vram_mb = float(parts[1].strip())
        except Exception:
            pass

        # 5. Safety validation
        is_safe = True
        safety_reasons: List[str] = []

        if not model_is_moe:
            is_safe = False
            safety_reasons.append("Model failed MoE verification: router or expert tensors missing in GGUF.")

        if disk_free_gb < 2.0:
            is_safe = False
            safety_reasons.append(f"Critically low disk space: {disk_free_gb} GB free (< 2 GB).")

        if total_vram_mb < 4000.0 and avail_ram_gb < 4.0:
            is_safe = False
            safety_reasons.append(
                f"Insufficient memory: Host RAM available is {avail_ram_gb} GB, VRAM is {total_vram_mb} MB. "
                "Minimum 4 GB RAM or VRAM required."
            )

        msg = (
            f"Genuine MoE model verified: {meta.expert_count} experts ({meta.expert_used_count} active/token), "
            f"{meta.block_count} layers. Safe to execute."
            if is_safe else "; ".join(safety_reasons)
        )

        profile = MoEModelEnvironmentProfile(
            model_path=str(model_file),
            file_size_bytes=file_bytes,
            file_size_mb=file_mb,
            file_size_gb=file_gb,
            model_is_moe=model_is_moe,
            expert_count=meta.expert_count,
            expert_used_count=meta.expert_used_count,
            router_present=router_present,
            expert_tensors_present=expert_tensors_present,
            total_layers=meta.block_count,
            quantization=meta.quantization,
            architecture=meta.architecture,
            available_disk_gb=disk_free_gb,
            total_ram_gb=total_ram_gb,
            available_ram_gb=avail_ram_gb,
            total_vram_mb=total_vram_mb,
            free_vram_mb=free_vram_mb,
            is_safe_to_load=is_safe,
            safety_message=msg,
            metadata=meta,
        )

        LOG.info(
            "MoE Model Profile: %s (%.2f GB) | MoE: %s | Experts: %d (top-%d) | VRAM Free: %.1f MB | Safe: %s",
            model_file.name, file_gb, model_is_moe, meta.expert_count, meta.expert_used_count, free_vram_mb, is_safe
        )

        return profile
