import logging
from typing import Any

from talk_with_reachy import voice_id
from talk_with_reachy.memory import add_memory_fact
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class Remember(Tool):
    """Save one short long-term memory fact about the user."""

    name = "remember"
    description = (
        "Save ONE short fact about the user to long-term memory so it is available in future sessions. "
        "Use this for stable user information they explicitly shared: name, preferences, hobbies, recurring projects, "
        "important people, or plans. Keep each fact atomic and under one sentence. Do not save sensitive data "
        "(passwords, addresses, payment info, health diagnoses) or fleeting details. Use this silently in the "
        'background; acknowledge naturally without saying "I will remember that".'
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": (
                    "A short, third-person statement about the user, such as "
                    '"Has a dog named Mochi" or "Prefers replies in French". One fact per call.'
                ),
            },
        },
        "required": ["fact"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Save one memory fact."""
        fact = kwargs.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            logger.warning("remember: empty fact")
            return {"error": "fact must be a non-empty string"}

        target = deps.instance_path
        speaker_id = None
        if voice_id.enabled():
            # Memories are per person: save to whoever voice ID says is speaking now.
            speaker_id = voice_id.current_speaker()
            target = voice_id.current_person_dir()
            if target is None:
                return {"error": "nothing was saved: voice ID does not know who is speaking right now"}

        stored = add_memory_fact(target, fact)
        if stored is None:
            return {"error": "fact was empty or invalid; nothing was saved"}

        logger.info("Tool call: remember fact=%s speaker=%s", stored.text[:120], speaker_id)
        result = {"saved": stored.text, "memory_id": stored.id}
        if speaker_id is not None:
            result["about"] = speaker_id
        return result
