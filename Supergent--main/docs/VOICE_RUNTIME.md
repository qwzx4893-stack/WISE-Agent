# WISE voice runtime

The supported voice stack is deliberately small and has one production path:

- TTS: the approved WISE IndexTTS-2.5 checkpoint at
  `assets/voice/models/wise-indextts-2.5-gpt.pth`, using
  `assets/voice/wise_indextts_reference_canonical.wav`.
- STT: a persistent `faster-whisper` **base** worker on CPU.
- TTS worker: the isolated official IndexTTS runtime in `tools/index-tts`.

## GPU ownership rule

Voice synthesis shares the GPU only when it is safe to do so.  The default
`balanced` profile uses BF16, single-beam decoding and word-safe 56-token
segments.  It therefore needs at least 5200 MiB of free VRAM before starting a
new voice worker, or 1200 MiB while an existing GPU voice worker is already
warm.  If the condition is not met, voice does **not** fall back to CPU and
does **not** start microphone capture.  The API and the web UI report:

> تعذر تشغيل صوت وايز: ذاكرة بطاقة الرسوم المتاحة غير كافية مع النموذج المحلي.

This protects the local model from eviction and out-of-memory failures.  With
a cloud model selected, TTS may use CPU as a functional fallback.

Selecting a local model also releases any warm GPU voice worker first.  The
voice worker unloads itself after 45 seconds of inactivity (configurable in
settings), and releases CUDA's temporary cache after every generated reply.

The optional `quality` profile retains the former three-beam / 80-token
decoding for a workstation devoted to speech, at a materially higher transient
VRAM cost.  It is deliberately not the laptop default.

## User behaviour

The listener is event-driven: VAD runs while voice mode is active; STT, the
agent turn, and TTS run only after speech is detected.  Stopping playback or
speaking while WISE is talking triggers barge-in and returns the runtime to
listening.  A headset is recommended: this runtime does not claim acoustic
echo cancellation, so laptop speakers can be picked up by the microphone.

Voice remains an optional interaction mode.  The text chat, model selection,
and the normal agent runtime work when voice is unavailable.

## Retained files

The runtime requires the canonical reference, approved checkpoint, the
official IndexTTS runtime and its weights, and `.voice-venv` for STT.  Old RVC,
SILMA, LoRA/training and evaluation artefacts are not runtime dependencies.
