"""Turns realtime conversation events into study records.

The realtime handler calls the methods here from its event loop. They only
note times and copy audio; the slow parts (voice ID, saving the audio clip,
writing the record) run on the study worker thread, in order.

Timing sources:

- **Person utterances.** The server reports where each turn starts and ends in
  the audio stream (``audio_start_ms`` / ``audio_end_ms``). ``AudioTimeline``
  maps those offsets back to the audio the app sent and to the clock times at
  which it was sent.
- **Reachy utterances.** Start is when the first audio for a response arrived
  (playback starts right away). End is start plus the length of the audio,
  or the moment a person started talking over it, whichever is earlier.
"""

from __future__ import annotations
import os
import time
import wave
import asyncio
import logging
from typing import Any
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Callable, Coroutine

import numpy as np
from numpy.typing import NDArray

from talk_with_reachy import voice_id, study_log
from talk_with_reachy.audio_timeline import AudioClip, AudioTimeline


logger = logging.getLogger(__name__)

SAVE_AUDIO_ENV = "TALK_WITH_REACHY_SAVE_AUDIO"
FALLBACK_PREROLL_MS = 300
STALE_TURN_S = 300.0
ANNOUNCE_STATUSES = ("matched", "new_voice", "enrolling", "enrolled")


def save_audio_enabled() -> bool:
    """Return whether utterance audio clips are saved (they are unless the env var is 0)."""
    return os.getenv(SAVE_AUDIO_ENV, "1").strip() != "0"


