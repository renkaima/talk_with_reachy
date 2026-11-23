"""Spoken "Breathe in." / "Breathe out." cues for the breathing break, in Reachy's own voice.

A tester found that the breathing activity only moved Reachy's head and never said when to breathe in or out
(2026-11-20). The cues must sound like Reachy and land on the head's movement, so they are not asked of the model
during the activity (its replies come one to three seconds late). Instead the realtime handler asks the speech
server once, outside the conversation, to say each cue, keeps the audio slowed like the rest of Reachy's speech,
and saves it here. The breathing activity then plays the saved files in time with the head. Without saved cues the
activity runs as before, silently.
"""

from __future__ import annotations
import re
import wave
import logging
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from talk_with_reachy import study_log


logger = logging.getLogger(__name__)

CUES = {"in": "Breathe in.", "out": "Breathe out."}
CUE_DIRNAME = "breath_cues"
# A cue that came back as audio but without text is kept only if it is about as long as two words.
MIN_CUE_S = 0.3
MAX_CUE_S = 3.0
_SILENCE_LEVEL = 300  # int16 amplitude treated as silence when trimming
_EDGE_PAD_S = 0.05

_voice = ""
_speed = 1.0


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-") or "default"


def cue_path(name: str, voice: str | None = None, speed: float | None = None) -> Path:
    """Return where the cue ``name`` is saved for a voice and a speech speed."""
    voice = _voice if voice is None else voice
    speed = _speed if speed is None else speed
    folder = study_log.configured_data_dir() / CUE_DIRNAME
    return folder / f"{_slug(voice)}_{round(speed * 100)}_{name}.wav"


def set_current(voice: str | None, speed: float) -> None:
    """Remember the voice and speed of the running session, so the breathing activity finds matching cues."""
    global _voice, _speed
    _voice, _speed = voice or "", speed


def current_paths() -> dict[str, Path] | None:
    """Return the saved cues for the running session's voice and speed, or None if any is missing."""
    paths = {name: cue_path(name) for name in CUES}
    return paths if all(path.is_file() for path in paths.values()) else None


def missing() -> list[str]:
    """Return the names of the cues not saved yet for the running session's voice and speed."""
    return [name for name in CUES if not cue_path(name).is_file()]


def matches(name: str, text: str) -> bool:
    """Tell whether the speech server said exactly the cue (case and punctuation aside)."""

    def words(value: str) -> str:
        return re.sub(r"[^a-z ]+", "", value.lower()).strip()

    return words(text) == words(CUES[name])


def trim(pcm: NDArray[np.int16], rate: int) -> NDArray[np.int16]:
    """Cut the silence before and after the words, keeping a short pad."""
    loud = np.flatnonzero(np.abs(pcm.astype(np.int32)) > _SILENCE_LEVEL)
    if loud.size == 0:
        return pcm[:0]
    pad = int(_EDGE_PAD_S * rate)
    return pcm[max(0, int(loud[0]) - pad) : min(pcm.size, int(loud[-1]) + pad + 1)]


def save(name: str, pcm: NDArray[np.int16], rate: int, text: str) -> bool:
    """Save a captured cue if it is the right words; return whether it was saved."""
    pcm = trim(pcm.reshape(-1), rate)
    seconds = pcm.size / rate
    if text.strip():
        right = matches(name, text)
    else:
        right = MIN_CUE_S <= seconds <= MAX_CUE_S
    if not right or seconds < MIN_CUE_S:
        logger.warning("Breath cue %r not saved (heard %r, %.2f s)", name, text, seconds)
        study_log.record_event("breath_cue_failed", cue=name, said=text[:80], seconds=round(seconds, 2))
        return False
    path = cue_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.astype(np.int16).tobytes())
    tmp.replace(path)
    study_log.record_event("breath_cue_saved", cue=name, seconds=round(seconds, 2), voice=_voice, speed=_speed)
    logger.info("Breath cue %r saved to %s (%.2f s)", name, path, seconds)
    return True


def is_cue_echo(text: str) -> bool:
    """Tell whether words heard during breathing are only Reachy's own cue coming back through the microphone."""
    norm = re.sub(r"[^a-z ]+", "", text.lower()).strip()
    return norm in {"breathe in", "breathe out", "breath in", "breath out", "breathe", "in", "out"}
