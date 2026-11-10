import logging
from typing import Any

from talk_with_reachy.tools import take_break
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class StopBreak(Tool):
    """Stop the break activity that is running (breathing, music, or the movement game)."""

    name = "stop_break"
    description = (
        "Stop the break activity that is running now, when someone asks to stop it. "
        "The app usually stops it already when it hears 'stop'; calling this again is harmless."
    )
    needs_response = False
    parameters_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Stop the running break, if any."""
        stopped = take_break.stop_running_break("tool")
        logger.info("Tool call: stop_break (%s)", "stopped" if stopped else "nothing running")
        # The same answer whether this call stopped it or the app had already stopped it on hearing "stop".
        return {"stopped": True, "say": "Okay. We stopped."}
