import math
import uuid
import wave
import random
import asyncio
import logging
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
MOVE_GAME_ROUNDS = 5
UPRIGHT_ANTENNA = 0.1745  # ~10° off vertical, as in the SDK's rest pose, to keep the antennas from shaking
# The same chimes open and close every activity, so the start and the end are always clear.
START_CHIME = SOUNDS_DIR / "chime_start.wav"
END_CHIME = SOUNDS_DIR / "chime_end.wav"

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

    def __init__(self, seconds: float) -> None:
        """Initialize a countdown lasting ``seconds``."""
        self.seconds = seconds

    @property
    def duration(self) -> float:
        """Duration property required by official Move interface."""
        return self.seconds

    def evaluate(self, t: float) -> tuple[NDArray[np.float64] | None, NDArray[np.float64] | None, float | None]:
        """Evaluate the countdown at time t: antennas fold from upright to 1.2 rad as the time runs out."""
        fold = UPRIGHT_ANTENNA + (1.2 - UPRIGHT_ANTENNA) * min(max(t / self.seconds, 0.0), 1.0)
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


class TakeBreak(Tool):
    """Lead one break activity and wait until it ends."""

    name = "take_break"
    description = (
        "Lead a short break once the person has chosen one of two activities. "
        "breathe: six slow breaths (one minute); your head rises and your antennas stand up on each in-breath. "
        "music: one calm song (one minute); your antennas fold down slowly to show how much is left. "
        "You cannot hear anyone while it plays, so it cannot be stopped. "
        "move: one short movement game (about 40 seconds) that anyone nearby can copy. "
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
                "enum": ["good", "not good", "unclear"],
                "description": "Their answer to 'Are you feeling good or not good?'",
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
        was_muted: bool | None = None  # mic state before the music, to restore afterwards
        try:
            if activity == "music" and deps.set_mic_muted is not None:
                was_muted = deps.set_mic_muted(True)
            await play_and_wait(deps, START_CHIME)
            if activity == "breathe":
                move = GuidedBreathingMove()
                deps.movement_manager.queue_move(move)
                await asyncio.sleep(move.duration)
            elif activity == "move":
                moves = [DanceQueueMove(move_name) for _ in range(MOVE_GAME_ROUNDS) for move_name in MOVE_GAME]
                for dance_move in moves:
                    deps.movement_manager.queue_move(dance_move)
                await asyncio.sleep(sum(dance_move.duration for dance_move in moves))
            else:
                track = random.choice(tracks)
                deps.movement_manager.queue_move(CountdownMove(wav_seconds(track)))
                await play_and_wait(deps, track)
            await play_and_wait(deps, END_CHIME)
            finished = True
        except asyncio.CancelledError:
            deps.movement_manager.clear_move_queue()
            raise
        finally:
            if was_muted is not None and deps.set_mic_muted is not None:
                deps.set_mic_muted(was_muted)
            TakeBreak._running = False
            study_log.record_event(
                "break_finished",
                break_id=break_id,
                activity=activity,
                seconds=round(loop.time() - started, 1),
                completed=finished,
            )

        return {
            "status": "finished",
            "break_id": break_id,
            "activity": activity,
            "next": "Ask one closing choice question, such as 'Do you feel better, or the same?', "
            "then call break_check with their answer.",
        }
