import logging
from typing import Any

from talk_with_reachy.tools import take_break
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class MusicChoice(Tool):
    """Go on with the break music after a pause, or stop it."""

    name = "music_choice"
    description = (
        "Use only after the break music paused because someone talked. Pass their answer to "
        "'Do you want more music, or stop?': more plays the rest of the song; stop ends the break."
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
        if not take_break.choose_after_pause(choice):
            return {"error": "no music is paused right now"}
        logger.info("Tool call: music_choice %s", choice)
        # Used only when the reply had no speech and the app asks the model to talk (see huggingface_realtime).
        return {"saved": choice, "say": "More music. Okay." if choice == "more" else "Okay. We stopped."}
