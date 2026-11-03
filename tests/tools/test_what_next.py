from pathlib import Path
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools import what_next as what_next_module
from talk_with_reachy.tools.core_tools import ToolDependencies


@pytest.fixture(autouse=True)
def schedule_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the schedule in a temp folder and drop study events."""
    monkeypatch.setenv(study_log.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: None)


def _speaker(monkeypatch: pytest.MonkeyPatch, speaker_id: str, name: str) -> None:
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: (speaker_id, name))


def _deps() -> ToolDependencies:
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())


@pytest.mark.asyncio
async def test_staff_can_set_the_week_and_change_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staff set the usual weekdays, then cancel gym and add swimming today."""
    _speaker(monkeypatch, "S01", "Renkai")
    tool = what_next_module.WhatNext()
    week = [{"time": "9:00", "activity": "Breakfast"}, {"time": "10:00", "activity": "Gym"}]

    assert (await tool(_deps(), action="set_day", weekday="weekdays", items=week))["saved"] is True
    result = await tool(
        _deps(), action="change_today", remove=["gym"], items=[{"time": "10:00", "activity": "Swimming"}]
    )

    schedule = what_next_module.load_schedule()
    assert schedule["week"]["monday"][0] == {"time": "09:00", "activity": "Breakfast", "person": ""}
    if date.today().weekday() < 5:
        assert [item["activity"] for item in result["today"]] == ["Breakfast", "Swimming"]


@pytest.mark.asyncio
async def test_moving_an_activity_keeps_it_at_the_new_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """'Gym moves to two' is sent as remove gym plus add gym at 14:00, and must not delete gym."""
    _speaker(monkeypatch, "S01", "Renkai")
    tool = what_next_module.WhatNext()
    weekday = what_next_module.WEEKDAYS[date.today().weekday()]
    await tool(_deps(), action="set_day", weekday=weekday, items=[{"time": "10:00", "activity": "Gym"}])

    await tool(_deps(), action="change_today", remove=["Gym"], items=[{"time": "14:00", "activity": "Gym"}])
    result = await tool(_deps(), action="change_today", remove=["gym"], items=[{"time": "15:00", "activity": "Gym"}])

    assert result["today"] == [{"time": "15:00", "activity": "Gym", "person": ""}]


@pytest.mark.asyncio
async def test_next_leads_with_todays_changes_and_counts_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant hears that today differs, with minutes until each next item; others' items stay hidden."""
    _speaker(monkeypatch, "S01", "Renkai")
    tool = what_next_module.WhatNext()
    weekday = what_next_module.WEEKDAYS[date.today().weekday()]
    await tool(_deps(), action="set_day", weekday=weekday, items=[{"time": "23:58", "activity": "Gym"}])
    await tool(
        _deps(),
        action="change_today",
        remove=["Gym"],
        items=[{"time": "23:59", "activity": "Swimming"}, {"time": "23:59", "activity": "Dentist", "person": "Noah"}],
    )
    monkeypatch.setattr(what_next_module, "datetime", MagicMock(now=lambda: datetime(2026, 11, 2, 23, 50)))
    _speaker(monkeypatch, "P02", "Maria")

    result = await tool(_deps(), action="next")

    assert result["changed_today"] == {"cancelled": ["Gym"], "added": ["Swimming"]}
    assert result["next"] == [{"time": "23:59", "activity": "Swimming", "person": "", "in_minutes": 9}]


@pytest.mark.asyncio
async def test_participants_cannot_change_the_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant voice must not be able to cancel activities."""
    _speaker(monkeypatch, "P01", "Noah")

    result = await what_next_module.WhatNext()(_deps(), action="change_today", remove=["gym"])

    assert "only staff" in result["error"]
    assert not what_next_module.schedule_path().exists()


def test_plan_includes_shared_items_and_only_the_persons_own_items() -> None:
    """Noah sees the shared plan plus his own appointment, not Maria's."""
    monday = date(2026, 11, 2)
    schedule = {
        "week": {
            "monday": [
                {"time": "14:00", "activity": "Music", "person": ""},
                {"time": "11:00", "activity": "Dentist", "person": "Noah"},
                {"time": "12:00", "activity": "Internship", "person": "Maria"},
            ]
        },
        "changes": {},
    }

    plan = what_next_module.plan_for(schedule, monday, "noah")

    assert [item["activity"] for item in plan] == ["Dentist", "Music"]


@pytest.mark.parametrize("bad_items", [[], [{"time": "two", "activity": "Music"}], [{"time": "14:00"}]])
def test_items_need_a_24_hour_time_and_an_activity(bad_items: list[dict[str, str]]) -> None:
    """Malformed items are rejected instead of being saved."""
    with pytest.raises(ValueError):
        what_next_module.parse_items(bad_items)
