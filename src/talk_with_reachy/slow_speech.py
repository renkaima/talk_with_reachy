"""Slow down Reachy's voice without changing its pitch.

The speech service has no speed setting, so the app stretches the audio it receives before playing it. A
profile sets the speed with ``speech_speed`` (the Day Center profile uses 0.55). The stretch uses WSOLA
(Verhelst & Roelands, 1993) from the MIT-licensed audiotsm package. The same code runs in the sister app
Talk with Reachy Math.
"""

from __future__ import annotations

import numpy as np
from audiotsm import wsola
from numpy.typing import NDArray
from audiotsm.io.array import ArrayReader, ArrayWriter


# audiotsm holds back the last part of the input until more arrives; at the end of a response, this much
# silence pushes it out (WSOLA's frame length plus its tolerance, in samples, with room to spare).
_END_PADDING = 2048


def _to_int16(samples: NDArray[np.float64]) -> NDArray[np.int16]:
    return np.clip(np.round(samples.reshape(-1) * 32768.0), -32768, 32767).astype(np.int16)


class SpeechSlower:
    """Stretch one response's audio as it streams in, chunk by chunk.

    Call ``process`` for each chunk, ``flush`` when the response's audio is done, and
    ``reset`` when someone interrupts (the rest of the response is not played).
    """

    def __init__(self, factor: float) -> None:
        """Slow speech to ``factor`` times its speed; 1 or more leaves it unchanged."""
        self.factor = factor
        self._tsm = wsola(1, speed=factor) if factor < 1.0 else None

    def process(self, pcm: NDArray[np.int16]) -> NDArray[np.int16]:
        """Return the slowed audio for one chunk of 16-bit mono samples (it can be shorter or empty)."""
        if self._tsm is None:
            return pcm.reshape(-1)
        reader = ArrayReader(pcm.reshape(1, -1).astype(np.float64) / 32768.0)
        writer = ArrayWriter(1)
        while True:
            self._tsm.read_from(reader)
            _, finished = self._tsm.write_to(writer)
            if finished and reader.empty:
                break
        return _to_int16(writer.data)

    def flush(self) -> NDArray[np.int16]:
        """Return the audio still held back at the end of a response, and get ready for the next one."""
        if self._tsm is None:
            return np.zeros(0, dtype=np.int16)
        tail = self.process(np.zeros(_END_PADDING, dtype=np.int16))
        writer = ArrayWriter(1)
        finished = False
        while not finished:
            _, finished = self._tsm.flush_to(writer)
        self._tsm.clear()
        return np.trim_zeros(np.concatenate([tail, _to_int16(writer.data)]), "b")  # drop the added silence

    def reset(self) -> None:
        """Drop any held-back audio, for example when someone starts talking."""
        if self._tsm is not None:
            self._tsm.clear()
