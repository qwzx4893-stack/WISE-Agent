# ==============================================================================
# WISE Persistent Voice Interface - Windows Microphone Driver
# Architecture: Native Win32 winmm.dll waveIn audio input with zero external deps
# ==============================================================================

from __future__ import annotations

import sys
import time
import queue
import ctypes
import logging
import threading
from typing import Optional, Callable, List
from ctypes import wintypes

LOG = logging.getLogger("WISE.Voice.Microphone")


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [
    ("lpData", ctypes.c_char_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_void_p),
    ("dwFlags", wintypes.DWORD),
    ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(WAVEHDR)),
    ("reserved", ctypes.c_void_p),
]


class WindowsMicrophoneDriver:
    """
    Direct Win32 Multimedia audio input driver.
    Captures 16-bit 16kHz mono PCM frames from default Windows recording device.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_duration_ms: int = 20,
        buffer_count: int = 4,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms
        self.frame_samples = int(sample_rate * (frame_duration_ms / 1000.0))
        self.buffer_size = self.frame_samples * 2 * channels  # 16-bit = 2 bytes per sample
        self.buffer_count = buffer_count

        self._lock = threading.RLock()
        self._is_win32 = sys.platform == "win32"
        self._h_wave_in = wintypes.HANDLE()
        self._is_recording = False
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._synthetic_queue: queue.Queue[bytes] = queue.Queue()

        self._buffers: List[ctypes.Array] = []
        self._headers: List[WAVEHDR] = []
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def is_available(self) -> bool:
        """Checks if a recording device is present in the operating system."""
        if not self._is_win32:
            return False
        try:
            winmm = ctypes.windll.winmm
            return int(winmm.waveInGetNumDevs()) > 0
        except Exception as e:
            LOG.warning("waveInGetNumDevs check failed: %s", e)
            return False

    def get_device_info(self) -> Dict[str, Any]:
        """Returns metadata about physical or virtual recording devices on Windows."""
        num_devs = 0
        if self._is_win32:
            try:
                num_devs = int(ctypes.windll.winmm.waveInGetNumDevs())
            except Exception:
                num_devs = 0
        return {
            "num_devices": num_devs,
            "is_available": self.is_available(),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frame_duration_ms": self.frame_duration_ms,
            "is_recording": self._is_recording,
        }

    def start(self) -> bool:
        """Alias for start_recording."""
        return self.start_recording()

    def stop(self) -> None:
        """Alias for stop_recording."""
        self.stop_recording()

    def start_recording(self) -> bool:
        """Initializes Win32 waveIn device and begins recording audio frames."""
        with self._lock:
            if self._is_recording:
                return True

            if not self.is_available():
                LOG.warning("No physical recording device detected on Windows.")
                return False

            try:
                winmm = ctypes.windll.winmm

                wfx = WAVEFORMATEX()
                wfx.wFormatTag = 1  # WAVE_FORMAT_PCM
                wfx.nChannels = 1
                wfx.nSamplesPerSec = self.sample_rate
                wfx.wBitsPerSample = 16
                wfx.nBlockAlign = 2
                wfx.nAvgBytesPerSec = self.sample_rate * 2
                wfx.cbSize = 0

                # Open default input device (WAVE_MAPPER = -1)
                res = winmm.waveInOpen(
                    ctypes.byref(self._h_wave_in),
                    -1,
                    ctypes.byref(wfx),
                    0,
                    0,
                    0,
                )
                if res != 0:
                    LOG.warning("waveInOpen returned non-zero code: %d", res)
                    return False

                # Allocate buffers & headers
                self._buffers = []
                self._headers = []
                for _ in range(self.buffer_count):
                    buf = ctypes.create_string_buffer(self.buffer_size)
                    hdr = WAVEHDR()
                    hdr.lpData = ctypes.cast(buf, ctypes.c_char_p)
                    hdr.dwBufferLength = self.buffer_size
                    hdr.dwBytesRecorded = 0
                    hdr.dwFlags = 0

                    winmm.waveInPrepareHeader(self._h_wave_in, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
                    winmm.waveInAddBuffer(self._h_wave_in, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))

                    self._buffers.append(buf)
                    self._headers.append(hdr)

                winmm.waveInStart(self._h_wave_in)
                self._is_recording = True
                self._stop_event.clear()

                # Start polling/worker thread for headers
                self._worker_thread = threading.Thread(
                    target=self._capture_loop,
                    name="WISE_WaveInCapture",
                    daemon=True,
                )
                self._worker_thread.start()
                LOG.info("Windows microphone recording started via winmm.dll")
                return True

            except Exception as e:
                LOG.error("Failed to start Windows microphone driver: %s", e)
                self._is_recording = False
                return False

    def _capture_loop(self) -> None:
        """Polls waveIn headers and recycles them."""
        winmm = ctypes.windll.winmm
        WHDR_DONE = 0x00000001

        while not self._stop_event.is_set() and self._is_recording:
            try:
                any_processed = False
                for i, hdr in enumerate(self._headers):
                    if hdr.dwFlags & WHDR_DONE:
                        data = ctypes.string_at(hdr.lpData, hdr.dwBytesRecorded)
                        if data:
                            try:
                                self._audio_queue.put_nowait(data)
                            except queue.Full:
                                pass  # Discard oldest to avoid backlog

                        # Recycle buffer
                        hdr.dwBytesRecorded = 0
                        hdr.dwFlags &= ~WHDR_DONE
                        winmm.waveInAddBuffer(self._h_wave_in, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
                        any_processed = True

                if not any_processed:
                    time.sleep(0.005)
            except Exception as e:
                LOG.error("Exception in capture loop: %s", e)
                break

    def read_frame(self, timeout: float = 0.1) -> Optional[bytes]:
        """Reads a captured PCM audio frame. Returns None if queue is empty."""
        # 1. Synthetic / injected audio has precedence
        try:
            return self._synthetic_queue.get_nowait()
        except queue.Empty:
            pass

        # 2. Real audio from waveIn
        try:
            return self._audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def inject_synthetic_frame(self, pcm_bytes: bytes) -> None:
        """Allows injecting audio frames for deterministic testing."""
        self._synthetic_queue.put_nowait(pcm_bytes)

    def stop_recording(self) -> None:
        """Stops recording and releases waveIn resources."""
        with self._lock:
            if not self._is_recording:
                return

            self._stop_event.set()
            self._is_recording = False

            if self._worker_thread and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=1.0)
                self._worker_thread = None

            if self._is_win32 and self._h_wave_in.value:
                try:
                    winmm = ctypes.windll.winmm
                    winmm.waveInStop(self._h_wave_in)
                    winmm.waveInReset(self._h_wave_in)

                    for hdr in self._headers:
                        winmm.waveInUnprepareHeader(self._h_wave_in, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))

                    winmm.waveInClose(self._h_wave_in)
                except Exception as e:
                    LOG.warning("Error releasing waveIn handle: %s", e)
                finally:
                    self._h_wave_in = wintypes.HANDLE()

            self._headers.clear()
            self._buffers.clear()
            LOG.info("Windows microphone recording stopped cleanly.")
