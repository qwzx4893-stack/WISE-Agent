import numpy as np
import soundcard as sc


class AudioInputStream:
    def __init__(self, channels=1, rate=16000, frames_per_buffer=1024):
        self.channels = channels
        self.rate = rate
        self.frames_per_buffer = frames_per_buffer
        self.sample_width = 2
        self._microphone = None
        self._recorder = None
        self._is_open = False
        self.device_name = None

    def open(self):
        if self._is_open:
            return

        microphones = sc.all_microphones(include_loopback=False)

        try:
            self._microphone = sc.default_microphone()
        except Exception as error:
            if not microphones:
                raise RuntimeError('No capture device found') from error

            self._microphone = microphones[0]

        self.device_name = str(self._microphone)
        self._recorder = self._microphone.recorder(
            samplerate=self.rate,
            channels=self.channels,
            blocksize=self.frames_per_buffer
        )
        self._recorder.__enter__()
        self._is_open = True

    def read(self, frames, exception_on_overflow=False):
        del exception_on_overflow
        audio_data = self._recorder.record(numframes=frames)
        audio_data = np.clip(audio_data, -1.0, 1.0)
        pcm_data = (audio_data * 32767.0).astype(np.int16)

        return pcm_data.tobytes()

    def stop_stream(self):
        return

    def close(self):
        self._is_open = False

        if self._recorder:
            self._recorder.__exit__(None, None, None)
            self._recorder = None

        self._microphone = None
