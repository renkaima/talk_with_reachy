"""Slow down Reachy's voice without changing its pitch.

The speech service has no speed setting, so the app slows the audio it receives before playing it. A profile
sets the speed with ``speech_speed`` (the Day Center profile uses 0.55). The slowing uses Sonic (Bill Cox), a
pitch-synchronous method made for speech: it repeats whole pitch periods of the voice, so slowed vowels stay
smooth. It comes from the Apache-2.0 libsonic package, a Rust port that matches sonic.c sample for sample.
Version 1.3.5 used WSOLA from audiotsm instead; at 55% speed it made the voice sound choppy.
"""

from __future__ import annotations

import numpy as np
from libsonic import SonicStream
from numpy.typing import NDArray


def _to_int16(samples: list[float]) -> NDArray[np.int16]:
    return np.clip(np.round(np.asarray(samples, dtype=np.float64) * 32768.0), -32768, 32767).astype(np.int16)


class SpeechSlower:
    """Slow one response's audio as it streams in, chunk by chunk.

    Call ``process`` for each chunk, ``flush`` when the response's audio is done, and
    ``reset`` when someone interrupts (the rest of the response is not played).
    """

    def __init__(self, factor: float, rate: int = 16000) -> None:
        """Slow speech to ``factor`` times its speed (1 or more leaves it unchanged); ``rate`` is the sample rate."""
        self.factor = factor
        self.rate = rate
        self._stream = self._new_stream() if factor < 1.0 else None

    def _new_stream(self) -> SonicStream:
        stream = SonicStream(self.rate, 1)
        stream.set_speed(self.factor)
        return stream

    def _read_available(self) -> NDArray[np.int16]:
        assert self._stream is not None
        frames = self._stream.available_frames()
        return _to_int16(self._stream.read(frames)) if frames else np.zeros(0, dtype=np.int16)

    def process(self, pcm: NDArray[np.int16]) -> NDArray[np.int16]:
        """Return the slowed audio for one chunk of 16-bit mono samples (it can be shorter or empty)."""
        if self._stream is None:
            return pcm.reshape(-1)
        self._stream.write((pcm.reshape(-1).astype(np.float32) / 32768.0).tolist())
        return self._read_available()

    def flush(self) -> NDArray[np.int16]:
        """Return the audio still held back at the end of a response, and get ready for the next one."""
        if self._stream is None:
            return np.zeros(0, dtype=np.int16)
        self._stream.flush()
        tail = self._read_available()
        self._stream = self._new_stream()
        return tail

    def reset(self) -> None:
        """Drop any held-back audio, for example when someone starts talking."""
        if self._stream is not None:
            self._stream = self._new_stream()
