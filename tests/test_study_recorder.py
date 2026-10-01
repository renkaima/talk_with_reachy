"""Tests for turning realtime events into timed, speaker-labeled study records."""

import wave
import asyncio
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock

import numpy as np
import pytest
from study_helpers import SR, FakeEmbedder, tone, basis, records, csv_rows, wait_for_writes

from talk_with_reachy import voice_id, study_log
from talk_with_reachy.study_recorder import StudyRecorder


A, B = 1, 2


@pytest.fixture
def ident(study_dir: Path, monkeypatch: pytest.MonkeyPatch) -> voice_id.VoiceIdentifier:
    """Install a voice identifier with a fake model as the app's identifier."""
    identifier = voice_id.VoiceIdentifier(study_dir, FakeEmbedder({A: basis(0), B: basis(1)}))
    monkeypatch.setattr(voice_id, "_identifier", identifier)
    return identifier


def _send(recorder: StudyRecorder, key: int, seconds: float) -> None:
    """Pretend the mic sent ``seconds`` of one voice, in 100 ms frames."""
    for _ in range(int(seconds * 10)):
        recorder.mic_audio(SR, tone(key, 0.1))


def _person_turn(recorder: StudyRecorder, item: str, key: int, seconds: float, text: str) -> None:
    start = recorder.timeline.now_ms()
    _send(recorder, key, seconds)
    recorder.user_speech_started(item, start)
    recorder.user_speech_stopped(item, recorder.timeline.now_ms())
    recorder.user_transcript(item, text)


def _utterances(data_dir: Path) -> list[dict[str, object]]:
    return [r for r in records(data_dir) if r["type"] == "utterance"]


def test_person_utterance_gets_audio_times_speaker_and_clip(study_dir: Path, ident: voice_id.VoiceIdentifier) -> None:
    """Start and end come from the audio offsets; the clip is saved and named after the speaker."""
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    _send(recorder, A, 1.0)  # silence before the turn
    recorder.user_speech_started("item1", 1000)
    _send(recorder, A, 2.5)
    recorder.user_speech_stopped("item1", 3500)
    recorder.user_transcript("item1", "Hello Reachy")
    study_log.stop()

    (utt,) = _utterances(study_dir)
    assert utt["speaker"] == "person"
    assert (utt["speaker_id"], utt["id_status"]) == ("V001", "new_voice")
    assert utt["duration_s"] == pytest.approx(2.5, abs=0.01)
    start, end = (datetime.fromisoformat(str(utt[k])) for k in ("start", "end"))
    assert (end - start).total_seconds() == pytest.approx(2.5, abs=0.01)
    assert utt["audio_file"] == f"audio/{Path(str(utt['audio_file'])).parent.name}/0001_V001.wav"
    with wave.open(str(study_dir / str(utt["audio_file"]))) as clip:
        assert (clip.getframerate(), clip.getnframes()) == (SR, int(2.5 * SR))


def test_a_new_connection_restarts_the_audio_offsets(study_dir: Path, ident: voice_id.VoiceIdentifier) -> None:
    """After a reconnect, offset 0 means the first audio sent on the new connection."""
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    _send(recorder, A, 3.0)
    recorder.connection_opened()  # the websocket reconnected
    _send(recorder, B, 3.0)
    recorder.user_speech_started("item1", 0)
    recorder.user_speech_stopped("item1", 3000)
    recorder.user_transcript("item1", "Hello again")
    study_log.stop()

    (utt,) = _utterances(study_dir)
    with wave.open(str(study_dir / str(utt["audio_file"]))) as clip:
        samples = np.frombuffer(clip.readframes(clip.getnframes()), dtype=np.int16)
    assert set(samples.tolist()) == {B}


