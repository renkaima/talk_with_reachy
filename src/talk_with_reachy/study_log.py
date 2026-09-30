"""Transcript logging for the Talk with Reachy study.

Every finalized utterance, what people said to Reachy and what Reachy said
back, is appended to one JSONL file per app run. The app cannot tell people
apart, so all human speech is logged as ``"speaker": "person"``. No audio or
video is stored.

Files live outside the installed package (``~/talk_with_reachy_data`` by
default) so that app updates never touch them. When a OneDrive client ID is
configured, a background uploader copies them to the signed-in account's
OneDrive app folder (see ``onedrive_upload``).

Logging must never interrupt a conversation, so every public function here
catches and logs its own errors.
"""

from __future__ import annotations
import os
import json
import uuid
import socket
import logging
import threading
from pathlib import Path
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version


logger = logging.getLogger(__name__)

LOGGING_ENABLED_ENV = "TALK_WITH_REACHY_LOGGING"
DATA_DIR_ENV = "TALK_WITH_REACHY_DATA_DIR"
ROBOT_ID_ENV = "TALK_WITH_REACHY_ROBOT_ID"
DEFAULT_DATA_DIR = Path.home() / "talk_with_reachy_data"
TRANSCRIPTS_SUBDIR = "transcripts"

# Upstream role names mapped to the labels used in the study data.
SPEAKER_BY_ROLE = {"user": "person", "assistant": "reachy"}

# Shared by the writer and the uploader so the uploader never reads a half-written line.
FILE_LOCK = threading.Lock()


def _now() -> str:
    """Return local time with its UTC offset, so timestamps are unambiguous."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _app_version() -> str:
    try:
        return version("talk_with_reachy")
    except PackageNotFoundError:
        return "unknown"


def transcripts_dir(data_dir: Path) -> Path:
    """Return the folder that holds transcript files."""
    return data_dir / TRANSCRIPTS_SUBDIR


class TranscriptSession:
    """Append-only JSONL log for one run of the app."""

    def __init__(self, data_dir: Path, robot_id: str) -> None:
        """Create the transcript file and write the session header."""
        self.session_id = uuid.uuid4().hex
        self._seq = 0
        started = datetime.now()
        folder = transcripts_dir(data_dir)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{robot_id}_{started:%Y%m%d-%H%M%S}_{self.session_id[:6]}.jsonl"
        self.path = folder / name
        self._write(
            {
                "type": "session_start",
                "time": _now(),
                "robot_id": robot_id,
                "app_version": _app_version(),
            }
        )

    def _write(self, record: dict[str, object]) -> None:
        record = {"session_id": self.session_id, **record}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with FILE_LOCK:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def utterance(self, role: str, text: str) -> None:
        """Append one finalized utterance."""
        self._seq += 1
        self._write(
            {
                "type": "utterance",
                "seq": self._seq,
                "time": _now(),
                "speaker": SPEAKER_BY_ROLE.get(role, role),
                "text": text,
            }
        )

    def close(self) -> None:
        """Write the session footer."""
        self._write({"type": "session_end", "time": _now(), "utterances": self._seq})


_session: TranscriptSession | None = None
_uploader_stop: threading.Event | None = None
_uploader_thread: threading.Thread | None = None


def start() -> None:
    """Open a transcript file for this run and start the OneDrive uploader if configured."""
    global _session, _uploader_stop, _uploader_thread
    if os.getenv(LOGGING_ENABLED_ENV, "1").strip() == "0":
        logger.info("Transcript logging disabled (%s=0)", LOGGING_ENABLED_ENV)
        return
    try:
        data_dir = Path(os.getenv(DATA_DIR_ENV) or DEFAULT_DATA_DIR).expanduser()
        robot_id = os.getenv(ROBOT_ID_ENV) or socket.gethostname()
        _session = TranscriptSession(data_dir, robot_id)
        logger.info("Logging transcripts to %s", _session.path)
    except Exception:
        logger.exception("Could not start transcript logging; the conversation will continue without it")
        _session = None
        return

    try:
        from talk_with_reachy.onedrive_upload import OneDriveUploader

        uploader = OneDriveUploader.from_env(data_dir)
        if uploader is None:
            return
        _uploader_stop = threading.Event()
        _uploader_thread = threading.Thread(
            target=uploader.run_until,
            args=(_uploader_stop,),
            daemon=True,
            name="onedrive-uploader",
        )
        _uploader_thread.start()
    except Exception:
        logger.exception("Could not start the OneDrive uploader; transcripts stay on the robot")


def record_utterance(role: str, text: str, final: bool) -> None:
    """Log one transcript event from the conversation handler; non-final text is skipped."""
    if _session is None or not final or not text.strip():
        return
    try:
        _session.utterance(role, text)
    except Exception:
        logger.exception("Failed to write transcript line")


def stop(upload_timeout_s: float = 8.0) -> None:
    """Close the transcript file and give the uploader one last chance to upload it.

    The daemon kills an app that has not exited 20 s after it was asked to stop, so the
    wait here stays short. Anything not uploaded now is uploaded on the next start.
    """
    global _session, _uploader_stop, _uploader_thread
    if _session is not None:
        try:
            _session.close()
        except Exception:
            logger.exception("Failed to close transcript file")
        _session = None
    if _uploader_stop is not None and _uploader_thread is not None:
        _uploader_stop.set()
        _uploader_thread.join(timeout=upload_timeout_s)
    _uploader_stop = None
    _uploader_thread = None
