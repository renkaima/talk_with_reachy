import logging
from typing import Any

from talk_with_reachy.tools import take_break
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class MusicChoice(Tool):
    """Go on with the break music after a pause, or stop it."""

    name = "music_choice"
    description = (
        "Use after the break music paused because someone talked, with their answer to 'More music, or stop?': "
        "more plays the rest of the song; stop ends the break. 'Yes' or 'okay' means more; 'no' means stop. "
        "The app often applies a short answer by itself; calling this again is harmless."
    )
    needs_response = False
    parameters_schema = {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": ["more", "stop"]}},
        "required": ["choice"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Hand the choice to the paused music in take_break."""
        choice = str(kwargs.get("choice", "")).strip()
        if choice not in ("more", "stop"):
            return {"error": "choice must be 'more' or 'stop'"}
        if choice == "stop":
            take_break.stop_running_break("tool")
        else:
            take_break.choose_after_pause("more")
        logger.info("Tool call: music_choice %s", choice)
        # The same answer when the app already applied the person's words, so a late call is not an error.
        return {"saved": choice, "say": "More music. Okay." if choice == "more" else "Okay. We stopped."}
