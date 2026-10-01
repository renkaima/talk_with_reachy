"""Recent microphone audio, indexed the same way the realtime server indexes it.

The server reports each person's turn as ``audio_start_ms`` / ``audio_end_ms``:
milliseconds of audio it has received on the current connection. This module
keeps the audio the app actually sent on that connection, so a turn can be cut
out exactly and given wall-clock and monotonic times.
"""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class AudioClip:
    """A cut-out piece of microphone audio with its times."""

    samples: NDArray[np.int16]
    sample_rate: int
    start_wall: float
    end_wall: float
    start_mono: float

    @property
    def duration_s(self) -> float:
        """Length of the clip in seconds."""
        return len(self.samples) / self.sample_rate if self.sample_rate else 0.0


@dataclass
class _Chunk:
    first_sample: int
    samples: NDArray[np.int16]
    sent_wall: float
    sent_mono: float


class AudioTimeline:
    """Ring buffer of the audio sent on one realtime connection."""

    def __init__(self, max_seconds: float = 120.0, sample_rate: int = 16000) -> None:
        """Keep at most ``max_seconds`` of audio."""
        self.max_seconds = max_seconds
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._chunks: list[_Chunk] = []
        self._total = 0
        self._kept = 0

    def reset(self) -> None:
        """Forget all audio; call when a new connection starts counting from zero."""
        with self._lock:
            self._chunks.clear()
            self._total = 0
            self._kept = 0

    def append(self, sample_rate: int, samples: NDArray[np.int16], wall: float | None = None) -> None:
        """Record a frame that was just sent to the server."""
        if samples.size == 0:
            return
        sent_wall = time.time() if wall is None else wall
        sent_mono = time.monotonic()
        with self._lock:
            if sample_rate != self.sample_rate:
                self.sample_rate = sample_rate
            self._chunks.append(_Chunk(self._total, samples.copy(), sent_wall, sent_mono))
            self._total += len(samples)
            self._kept += len(samples)
            limit = int(self.max_seconds * self.sample_rate)
            while self._chunks and self._kept - len(self._chunks[0].samples) >= limit:
                self._kept -= len(self._chunks.pop(0).samples)

    def now_ms(self) -> int:
        """Milliseconds of audio sent so far on this connection."""
        with self._lock:
            return int(self._total * 1000 / self.sample_rate)

    def cut(self, start_ms: int, end_ms: int) -> AudioClip | None:
        """Return the audio between two server offsets, or None if none of it is still kept."""
        with self._lock:
            if not self._chunks or end_ms <= start_ms:
                return None
            rate = self.sample_rate
            first = max(int(start_ms * rate / 1000), self._chunks[0].first_sample)
            last = min(int(end_ms * rate / 1000), self._total)
            if last <= first:
                return None
            pieces: list[NDArray[np.int16]] = []
            for chunk in self._chunks:
                chunk_end = chunk.first_sample + len(chunk.samples)
                if chunk_end <= first or chunk.first_sample >= last:
                    continue
                lo = max(first - chunk.first_sample, 0)
                hi = min(last - chunk.first_sample, len(chunk.samples))
                pieces.append(chunk.samples[lo:hi])
            start_wall, start_mono = self._times_at(first)
        # The sample count is exact; send times can bunch up, so only the start uses them.
        return AudioClip(np.concatenate(pieces), rate, start_wall, start_wall + (last - first) / rate, start_mono)

    def _times_at(self, sample: int) -> tuple[float, float]:
        """Wall and monotonic time at which ``sample`` was sent (interpolated within its chunk)."""
        chunk = self._chunks[-1]
        for candidate in self._chunks:
            if candidate.first_sample + len(candidate.samples) >= sample:
                chunk = candidate
                break
        # A chunk is sent right after its last sample was captured, so count back from there.
        behind_s = (chunk.first_sample + len(chunk.samples) - sample) / self.sample_rate
        return chunk.sent_wall - behind_s, chunk.sent_mono - behind_s
