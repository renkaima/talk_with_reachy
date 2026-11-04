"""Shared pieces of the Day Center profile: who is speaking, who is staff, and today's study events."""

import json
import logging
from typing import Any
from datetime import date, datetime

from talk_with_reachy import voice_id, study_log


logger = logging.getLogger(__name__)

# Staff are enrolled with IDs that start with S (S01, S02, ...); participants with P (P01, P02, ...).
STAFF_ID_PREFIX = "S"
# Staff-only answers need a voice identified seconds ago: voice ID keeps the last confident match for
# two minutes and does not update it for very short utterances, which would let a participant inherit it.
STAFF_MAX_AGE_S = 15.0


def current_speaker(max_age_s: float = voice_id.CURRENT_SPEAKER_MAX_AGE_S) -> tuple[str | None, str]:
    """ID and name of the voice identified within ``max_age_s``, as far as voice ID knows."""
    ident = voice_id.identifier()
    speaker_id = ident.current_speaker(max_age_s) if ident is not None else None
    name = ident.name_of(speaker_id) if ident is not None and speaker_id is not None else ""
    return speaker_id, name


def staff_is_speaking() -> bool:
    """Return whether the voice identified in the last few seconds belongs to a staff member."""
    speaker_id, _ = current_speaker(STAFF_MAX_AGE_S)
    return speaker_id is not None and speaker_id.upper().startswith(STAFF_ID_PREFIX)


def _todays_records(day: date) -> list[dict[str, Any]]:
    """Every transcript record written on ``day``, across all sessions."""
    folder = study_log.transcripts_dir(study_log.configured_data_dir())
    records: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.jsonl")):
        if datetime.fromtimestamp(path.stat().st_mtime).date() < day:
            continue
        with study_log.FILE_LOCK, path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Skipping unreadable line in %s: %s", path.name, e)
                continue
            if str(record.get("time", "")).startswith(day.isoformat()):
                records.append(record)
    return records


def todays_events(names: set[str], today: date | None = None) -> list[dict[str, Any]]:
    """Study log events named in ``names`` that were recorded today, oldest first, across all sessions."""
    events = [
        record
        for record in _todays_records(today or date.today())
        if record.get("type") == "event" and record.get("event") in names
    ]
    return sorted(events, key=lambda record: str(record["time"]))


def todays_reachy_lines(today: date | None = None) -> list[str]:
    """Return what Reachy said today, across all sessions."""
    return [
        str(record.get("text", ""))
        for record in _todays_records(today or date.today())
        if record.get("type") == "utterance" and record.get("speaker") == "reachy"
    ]
