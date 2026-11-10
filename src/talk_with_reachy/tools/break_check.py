import asyncio
import logging
from typing import Any

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

# Breaks whose after-break answer was saved; a finished break with none gets "no answer" after NO_ANSWER_AFTER_S.
answered: set[str] = set()
NO_ANSWER_AFTER_S = 60.0


async def record_no_answer_later(break_id: str) -> None:
    """Save "no answer" for a finished break if nobody answered "How do you feel now?" in time."""
    await asyncio.sleep(NO_ANSWER_AFTER_S)
    if break_id not in answered:
        answered.add(break_id)
        study_log.record_event("break_check", break_id=break_id, answer="no answer", source="timeout")


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
        break_id = str(kwargs.get("break_id", ""))
        answered.add(break_id)
        study_log.record_event("break_check", break_id=break_id, answer=answer, speaker_id=speaker_id)
        logger.info("Tool call: break_check answer=%s", answer[:80])
        if answer == "not good":
            return {"saved": answer, "say": "One more break, or help from staff?"}
        return {"saved": answer, "say": "Come back any time you need me."}
