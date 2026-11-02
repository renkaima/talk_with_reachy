import logging
from typing import Any

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class AskedToRepeat(Tool):
    """Count the times Reachy asks someone to repeat because it did not understand them."""

    name = "asked_to_repeat"
    description = (
        "Call this every time you ask someone to say something again or to choose again "
        "because you did not understand what they said."
    )
    needs_response = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "heard": {"type": "string", "description": "What you heard, word for word, even if it made no sense."},
        },
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Record the request in the study log."""
        speaker_id, _ = day_center.current_speaker()
        study_log.record_event("asked_to_repeat", heard=str(kwargs.get("heard", ""))[:200], speaker_id=speaker_id)
        logger.info("Tool call: asked_to_repeat speaker=%s", speaker_id)
        return {"saved": True}
