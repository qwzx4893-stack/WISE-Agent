"""Persistent JSON-lines worker for the official IndexTTS-2.5 runtime.

The main WISE process deliberately stays independent from the model's Python
and CUDA requirements.  This worker writes protocol messages only to stdout;
all model diagnostics are redirected to stderr so a noisy dependency cannot
corrupt the parent process' response stream.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import numpy as np

from speech_frontend import prepare_spoken_text
from audio_master import polish


_MODEL: Any = None
_ARABIC_DIACRITIZER: Any = None
_DIACRITIZER_UNAVAILABLE = False


def _quality_profile() -> dict[str, int | float]:
    """Return bounded decoding controls for the selected resource profile.

    ``quality`` remains available for a workstation devoted to speech.  WISE
    defaults to ``balanced`` because a multi-beam autoregressive search is the
    largest avoidable source of transient VRAM on a laptop and is not needed
    for short conversational replies.
    """
    if os.environ.get("WISE_TTS_QUALITY_PROFILE", "balanced").strip().lower() == "quality":
        return {"max_tokens": 80, "num_beams": 3, "top_k": 30}
    return {"max_tokens": 56, "num_beams": 1, "top_k": 24}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _model_dir() -> Path:
    configured = os.environ.get("WISE_INDEXTTS_MODEL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    # Kept next to the official checkout so the large third-party weights are
    # not mistaken for WISE source assets or shipped as application code.
    return _repo_root() / "tools" / "index-tts" / "assets" / "voice" / "models" / "index-tts-2.5"


def _runtime_root() -> Path:
    return _repo_root() / "tools" / "index-tts"


def _candidate_gpt_checkpoint() -> Path | None:
    """Return an explicitly configured, separately exported GPT candidate."""
    configured = os.environ.get("WISE_INDEXTTS_GPT_CHECKPOINT", "").strip()
    if not configured:
        return None
    candidate = Path(configured).expanduser().resolve()
    if not candidate.is_file():
        raise RuntimeError(f"configured IndexTTS candidate GPT is missing: {candidate}")
    return candidate


def _load_model() -> Any:
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    runtime_root = _runtime_root()
    model_dir = _model_dir()
    config_path = model_dir / "config.yaml"
    if not runtime_root.is_dir():
        raise RuntimeError("IndexTTS runtime is not installed under tools/index-tts")
    if not config_path.is_file():
        raise RuntimeError(f"IndexTTS-2.5 weights are not ready: missing {config_path}")

    # The upstream checkout is intentionally not installed into WISE's own
    # environment.  It is present on this worker's isolated Python path.
    sys.path.insert(0, str(runtime_root))
    os.environ.setdefault("HF_HUB_CACHE", str(model_dir / "hf_cache"))
    device = os.environ.get("WISE_TTS_DEVICE", "cpu").strip() or "cpu"
    try:
        from indextts.infer_v2_5 import IndexTTS2
    except Exception as exc:  # imports can fail before a useful model error
        raise RuntimeError(f"IndexTTS runtime import failed: {exc}") from exc

    # Upstream prints its load diagnostics.  Send them away from our JSON
    # protocol, without suppressing them for an operator who launches WISE.
    with contextlib.redirect_stdout(sys.stderr):
        if device.startswith("cuda"):
            # BF16 is already selected below.  TF32 reduces compute overhead
            # for eligible kernels without changing the stored model weights.
            import torch

            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        _MODEL = IndexTTS2(
            cfg_path=str(config_path),
            model_dir=str(model_dir),
            device=device,
            use_bf16=device.startswith("cuda"),
            use_cuda_kernel=False,
            use_deepspeed=False,
            use_accel=False,
            use_torch_compile=False,
            use_qwen_emo=False,
        )
        candidate = _candidate_gpt_checkpoint()
        if candidate is not None:
            # ``load_checkpoint`` accepts the same merged checkpoint format as
            # upstream ``gpt.pth``.  Keep it outside the model directory: the
            # base remains intact and clearing the setting is an instant
            # rollback.
            from indextts.utils.checkpoint import load_checkpoint

            load_checkpoint(_MODEL.gpt, str(candidate))
            _MODEL.gpt.eval()
    return _MODEL


def _diacritize_arabic(text: str) -> str:
    """Run the compact CATT encoder-only model on CPU, once per worker.

    A failure must not make speech unavailable: raw Arabic remains a valid
    fallback and the error stays in the worker diagnostics rather than the
    JSON protocol.
    """
    global _ARABIC_DIACRITIZER, _DIACRITIZER_UNAVAILABLE
    if _DIACRITIZER_UNAVAILABLE:
        return text
    try:
        if _ARABIC_DIACRITIZER is None:
            from catt_tashkeel import CATTEncoderOnly

            _ARABIC_DIACRITIZER = CATTEncoderOnly()
            for session in (_ARABIC_DIACRITIZER.encoder_session, _ARABIC_DIACRITIZER.decoder_session):
                session.set_providers(["CPUExecutionProvider"])
        return str(_ARABIC_DIACRITIZER.do_tashkeel(text, verbose=False))
    except Exception as exc:
        _DIACRITIZER_UNAVAILABLE = True
        print(f"Arabic diacritizer unavailable; using raw text: {exc}", file=sys.stderr)
        return text


def _token_count(model: Any, text: str, language: str) -> int:
    return len(model.tokenizer.encode(f"<|{language.lower()}|> {text}", allowed_special="all"))


def _synthesize_single(model: Any, text: str, language: str, output: Path) -> None:
    """Decode one word-safe bounded segment without upstream 40-char splits."""
    profile = _quality_profile()
    max_tokens = int(profile["max_tokens"])
    if _token_count(model, text, language) > max_tokens:
        raise ValueError("utterance exceeds the safe single-pass token budget")
    old_low_vram = model.low_vram
    try:
        # We split on sentence/word boundaries ourselves.  Upstream's
        # ``low_vram`` flag only switches to a 40-*character* splitter; it
        # does not reduce model-weight VRAM and damages Arabic phrasing.
        model.low_vram = False
        model.infer(
            spk_audio_prompt=str(_CURRENT_REFERENCE),
            text=text,
            lang=language,
            output_path=str(output),
            interval_silence=0,
            use_random=False,
            verbose=False,
            max_text_tokens_per_segment=max_tokens,
            text_normalization=False,
            temperature=0.8,
            top_p=0.8,
            top_k=int(profile["top_k"]),
            num_beams=int(profile["num_beams"]),
            repetition_penalty=10.0,
        )
    finally:
        model.low_vram = old_low_vram


def _sentence_chunks(model: Any, text: str, language: str, max_tokens: int | None = None) -> list[str]:
    """Split only when the model token limit requires it, never at 40 chars."""
    import re

    max_tokens = max_tokens or int(_quality_profile()["max_tokens"])

    pieces = [piece.strip() for piece in re.split(r"(?<=[.!?؟؛])\s+", text) if piece.strip()]
    chunks: list[str] = []
    current = ""
    for piece in pieces or [text]:
        proposal = f"{current} {piece}".strip()
        if _token_count(model, proposal, language) <= max_tokens:
            current = proposal
            continue
        if current:
            chunks.append(current)
            current = ""
        # An Arabic sentence can encode to more tokens than its visual length
        # suggests. Split it only on spaces, never through a word.
        for word in piece.split():
            proposal = f"{current} {word}".strip()
            if current and _token_count(model, proposal, language) > max_tokens:
                chunks.append(current)
                current = word
            else:
                current = proposal
    if current:
        chunks.append(current)
    return chunks


def _join_with_crossfade(inputs: list[Path], output: Path) -> None:
    """Assemble true long-form chunks without abrupt zero-silence joins."""
    import soundfile as sf

    rendered: list[np.ndarray] = []
    sample_rate: int | None = None
    for item in inputs:
        data, rate = sf.read(item, dtype="float32", always_2d=True)
        if sample_rate is None:
            sample_rate = rate
        elif rate != sample_rate:
            raise RuntimeError("IndexTTS chunk sample rates do not match")
        rendered.append(data)
    if not rendered or sample_rate is None:
        raise RuntimeError("no speech chunks were generated")
    combined = rendered[0]
    crossfade = max(1, int(sample_rate * 0.018))
    pause = np.zeros((int(sample_rate * 0.09), combined.shape[1]), dtype=np.float32)
    for part in rendered[1:]:
        overlap = min(crossfade, len(combined), len(part))
        if overlap:
            fade_out = np.linspace(1.0, 0.0, overlap, dtype=np.float32)[:, None]
            fade_in = 1.0 - fade_out
            mixed = combined[-overlap:] * fade_out + part[:overlap] * fade_in
            combined = np.concatenate((combined[:-overlap], mixed, pause, part[overlap:]))
        else:
            combined = np.concatenate((combined, part))
    sf.write(output, combined, sample_rate, subtype="PCM_16")


_CURRENT_REFERENCE: Path


def _synthesize(job: Dict[str, Any]) -> Dict[str, Any]:
    global _CURRENT_REFERENCE
    text = str(job.get("text") or "").strip()
    reference = Path(str(job.get("reference_audio") or "")).expanduser()
    output = Path(str(job.get("output") or "")).expanduser()
    language = str(job.get("language") or "AR").upper()
    if language not in {"AR", "EN", "ZH", "JA", "ES"}:
        language = "AR"
    if not text:
        raise ValueError("text is required")
    if not reference.is_file():
        raise ValueError("an existing reference_audio is required")
    if not output.name:
        raise ValueError("output is required")

    output.parent.mkdir(parents=True, exist_ok=True)
    spoken_text, language = prepare_spoken_text(
        text,
        language,
        diacritize_arabic=(
            _diacritize_arabic
            if language == "AR" and os.environ.get("WISE_TTS_DIACRITIZE_ARABIC") == "1"
            else None
        ),
    )
    _CURRENT_REFERENCE = reference
    with contextlib.redirect_stdout(sys.stderr):
        model = _load_model()
        chunks = _sentence_chunks(model, spoken_text, language)
        if len(chunks) == 1:
            _synthesize_single(model, chunks[0], language, output)
        else:
            with tempfile.TemporaryDirectory(prefix="wise-indextts-", dir=output.parent) as temporary:
                chunk_paths = [Path(temporary) / f"chunk-{index}.wav" for index in range(len(chunks))]
                for chunk, chunk_path in zip(chunks, chunk_paths):
                    _synthesize_single(model, chunk, language, chunk_path)
                _join_with_crossfade(chunk_paths, output)
        # Release only temporary allocator blocks.  The model stays warm for
        # the short configured conversation window; its process is terminated
        # by the parent when that window expires.
        if str(os.environ.get("WISE_TTS_DEVICE", "")).startswith("cuda"):
            import gc
            import torch

            gc.collect()
            torch.cuda.empty_cache()
    if not output.is_file():
        raise RuntimeError("IndexTTS completed without creating an output WAV")
    import soundfile as sf

    # Apply the same conservative mastering as the owner-approved sample.
    waveform, sample_rate = sf.read(output, dtype="float32", always_2d=True)
    sf.write(output, polish(waveform, sample_rate), sample_rate, subtype="PCM_24")
    info = sf.info(str(output))
    return {
        "ok": True,
        "output": str(output),
        "sample_rate": info.samplerate,
        "duration_seconds": round(info.frames / info.samplerate, 3),
        "engine": "indextts-2.5",
        "language": language,
        "quality_profile": os.environ.get("WISE_TTS_QUALITY_PROFILE", "balanced"),
        "spoken_text": spoken_text,
    }


def main() -> int:
    for raw in sys.stdin:
        try:
            job = json.loads(raw)
            if job.get("operation") == "shutdown":
                print(json.dumps({"ok": True, "shutdown": True}), flush=True)
                return 0
            print(json.dumps(_synthesize(job), ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)[:900]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
