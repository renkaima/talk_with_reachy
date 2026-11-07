"""Tests for playing Reachy's voice more slowly without changing its pitch."""

import numpy as np
import pytest

from talk_with_reachy.slow_speech import SpeechSlower


RATE = 16000


def _tone(seconds: float, hz: float = 220.0) -> np.ndarray:
    return (np.sin(2 * np.pi * hz * np.arange(int(RATE * seconds)) / RATE) * 8000).astype(np.int16)


def _slow(chunks: list[np.ndarray], factor: float = 0.8) -> np.ndarray:
    slower = SpeechSlower(factor)
    return np.concatenate([*(slower.process(c) for c in chunks), slower.flush()])


def test_slowed_speech_lasts_longer_and_keeps_its_pitch() -> None:
    """At 0.8, one second lasts about 1.25 seconds, and a 440 Hz tone stays at 440 Hz."""
    tone = _tone(1.0, 440.0)
    out = _slow([tone])
    assert 1.21 < out.size / tone.size < 1.29
    spectrum = np.abs(np.fft.rfft(out[2000:18000] * np.hanning(16000)))
    assert abs(np.fft.rfftfreq(16000, 1 / RATE)[np.argmax(spectrum)] - 440) <= 2


def test_streaming_in_small_chunks_gives_the_same_audio_and_keeps_the_end() -> None:
    """Chunks of any size give the same result as one piece, and the last word is not cut off."""
    tone = _tone(0.5)
    whole = _slow([tone])
    pieces = _slow(np.array_split(tone, 37))
    np.testing.assert_array_equal(whole, pieces)
    assert np.abs(whole[-160:]).max() > 4000


def test_a_speed_of_one_plays_the_audio_unchanged_and_reset_drops_held_audio() -> None:
    """Speed 1 passes chunks through; after someone interrupts, nothing held back is played."""
    tone = _tone(0.1)
    np.testing.assert_array_equal(SpeechSlower(1.0).process(tone), tone)
    assert SpeechSlower(1.0).flush().size == 0
    slower = SpeechSlower(0.8)
    slower.process(tone)
    slower.reset()
    assert slower.flush().size == 0


def _pause_lengths(audio: np.ndarray) -> list[float]:
    """Lengths in seconds of the silent stretches between sounds (not before the first or after the last)."""
    quiet = np.abs(audio.astype(np.int32)) < 330
    runs = np.split(quiet, np.flatnonzero(np.diff(quiet.astype(np.int8))) + 1)[1:-1]
    return [run.size / RATE for run in runs if run.all() and run.size > RATE * 0.02]


def test_pauses_between_words_get_longer_but_short_gaps_and_the_ends_do_not() -> None:
    """A 0.2 s pause gains 0.3 s once; a 0.05 s gap, the lead-in and the trailing silence stay as they were."""
    silence = np.zeros(int(RATE * 0.2), dtype=np.int16)
    gap = np.zeros(int(RATE * 0.05), dtype=np.int16)
    speech = np.concatenate([silence, _tone(0.3), gap, _tone(0.3), silence, _tone(0.3), silence])

    slower = SpeechSlower(1.0, added_pause_ms=300, rate=RATE)
    out = np.concatenate([*(slower.process(c) for c in np.array_split(speech, 29)), slower.flush()])

    assert out.size - speech.size == int(RATE * 0.3)
    assert _pause_lengths(out) == pytest.approx([0.05, 0.5], abs=0.002)
    first_sound = np.flatnonzero(np.abs(speech) >= 330)[0]
    assert np.flatnonzero(np.abs(out) >= 330)[0] == first_sound


def test_added_pauses_work_together_with_slower_speech_and_restart_each_response() -> None:
    """With slowing, the stretched pause gains 0.3 s; a new response's lead-in is not lengthened."""
    silence = np.zeros(int(RATE * 0.2), dtype=np.int16)
    speech = np.concatenate([_tone(0.3), silence, _tone(0.3)])
    plain = _slow([speech])
    slower = SpeechSlower(0.8, added_pause_ms=300, rate=RATE)

    first = np.concatenate([slower.process(speech), slower.flush()])
    second = np.concatenate([slower.process(np.concatenate([silence, speech])), slower.flush()])

    assert first.size - plain.size == int(RATE * 0.3)
    assert max(_pause_lengths(first)) - max(_pause_lengths(plain)) == pytest.approx(0.3, abs=0.001)
    assert (second.size - first.size) / RATE == pytest.approx(0.2 / 0.8, abs=0.03)
