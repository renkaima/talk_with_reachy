"""Tests for study transcript logging."""

import json
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock

import httpx
import pytest

from talk_with_reachy import study_log, onedrive_upload
from talk_with_reachy.console import LocalStream


@pytest.fixture
def logging_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable logging into a temp dir with OneDrive upload switched off."""
    monkeypatch.setenv(study_log.LOGGING_ENABLED_ENV, "1")
    monkeypatch.setenv(study_log.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(study_log.ROBOT_ID_ENV, "robotA")
    monkeypatch.delenv(onedrive_upload.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(onedrive_upload, "CLIENT_ID", "")
    yield tmp_path
    study_log.stop()


def _read_records(data_dir: Path) -> list[dict[str, object]]:
    files = list(study_log.transcripts_dir(data_dir).glob("*.jsonl"))
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def test_logs_people_and_reachy_between_header_and_footer(logging_on: Path) -> None:
    """Logs people and reachy between header and footer."""
    study_log.start()
    study_log.record_utterance("user", "Hi Reachy", True)
    study_log.record_utterance("assistant", "Hello! Nice to see you.", True)
    study_log.stop()

    records = _read_records(logging_on)
    assert [r["type"] for r in records] == ["session_start", "utterance", "utterance", "session_end"]
    assert records[0]["robot_id"] == "robotA"
    assert [(r["seq"], r["speaker"], r["text"]) for r in records[1:3]] == [
        (1, "person", "Hi Reachy"),
        (2, "reachy", "Hello! Nice to see you."),
    ]
    assert records[3]["utterances"] == 2
    assert len({r["session_id"] for r in records}) == 1
    assert all(datetime.fromisoformat(str(r["time"])).tzinfo is not None for r in records)


def test_skips_partial_and_blank_text(logging_on: Path) -> None:
    """Skips partial and blank text."""
    study_log.start()
    study_log.record_utterance("user", "hel", False)
    study_log.record_utterance("user", "   ", True)
    study_log.stop()

    assert [r["type"] for r in _read_records(logging_on)] == ["session_start", "session_end"]


def test_keeps_non_ascii_text_readable(logging_on: Path) -> None:
    """Keeps non ascii text readable."""
    study_log.start()
    study_log.record_utterance("user", "你好，Reachy", True)
    study_log.stop()

    raw = next(study_log.transcripts_dir(logging_on).glob("*.jsonl")).read_text(encoding="utf-8")
    assert "你好，Reachy" in raw


def test_file_name_identifies_robot_and_start_time(logging_on: Path) -> None:
    """File name identifies robot and start time."""
    study_log.start()
    study_log.stop()

    (path,) = study_log.transcripts_dir(logging_on).glob("*.jsonl")
    robot_id, stamp, short_id = path.stem.split("_")
    assert robot_id == "robotA"
    datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    assert len(short_id) == 6


def test_disabled_logging_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled logging writes nothing."""
    monkeypatch.setenv(study_log.LOGGING_ENABLED_ENV, "0")
    monkeypatch.setenv(study_log.DATA_DIR_ENV, str(tmp_path))
    study_log.start()
    study_log.record_utterance("user", "Hi", True)
    study_log.stop()

    assert not study_log.transcripts_dir(tmp_path).exists()


def test_write_errors_never_reach_the_conversation(logging_on: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write errors never reach the conversation."""
    study_log.start()
    monkeypatch.setattr(study_log.TranscriptSession, "utterance", MagicMock(side_effect=OSError("disk full")))

    study_log.record_utterance("user", "Hi", True)  # must not raise


def test_console_forwards_transcripts_to_study_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Console forwards transcripts to study log."""
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(study_log, "record_utterance", lambda role, text, final: calls.append((role, text, final)))
    stream = LocalStream(MagicMock(), MagicMock())

    stream._dispatch_transcript("user", "Hi Reachy", True)
    stream._dispatch_transcript("assistant", "Hello!", True)

    assert calls == [("user", "Hi Reachy", True), ("assistant", "Hello!", True)]


def test_stop_uploads_the_finished_file_including_its_footer(
    logging_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping the app closes the file first, so the last upload contains the whole session."""
    uploaded: list[bytes] = []

    def graph(request: httpx.Request) -> httpx.Response:
        uploaded.append(request.content)
        return httpx.Response(201, json={})

    client = httpx.Client(transport=httpx.MockTransport(graph))
    monkeypatch.setattr(
        onedrive_upload.OneDriveUploader,
        "from_env",
        classmethod(lambda cls, data_dir: cls(data_dir, lambda: "tok", interval_s=3600, client=client)),
    )

    study_log.start()
    study_log.record_utterance("user", "Bye Reachy", True)
    study_log.stop()

    last = [json.loads(line) for line in uploaded[-1].decode("utf-8").splitlines()]
    assert [r["type"] for r in last] == ["session_start", "utterance", "session_end"]
