"""Slow down Reachy's voice without changing its pitch, and lengthen its pauses.

The speech service has no speed setting, so the app stretches the audio it receives before playing it. A
profile sets the speed with ``speech_speed`` (the Day Center profile uses 0.8). The stretch uses WSOLA
(Verhelst & Roelands, 1993) from the MIT-licensed audiotsm package. The same code runs in the sister app
Talk with Reachy Math.

A profile can also add silence to the pauses the voice already makes between phrases and sentences, with
``added_pause_ms`` (the Day Center profile uses 300). Listeners understand slowed synthetic speech better when
the added time goes into pauses at phrase boundaries than when it is spread evenly over the words
(Higginbotham et al., 1994; Wingfield et al., 1999).
"""

from __future__ import annotations

import numpy as np
from audiotsm import wsola
from numpy.typing import NDArray
from audiotsm.io.array import ArrayReader, ArrayWriter


# audiotsm holds back the last part of the input until more arrives; at the end of a response, this much
# silence pushes it out (WSOLA's frame length plus its tolerance, in samples, with room to spare).
_END_PADDING = 2048
# A sample quieter than this (about -40 dBFS) counts as silence.
_QUIET_LEVEL = 330
# Silence this long is a pause between phrases or sentences. Gaps inside words, such as before a "p" or "t",
# are shorter.
_MIN_PAUSE_S = 0.15


def _to_int16(samples: NDArray[np.float64]) -> NDArray[np.int16]:
    return np.clip(np.round(samples.reshape(-1) * 32768.0), -32768, 32767).astype(np.int16)


class SpeechSlower:
    """Stretch one response's audio as it streams in, chunk by chunk, and lengthen its pauses.

    Call ``process`` for each chunk, ``flush`` when the response's audio is done, and
    ``reset`` when someone interrupts (the rest of the response is not played).
    """

    def __init__(self, factor: float, added_pause_ms: int = 0, rate: int = 16000) -> None:
        """Slow speech to ``factor`` times its speed (1 or more leaves it unchanged).

        Each pause of at least 0.15 s between words also gets ``added_pause_ms`` of extra silence; ``rate``
        is the sample rate of the audio.
        """
        self.factor = factor
        self._tsm = wsola(1, speed=factor) if factor < 1.0 else None
        self._added_pause = np.zeros(int(rate * added_pause_ms / 1000), dtype=np.int16)
        self._min_pause = int(rate * _MIN_PAUSE_S)
        self._start_response()

    def _start_response(self) -> None:
        self._quiet_samples = 0
        self._has_spoken = False

    def _lengthen_pauses(self, pcm: NDArray[np.int16]) -> NDArray[np.int16]:
        """Add silence where the voice resumes after a pause of at least the minimum length.

        Only pauses between words get longer: the silence before the first word and after the last one does
        not, so Reachy starts and finishes speaking as soon as before.
        """
        if self._added_pause.size == 0 or pcm.size == 0:
            return pcm
        quiet = np.abs(pcm.astype(np.int32)) < _QUIET_LEVEL
        # Split the chunk into runs of quiet and of sound; a pause can continue from the previous chunk.
        edges = np.flatnonzero(np.diff(quiet.astype(np.int8))) + 1
        pieces: list[NDArray[np.int16]] = []
        for start, end in zip(np.concatenate([[0], edges]), np.concatenate([edges, [pcm.size]])):
            if quiet[start]:
                self._quiet_samples += end - start
            else:
                if self._has_spoken and self._quiet_samples >= self._min_pause:
                    pieces.append(self._added_pause)
                self._quiet_samples = 0
                self._has_spoken = True
            pieces.append(pcm[start:end])
        return np.concatenate(pieces)

    def _stretch(self, pcm: NDArray[np.int16]) -> NDArray[np.int16]:
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

    def process(self, pcm: NDArray[np.int16]) -> NDArray[np.int16]:
        """Return the slowed audio for one chunk of 16-bit mono samples (it can be shorter or empty)."""
        return self._lengthen_pauses(self._stretch(pcm))

    def flush(self) -> NDArray[np.int16]:
        """Return the audio still held back at the end of a response, and get ready for the next one."""
        if self._tsm is None:
            self._start_response()
            return np.zeros(0, dtype=np.int16)
        tail = self._stretch(np.zeros(_END_PADDING, dtype=np.int16))
        writer = ArrayWriter(1)
        finished = False
        while not finished:
            _, finished = self._tsm.flush_to(writer)
        self._tsm.clear()
        tail = self._lengthen_pauses(np.concatenate([tail, _to_int16(writer.data)]))
        self._start_response()
        return np.trim_zeros(tail, "b")  # drop the silence added to push the audio out

    def reset(self) -> None:
        """Drop any held-back audio, for example when someone starts talking."""
        if self._tsm is not None:
            self._tsm.clear()
        self._start_response()
