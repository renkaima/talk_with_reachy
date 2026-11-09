import math
import time
import uuid
import wave
import random
import shutil
import asyncio
import logging
import tempfile
from typing import Any, ClassVar
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from reachy_mini.utils import create_head_pose
from reachy_mini.motion.move import Move
from talk_with_reachy import study_log, day_center
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
MAX_PAUSE_S = 45.0
# Hidden note sent to the model when the music pauses because someone started talking.
MUSIC_PAUSED_NOTE = (
    "The break music is paused because someone started talking. Answer what they said in one short sentence. "
    'Then ask: "Do you want more music, or stop?" and call music_choice with "more" or "stop". '
    'If they already said stop, say "Okay. We stopped." and call music_choice with "stop" without asking.'
)

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

    async def wait_until_paused(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for a pause; True if someone started talking."""
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


# The song of the break that is running now, if any; the realtime handler pauses it when someone talks.
_music: BreakMusic | None = None


def pause_music_for_speech() -> bool:
    """Pause the break music because someone started talking; True if music was playing."""
    return _music.pause() if _music is not None else False


def choose_after_pause(choice: str) -> bool:
    """Go on with the paused music ("more") or end it ("stop"); False if no music is waiting for a choice."""
    return _music.choose(choice) if _music is not None else False


async def play_break_music(deps: ToolDependencies, track: Path, break_id: str) -> str:
    """Play one song with an antenna countdown; return "finished", "stop", or "no answer".

    When someone starts talking, the song stops where it is and waits for "more" (the rest plays, and the
    countdown goes on from where it was) or "stop".
    """
    global _music
    music = BreakMusic(track)
    _music = music
    try:
        while (remaining := music.remaining_s()) > 0.1:
            sound = await asyncio.to_thread(music.sound_from_position)
            deps.movement_manager.queue_move(CountdownMove(remaining, music.position_s / music.length_s))
            music.mark_playing()
            await asyncio.to_thread(deps.reachy_mini.media.play_sound, str(sound))
            if not await music.wait_until_paused(remaining + PLAYBACK_LAG_S):
                break
            await asyncio.to_thread(deps.reachy_mini.media.play_sound, str(SILENCE))
            deps.movement_manager.clear_move_queue()
            study_log.record_event("music_paused", break_id=break_id, at_s=round(music.position_s, 1))
            choice = await music.wait_for_choice()
            study_log.record_event("music_choice", break_id=break_id, choice=choice)
            if choice != "more":
                return choice
            await wait_until_reachy_is_quiet(deps)
        return "finished"
    except asyncio.CancelledError:
        deps.reachy_mini.media.play_sound(str(SILENCE))
        raise
    finally:
        _music = None
        music.cleanup()


class TakeBreak(Tool):
    """Lead one break activity and wait until it ends."""

    name = "take_break"
    description = (
        "Lead a short break once the person has chosen one of two activities. "
        "breathe: six slow breaths (one minute); your head rises and your antennas stand up on each in-breath, "
        "and people can watch or breathe with you. "
        "music: one calm song (one minute); your antennas fold down slowly to show how much is left. "
        "If someone talks during the song, it pauses by itself; then ask whether they want more music or to stop, "
        "and call music_choice. "
        "move: one short movement game (about 35 seconds) that anyone nearby can copy. "
        "A soft chime marks the start and a two-note chime marks the end. "
        "The tool returns when the activity is over."
    )
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
            "feeling": {
                "type": "string",
                "enum": ["good", "not good", "don't know", "unclear"],
                "description": "Their answer to 'Are you feeling good, or not good?'",
            },
        },
        "required": ["activity", "started_by"],
    }
    _running: ClassVar[bool] = False

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Run the chosen activity, then report how long it lasted."""
        activity = str(kwargs.get("activity", ""))
        if activity not in ("breathe", "music", "move"):
            return {"error": f"unknown activity {activity!r}"}
        if activity == "move" and not DANCE_AVAILABLE:
            return {"error": "the movement game is not available on this robot; offer breathing or music"}
        if TakeBreak._running:
            return {"error": "a break is already running"}
        tracks = sorted(SOUNDS_DIR.glob("calm_*.wav"))
        if activity == "music" and not tracks:
            logger.error("No music found in %s", SOUNDS_DIR)
            return {"error": "no music is installed; offer breathing instead"}

        speaker_id, speaker_name = day_center.current_speaker()
        started_by = str(kwargs.get("started_by", "self"))
        break_id = uuid.uuid4().hex[:8]
        study_log.record_event(
            "break_started",
            break_id=break_id,
            activity=activity,
            started_by=started_by,
            # When staff start a break, the voice is theirs, not the person's taking it.
            person=str(kwargs.get("person") or (speaker_name if started_by == "self" else "")),
            feeling=str(kwargs.get("feeling", "unclear")),
            speaker_id=speaker_id,
        )
        logger.info("Tool call: take_break activity=%s break_id=%s", activity, break_id)

        TakeBreak._running = True
        loop = asyncio.get_running_loop()
        started = loop.time()
        finished = False
        music_outcome = "finished"
        try:
            await wait_until_reachy_is_quiet(deps)
            started = loop.time()
            await play_and_wait(deps, START_CHIME)
            if activity == "breathe":
                move = GuidedBreathingMove()
                deps.movement_manager.queue_move(move)
                await asyncio.sleep(move.duration)
            elif activity == "move":
                moves = [DanceQueueMove(move_name) for move_name in MOVE_GAME for _ in range(MOVE_REPEATS)]
                for dance_move in moves:
                    deps.movement_manager.queue_move(dance_move)
                await asyncio.sleep(sum(dance_move.duration for dance_move in moves))
            else:
                music_outcome = await play_break_music(deps, random.choice(tracks), break_id)
                await wait_until_reachy_is_quiet(deps)
            await play_and_wait(deps, END_CHIME)
            finished = music_outcome == "finished"
        except asyncio.CancelledError:
            deps.movement_manager.clear_move_queue()
            raise
        finally:
            TakeBreak._running = False
            study_log.record_event(
                "break_finished",
                break_id=break_id,
                activity=activity,
                seconds=round(loop.time() - started, 1),
                completed=finished,
            )

        if music_outcome == "stop":
            return {
                "status": "stopped",
                "break_id": break_id,
                "activity": activity,
                "next": "The person chose to stop the music. If you have not said it yet, say 'Okay. We stopped.' "
                "Do not ask how they feel.",
            }
        if music_outcome == "no answer":
            return {
                "status": "stopped",
                "break_id": break_id,
                "activity": activity,
                "next": "The music stayed paused with no choice, so the break ended. Say 'All done.'",
            }
        return {
            "status": "finished",
            "break_id": break_id,
            "activity": activity,
            "next": "Say 'All done.' Ask again: 'Are you feeling good, or not good?', "
            "then call break_check with their answer.",
        }
