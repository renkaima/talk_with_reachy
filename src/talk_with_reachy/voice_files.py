"""Where voice-ID files live, and what a speaker ID may look like.

Standard library only, so ``voices_cli`` can run with any Python 3 (for example
on a Mac in simulation) without the app's dependencies.
"""

from __future__ import annotations
import re
from typing import Any
from pathlib import Path


PEOPLE_SUBDIR = "people"
MODELS_SUBDIR = "models"
LIBRARY_FILENAME = "library.json"
REQUESTS_SUBDIR = "requests"
REQUEST_LOG_FILENAME = "requests_log.jsonl"

UNKNOWN = "unknown"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
AUTO_ID_PATTERN = re.compile(r"^V\d{3,}$")


def people_dir(data_dir: Path) -> Path:
    """Folder holding the voice library, per-person memory, and pending commands."""
    return data_dir / PEOPLE_SUBDIR


def person_dir(data_dir: Path, speaker_id: str) -> Path:
    """Folder for one person's memory file."""
    return people_dir(data_dir) / speaker_id


def requests_dir(data_dir: Path) -> Path:
    """Folder where ``talk-with-reachy-voices`` drops commands for the running app."""
    return people_dir(data_dir) / REQUESTS_SUBDIR


def valid_id(value: Any, allow_auto: bool) -> str:
    """Return ``value`` as a speaker ID, or raise ValueError explaining what is wrong."""
    speaker_id = str(value or "").strip()
    if not ID_PATTERN.match(speaker_id) or speaker_id == UNKNOWN:
        raise ValueError(f"invalid ID {speaker_id!r}: use letters, digits, - or _ (for example P01)")
    if not allow_auto and AUTO_ID_PATTERN.match(speaker_id):
        raise ValueError(f"{speaker_id!r} looks like an automatic label; use an ID such as P01")
    return speaker_id
