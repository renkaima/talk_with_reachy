import wave
import asyncio
from typing import Any
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from collections.abc import Iterator

import numpy as np
import pytest

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools import take_break as take_break_module
from talk_with_reachy.tools.core_tools import ToolDependencies


@pytest.fixture(autouse=True)
def no_break_left_over() -> Iterator[None]:
    """Each test starts and ends with no break running."""
    take_break_module._break = None
    take_break_module._music = None
    yield
    take_break_module._break = None
    take_break_module._music = None


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture study events and skip the waiting (unless the break is stopped), with P01 'Sam' speaking."""
    recorded: list[tuple[str, dict[str, Any]]] = []

    async def no_wait(_seconds: float) -> None:
        return None

    async def no_wait_unless_stopped(stop: asyncio.Event, _seconds: float) -> bool:
        await asyncio.sleep(0)
        return stop.is_set()

    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(take_break_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(take_break_module, "_wait_or_stop", no_wait_unless_stopped)
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("P01", "Sam"))
    return recorded


def _deps() -> ToolDependencies:
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock(), break_ended=AsyncMock())


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


async def _start(deps: ToolDependencies, **kwargs: Any) -> tuple[dict[str, Any], "asyncio.Task[None]"]:
    """Call take_break; return its result and the task running the activity."""
    result = await take_break_module.TakeBreak()(deps, **kwargs)
    running = take_break_module._break
    assert running is not None and running.task is not None
    return result, running.task


def _ended(deps: ToolDependencies) -> dict[str, Any]:
    """Return the outcome take_break reported to the realtime handler when the activity ended."""
    assert isinstance(deps.break_ended, AsyncMock)
    deps.break_ended.assert_awaited_once()
    outcome: dict[str, Any] = deps.break_ended.await_args.args[0]
    return outcome


@pytest.mark.asyncio
async def test_take_break_returns_at_once_and_reports_the_end_later(events: list[tuple[str, dict[str, Any]]]) -> None:
    """The tool result comes back before the activity runs, so the speech server can answer during the break."""
    deps = _deps()

    result, task = await _start(deps, activity="breathe", started_by="self", feeling_said="I don't feel well")

    assert result["status"] == "started" and result["say"].startswith("Breathing. Okay.")
    assert not task.done()
    await task
    assert isinstance(deps.movement_manager.queue_move.call_args.args[0], take_break_module.GuidedBreathingMove)
    outcome = _ended(deps)
    assert outcome["status"] == "finished" and outcome["respond"] is True
    assert "All done. How do you feel now: good, or not good?" in outcome["note"]
    assert result["break_id"] in outcome["note"]
    assert [name for name, _ in events] == ["break_started", "break_finished"]
    started = events[0][1]
    assert started["person"] == "Sam" and started["speaker_id"] == "P01"
    assert started["feeling_said"] == "I don't feel well" and "feeling" not in started
    assert events[1][1]["completed"] is True and events[1][1]["ended"] == "finished"
    assert take_break_module.running_activity() is None


@pytest.mark.asyncio
async def test_stop_during_breathing_ends_it_with_the_end_chime(events: list[tuple[str, dict[str, Any]]]) -> None:
    """Hearing "stop" stops the breaths at once; the person's own "stop" already gets a reply, so no second one."""
    deps = _deps()
    started = asyncio.Event()

    async def wait_until_stopped(stop: asyncio.Event, _seconds: float) -> bool:
        started.set()
        await stop.wait()
        return True

    take_break_module._wait_or_stop = wait_until_stopped  # type: ignore[assignment]  # undone by monkeypatch
    _, task = await _start(deps, activity="breathe", started_by="self")
    await started.wait()

    assert take_break_module.running_activity() == "breathe"
    assert take_break_module.stop_running_break("words", "Stop.") is True
    assert take_break_module.stop_running_break("words", "Stop!") is False  # already stopping
    await task

    deps.movement_manager.clear_move_queue.assert_called_once()
    assert _played(deps)[-1] == "chime_end.wav"
    outcome = _ended(deps)
    assert outcome["status"] == "stopped" and outcome["respond"] is False
    assert ("break_stop_heard", {"break_id": outcome["break_id"], "source": "words", "words": "Stop."}) in events
    assert events[-1][1]["ended"] == "stopped" and events[-1][1]["stopped_by"] == "words"


