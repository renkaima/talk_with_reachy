import asyncio
from typing import Any
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools import take_break as take_break_module
from talk_with_reachy.tools.core_tools import ToolDependencies


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture study events and skip the waiting, with P01 'Sam' speaking."""
    recorded: list[tuple[str, dict[str, Any]]] = []

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(take_break_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("P01", "Sam"))
    return recorded


def _deps() -> ToolDependencies:
    return ToolDependencies(
        reachy_mini=MagicMock(), movement_manager=MagicMock(), set_mic_muted=MagicMock(return_value=False)
    )


@pytest.mark.asyncio
async def test_breathing_queues_guided_breaths_and_logs_the_break(events: list[tuple[str, dict[str, Any]]]) -> None:
    """A self-started breathing break should move the robot and log its start and finish."""
    deps = _deps()

    result = await take_break_module.TakeBreak()(deps, activity="breathe", started_by="self", feeling="not good")

    assert result["status"] == "finished"
    assert isinstance(deps.movement_manager.queue_move.call_args.args[0], take_break_module.GuidedBreathingMove)
    assert [name for name, _ in events] == ["break_started", "break_finished"]
    started = events[0][1]
    assert started["person"] == "Sam" and started["speaker_id"] == "P01" and started["feeling"] == "not good"
    assert events[1][1]["completed"] is True


@pytest.mark.asyncio
async def test_music_plays_between_the_chimes_with_a_countdown_and_the_microphone_muted(
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    """Music plays one calm track between the start and end chimes, shows a countdown, and unmutes afterwards."""
    deps = _deps()

    await take_break_module.TakeBreak()(deps, activity="music", started_by="staff", person="Noah")

    played = [Path(call.args[0]).name for call in deps.reachy_mini.media.play_sound.call_args_list]
    assert played[0] == "chime_start.wav" and played[-1] == "chime_end.wav"
    assert played[1].startswith("calm_") and len(played) == 3
    assert isinstance(deps.movement_manager.queue_move.call_args.args[0], take_break_module.CountdownMove)
    assert [call.args[0] for call in deps.set_mic_muted.call_args_list] == [True, False]
    assert events[0][1]["started_by"] == "staff" and events[0][1]["person"] == "Noah"


@pytest.mark.asyncio
async def test_music_leaves_a_microphone_muted_by_staff_muted(events: list[tuple[str, dict[str, Any]]]) -> None:
    """If staff had muted the microphone before the music, it stays muted afterwards."""
    deps = _deps()
    deps.set_mic_muted = MagicMock(return_value=True)

    await take_break_module.TakeBreak()(deps, activity="music", started_by="self")

    assert [call.args[0] for call in deps.set_mic_muted.call_args_list] == [True, True]


@pytest.mark.asyncio
async def test_staff_started_break_does_not_log_the_staff_member_as_the_person(
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    """Without a name from staff, the person stays empty instead of taking the speaker's name."""
    await take_break_module.TakeBreak()(_deps(), activity="breathe", started_by="staff")

    assert events[0][1]["person"] == ""


@pytest.mark.asyncio
async def test_cancelled_break_stops_moving_and_unmutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling mid-activity should clear queued moves, unmute, and log the break as not completed."""
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: (None, ""))
    deps = _deps()

    task = asyncio.create_task(take_break_module.TakeBreak()(deps, activity="music", started_by="self"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deps.movement_manager.clear_move_queue.assert_called_once()
    assert deps.set_mic_muted.call_args.args[0] is False
    assert recorded[-1] == ("break_finished", {**recorded[-1][1], "completed": False})


@pytest.mark.asyncio
async def test_activity_starts_only_after_reachy_finishes_speaking(events: list[tuple[str, dict[str, Any]]]) -> None:
    """The start chime waits until Reachy's spoken instructions have finished playing."""
    deps = _deps()
    quiet = MagicMock(side_effect=[1.5, 0.4, 0.0])
    deps.seconds_until_reachy_is_quiet = quiet
    checks_before_each_sound: list[tuple[str, int]] = []
    deps.reachy_mini.media.play_sound.side_effect = lambda path: checks_before_each_sound.append(
        (Path(path).name, quiet.call_count)
    )

    await take_break_module.TakeBreak()(deps, activity="breathe", started_by="self")

    assert checks_before_each_sound[0] == ("chime_start.wav", 3)
    assert quiet.call_count == 3


@pytest.mark.skipif(not take_break_module.DANCE_AVAILABLE, reason="dance library not installed")
@pytest.mark.asyncio
async def test_movement_game_repeats_each_move_before_the_next(events: list[tuple[str, dict[str, Any]]]) -> None:
    """Each move is shown several times in a row, so people have time to copy it."""
    deps = _deps()

    await take_break_module.TakeBreak()(deps, activity="move", started_by="self")

    names = [call.args[0].move_name for call in deps.movement_manager.queue_move.call_args_list]
    repeats = take_break_module.MOVE_REPEATS
    assert names == [name for name in take_break_module.MOVE_GAME for _ in range(repeats)]


@pytest.mark.asyncio
async def test_unknown_activity_is_rejected(events: list[tuple[str, dict[str, Any]]]) -> None:
    """An activity outside the three choices must not start anything."""
    result = await take_break_module.TakeBreak()(_deps(), activity="dance", started_by="self")

    assert "error" in result
    assert events == []


def test_countdown_folds_the_antennas_from_upright_to_down() -> None:
    """The visual timer starts upright and is fully folded when the time is up."""
    move = take_break_module.CountdownMove(60.0)

    _, start, _ = move.evaluate(0.0)
    _, end, _ = move.evaluate(60.0)

    assert start is not None and end is not None
    assert np.allclose(np.abs(start), take_break_module.UPRIGHT_ANTENNA)
    assert abs(end[1]) == pytest.approx(1.2)


def test_guided_breath_antennas_stand_up_on_the_in_breath() -> None:
    """Antennas are spread when the breath is out and upright at the top of the in-breath."""
    move = take_break_module.GuidedBreathingMove(breaths=1)

    _, out_antennas, _ = move.evaluate(0.0)
    _, in_antennas, _ = move.evaluate(take_break_module.INHALE_S)

    assert out_antennas is not None and in_antennas is not None
    assert abs(out_antennas[1]) > 0.5
    assert np.allclose(np.abs(in_antennas), take_break_module.UPRIGHT_ANTENNA)
    assert move.duration == take_break_module.INHALE_S + take_break_module.EXHALE_S
