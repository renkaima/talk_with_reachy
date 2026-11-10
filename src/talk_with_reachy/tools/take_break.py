import math
import time
import uuid
import wave
import random
import shutil
import asyncio
import logging
import tempfile
from typing import Any
from pathlib import Path
from dataclasses import field, dataclass

import numpy as np
from numpy.typing import NDArray

from reachy_mini.utils import create_head_pose
from reachy_mini.motion.move import Move
from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools import break_check
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
BREATHS = 6
INHALE_S = 4.0
EXHALE_S = 6.0  # 10 s per breath, 6 breaths per minute
MOVE_GAME = ("simple_nod", "side_to_side_sway", "head_tilt_roll", "pendulum_swing")
# Each move (about 2 s) repeats four times before the next one, so people have time to copy it.
MOVE_REPEATS = 4
# Longest wait for Reachy to finish its spoken instructions before the start chime, and a short extra pause
# after them, because the robot's speaker starts a little after the audio reaches the app.
QUIET_WAIT_MAX_S = 15.0
PLAYBACK_LAG_S = 0.3
UPRIGHT_ANTENNA = 0.1745  # ~10° off vertical, as in the SDK's rest pose, to keep the antennas from shaking
# The same chimes open and close every activity, so the start and the end are always clear.
START_CHIME = SOUNDS_DIR / "chime_start.wav"
END_CHIME = SOUNDS_DIR / "chime_end.wav"
# Playing this short silence replaces the song, which stops it.
SILENCE = SOUNDS_DIR / "silence.wav"
# When the song goes on after a pause, it fades in over this time instead of starting abruptly.
RESUME_FADE_IN_S = 0.5
# Longest time the music stays paused without a choice; then the break ends.
MAX_PAUSE_S = 30.0
# After "more", the song waits this long for Reachy to start its reply, then for the reply to end, so the song
# does not play over Reachy's voice.
REPLY_START_WAIT_S = 3.0

try:
    from talk_with_reachy.dance_emotion_moves import DanceQueueMove

    DANCE_AVAILABLE = True
except ImportError as e:
    logger.warning("Dance library not available, the movement game is off: %s", e)
    DANCE_AVAILABLE = False


class GuidedBreathingMove(Move):  # type: ignore
    """Slow breaths to follow: the head rises and the antennas stand up on the in-breath, then both settle."""

    def __init__(self, breaths: int = BREATHS) -> None:
        """Initialize a guided breathing move of ``breaths`` breaths."""
        self.breaths = breaths

    @property
    def duration(self) -> float:
        """Duration property required by official Move interface."""
        return self.breaths * (INHALE_S + EXHALE_S)

    def evaluate(self, t: float) -> tuple[NDArray[np.float64] | None, NDArray[np.float64] | None, float | None]:
        """Evaluate the breath at time t: 0 is fully out, 1 is fully in."""
        phase = t % (INHALE_S + EXHALE_S)
        if phase < INHALE_S:
            breath = (1 - math.cos(math.pi * phase / INHALE_S)) / 2
        else:
            breath = (1 + math.cos(math.pi * (phase - INHALE_S) / EXHALE_S)) / 2
        head_pose = create_head_pose(z=0.008 * breath, pitch=-10 * breath, degrees=True, mm=False)
        spread = UPRIGHT_ANTENNA + 0.6 * (1 - breath)
        return (head_pose, np.array([-spread, spread], dtype=np.float64), 0.0)


class CountdownMove(Move):  # type: ignore
    """A visual timer: the antennas start upright and fold down steadily until the time is up."""

    def __init__(self, seconds: float, start_fraction: float = 0.0) -> None:
        """Initialize a countdown lasting ``seconds``, starting ``start_fraction`` of the way down (after a pause)."""
        self.seconds = seconds
        self.start_fraction = start_fraction

    @property
    def duration(self) -> float:
        """Duration property required by official Move interface."""
        return self.seconds

    def evaluate(self, t: float) -> tuple[NDArray[np.float64] | None, NDArray[np.float64] | None, float | None]:
        """Evaluate the countdown at time t: antennas fold from upright to 1.2 rad as the time runs out."""
        progress = self.start_fraction + (1 - self.start_fraction) * min(max(t / self.seconds, 0.0), 1.0)
        fold = UPRIGHT_ANTENNA + (1.2 - UPRIGHT_ANTENNA) * progress
        return (create_head_pose(), np.array([-fold, fold], dtype=np.float64), 0.0)