@pytest.mark.asyncio
async def test_music_plays_between_the_chimes_with_a_countdown(short_song: list[tuple[str, dict[str, Any]]]) -> None:
    """Music plays one calm track between the start and end chimes and shows a countdown."""
    deps = _deps()

    _, task = await _start(deps, activity="music", started_by="staff", person="Noah")
    await task

    assert _played(deps) == ["chime_start.wav", "calm_test.wav", "chime_end.wav"]
    assert isinstance(deps.movement_manager.queue_move.call_args.args[0], take_break_module.CountdownMove)
    assert _ended(deps)["status"] == "finished"
    assert short_song[0][1]["started_by"] == "staff" and short_song[0][1]["person"] == "Noah"


@pytest.mark.asyncio
async def test_music_pauses_when_someone_talks_and_goes_on_from_there(
    short_song: list[tuple[str, dict[str, Any]]],
) -> None:
    """Talking stops the song; after "more", the rest of it plays and the countdown goes on from where it was."""
    deps = _deps()
    result, task = await _start(deps, activity="music", started_by="self")
    await _until(lambda: "calm_test.wav" in _played(deps))
    await asyncio.sleep(0.2)

    assert take_break_module.pause_music_for_speech() is True
    await _until(lambda: "silence.wav" in _played(deps))
    assert take_break_module.pause_music_for_speech() is False  # already paused
    assert take_break_module.music_waiting_for_choice() is True
    assert take_break_module.choose_after_pause("more") is True
    await task

    played = _played(deps)
    assert played[:3] == ["chime_start.wav", "calm_test.wav", "silence.wav"]
    assert played[3].startswith("rest_") and played[4] == "chime_end.wav"
    countdowns = [call.args[0] for call in deps.movement_manager.queue_move.call_args_list]
    assert countdowns[0].start_fraction == 0 and 0.2 < countdowns[1].start_fraction < 0.9
    assert ("music_choice", {"break_id": result["break_id"], "choice": "more"}) in short_song
    assert _ended(deps)["status"] == "finished"


