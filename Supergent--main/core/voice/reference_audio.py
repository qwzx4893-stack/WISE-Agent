"""Tracked input bounds for the controlled IndexTTS reference decoder.

Imports of media/model libraries are lazy. Paths and conditioning cache keys are
preserved; this is an input budget, not a native-code decoder sandbox.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import math
from numbers import Real
import os
from pathlib import Path
import stat
from types import MethodType
from typing import Any, Optional


MAX_REFERENCE_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_CHANNELS = 8
MAX_REFERENCE_SAMPLE_RATE = 192000
MAX_REFERENCE_DURATION_SECONDS = 600.0
MAX_CONDITIONING_SECONDS = 15.0
RESAMPLE_TAIL_SECONDS = 0.25


@dataclass(frozen=True)
class ReferenceAudioInfo:
    path: Path
    size_bytes: int
    channels: int
    sample_rate: int
    frames: int
    duration_seconds: float
    identity: tuple


def _identity(attributes) -> tuple:
    # Windows path stat and descriptor fstat disagree on ctime semantics.
    # Device/inode, size and modification time are comparable on both paths.
    return (attributes.st_dev, attributes.st_ino, attributes.st_size,
            attributes.st_mtime_ns)


def _check_file(attributes) -> None:
    if not stat.S_ISREG(attributes.st_mode):
        raise ValueError("reference audio must be a regular file")
    if not 0 < attributes.st_size <= MAX_REFERENCE_BYTES:
        raise ValueError("reference audio is empty or exceeds the 64 MiB input budget")


@contextmanager
def _opened_reference(path: Path, expected: Optional[ReferenceAudioInfo] = None):
    before = path.stat()
    _check_file(before)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        _check_file(opened)
        identity = _identity(opened)
        if _identity(before) != identity:
            raise ValueError("reference audio changed while opening")
        if expected is not None and (expected.path != path or expected.identity != identity):
            raise ValueError("reference audio changed after validation")
        yield stream, opened
        if _identity(os.fstat(stream.fileno())) != identity or _identity(path.stat()) != identity:
            raise ValueError("reference audio changed while being read")


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"invalid reference {name}")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"invalid reference {name}") from exc
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        raise ValueError(f"invalid reference {name}")
    return int(number)


def _positive_seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"invalid reference {name}")
    try:
        seconds = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"invalid reference {name}") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"invalid reference {name}")
    return seconds


def _metadata(stream, path: Path, attributes) -> ReferenceAudioInfo:
    import soundfile as sf

    metadata = sf.info(stream)
    channels = _positive_integer(metadata.channels, "channels")
    sample_rate = _positive_integer(metadata.samplerate, "sample rate")
    frames = _positive_integer(metadata.frames, "frames")
    duration = _positive_seconds(metadata.duration, "duration")
    if channels > MAX_REFERENCE_CHANNELS or sample_rate > MAX_REFERENCE_SAMPLE_RATE:
        raise ValueError("reference channel count or sample rate exceeds its input budget")
    derived_duration = frames / sample_rate
    if (not math.isfinite(duration) or duration <= 0
            or max(duration, derived_duration) > MAX_REFERENCE_DURATION_SECONDS
            or abs(duration - derived_duration) > max(1 / sample_rate, 1e-6)):
        raise ValueError("invalid reference duration or duration exceeds 600 seconds")
    return ReferenceAudioInfo(path, attributes.st_size, channels, sample_rate,
                              frames, duration, _identity(attributes))


def validate_reference_audio(path) -> ReferenceAudioInfo:
    """Inspect the opened reference before model work, without waveform decoding."""
    reference = Path(path)
    with _opened_reference(reference) as (stream, attributes):
        info = _metadata(stream, reference, attributes)
    return info


def load_bounded_reference(audio_path, max_audio_length_seconds, verbose=False,
                           sr=None, *, expected: Optional[ReferenceAudioInfo] = None):
    """Decode a finite prefix, retain resampler context, then preserve the 15s crop."""
    seconds = _positive_seconds(max_audio_length_seconds, "conditioning duration")
    seconds = min(seconds, MAX_CONDITIONING_SECONDS)
    target_rate = _positive_integer(sr, "output sample rate") if sr else 22050
    if target_rate > MAX_REFERENCE_SAMPLE_RATE:
        raise ValueError("reference output sample rate exceeds its budget")
    reference = Path(audio_path)
    import librosa

    with _opened_reference(reference, expected) as (stream, attributes):
        info = expected if expected is not None else _metadata(stream, reference, attributes)
        duration = min(info.duration_seconds, seconds + RESAMPLE_TAIL_SECONDS)
        stream.seek(0)
        kwargs = {"duration": duration}
        if sr:
            kwargs["sr"] = target_rate
        audio, sample_rate = librosa.load(stream, **kwargs)
        sample_rate = _positive_integer(sample_rate, "decoded sample rate")
        if sample_rate != target_rate:
            raise ValueError("reference decoder returned an unexpected sample rate")
        if (len(audio.shape) != 1 or not 0 < audio.shape[0]
                <= math.ceil(duration * sample_rate) + 2):
            raise ValueError("reference decoder returned samples outside its budget")
    import torch

    audio = torch.tensor(audio).unsqueeze(0)
    maximum = int(seconds * sample_rate)
    if audio.shape[1] > maximum:
        if verbose:
            print(f"Audio too long ({audio.shape[1]} samples), truncating to {maximum} samples")
        audio = audio[:, :maximum]
    return audio, sample_rate


def _guarded_load_and_cut_audio(model, audio_path, max_audio_length_seconds,
                                verbose=False, sr=None):
    return load_bounded_reference(
        audio_path, max_audio_length_seconds, verbose, sr,
        expected=getattr(model, "_wise_reference_audio", None))


def install_reference_audio_guard(model) -> None:
    """Bind only this model instance; never replace global decoder functions."""
    if getattr(model, "_wise_reference_audio_guard", False):
        return
    if not callable(getattr(model, "_load_and_cut_audio", None)):
        raise RuntimeError("IndexTTS reference decoder contract is unavailable")
    model._load_and_cut_audio = MethodType(_guarded_load_and_cut_audio, model)
    model._wise_reference_audio_guard = True
