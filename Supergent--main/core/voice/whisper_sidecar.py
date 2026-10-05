"""Persistent CPU speech recognizer for 16 kHz mono PCM voice turns."""

from __future__ import annotations

import base64
import json
import os
import sys

import numpy as np

_MODEL = None


def transcribe(job: dict) -> dict:
    global _MODEL
    pcm = base64.b64decode(job["pcm16"])
    if len(pcm) < 3200 or len(pcm) % 2:
        return {"ok": False, "error": "audio is too short or not 16-bit PCM"}
    if _MODEL is None:
        from faster_whisper import WhisperModel

        _MODEL = WhisperModel(
            os.environ.get("WISE_STT_MODEL", "base"),
            device="cpu",
            compute_type="int8",
            cpu_threads=max(1, min(4, (os.cpu_count() or 2) // 2)),
            num_workers=1,
            local_files_only=True,
        )
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    requested = str(job.get("language") or "auto").lower()
    language = None if requested == "auto" else requested.split("-")[0]
    segments, info = _MODEL.transcribe(
        samples,
        language=language,
        beam_size=3,
        condition_on_previous_text=False,
        no_speech_threshold=0.55,
        vad_filter=False,  # The live microphone already performs endpointing.
    )
    text = " ".join(part.text.strip() for part in segments if part.text.strip()).strip()
    return {
        "ok": True,
        "text": text,
        "language": info.language,
        "confidence": round(float(info.language_probability), 3),
    }


def main() -> None:
    for line in sys.stdin:
        try:
            job = json.loads(line)
            if job.get("operation") == "shutdown":
                print(json.dumps({"ok": True, "shutdown": True}), flush=True)
                return
            result = transcribe(job)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:800]}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