def write_wav(path: Path, samples: NDArray[np.int16], sample_rate: int) -> None:
    """Write mono 16-bit PCM, via a hidden temp file so the uploader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.astype("<i2").tobytes())
    tmp.replace(path)


@dataclass
class _PersonTurn:
    start_ms: int | None
    end_ms: int | None = None
    clip: AudioClip | None = None
    overlaps_reachy: bool = False
    created_mono: float = field(default_factory=time.monotonic)


@dataclass
class _ReachyTurn:
    start_wall: float
    start_mono: float
    samples: int = 0
    sample_rate: int = 16000
    text: str | None = None
    done: bool = False
    interrupted_mono: float | None = None

    @property
    def playback_end_mono(self) -> float:
        return self.start_mono + (self.samples / self.sample_rate if self.sample_rate else 0.0)


class StudyRecorder:
    """Per-handler bridge between the realtime events and ``study_log`` / ``voice_id``."""

    def __init__(self, notify_model: Callable[[str], Coroutine[Any, Any, None]] | None = None) -> None:
        """``notify_model`` sends a system note to the conversation (who is speaking now)."""
        self.timeline = AudioTimeline()
        self._notify_model = notify_model
        self._loop: asyncio.AbstractEventLoop | None = None
        self._turns: dict[str, _PersonTurn] = {}
        self._reachy: _ReachyTurn | None = None
        self._finished_reachy: list[_ReachyTurn] = []
        self._announced: str | None = None

    @staticmethod
    def active() -> bool:
        """Whether a study session is recording."""
        return study_log.is_active()

    # ── Connection ────────────────────────────────────────────────

    def connection_opened(self) -> None:
        """Start over for a new connection: its audio offsets restart at zero and it has not heard who is speaking."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self.timeline.reset()
        self._turns.clear()
        self._announced = None
        study_log.record_event("backend_connected")

    def connection_closed(self, reason: str) -> None:
        """Record why the connection ended and write any Reachy utterance still pending."""
        self.flush()
        study_log.record_event("backend_disconnected", reason=reason)

    def flush(self) -> None:
        """Write every Reachy utterance that is still waiting for its playback to end."""
        self._finish_reachy_turn()
        self._write_finished_reachy(force=True)

    # ── People ────────────────────────────────────────────────────

    def mic_audio(self, sample_rate: int, samples: NDArray[np.int16]) -> None:
        """Keep a copy of a microphone frame that was just sent to the server."""
        if self.active():
            self.timeline.append(sample_rate, samples)

    def user_speech_started(self, item_id: str, audio_start_ms: int | None) -> None:
        """Note that a person started talking, and whether they talked over Reachy."""
        if not self.active():
            return
        now = time.monotonic()
        overlaps = self._mark_interrupted(now)
        if overlaps:
            study_log.record_event("interruption", now, by="person")
        start_ms = (
            audio_start_ms if audio_start_ms is not None else max(0, self.timeline.now_ms() - FALLBACK_PREROLL_MS)
        )
        self._turns[item_id] = _PersonTurn(start_ms=start_ms, overlaps_reachy=overlaps)
        self._drop_stale_turns(now)

    def user_speech_stopped(self, item_id: str, audio_end_ms: int | None) -> None:
        """Note that a person stopped talking, and cut their audio out while it is still in the buffer."""
        if not self.active():
            return
        turn = self._turns.setdefault(item_id, _PersonTurn(start_ms=None))
        turn.end_ms = audio_end_ms if audio_end_ms is not None else self.timeline.now_ms()
        if turn.start_ms is not None:
            turn.clip = self.timeline.cut(turn.start_ms, turn.end_ms)

    def user_transcript(self, item_id: str, text: str) -> None:
        """Log a person's finished transcript, with voice ID, on the worker thread."""
        if not self.active() or not text.strip():
            return
        turn = self._turns.pop(item_id, None) or _PersonTurn(start_ms=None)
        if turn.clip is None and turn.start_ms is not None:
            turn.clip = self.timeline.cut(turn.start_ms, turn.end_ms or self.timeline.now_ms())
        study_log.submit(self._write_person, turn, text, time.time(), time.monotonic())

    def _drop_stale_turns(self, now: float) -> None:
        for key in [k for k, t in self._turns.items() if now - t.created_mono > STALE_TURN_S]:
            del self._turns[key]

    def _write_person(self, turn: _PersonTurn, text: str, received_wall: float, received_mono: float) -> None:
        """Worker thread: identify the speaker, save the clip, write the record, tell Reachy."""
        session = study_log.current_session()
        if session is None:
            return
        seq = session.next_seq()
        clip = turn.clip
        ident = voice_id.identifier()
        assignment = voice_id.Assignment(status="disabled" if ident is None else "no_audio")
        if clip is not None and ident is not None:
            assignment = ident.assign(clip.samples, clip.sample_rate)
        fields: dict[str, Any] = {
            "speaker_id": assignment.speaker_id,
            "match_score": assignment.match_score,
            "id_status": assignment.status,
        }
        if turn.overlaps_reachy:
            fields["overlaps_reachy"] = True
        if clip is not None and save_audio_enabled():
            relative = f"{study_log.AUDIO_SUBDIR}/{session.stem}/{seq:04d}_{assignment.speaker_id}.wav"
            write_wav(session.data_dir / relative, clip.samples, clip.sample_rate)
            fields["audio_file"] = relative
        if clip is not None:
            start_wall, end_wall, start_mono = clip.start_wall, clip.end_wall, clip.start_mono
        else:
            start_wall, end_wall, start_mono = received_wall, received_wall, received_mono
        session.utterance(
            "person", text, seq=seq, start_wall=start_wall, end_wall=end_wall, start_mono=start_mono, fields=fields
        )
        if ident is not None and assignment.status in ANNOUNCE_STATUSES and assignment.speaker_id != self._announced:
            self._announce(ident, assignment.speaker_id)

    def _announce(self, ident: voice_id.VoiceIdentifier, speaker_id: str) -> None:
        note = ident.identity_note(speaker_id)
        if note is None or self._notify_model is None or self._loop is None:
            return
        self._announced = speaker_id
        asyncio.run_coroutine_threadsafe(self._notify_model(note), self._loop)
        study_log.record_event("speaker_note_sent", speaker_id=speaker_id, note=note)

    # ── Reachy ────────────────────────────────────────────────────

    def response_created(self) -> None:
        """Finish the previous Reachy response; a new one begins."""
        if self.active():
            self._finish_reachy_turn()

    def assistant_audio(self, sample_count: int, sample_rate: int) -> None:
        """Audio for Reachy's current response arrived (playback starts immediately)."""
        if not self.active():
            return
        if self._reachy is None:
            self._reachy = _ReachyTurn(time.time(), time.monotonic(), sample_rate=sample_rate)
        self._reachy.samples += sample_count

    def assistant_transcript(self, text: str) -> None:
        """Store the text of what Reachy said in the current response."""
        if not self.active() or not text.strip():
            return
        if self._reachy is None:
            self._reachy = _ReachyTurn(time.time(), time.monotonic(), done=False)
        self._reachy.text = text
        if self._reachy.done:
            self._finish_reachy_turn()

    def response_done(self) -> None:
        """Mark the current response finished by the server (its audio may still be playing)."""
        if not self.active() or self._reachy is None:
            return
        self._reachy.done = True
        if self._reachy.text is not None:
            self._finish_reachy_turn()

    def _mark_interrupted(self, now: float) -> bool:
        """Mark every Reachy utterance still playing at ``now`` as interrupted."""
        interrupted = False
        for turn in [*self._finished_reachy, *([self._reachy] if self._reachy else [])]:
            if turn.interrupted_mono is None and turn.start_mono <= now < turn.playback_end_mono:
                turn.interrupted_mono = now
                interrupted = True
        if interrupted:
            self._write_finished_reachy(force=True)
        return interrupted

    def _finish_reachy_turn(self) -> None:
        """Move the current response to the queue of utterances waiting for playback to end."""
        turn, self._reachy = self._reachy, None
        if turn is None or turn.text is None:
            return
        self._finished_reachy.append(turn)
        delay = max(0.0, turn.playback_end_mono - time.monotonic())
        if self._loop is None:  # no event loop to wait on; log it now with the planned end
            self._write_finished_reachy(force=True)
        elif turn.interrupted_mono is not None or delay == 0.0:
            self._write_finished_reachy()
        else:
            self._loop.call_later(delay + 0.05, self._write_finished_reachy)

    def _write_finished_reachy(self, force: bool = False) -> None:
        """Log Reachy utterances whose playback has ended (or all of them with ``force``)."""
        now = time.monotonic()
        due = [
            t for t in self._finished_reachy if force or t.interrupted_mono is not None or t.playback_end_mono <= now
        ]
        for turn in due:
            self._finished_reachy.remove(turn)
            study_log.submit(self._write_reachy, turn)

    @staticmethod
    def _write_reachy(turn: _ReachyTurn) -> None:
        session = study_log.current_session()
        if session is None or turn.text is None:
            return
        end_mono = turn.playback_end_mono
        fields: dict[str, Any] = {"speaker_id": "reachy"}
        if turn.interrupted_mono is not None and turn.interrupted_mono < end_mono:
            end_mono = turn.interrupted_mono
            fields["interrupted"] = True
        end_wall = turn.start_wall + (end_mono - turn.start_mono)
        session.utterance(
            "reachy",
            turn.text,
            seq=session.next_seq(),
            start_wall=turn.start_wall,
            end_wall=end_wall,
            start_mono=turn.start_mono,
            fields=fields,
        )
