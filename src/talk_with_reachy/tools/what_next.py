import re
import json
import logging
from typing import Any
from pathlib import Path
from datetime import date, datetime

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SCHEDULE_FILE = "schedule.json"
UPCOMING_ITEMS = 2


def schedule_path() -> Path:
    """Where the weekly schedule and today's changes are saved."""
    return study_log.configured_data_dir() / "day_center" / SCHEDULE_FILE


def load_schedule() -> dict[str, Any]:
    """Read the schedule file, or return an empty schedule."""
    path = schedule_path()
    if not path.exists():
        return {"week": {}, "changes": {}}
    schedule: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schedule


def save_schedule(schedule: dict[str, Any]) -> None:
    """Write the schedule, dropping changes for days that are over."""
    today = date.today().isoformat()
    schedule["changes"] = {day: change for day, change in schedule["changes"].items() if day >= today}
    path = schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schedule, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_items(raw_items: Any) -> list[dict[str, str]]:
    """Validate schedule items given by the model; times become HH:MM."""
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty list")
    items = []
    for raw in raw_items:
        match = TIME_PATTERN.match(str(raw.get("time", "")).strip()) if isinstance(raw, dict) else None
        activity = str(raw.get("activity", "")).strip() if isinstance(raw, dict) else ""
        if match is None or not activity:
            raise ValueError(f"each item needs a 24-hour time (HH:MM) and an activity, got {raw!r}")
        person = str(raw.get("person", "")).strip()
        items.append({"time": f"{int(match.group(1)):02d}:{match.group(2)}", "activity": activity, "person": person})
    return items


def plan_for(schedule: dict[str, Any], day: date, person: str) -> list[dict[str, str]]:
    """Return the day's plan for everyone plus ``person``'s own items, after today's changes, in time order."""
    change = schedule["changes"].get(day.isoformat(), {})
    removed = {activity.lower() for activity in change.get("remove", [])}
    usual = [
        item for item in schedule["week"].get(WEEKDAYS[day.weekday()], []) if item["activity"].lower() not in removed
    ]
    items = usual + change.get("add", [])
    return sorted(
        (item for item in items if not item["person"] or item["person"].lower() == person.lower()),
        key=lambda item: item["time"],
    )


class WhatNext(Tool):
    """Answer schedule questions, and let staff enter the weekly schedule and today's changes."""

    name = "what_next"
    description = (
        "The day center schedule. "
        "next: what comes next today; today: the whole day. Both include the person's own items. "
        "set_day: STAFF ONLY, replace the usual schedule for a weekday (or 'weekdays' for Monday to Friday). "
        "change_today: STAFF ONLY, cancel activities today and/or add new ones. "
        "Times are 24-hour HH:MM; 'two in the afternoon' is 14:00."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["next", "today", "set_day", "change_today"]},
            "person": {
                "type": "string",
                "description": "Whose schedule to read. Omit to use the person speaking now.",
            },
            "weekday": {"type": "string", "enum": [*WEEKDAYS, "weekdays"], "description": "For set_day."},
            "items": {
                "type": "array",
                "description": "For set_day and change_today: activities to save or add.",
                "items": {
                    "type": "object",
                    "properties": {
                        "time": {"type": "string", "description": "HH:MM, 24-hour"},
                        "activity": {"type": "string"},
                        "person": {"type": "string", "description": "Only for one person's own item; else omit."},
                    },
                    "required": ["time", "activity"],
                },
            },
            "remove": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For change_today: names of activities cancelled today.",
            },
        },
        "required": ["action"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Read or change the schedule."""
        action = str(kwargs.get("action", ""))
        speaker_id, speaker_name = day_center.current_speaker()
        try:
            schedule = load_schedule()
        except (OSError, ValueError) as e:
            logger.error("Could not read the schedule: %s", e)
            return {"error": "the schedule file could not be read"}

        if action in ("next", "today"):
            person = str(kwargs.get("person") or speaker_name)
            plan = plan_for(schedule, date.today(), person)
            now = datetime.now().strftime("%H:%M")
            study_log.record_event("schedule_asked", action=action, person=person, speaker_id=speaker_id)
            logger.info("Tool call: what_next action=%s person=%s", action, person)
            if action == "today":
                return {"now": now, "today": plan}
            upcoming = [item for item in plan if item["time"] >= now][:UPCOMING_ITEMS]
            return (
                {"now": now, "next": upcoming} if upcoming else {"now": now, "next": [], "note": "Nothing else today."}
            )

        if action not in ("set_day", "change_today"):
            return {"error": f"unknown action {action!r}"}
        if not day_center.staff_is_speaking():
            return {"error": "only staff can change the schedule; I did not recognize a staff voice"}
        try:
            items = parse_items(kwargs.get("items")) if action == "set_day" or kwargs.get("items") else []
        except ValueError as e:
            return {"error": str(e)}

        if action == "set_day":
            weekday = str(kwargs.get("weekday", ""))
            days = WEEKDAYS[:5] if weekday == "weekdays" else (weekday,) if weekday in WEEKDAYS else ()
            if not days:
                return {"error": "set_day needs a weekday"}
            for day in days:
                schedule["week"][day] = items
        else:
            removed = [str(activity).strip() for activity in kwargs.get("remove") or [] if str(activity).strip()]
            if not items and not removed:
                return {"error": "change_today needs items to add or activities to remove"}
            change = schedule["changes"].setdefault(date.today().isoformat(), {"add": [], "remove": []})
            # Removing an activity also drops it from earlier changes today, so "gym moves to two" (remove gym,
            # add gym at 14:00) works, and so does changing a change.
            cancelled = {activity.lower() for activity in removed}
            change["add"] = [item for item in change["add"] if item["activity"].lower() not in cancelled] + items
            change["remove"].extend(removed)
        save_schedule(schedule)
        study_log.record_event(
            "schedule_changed",
            action=action,
            weekday=str(kwargs.get("weekday", "")),
            items=items,
            removed=kwargs.get("remove") or [],
            speaker_id=speaker_id,
        )
        logger.info("Tool call: what_next action=%s by %s", action, speaker_id)
        return {"saved": True, "today": plan_for(schedule, date.today(), "")}
