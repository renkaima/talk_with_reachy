import logging
from typing import Any

from talk_with_reachy import choices, study_log, day_center
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class AskedToRepeat(Tool):
    """Decide in code what to say when an answer to a two-choice question did not pick a choice."""

    name = "asked_to_repeat"
    description = (
        "Call this, and say nothing else, when the answer to your two-choice question did not clearly pick one of "
        "the two choices. It checks whether the words sound like one of the choices, and it tells you exactly "
        "what to say next: the choice they meant, the two choices once more, or that you will wait."
    )
    needs_response = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "heard": {"type": "string", "description": "What you heard, word for word, even if it made no sense."},
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The two choices you offered, for example ['breathing', 'music'].",
            },
        },
        "required": ["heard"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Match the answer to a choice, or allow one more ask, or end the asking."""
        heard = str(kwargs.get("heard", ""))[:200]
        given = kwargs.get("choices")
        options = [str(option) for option in given if str(option).strip()] if isinstance(given, list) else []
        if len(options) != 2:
            options = choices.last_question_choices()
        speaker_id, _ = day_center.current_speaker()
        if len(options) == 2:
            matched = choices.match(heard, options)
            if matched is not None:
                choices.repeats.reset()
                study_log.record_event("answer_matched", heard=heard, choice=matched, speaker_id=speaker_id)
                logger.info("Tool call: asked_to_repeat matched %r to %r", heard, matched)
                return {
                    "they_chose": matched,
                    "say": f"{matched.capitalize()}. Okay.",
                    "next": f"Take '{matched}' as their answer and go on as if they had said it.",
                }
        tries = choices.repeats.unclear(options or [heard])
        study_log.record_event("asked_to_repeat", heard=heard, choices=options, tries=tries, speaker_id=speaker_id)
        logger.info("Tool call: asked_to_repeat speaker=%s tries=%d", speaker_id, tries)
        if tries < choices.MAX_TRIES and len(options) == 2:
            return {
                "say": f"Which one: {options[0]}, or {options[1]}?",
                "next": "Ask only these two choices, once. If the next answer is still unclear, call this again.",
            }
        # The count is kept, so more unclear answers to this question get the same "I will wait here".
        study_log.record_event("question_given_up", heard=heard, choices=options, speaker_id=speaker_id)
        if len(options) == 2:
            wait = f"I will wait here. You can say {options[0]}, or {options[1]}, any time. You can also ask staff."
        else:
            wait = "I will wait here. You can also ask staff."
        return {"say": wait, "next": "Do not ask this question again. Wait for the person to talk."}
