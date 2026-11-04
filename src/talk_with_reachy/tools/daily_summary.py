import logging
from typing import Any
from collections import Counter

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

STAFF_QUESTIONS = (
    "Would you like to add anything about today?",
    "Did anyone do something with Reachy together today?",
)
# What the profile has Reachy say when it did not understand someone.
REPEAT_PHRASE = "say it again"


class DailySummary(Tool):
    """Summarize today's breaks for anyone, with names only for staff, and save staff notes."""

    name = "daily_summary"
    description = (
        "summary: how today went, counted from today's records. Anyone may ask; only staff voices get names. "
        "note: STAFF ONLY, save what a staff member added after the summary."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["summary", "note"]},
            "note": {"type": "string", "description": "For note: what the staff member said, in their words."},
            "question": {"type": "string", "description": "For note: the question the note answers."},
        },
        "required": ["action"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Count today's events, or save a staff note."""
        action = str(kwargs.get("action", ""))
        speaker_id, speaker_name = day_center.current_speaker()
        staff = day_center.staff_is_speaking()

        if action == "note":
            note = str(kwargs.get("note", "")).strip()
            if not staff:
                return {"error": "only staff can add notes; I did not recognize a staff voice"}
            if not note:
                return {"error": "note must be a non-empty string"}
            study_log.record_event(
                "staff_note", note=note, question=str(kwargs.get("question", "")), speaker_id=speaker_id
            )
            logger.info("Tool call: daily_summary note by %s", speaker_id)
            return {"saved": True}
        if action != "summary":
            return {"error": f"unknown action {action!r}"}

        events = day_center.todays_events({"break_started", "break_check", "asked_to_repeat"})
        breaks = [event for event in events if event["event"] == "break_started"]
        checks = {event.get("break_id"): event.get("answer") for event in events if event["event"] == "break_check"}
        # The model can forget to call asked_to_repeat, so Reachy's own words "Please say it again" in today's
        # transcripts are a backup count. Both usually count the same moments, so the larger one is used.
        repeats_logged = sum(event["event"] == "asked_to_repeat" for event in events)
        repeats_said = sum(REPEAT_PHRASE in line.lower() for line in day_center.todays_reachy_lines())
        summary: dict[str, Any] = {
            "breaks": len(breaks),
            "self_started": sum(event.get("started_by") == "self" for event in breaks),
            "staff_started": sum(event.get("started_by") == "staff" for event in breaks),
            "activities": dict(Counter(str(event.get("activity")) for event in breaks)),
            "times_I_asked_someone_to_repeat": max(repeats_logged, repeats_said),
        }
        if staff:
            summary["breaks_by_person"] = [
                {
                    "time": str(event["time"])[11:16],
                    "person": event.get("person") or "someone I did not recognize",
                    "started_by": event.get("started_by"),
                    "activity": event.get("activity"),
                    "afterwards": checks.get(event.get("break_id"), "not asked"),
                }
                for event in breaks
            ]
            summary["then_ask_one_at_a_time"] = list(STAFF_QUESTIONS)
        else:
            summary["names"] = "Only staff hear names."
        study_log.record_event("daily_summary_given", level="staff" if staff else "totals", speaker_id=speaker_id)
        logger.info("Tool call: daily_summary level=%s speaker=%s", "staff" if staff else "totals", speaker_name)
        return summary