def test_audio_saving_can_be_switched_off(
    study_dir: Path, ident: voice_id.VoiceIdentifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With TALK_WITH_REACHY_SAVE_AUDIO=0 the speaker is still identified but no clip is kept."""
    monkeypatch.setenv("TALK_WITH_REACHY_SAVE_AUDIO", "0")
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    _person_turn(recorder, "item1", A, 2.5, "Hello")
    study_log.stop()

    (utt,) = _utterances(study_dir)
    assert utt["speaker_id"] == "V001" and "audio_file" not in utt
    assert not (study_dir / "audio").exists()


def test_reachy_utterance_ends_when_its_audio_ends(study_dir: Path) -> None:
    """Reachy's end time is its start plus the length of the audio it played."""
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    recorder.response_created()
    recorder.assistant_audio(2 * SR, SR)
    recorder.assistant_audio(SR, SR)
    recorder.assistant_transcript("Want to see a dance?")
    recorder.response_done()
    study_log.stop()

    (utt,) = _utterances(study_dir)
    assert (utt["speaker"], utt["speaker_id"], utt["text"]) == ("reachy", "reachy", "Want to see a dance?")
    assert utt["duration_s"] == pytest.approx(3.0, abs=0.01)
    assert "interrupted" not in utt


def test_talking_over_reachy_is_marked_on_both_sides(study_dir: Path, ident: voice_id.VoiceIdentifier) -> None:
    """A person starting while Reachy's audio still plays cuts Reachy short and is logged as an interruption."""
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    recorder.response_created()
    recorder.assistant_audio(10 * SR, SR)  # ten seconds still to play
    recorder.assistant_transcript("Let me tell you a long story about")
    start = recorder.timeline.now_ms()
    _send(recorder, A, 2.5)
    recorder.user_speech_started("item1", start)
    recorder.response_done()  # the server cancels Reachy's response
    recorder.user_speech_stopped("item1", recorder.timeline.now_ms())
    recorder.user_transcript("item1", "Wait, Reachy")
    study_log.stop()

    reachy, person = _utterances(study_dir)
    assert reachy["interrupted"] is True and reachy["duration_s"] < 1.0
    assert person["overlaps_reachy"] is True
    assert any(r.get("event") == "interruption" for r in records(study_dir))
    assert [row["speaker"] for row in csv_rows(study_dir) if row["kind"] == "utterance"] == ["reachy", "person"]


def test_without_voice_id_people_are_logged_as_unknown(study_dir: Path) -> None:
    """If voice ID is off, utterances are still logged with times and audio."""
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    _person_turn(recorder, "item1", A, 1.5, "Hi")
    study_log.stop()

    (utt,) = _utterances(study_dir)
    assert (utt["speaker_id"], utt["id_status"]) == ("unknown", "disabled")
    assert "audio_file" in utt


def test_transcript_without_speech_events_is_still_logged(study_dir: Path) -> None:
    """A transcript the recorder never saw start is logged at its arrival time, without audio."""
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    recorder.user_transcript("orphan", "Hello?")
    recorder.user_transcript("orphan2", "   ")
    study_log.stop()

    (utt,) = _utterances(study_dir)
    assert utt["text"] == "Hello?" and utt["duration_s"] == 0.0 and "audio_file" not in utt


def test_nothing_is_recorded_without_a_session(tmp_path: Path) -> None:
    """With logging off the recorder ignores events and keeps no audio."""
    recorder = StudyRecorder()
    recorder.connection_opened()
    _person_turn(recorder, "item1", A, 2.0, "Hi")
    assert recorder.timeline.now_ms() == 0


@pytest.mark.asyncio
async def test_reachy_is_told_who_is_speaking_only_when_it_changes(
    study_dir: Path, ident: voice_id.VoiceIdentifier
) -> None:
    """A system note goes to the model on the first utterance of each new speaker."""
    notify = AsyncMock()
    study_log.start()
    recorder = StudyRecorder(notify_model=notify)
    recorder.connection_opened()

    for item, key in (("i1", A), ("i2", A), ("i3", B), ("i4", A)):
        _person_turn(recorder, item, key, 2.5, "hello")
        wait_for_writes()
        await asyncio.sleep(0.05)

    notes = [call.args[0] for call in notify.await_args_list]
    assert len(notes) == 3
    assert "V001" in notes[0] and "V002" in notes[1] and "V001" in notes[2]
    study_log.stop()
    sent = [r["speaker_id"] for r in records(study_dir) if r.get("event") == "speaker_note_sent"]
    assert sent == ["V001", "V002", "V001"]


@pytest.mark.asyncio
async def test_reachy_utterance_waits_for_playback_before_logging(study_dir: Path) -> None:
    """With an event loop, Reachy's record is written once its audio has finished playing."""
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    recorder.response_created()
    recorder.assistant_audio(int(0.2 * SR), SR)
    recorder.assistant_transcript("Hi!")
    recorder.response_done()

    wait_for_writes()
    assert _utterances(study_dir) == []
    await asyncio.sleep(0.4)
    wait_for_writes()
    assert [u["text"] for u in _utterances(study_dir)] == ["Hi!"]


def test_identification_runs_off_the_event_loop(study_dir: Path, ident: voice_id.VoiceIdentifier) -> None:
    """The recorder only queues work; the model runs on the study worker thread."""
    study_log.start()
    recorder = StudyRecorder()
    recorder.connection_opened()
    embedder = ident._embedder
    assert isinstance(embedder, FakeEmbedder)
    _person_turn(recorder, "item1", A, 2.5, "Hello")
    wait_for_writes()
    assert embedder.calls == 1
    np.testing.assert_array_equal(ident.library.voices["V001"].embedding, embedder.vectors[A])