def wav_seconds(sound: Path) -> float:
    """Length of a WAV file in seconds."""
    with wave.open(str(sound), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


async def play_and_wait(deps: ToolDependencies, sound: Path) -> None:
    """Play a WAV file on the robot's speaker and return when it has finished."""
    # Off the event loop: with a remote robot, play_sound uploads the file first.
    await asyncio.to_thread(deps.reachy_mini.media.play_sound, str(sound))
    await asyncio.sleep(wav_seconds(sound) + 0.3)


async def wait_until_reachy_is_quiet(deps: ToolDependencies) -> None:
    """Return once Reachy has finished speaking, or after QUIET_WAIT_MAX_S.

    The speech server sends the tool call while Reachy is still saying what will happen, so without this wait
    the chime and the activity would start over Reachy's instructions.
    """
    if deps.seconds_until_reachy_is_quiet is None:
        return
    deadline = asyncio.get_running_loop().time() + QUIET_WAIT_MAX_S
    waited = False
    while (remaining := deps.seconds_until_reachy_is_quiet()) > 0 and asyncio.get_running_loop().time() < deadline:
        waited = True
        await asyncio.sleep(min(remaining, 0.25))
    if waited:
        await asyncio.sleep(PLAYBACK_LAG_S)


class BreakMusic:
    """One song for a break that pauses when someone starts talking, then goes on or stops as they choose."""

    def __init__(self, track: Path) -> None:
        """Load ``track`` (a 16-bit WAV file)."""
        self.track = track
        with wave.open(str(track), "rb") as handle:
            self.rate = handle.getframerate()
            self.channels = handle.getnchannels()
            frames = handle.readframes(handle.getnframes())
        self.samples = np.frombuffer(frames, dtype=np.int16).reshape(-1, self.channels)
        self.length_s = len(self.samples) / self.rate
        self.position_s = 0.0
        self._playing_since: float | None = None
        self._paused = asyncio.Event()
        self._choice: asyncio.Future[str] | None = None
        self._tmp_dir: Path | None = None
        self.stop_requested = False

    def remaining_s(self) -> float:
        """Seconds of the song still to play."""
        return max(self.length_s - self.position_s, 0.0)

    def sound_from_position(self) -> Path:
        """Return the file to play now: the song itself at the start, otherwise the rest of it with a fade-in."""
        if self.position_s <= 0:
            return self.track
        rest = self.samples[int(self.position_s * self.rate) :].astype(np.float64)
        fade = min(int(RESUME_FADE_IN_S * self.rate), len(rest))
        rest[:fade] *= np.linspace(0.0, 1.0, fade)[:, None]
        if self._tmp_dir is None:
            self._tmp_dir = Path(tempfile.mkdtemp(prefix="talk_with_reachy_music_"))
        path = self._tmp_dir / f"rest_{int(self.position_s * 1000)}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(self.channels)
            handle.setsampwidth(2)
            handle.setframerate(self.rate)
            handle.writeframes(rest.astype(np.int16).tobytes())
        return path

    def mark_playing(self) -> None:
        """Note that the song (or its rest) starts playing now."""
        self._paused.clear()
        self._choice = None
        self._playing_since = time.monotonic()

    def pause(self) -> bool:
        """Remember where the song is and wake the player; False if it was not playing."""
        if self._playing_since is None:
            return False
        self.position_s = min(self.position_s + time.monotonic() - self._playing_since, self.length_s)
        self._playing_since = None
        self._choice = asyncio.get_running_loop().create_future()
        self._paused.set()
        return True

    def choose(self, choice: str) -> bool:
        """Pass on "more" or "stop" while the music is paused; False if nothing is waiting for a choice."""
        if self._choice is None or self._choice.done():
            return False
        self._choice.set_result(choice)
        return True

    def waiting_for_choice(self) -> bool:
        """Tell whether the music is paused and waiting for "more" or "stop"."""
        return self._choice is not None and not self._choice.done()

    def request_stop(self) -> None:
        """End the song whether it is playing or paused."""
        self.stop_requested = True
        if not self.choose("stop"):
            self._paused.set()  # wakes the player, which sees stop_requested

    async def wait_until_paused(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for a pause or a stop; True if one came."""
        try:
            await asyncio.wait_for(self._paused.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def wait_for_choice(self) -> str:
        """Wait for "more" or "stop"; "no answer" after MAX_PAUSE_S."""
        assert self._choice is not None
        try:
            return await asyncio.wait_for(asyncio.shield(self._choice), timeout=MAX_PAUSE_S)
        except asyncio.TimeoutError:
            return "no answer"

    def cleanup(self) -> None:
        """Delete the files made for resuming."""
        if self._tmp_dir is not None:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)


async def wait_for_reply_then_quiet(deps: ToolDependencies) -> None:
    """Give Reachy up to REPLY_START_WAIT_S to start its reply, then wait until it has finished speaking."""
    if deps.seconds_until_reachy_is_quiet is None:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REPLY_START_WAIT_S
    while deps.seconds_until_reachy_is_quiet() <= 0 and loop.time() < deadline:
        await asyncio.sleep(0.1)
    await wait_until_reachy_is_quiet(deps)


async def play_break_music(
    deps: ToolDependencies, track: Path, break_id: str, stop: asyncio.Event | None = None
) -> str:
    """Play one song with an antenna countdown; return "finished", "stop", or "no answer".

    When someone starts talking, the song stops where it is and waits for "more" (the rest plays, and the
    countdown goes on from where it was) or "stop". The choice comes from the person's own words (the realtime
    handler hears "more", "yes", "stop", "no") or from the model's music_choice call.
    """
    global _music
    music = BreakMusic(track)
    _music = music
    if stop is not None and stop.is_set():
        music.request_stop()
    try:
        while (remaining := music.remaining_s()) > 0.1:
            if music.stop_requested:
                return "stop"
            sound = await asyncio.to_thread(music.sound_from_position)
            deps.movement_manager.queue_move(CountdownMove(remaining, music.position_s / music.length_s))
            music.mark_playing()
            await asyncio.to_thread(deps.reachy_mini.media.play_sound, str(sound))
            if not await music.wait_until_paused(remaining + PLAYBACK_LAG_S):
                break
            await asyncio.to_thread(deps.reachy_mini.media.play_sound, str(SILENCE))
            deps.movement_manager.clear_move_queue()
            if music.stop_requested:
                return "stop"
            study_log.record_event("music_paused", break_id=break_id, at_s=round(music.position_s, 1))
            choice = await music.wait_for_choice()
            study_log.record_event("music_choice", break_id=break_id, choice=choice)
            if choice != "more":
                return choice
            await wait_for_reply_then_quiet(deps)
        return "finished"
    except asyncio.CancelledError:
        deps.reachy_mini.media.play_sound(str(SILENCE))
        raise
    finally:
        _music = None
        music.cleanup()


@dataclass
class RunningBreak:
    """The break activity that is running now."""

    break_id: str
    activity: str
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    stopped_by: str = ""
    task: "asyncio.Task[None] | None" = None


# The song of the break that is running now, if any; the realtime handler pauses it when someone talks.
_music: BreakMusic | None = None
# The break that is running now, if any. Only one break runs at a time.
_break: RunningBreak | None = None
# Timers that save "no answer" when nobody answers the question after a break (kept so they are not collected).
_no_answer_tasks: set["asyncio.Task[None]"] = set()


def pause_music_for_speech() -> bool:
    """Pause the break music because someone started talking; True if music was playing."""
    return _music.pause() if _music is not None else False


def choose_after_pause(choice: str) -> bool:
    """Go on with the paused music ("more") or end it ("stop"); False if no music is waiting for a choice."""
    return _music.choose(choice) if _music is not None else False


def music_waiting_for_choice() -> bool:
    """Tell whether the break music is paused and waiting for "more" or "stop"."""
    return _music is not None and _music.waiting_for_choice()


def running_activity() -> str | None:
    """Return the activity of the break running now ("breathe", "music", "move"), or None."""
    if _break is None or _break.stop.is_set():
        return None
    return _break.activity


def stop_running_break(source: str, words: str = "") -> bool:
    """Stop the running break now; ``source`` is "words" (the app heard stop) or "tool". False if none runs."""
    running = _break
    if running is None or running.stop.is_set():
        return False
    running.stopped_by = source
    running.stop.set()
    study_log.record_event("break_stop_heard", break_id=running.break_id, source=source, words=words[:200])
    if _music is not None:
        _music.request_stop()
    return True


async def cancel_running_break() -> None:
    """Cancel the running break without telling the model (the session is ending)."""
    running = _break
    if running is not None and running.task is not None and not running.task.done():
        running.task.cancel()
        try:
            await running.task
        except (asyncio.CancelledError, Exception):
            pass


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Wait ``seconds``; return True early if the break was stopped."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    return True


# What to tell the model when a break ends, by how it ended. The app adds it as a note and, when nobody is
# talking to Reachy at that moment, asks the model to speak.
END_NOTES = {
    "finished": "Say: 'All done. How do you feel now: good, or not good?' When they answer, call break_check "
    "with break_id {break_id} and their answer.",
    "stopped": "It stopped because someone said stop. If you have not said it yet, say only: 'Okay. We stopped.' "
    "Do not ask how they feel.",
    "no answer": "Nobody chose more music or stop, so it ended. Say only: 'All done.'",
}


async def _run_break(deps: ToolDependencies, running: RunningBreak, tracks: list[Path], started_by: str) -> None:
    """Run one break activity to its end, then tell the model how it ended."""
    global _break
    activity = running.activity
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = "finished"
    try:
        # If the model called take_break without speaking, the app asks it to speak; wait for that too.
        await wait_for_reply_then_quiet(deps)
        started = loop.time()
        if not running.stop.is_set():
            await play_and_wait(deps, START_CHIME)
        if running.stop.is_set():
            outcome = "stopped"
        elif activity == "breathe":
            move = GuidedBreathingMove()
            deps.movement_manager.queue_move(move)
            if await _wait_or_stop(running.stop, move.duration):
                deps.movement_manager.clear_move_queue()
                outcome = "stopped"
        elif activity == "move":
            moves = [DanceQueueMove(move_name) for move_name in MOVE_GAME for _ in range(MOVE_REPEATS)]
            for dance_move in moves:
                deps.movement_manager.queue_move(dance_move)
            if await _wait_or_stop(running.stop, sum(dance_move.duration for dance_move in moves)):
                deps.movement_manager.clear_move_queue()
                outcome = "stopped"
        else:
            music_outcome = await play_break_music(deps, random.choice(tracks), running.break_id, running.stop)
            outcome = {"finished": "finished", "stop": "stopped"}.get(music_outcome, "no answer")
            await wait_until_reachy_is_quiet(deps)
        await play_and_wait(deps, END_CHIME)
    except asyncio.CancelledError:
        deps.movement_manager.clear_move_queue()
        study_log.record_event(
            "break_finished",
            break_id=running.break_id,
            activity=activity,
            seconds=round(loop.time() - started, 1),
            completed=False,
            ended="cancelled",
        )
        raise
    finally:
        if _break is running:
            _break = None

    study_log.record_event(
        "break_finished",
        break_id=running.break_id,
        activity=activity,
        seconds=round(loop.time() - started, 1),
        completed=outcome == "finished",
        ended=outcome,
        stopped_by=running.stopped_by,
    )
    logger.info("Break %s (%s) ended: %s", running.break_id, activity, outcome)
    if outcome == "finished":
        _no_answer_tasks.add(asyncio.create_task(break_check.record_no_answer_later(running.break_id)))
        for task in [task for task in _no_answer_tasks if task.done()]:
            _no_answer_tasks.discard(task)
    if deps.break_ended is not None:
        note = END_NOTES[outcome].format(break_id=running.break_id)
        await deps.break_ended(
            {
                "status": outcome,
                "break_id": running.break_id,
                "activity": activity,
                "started_by": started_by,
                "note": f"The {ACTIVITY_WORDS[activity]} is over. {note}",
                # After a stop, the person's own "stop" already gets a reply; a second one would repeat it.
                "respond": outcome != "stopped",
            }
        )


ACTIVITY_WORDS = {"breathe": "breathing", "music": "music", "move": "movement game"}
# Words the model may use for an activity, mapped to the tool's names.
ACTIVITY_ALIASES = {
    "breathe": "breathe",
    "breathing": "breathe",
    "breath": "breathe",
    "breathe with me": "breathe",
    "music": "music",
    "song": "music",
    "listen to music": "music",
    "move": "move",
    "moving": "move",
    "movement": "move",
    "movement game": "move",
    "game": "move",
    "move with me": "move",
}
ANNOUNCEMENTS = {
    "breathe": "Breathing. Okay. Watch my head go up and down, six slow times. Breathe with me if you want. "
    "Say stop any time.",
    "music": "Music. Okay. One calm song, for one minute. My antennas go down slowly. When they are down, "
    "the song is over. Say stop any time.",
    "move": "Moving. Okay. One short game. Copy my moves if you want. Say stop any time.",
}


class TakeBreak(Tool):
    """Start one break activity; it runs on its own, and the app tells the model when it is over."""

    name = "take_break"
    description = (
        "Start a short break activity the person asked for or chose. "
        "breathe: six slow breaths (one minute); your head rises and your antennas stand up on each in-breath. "
        "music: one calm song (one minute); your antennas fold down slowly to show how much is left. "
        "If someone talks during the song, it pauses; ask 'More music, or stop?'. "
        "move: one short movement game (about 35 seconds) that anyone nearby can copy. "
        "A soft chime marks the start and a two-note chime marks the end. "
        "The tool returns at once; the activity starts when you finish speaking, and a note tells you when it "
        "is over. If someone says stop during it, call stop_break."
    )
    needs_response = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "activity": {"type": "string", "enum": ["breathe", "music", "move"]},
            "started_by": {
                "type": "string",
                "enum": ["self", "staff"],
                "description": "self if the person asked for the break (voice or AAC device); staff if staff asked.",
            },
            "person": {
                "type": "string",
                "description": "Name of the person taking the break, if known (staff usually say it).",
            },
            "feeling_said": {
                "type": "string",
                "description": "Their own words about how they feel, if they said any (for example 'I am upset').",
            },
        },
        "required": ["activity", "started_by"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Start the chosen activity in the background and return at once."""
        global _break
        raw = str(kwargs.get("activity", "")).strip().lower()
        activity = ACTIVITY_ALIASES.get(raw, raw)
        if activity not in ("breathe", "music", "move"):
            # Only the error text reaches the model, so it carries the next step.
            return {"error": f"unknown activity {raw!r}; call take_break again with breathe, music, or move"}
        if activity == "move" and not DANCE_AVAILABLE:
            return {
                "error": "the movement game is not available on this robot; say 'I cannot move now. Breathing, or music?'"
            }
        if _break is not None:
            return {
                "status": "already running",
                "activity": _break.activity,
                "say": f"The {ACTIVITY_WORDS[_break.activity]} is on now. Say stop any time.",
            }
        tracks = sorted(SOUNDS_DIR.glob("calm_*.wav"))
        if activity == "music" and not tracks:
            logger.error("No music found in %s", SOUNDS_DIR)
            return {"error": "no music is installed; say 'I cannot play music now. Breathing, or something else?'"}

        speaker_id, speaker_name = day_center.current_speaker()
        started_by = str(kwargs.get("started_by", "self"))
        running = RunningBreak(break_id=uuid.uuid4().hex[:8], activity=activity)
        study_log.record_event(
            "break_started",
            break_id=running.break_id,
            activity=activity,
            started_by=started_by,
            # When staff start a break, the voice is theirs, not the person's taking it.
            person=str(kwargs.get("person") or (speaker_name if started_by == "self" else "")),
            feeling_said=str(kwargs.get("feeling_said", ""))[:200],
            speaker_id=speaker_id,
        )
        logger.info("Tool call: take_break activity=%s break_id=%s", activity, running.break_id)
        _break = running
        running.task = asyncio.create_task(_run_break(deps, running, tracks, started_by), name="take-break")
        return {
            "status": "started",
            "break_id": running.break_id,
            "activity": activity,
            "say": ANNOUNCEMENTS[activity],
            "next": "If you have not said it yet, say the 'say' text. Then stay quiet until the app says the "
            "activity is over.",
        }
