"""Tests for the study session files: JSONL records, the CSV timeline, events and timing."""

import time
from pathlib import Path
from datetime import datetime

import pytest
from study_helpers import records, csv_rows, wait_for_writes

from talk_with_reachy import study_log


def _utterance(speaker: str, text: str, start_offset_s: float, seconds: float, **fields: object) -> None:
    def write() -> None:
        session = study_log.current_session()
        assert session is not None
        start_wall = time.time() + start_offset_s
        session.utterance(
            speaker,
            text,
            seq=session.next_seq(),
            start_wall=start_wall,
            end_wall=start_wall + seconds,
            start_mono=session.started_mono + 5.0,
            fields=dict(fields),
        )

    study_log.submit(write)


def test_session_has_header_utterances_events_and_footer(study_dir: Path) -> None:
    """Records come out in submission order, between a header and a footer."""
    study_log.start()
    _utterance("person", "Hi Reachy", 0.0, 1.5, speaker_id="P01", match_score=0.71)
    study_log.record_event("tool_started", tool="dance")
    _utterance("reachy", "Hello!", 2.0, 1.0, speaker_id="reachy")
    study_log.stop()

    recs = records(study_dir)
    assert [r["type"] for r in recs] == [
        "session_start",
        "utterance",
        "event",
        "utterance",
        "speaker_summary",
        "speaker_summary",
        "session_end",
    ]
    header = recs[0]
    assert header["schema_version"] == study_log.SCHEMA_VERSION
    assert header["robot_id"] == "robotA"
    assert header["clock_synced"] is True
    assert header["voice_id"] == {"enabled": False}
    assert recs[1]["seq"] == 1 and recs[3]["seq"] == 2
    assert recs[1]["speaker_id"] == "P01" and recs[1]["match_score"] == 0.71
    assert recs[2]["event"] == "tool_started" and recs[2]["tool"] == "dance"
    assert recs[-1]["utterances"] == 2
    assert len({r["session_id"] for r in recs}) == 1


def test_utterance_times_are_unambiguous(study_dir: Path) -> None:
    """Start and end carry a UTC offset; duration and elapsed time are in seconds."""
    study_log.start()
    _utterance("person", "Hello", 0.0, 2.25)
    study_log.stop()

    (utt,) = [r for r in records(study_dir) if r["type"] == "utterance"]
    start = datetime.fromisoformat(str(utt["start"]))
    end = datetime.fromisoformat(str(utt["end"]))
    assert start.tzinfo is not None and end.tzinfo is not None
    assert (end - start).total_seconds() == pytest.approx(2.25, abs=0.002)
    assert utt["duration_s"] == pytest.approx(2.25, abs=0.002)
    assert utt["elapsed_s"] == pytest.approx(5.0, abs=0.01)


def test_csv_timeline_matches_the_jsonl(study_dir: Path) -> None:
    """The CSV has one row per record, readable without the JSONL."""
    study_log.start()
    _utterance(
        "person",
        "你好，Reachy",
        0.0,
        1.0,
        speaker_id="V001",
        speaker_name="Alice",
        match_score=0.5,
        id_status="matched",
        overlaps_reachy=True,
        audio_file="audio/x/0001_V001.wav",
    )
    study_log.record_event("interruption", by="person")
    study_log.stop()

    rows = csv_rows(study_dir)
    assert [r["kind"] for r in rows] == ["event", "utterance", "event", "summary", "event"]
    person = rows[1]
    assert person["speaker"] == "person"
    assert (person["speaker_id"], person["speaker_name"]) == ("V001", "Alice")
    assert person["match_score"] == "0.50"
    assert (person["id_status"], person["flags"]) == ("matched", "overlaps_reachy")
    assert person["text"] == "你好，Reachy"
    assert person["audio_file"] == "audio/x/0001_V001.wav"
    assert person["elapsed"] == "0:00:05.0"
    assert rows[2]["text"] == "interruption by=person"
    assert rows[0]["text"] == "session_start" and rows[-1]["text"] == "session_end"


def test_clock_sync_changes_are_logged(study_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clock that becomes synchronized after boot shows up as an event."""
    state = [False]
    monkeypatch.setattr(study_log, "clock_synced", lambda: state[0])
    monkeypatch.setattr(study_log, "CLOCK_CHECK_INTERVAL_S", 0.0)
    study_log.start()
    wait_for_writes()
    state[0] = True
    study_log.record_event("tick")
    study_log.stop()

    recs = records(study_dir)
    assert recs[0]["clock_synced"] is False
    assert [r.get("synced") for r in recs if r.get("event") == "clock_sync"] == [True]


def test_disabled_logging_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With logging off there is no session and submit is a no-op."""
    monkeypatch.setenv(study_log.LOGGING_ENABLED_ENV, "0")
    monkeypatch.setenv(study_log.DATA_DIR_ENV, str(tmp_path))
    study_log.start()
    study_log.record_event("tool_started")
    assert study_log.submit(lambda: None) is None
    study_log.stop()

    assert not study_log.transcripts_dir(tmp_path).exists()


def test_job_errors_never_reach_the_caller(study_dir: Path) -> None:
    """A failing job is logged and the next one still runs."""
    study_log.start()

    def boom() -> None:
        raise OSError("disk full")

    study_log.submit(boom)
    study_log.record_event("after_failure")
    study_log.stop()

    assert any(r.get("event") == "after_failure" for r in records(study_dir))


def test_file_name_identifies_robot_and_start_time(study_dir: Path) -> None:
    """File name identifies robot and start time; the CSV shares it."""
    study_log.start()
    study_log.stop()

    (path,) = study_log.transcripts_dir(study_dir).glob("*.jsonl")
    robot_id, stamp, short_id = path.stem.split("_")
    assert robot_id == "robotA"
    datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    assert len(short_id) == 6
    assert path.with_suffix(".csv").exists()


def test_format_elapsed() -> None:
    """Elapsed time reads as H:MM:SS.s."""
    assert study_log.format_elapsed(0) == "0:00:00.0"
    assert study_log.format_elapsed(3725.44) == "1:02:05.4"


def test_session_ends_with_a_summary_per_speaker(study_dir: Path) -> None:
    """Each speaker gets one summary with their utterance count, total speech, and latest name."""
    study_log.start()
    _utterance("person", "Hi", 0.0, 1.5, speaker_id="P01")
    _utterance("reachy", "Hello!", 2.0, 1.0, speaker_id="reachy", speaker_name="Reachy")
    _utterance("person", "I'm Alice", 3.0, 2.25, speaker_id="P01", speaker_name="Alice")
    _utterance("person", "And I'm Bob", 6.0, 0.5, speaker_id="V002")
    study_log.stop()

    summaries = [r for r in records(study_dir) if r["type"] == "speaker_summary"]
    assert [(s["speaker_id"], s["speaker_name"], s["utterances"]) for s in summaries] == [
        ("P01", "Alice", 2),
        ("reachy", "Reachy", 1),
        ("V002", "", 1),
    ]
    assert summaries[0]["speech_s"] == pytest.approx(3.75, abs=0.01)
    assert summaries[0]["speaker"] == "person"

    rows = [r for r in csv_rows(study_dir) if r["kind"] == "summary"]
    assert [(r["speaker_id"], r["speaker_name"]) for r in rows] == [
        ("P01", "Alice"),
        ("reachy", "Reachy"),
        ("V002", ""),
    ]
    assert rows[0]["text"] == "2 utterances, 0:00:03.8 of speech"
    assert rows[2]["text"] == "1 utterance, 0:00:00.5 of speech"
