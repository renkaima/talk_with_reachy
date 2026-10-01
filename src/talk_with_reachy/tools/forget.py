import logging
from typing import Any

from talk_with_reachy import voice_id
from talk_with_reachy.memory import forget_memory_fact
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class Forget(Tool):
    """Remove one long-term memory fact."""

    name = "forget"
    description = (
        "Remove a previously saved fact from long-term memory. Call this when the user asks you to forget something, "
        "or when saved information becomes obsolete. Match by a specific free-text phrase present in the fact."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "A short search phrase that should be present in the fact to remove. Matching is case-insensitive."
                ),
            },
        },
        "required": ["query"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Forget one memory fact by query."""
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            logger.warning("forget: empty query")
            return {"error": "query must be a non-empty string"}

        target = deps.instance_path
        if voice_id.enabled():
            target = voice_id.current_person_dir()
            if target is None:
                return {"error": "nothing was removed: voice ID does not know who is speaking right now"}

        result = forget_memory_fact(target, query=query)
        if result.removed is None:
            logger.info("Tool call: forget query=%s no_match", query[:120])
            return {"error": f'no memory matched "{query}"; nothing was removed'}

        response: dict[str, Any] = {
            "removed": result.removed.text,
            "memory_id": result.removed.id,
        }
        if len(result.candidates) > 1:
            response["other_matches"] = [fact.text for fact in result.candidates[1:]]

        logger.info("Tool call: forget query=%s removed=%s", query[:120], result.removed.text[:120])
        return response
