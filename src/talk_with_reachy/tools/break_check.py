import asyncio
import logging
from typing import Any

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

# Breaks whose after-break answer was saved; a finished break with none gets "no answer" after NO_ANSWER_AFTER_S.
answered: set[str] = set()
NO_ANSWER_AFTER_S = 60.0
# The newest finished break that still waits for its answer to "How do you feel now: good, or not good?".
awaiting: str | None = None
# Seconds the model has to call break_check itself before the app saves the answer it heard.
SAVE_HEARD_ANSWER_AFTER_S = 15.0


def expect_answer(break_id: str) -> None:
    """Mark a finished break as waiting for the answer to the question after it."""
    global awaiting
    awaiting = break_id


def waiting_break_id() -> str | None:
    """Return the finished break whose after-break answer has not been saved yet, or None."""
    return awaiting if awaiting is not None and awaiting not in answered else None


def _save(break_id: str, answer: str, **fields: Any) -> None:
    global awaiting
    answered.add(break_id)
    if awaiting == break_id:
        awaiting = None
    study_log.record_event("break_check", break_id=break_id, answer=answer, **fields)


async def record_no_answer_later(break_id: str) -> None:
    """Save "no answer" for a finished break if nobody answered "How do you feel now?" in time."""
    await asyncio.sleep(NO_ANSWER_AFTER_S)
    if break_id not in answered:
        _save(break_id, "no answer", source="timeout")


async def record_heard_answer_later(break_id: str, answer: str, speaker_id: str | None) -> None:
    """Save the answer the app heard, if the model has not called break_check by then.

    In a test the model took "Not good." after a break as a new complaint and never called break_check, so the
    self-report for that break would have been lost.
    """
    await asyncio.sleep(SAVE_HEARD_ANSWER_AFTER_S)
    if break_id not in answered:
        _save(break_id, answer, source="app", speaker_id=speaker_id)
        logger.info("break_check saved by the app: %s", answer)


class BreakCheck(Tool):
    """Save the person's answer when 'good, or not good?' is asked again after a break."""

    name = "break_check"
    description = (
        "Save the answer to 'How do you feel now: good, or not good?', asked once after a break activity "
        "finished. Use 'unclear' for any other answer."
    )
    needs_response = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "break_id": {"type": "string", "description": "The break_id that take_break returned."},
            "answer": {"type": "string", "enum": ["good", "not good", "don't know", "unclear", "no answer"]},
        },
        "required": ["break_id", "answer"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Record the answer in the study log."""
        answer = str(kwargs.get("answer", "")).strip()
        if not answer:
            return {"error": "answer must be a non-empty string"}
        speaker_id, _ = day_center.current_speaker()
        break_id = str(kwargs.get("break_id", "")) or waiting_break_id() or ""
        if break_id in answered:
            # The app already saved the answer it heard; a late call must not log the break twice.
            logger.info("Tool call: break_check for %s was already saved", break_id)
        else:
            _save(break_id, answer, speaker_id=speaker_id)
        logger.info("Tool call: break_check answer=%s", answer[:80])
        if answer == "not good":
            return {"saved": answer, "say": "One more break, or help from staff?"}
        return {"saved": answer, "say": "Come back any time you need me."}
