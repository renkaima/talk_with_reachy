import json
from typing import Any
from pathlib import Path
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools import daily_summary as daily_summary_module
from talk_with_reachy.tools.core_tools import ToolDependencies


def _event(day: date, clock: str, name: str, **fields: Any) -> dict[str, Any]:
    return {"type": "event", "event": name, "time": f"{day.isoformat()} {clock}:00.000-05:00", **fields}


def _utterance(day: date, clock: str, speaker: str, text: str) -> dict[str, Any]:
    return {"type": "utterance", "speaker": speaker, "time": f"{day.isoformat()} {clock}:00.000-05:00", "text": text}


@pytest.fixture
def transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two of today's breaks, one logged repeat request and two spoken ones, plus yesterday's records."""
    monkeypatch.setenv(study_log.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: None)
    today = date.today()
    records = [
        _event(today - timedelta(days=1), "09:00", "break_started", break_id="old", started_by="self"),
        _event(today, "10:00", "break_started", break_id="a", started_by="self", person="Sam", activity="breathe"),
        _event(today, "10:02", "break_check", break_id="a", answer="good"),
        _event(today, "13:00", "break_started", break_id="b", started_by="staff", person="Noah", activity="music"),
        _event(today, "13:05", "asked_to_repeat", heard="mmh"),
        {"type": "utterance", "time": f"{today.isoformat()} 10:00:01.000-05:00", "text": "hi"},
        # Reachy asked three times (with the pre-1.3.10, 1.3.10, and 1.3.11 wording), but the model logged
        # asked_to_repeat only once; a person saying it does not count.
        _utterance(today, "13:05", "reachy", "Sorry. Please say it again."),
        _utterance(
            today, "13:06", "reachy", "I did not hear that well. Do you want to talk with me, or hear what I can do?"
        ),
        _utterance(
            today, "13:06", "reachy", "I did not understand. Do you want to talk with me, or hear what I can do?"
        ),
        _utterance(today, "13:07", "person", "Reachy, please say it again."),
        _utterance(today - timedelta(days=1), "09:01", "reachy", "Sorry. Please say it again."),
    ]
    folder = study_log.transcripts_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / f"robotA_{datetime.now():%Y%m%d-%H%M%S}_abc123.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _deps() -> ToolDependencies:
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())


@pytest.mark.asyncio
@pytest.mark.usefixtures("transcripts")
async def test_participants_hear_totals_without_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant asking gets today's counts and no names."""
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("P01", "Sam"))

    summary = await daily_summary_module.DailySummary()(_deps(), action="summary")

    assert summary["breaks"] == 2
    assert summary["self_started"] == 1 and summary["staff_started"] == 1
    assert summary["times_I_asked_someone_to_repeat"] == 3
    assert "breaks_by_person" not in summary
    assert "Sam" not in json.dumps(summary) and "Noah" not in json.dumps(summary)


@pytest.mark.asyncio
@pytest.mark.usefixtures("transcripts")
async def test_staff_hear_who_took_each_break_and_the_follow_up_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staff get each break with its person, time, and closing answer, then the two note questions."""
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("S01", "Renkai"))

    summary = await daily_summary_module.DailySummary()(_deps(), action="summary")

    assert summary["breaks_by_person"][0] == {
        "time": "10:00",
        "person": "Sam",
        "started_by": "self",
        "activity": "breathe",
        "afterwards": "good",
    }
    assert summary["then_ask_one_at_a_time"] == list(daily_summary_module.STAFF_QUESTIONS)


@pytest.mark.asyncio
async def test_only_staff_can_add_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A note from a participant voice is refused; a staff note is saved to the study log."""
    saved: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: saved.append((name, fields)))
    tool = daily_summary_module.DailySummary()

    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("P02", "Maria"))
    assert "error" in await tool(_deps(), action="note", note="hi")
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("S01", "Renkai"))
    assert await tool(_deps(), action="note", note="Sam was calmer", question="anything to add") == {"saved": True}

    assert saved == [("staff_note", {"note": "Sam was calmer", "question": "anything to add", "speaker_id": "S01"})]
