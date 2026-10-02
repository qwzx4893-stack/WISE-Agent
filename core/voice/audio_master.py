"""Conservative final mastering for the approved WISE voice."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt


def polish(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Remove subsonic rumble and slightly tame bass without changing timbre."""
    high_pass = butter(2, 45, btype="highpass", fs=sample_rate, output="sos")
    result = sosfiltfilt(high_pass, samples, axis=0)

    amplitude = 10 ** (-0.5 / 40)
    omega = 2 * np.pi * 170 / sample_rate
    cosine, sine = np.cos(omega), np.sin(omega)
    alpha = sine / np.sqrt(2)
    root = 2 * np.sqrt(amplitude) * alpha
    b0 = amplitude * ((amplitude + 1) - (amplitude - 1) * cosine + root)
    b1 = 2 * amplitude * ((amplitude - 1) - (amplitude + 1) * cosine)
    b2 = amplitude * ((amplitude + 1) - (amplitude - 1) * cosine - root)
    a0 = (amplitude + 1) + (amplitude - 1) * cosine + root
    a1 = -2 * ((amplitude - 1) + (amplitude + 1) * cosine)
    a2 = (amplitude + 1) + (amplitude - 1) * cosine - root
    shelf = np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])
    result = sosfiltfilt(shelf, result, axis=0)
    peak = float(np.max(np.abs(result)))
    if peak:
        result *= min(1.0, 10 ** (-1 / 20) / peak)
    return result.astype(np.float32)
