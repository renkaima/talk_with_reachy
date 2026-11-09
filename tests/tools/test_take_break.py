import wave
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
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())


@pytest.fixture
def short_song(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Use a 0.6 s song instead of the one-minute tracks and chimes that take no time; capture study events."""
    with wave.open(str(tmp_path / "calm_test.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes((np.sin(np.arange(4800) / 3) * 8000).astype(np.int16).tobytes())
    monkeypatch.setattr(take_break_module, "SOUNDS_DIR", tmp_path)
    monkeypatch.setattr(take_break_module, "wav_seconds", lambda _sound: 0.0)
    monkeypatch.setattr(take_break_module, "PLAYBACK_LAG_S", 0.0)
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("P01", "Sam"))
    return recorded


async def _until(condition: Any) -> None:
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _played(deps: ToolDependencies) -> list[str]:
    return [Path(call.args[0]).name for call in deps.reachy_mini.media.play_sound.call_args_list]


@pytest.mark.asyncio
async def test_breathing_queues_guided_breaths_and_logs_the_break(events: list[tuple[str, dict[str, Any]]]) -> None:
    """A self-started breathing break should move the robot and log its start and finish."""
    deps = _deps()

    result = await take_break_module.TakeBreak()(
        deps, activity="breathe", started_by="self", feeling="not good", feeling_said="I don't feel well"
    )

    assert result["status"] == "finished"
    assert isinstance(deps.movement_manager.queue_move.call_args.args[0], take_break_module.GuidedBreathingMove)
    assert [name for name, _ in events] == ["break_started", "break_finished"]
    started = events[0][1]
    assert started["person"] == "Sam" and started["speaker_id"] == "P01" and started["feeling"] == "not good"
    assert started["feeling_said"] == "I don't feel well"
    assert events[1][1]["completed"] is True


@pytest.mark.asyncio
async def test_music_plays_between_the_chimes_with_a_countdown(short_song: list[tuple[str, dict[str, Any]]]) -> None:
    """Music plays one calm track between the start and end chimes and shows a countdown."""
    deps = _deps()

    result = await take_break_module.TakeBreak()(deps, activity="music", started_by="staff", person="Noah")

    assert _played(deps) == ["chime_start.wav", "calm_test.wav", "chime_end.wav"]
    assert isinstance(deps.movement_manager.queue_move.call_args.args[0], take_break_module.CountdownMove)
    assert result["status"] == "finished"
    assert short_song[0][1]["started_by"] == "staff" and short_song[0][1]["person"] == "Noah"


@pytest.mark.asyncio
async def test_music_pauses_when_someone_talks_and_goes_on_from_there(
    short_song: list[tuple[str, dict[str, Any]]],
) -> None:
    """Talking stops the song; after "more", the rest of it plays and the countdown goes on from where it was."""
    deps = _deps()
    task = asyncio.create_task(take_break_module.TakeBreak()(deps, activity="music", started_by="self"))
    await _until(lambda: "calm_test.wav" in _played(deps))
    await asyncio.sleep(0.2)

    assert take_break_module.pause_music_for_speech() is True
    await _until(lambda: "silence.wav" in _played(deps))
    assert take_break_module.pause_music_for_speech() is False  # already paused
    assert take_break_module.choose_after_pause("more") is True
    result = await task

    played = _played(deps)
    assert played[:3] == ["chime_start.wav", "calm_test.wav", "silence.wav"]
    assert played[3].startswith("rest_") and played[4] == "chime_end.wav"
    countdowns = [call.args[0] for call in deps.movement_manager.queue_move.call_args_list]
    assert countdowns[0].start_fraction == 0 and 0.2 < countdowns[1].start_fraction < 0.9
    assert ("music_choice", {"break_id": result["break_id"], "choice": "more"}) in short_song
    assert result["status"] == "finished"


@pytest.mark.asyncio
async def test_music_ends_when_they_choose_stop_or_nobody_chooses(
    short_song: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Choosing stop ends the break without the feelings question; with no choice, it ends after MAX_PAUSE_S."""
    for choice, status_hint in (("stop", "Okay. We stopped."), (None, "All done.")):
        monkeypatch.setattr(take_break_module, "MAX_PAUSE_S", 0.05)
        deps = _deps()
        task = asyncio.create_task(take_break_module.TakeBreak()(deps, activity="music", started_by="self"))
        await _until(lambda deps=deps: "calm_test.wav" in _played(deps))
        take_break_module.pause_music_for_speech()
        if choice:
            await _until(lambda deps=deps: "silence.wav" in _played(deps))
            take_break_module.choose_after_pause(choice)
        result = await task

        assert result["status"] == "stopped" and status_hint in result["next"]
        assert _played(deps)[-1] == "chime_end.wav"
        assert short_song[-1][0] == "break_finished" and short_song[-1][1]["completed"] is False


def test_nothing_happens_when_no_music_is_playing() -> None:
    """Talking outside the music, or a choice with nothing paused, changes nothing."""
    assert take_break_module.pause_music_for_speech() is False
    assert take_break_module.choose_after_pause("more") is False


@pytest.mark.asyncio
async def test_staff_started_break_does_not_log_the_staff_member_as_the_person(
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    """Without a name from staff, the person stays empty instead of taking the speaker's name."""
    await take_break_module.TakeBreak()(_deps(), activity="breathe", started_by="staff")

    assert events[0][1]["person"] == ""


@pytest.mark.asyncio
async def test_cancelled_music_stops_the_song_and_moving(short_song: list[tuple[str, dict[str, Any]]]) -> None:
    """Cancelling during the song stops it, clears queued moves, and logs the break as not completed."""
    deps = _deps()

    task = asyncio.create_task(take_break_module.TakeBreak()(deps, activity="music", started_by="self"))
    await _until(lambda: "calm_test.wav" in _played(deps))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _played(deps)[-1] == "silence.wav"
    deps.movement_manager.clear_move_queue.assert_called_once()
    assert short_song[-1] == ("break_finished", {**short_song[-1][1], "completed": False})


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
