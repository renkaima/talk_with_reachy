import logging
from typing import Any

from talk_with_reachy import choices, study_log, day_center
from talk_with_reachy.tools import take_break
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

STAFF = "Okay. Please go to a staff member, or wave to one."
SAFE = "Please tell a staff member now. This is important."
# What to say and do once an answer is matched to a known choice, so the step goes on and does not stop at
# "Sick. Okay." (keys are choices.canonical names).
NEXT_STEP: dict[str, tuple[str, str]] = {
    "sick": ("Sick. Okay. Please tell a staff member now. This is important.", "Do not start an activity."),
    "upset": ("Upset. Okay. I am sorry you are upset. Breathing, or music?", "Then follow TAKE A BREAK."),
    "staff": (STAFF, "Do not ask anything else now."),
    "break": ("Breathing, or music?", "Then follow TAKE A BREAK."),
    "one more break": ("Breathing, or music?", "Then follow TAKE A BREAK."),
    "something else": ("Okay. I am here if you need me.", "Do not ask what they want instead."),
    "talk": ("Okay. Tell me about your day, or ask me a question.", "Then talk about what they say."),
    "what you can do": ("I can talk with you, help with a break, or tell you what is next. I can also dance.", ""),
    "good": ("", "Call break_check with 'good' if this was the question after a break, and say what it returns."),
    "not good": (
        "",
        "Call break_check with 'not good' if this was the question after a break, and say what it returns.",
    ),
}
ACTIVITY = {"breathing": "breathe", "music": "music", "move": "move"}


def _activity_step(activity: str) -> dict[str, Any]:
    return {
        "they_chose": activity,
        "say": take_break.ANNOUNCEMENTS[activity],
        "next": f"Say this and call take_break with activity '{activity}' in this same reply.",
    }


class AskedToRepeat(Tool):
    """Decide in code what to say when an answer to a two-choice question did not pick a choice."""

    name = "asked_to_repeat"
    description = (
        "Call this, and say nothing else, when the answer to your decision question (a break, feelings, help, "
        "music; never a chat question) did not clearly pick one of the two choices. It checks whether the words sound like one of the choices, and it tells you exactly "
        "what to say next and what to do: the step for the choice they meant, the two choices once more, or that "
        "you will wait. Say those words, then do what 'next' says."
    )
    # The result must reach the model's next spoken reply, also when the model said something with the call.
    needs_response = True
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
        norm = choices.normalize(heard)

        def matched(choice: str, result: dict[str, Any]) -> dict[str, Any]:
            choices.repeats.reset()
            study_log.record_event("answer_matched", heard=heard, choice=choice, speaker_id=speaker_id)
            logger.info("Tool call: asked_to_repeat took %r as %r", heard, choice)
            return result

        names = [set(choices.canonical(option).split("+")) for option in options]
        if len(options) == 2 and any("more" in n for n in names) and any("stop" in n for n in names):
            # "More music, or stop?": "yes" or "okay" means more and "no" means stop (the app may have acted already).
            answer = choices.music_answer(heard)
            if answer == "stop":
                take_break.stop_running_break("tool", heard)
                return matched("stop", {"they_chose": "stop", "say": "Okay. We stopped."})
            if answer == "more":
                if not take_break.choose_after_pause("more") and take_break.running_activity() != "music":
                    return matched("more", {"say": "The song is over."})
                return matched("more", {"they_chose": "more", "say": "More music. Okay."})
        if names and any("talk" in n for n in names) and norm in {"yes", "yeah", "yep", "okay", "ok", "sure"}:
            # The greeting: "yes" to "Do you want to talk with me, or hear what I can do?" means talk.
            return matched("talk", {"they_chose": "talk", "say": NEXT_STEP["talk"][0], "next": NEXT_STEP["talk"][1]})
        if choices.is_decline(heard):
            # "No" picks neither choice; asking again would push.
            return matched("neither", {"say": "Okay. I am here if you need me.", "next": "Do not ask again."})
        if len(options) == 2:
            choice = choices.match(heard, options)
            if choice is not None:
                name = choices.canonical(choice)
                if name in ACTIVITY:
                    return matched(choice, _activity_step(ACTIVITY[name]))
                say, then = NEXT_STEP.get(name, ("", ""))
                return matched(
                    choice,
                    {
                        "they_chose": choice,
                        "say": say or f"{choice.capitalize()}. Okay.",
                        "next": then or f"Take '{choice}' as their answer and go on as if they had said it.",
                    },
                )
        # Hurt, sick, or scared always goes to staff (CALM AND SAFE), even right after another question.
        if choices.says_unwell(heard):
            take_break.stop_running_break("tool", heard)
            return matched("sick", {"say": SAFE, "next": "Do not start an activity."})
        # Whole requests do what they say, even right after another question (exact words only).
        request = choices.request_in(heard)
        if request == "staff":
            take_break.stop_running_break("tool", heard)
            return matched("staff", {"say": STAFF})
        if request is not None:
            return matched(request, _activity_step(request))
        if "don't know" in norm or "dont know" in norm or "not sure" in norm:
            wait = "Okay. I will wait here."
            if len(options) == 2:
                wait += f" Say {options[0]}, or {options[1]}, any time."
            return matched("don't know", {"say": wait + " You can also ask staff.", "next": "Do not ask again."})

        tries = choices.repeats.unclear(options or [heard])
        study_log.record_event("asked_to_repeat", heard=heard, choices=options, tries=tries, speaker_id=speaker_id)
        logger.info("Tool call: asked_to_repeat speaker=%s tries=%d", speaker_id, tries)
        if tries < choices.MAX_TRIES and len(options) == 2:
            return {
                "say": f"Which one: {options[0]}, or {options[1]}?",
                "next": "Ask only these two choices, once. If the next answer is still unclear, call this again.",
            }
        if tries > choices.MAX_TRIES:
            # Already said "I will wait here"; saying it again would sound like a loop.
            return {"say": "I am here.", "next": "Wait for the person to talk."}
        # The count is kept, so more unclear answers to this question get "I am here" until they talk again.
        study_log.record_event("question_given_up", heard=heard, choices=options, speaker_id=speaker_id)
        if len(options) == 2:
            wait = f"I will wait here. You can say {options[0]}, or {options[1]}, any time. You can also ask staff."
        else:
            wait = "I will wait here. You can also ask staff."
        return {"say": wait, "next": "Do not ask this question again. Wait for the person to talk."}
