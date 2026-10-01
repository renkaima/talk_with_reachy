"""Tests for cutting person turns out of the sent microphone audio."""

import numpy as np
import pytest

from talk_with_reachy.audio_timeline import AudioTimeline


SR = 16000


WRAP = 30000  # sample values count up and wrap here to stay within int16


def _value(seconds: float) -> int:
    return int(seconds * SR) % WRAP


def _feed(timeline: AudioTimeline, seconds: float, start_wall: float = 1000.0, chunk_s: float = 0.1) -> None:
    """Send ``seconds`` of audio whose sample values count up, in 100 ms chunks."""
    chunk = int(chunk_s * SR)
    sent = 0
    total = int(seconds * SR)
    while sent < total:
        n = min(chunk, total - sent)
        samples = (np.arange(sent, sent + n) % WRAP).astype(np.int16)
        sent += n
        timeline.append(SR, samples, wall=start_wall + sent / SR)


def test_cut_returns_exactly_the_requested_span() -> None:
    """Server offsets map to the same samples the app sent."""
    timeline = AudioTimeline()
    _feed(timeline, 3.0)

    clip = timeline.cut(500, 2000)

    assert clip is not None
    assert clip.duration_s == pytest.approx(1.5)
    assert clip.samples[0] == _value(0.5)
    assert clip.samples[-1] == _value(2.0) - 1


def test_cut_gives_the_wall_clock_time_of_each_end() -> None:
    """A chunk is stamped when sent, so a sample's time counts back from its chunk's end."""
    timeline = AudioTimeline()
    _feed(timeline, 3.0, start_wall=1000.0)

    clip = timeline.cut(500, 2000)

    assert clip is not None
    assert clip.start_wall == pytest.approx(1000.5)
    assert clip.end_wall == pytest.approx(1002.0)


def test_old_audio_is_dropped_but_offsets_keep_counting() -> None:
    """The buffer is bounded; offsets stay relative to the start of the connection."""
    timeline = AudioTimeline(max_seconds=1.0)
    _feed(timeline, 3.0)

    assert timeline.now_ms() == 3000
    assert timeline.cut(0, 1000) is None
    clip = timeline.cut(2500, 3000)
    assert clip is not None and clip.samples[0] == _value(2.5)


def test_reset_starts_a_new_connection_at_zero() -> None:
    """A new connection counts from zero again."""
    timeline = AudioTimeline()
    _feed(timeline, 1.0)
    timeline.reset()

    assert timeline.now_ms() == 0
    assert timeline.cut(0, 500) is None
