import logging
from typing import Any

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class BreakCheck(Tool):
    """Save the person's answer to the closing question after a break."""

    name = "break_check"
    description = (
        "Save the answer to the closing question after take_break finishes, "
        "for example 'better' or 'the same'. Use 'no answer' if they did not answer."
    )
    needs_response = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "break_id": {"type": "string", "description": "The break_id that take_break returned."},
            "answer": {"type": "string", "description": "Their answer in a few words."},
        },
        "required": ["break_id", "answer"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Record the answer in the study log."""
        answer = str(kwargs.get("answer", "")).strip()
        if not answer:
            return {"error": "answer must be a non-empty string"}
        speaker_id, _ = day_center.current_speaker()
        study_log.record_event(
            "break_check", break_id=str(kwargs.get("break_id", "")), answer=answer, speaker_id=speaker_id
        )
        logger.info("Tool call: break_check answer=%s", answer[:80])
        return {"saved": answer}