@pytest.mark.asyncio
async def test_music_ends_on_stop_or_when_nobody_chooses(
    short_song: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop ends the song with no feelings question; with no choice, the break ends and Reachy says 'All done.'."""
    monkeypatch.setattr(take_break_module, "MAX_PAUSE_S", 0.05)
    for stop, status, respond in ((True, "stopped", False), (False, "no answer", True)):
        deps = _deps()
        _, task = await _start(deps, activity="music", started_by="self")
        await _until(lambda deps=deps: "calm_test.wav" in _played(deps))
        take_break_module.pause_music_for_speech()
        await _until(lambda deps=deps: "silence.wav" in _played(deps))
        if stop:
            assert take_break_module.stop_running_break("words", "No.") is True
        await task

        outcome = _ended(deps)
        assert outcome["status"] == status and outcome["respond"] is respond
        assert _played(deps)[-1] == "chime_end.wav"
        assert short_song[-1][0] == "break_finished" and short_song[-1][1]["completed"] is False


@pytest.mark.asyncio
async def test_stop_while_the_song_plays_ends_it(short_song: list[tuple[str, dict[str, Any]]]) -> None:
    """A stop that comes while the song is still playing (from the model's stop_break) ends it too."""
    deps = _deps()
    _, task = await _start(deps, activity="music", started_by="self")
    await _until(lambda: "calm_test.wav" in _played(deps))

    assert take_break_module.stop_running_break("tool") is True
    await task

    assert _played(deps)[-2:] == ["silence.wav", "chime_end.wav"]
    assert _ended(deps)["status"] == "stopped"


@pytest.mark.asyncio
async def test_a_second_break_is_not_started_while_one_runs(events: list[tuple[str, dict[str, Any]]]) -> None:
    """Asking again during a break says what is on now instead of starting another."""
    deps = _deps()
    _, task = await _start(deps, activity="breathe", started_by="self")

    again = await take_break_module.TakeBreak()(deps, activity="music", started_by="self")
    await task

    assert again["status"] == "already running" and "breathing is on now" in again["say"]


def test_nothing_happens_when_no_music_is_playing() -> None:
    """Talking outside the music, or a choice with nothing paused, changes nothing."""
    assert take_break_module.pause_music_for_speech() is False
    assert take_break_module.choose_after_pause("more") is False
    assert take_break_module.stop_running_break("words") is False
    assert take_break_module.running_activity() is None


@pytest.mark.asyncio
async def test_staff_started_break_does_not_log_the_staff_member_as_the_person(
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    """Without a name from staff, the person stays empty instead of taking the speaker's name."""
    _, task = await _start(_deps(), activity="breathe", started_by="staff")
    await task

    assert events[0][1]["person"] == ""


@pytest.mark.asyncio
async def test_ending_the_session_cancels_the_music_quietly(short_song: list[tuple[str, dict[str, Any]]]) -> None:
    """Cancelling stops the song and moving, logs the break as cancelled, and tells the model nothing."""
    deps = _deps()
    _, task = await _start(deps, activity="music", started_by="self")
    await _until(lambda: "calm_test.wav" in _played(deps))

    await take_break_module.cancel_running_break()

    assert task.cancelled()
    assert _played(deps)[-1] == "silence.wav"
    deps.movement_manager.clear_move_queue.assert_called()
    assert short_song[-1][0] == "break_finished" and short_song[-1][1]["ended"] == "cancelled"
    assert isinstance(deps.break_ended, AsyncMock)
    deps.break_ended.assert_not_awaited()
    assert take_break_module.running_activity() is None


@pytest.mark.asyncio
async def test_activity_starts_only_after_reachy_finishes_speaking(events: list[tuple[str, dict[str, Any]]]) -> None:
    """The start chime waits until Reachy's spoken instructions have finished playing."""
    deps = _deps()
    quiet = MagicMock(side_effect=[1.5, 1.5, 0.4, 0.0, 0.0])
    deps.seconds_until_reachy_is_quiet = quiet
    checks_before_each_sound: list[tuple[str, int]] = []
    deps.reachy_mini.media.play_sound.side_effect = lambda path: checks_before_each_sound.append(
        (Path(path).name, quiet.call_count)
    )

    _, task = await _start(deps, activity="breathe", started_by="self")
    await task

    assert checks_before_each_sound[0] == ("chime_start.wav", 4)


@pytest.mark.skipif(not take_break_module.DANCE_AVAILABLE, reason="dance library not installed")
@pytest.mark.asyncio
async def test_movement_game_repeats_each_move_before_the_next(events: list[tuple[str, dict[str, Any]]]) -> None:
    """Each move is shown several times in a row, so people have time to copy it."""
    deps = _deps()

    _, task = await _start(deps, activity="move", started_by="self")
    await task

    names = [call.args[0].move_name for call in deps.movement_manager.queue_move.call_args_list]
    repeats = take_break_module.MOVE_REPEATS
    assert names == [name for name in take_break_module.MOVE_GAME for _ in range(repeats)]


@pytest.mark.asyncio
async def test_activity_words_are_understood_and_others_rejected(events: list[tuple[str, dict[str, Any]]]) -> None:
    """'breathing' is taken as breathe; an activity outside the three choices starts nothing."""
    result = await take_break_module.TakeBreak()(_deps(), activity="dance", started_by="self")
    assert "error" in result and "next" in result
    assert events == []

    _, task = await _start(_deps(), activity="Breathing", started_by="self")
    await task
    assert events[0][1]["activity"] == "breathe"


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


@pytest.mark.asyncio
async def test_the_robot_test_now_closes_talking_pauses_music_and_yes_resumes_it(
    short_song: list[tuple[str, dict[str, Any]]],
) -> None:
    """The 1.3.11 robot test, end to end in code: "Oh my god!" pauses the song, "Yes." plays the rest."""
    from talk_with_reachy.huggingface_realtime import HuggingFaceRealtimeHandler

    deps = _deps()
    _, task = await _start(deps, activity="music", started_by="self")
    await _until(lambda: "calm_test.wav" in _played(deps))
    await asyncio.sleep(0.1)

    take_break_module.pause_music_for_speech()  # the speech server heard someone start talking
    await _until(lambda: "silence.wav" in _played(deps))
    HuggingFaceRealtimeHandler._apply_break_words("Oh my god!")
    assert take_break_module.music_waiting_for_choice()  # still paused: Reachy asks "More music, or stop?"
    HuggingFaceRealtimeHandler._apply_break_words("Yes.")
    await task

    assert _played(deps)[3].startswith("rest_")
    assert _ended(deps)["status"] == "finished"


@pytest.mark.asyncio
async def test_saying_stop_during_breathing_stops_it_without_the_model(
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    """The app hears "Stop please." and stops the breaths, whatever the model does."""
    from talk_with_reachy.huggingface_realtime import HuggingFaceRealtimeHandler

    deps = _deps()
    started = asyncio.Event()

    async def wait_until_stopped(stop: asyncio.Event, _seconds: float) -> bool:
        started.set()
        await stop.wait()
        return True

    take_break_module._wait_or_stop = wait_until_stopped  # type: ignore[assignment]  # undone by monkeypatch
    _, task = await _start(deps, activity="breathe", started_by="self")
    await started.wait()

    HuggingFaceRealtimeHandler._apply_break_words("Stop please.")
    await task

    assert _ended(deps)["status"] == "stopped"
