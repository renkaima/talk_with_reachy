"""Study data logging for Talk with Reachy.

Each app run writes two files to ``<data dir>/transcripts/``:

- ``<robot>_<YYYYMMDD-HHMMSS>_<id>.jsonl``: one JSON record per line. The first
  record is ``session_start`` and the last is ``session_end``. In between are
  ``utterance`` records (what a person or Reachy said, with start and end times
  and, for people, the voice-ID result) and ``event`` records (tool calls,
  interruptions, connection and upload status, clock sync).
- ``<same name>.csv``: the same timeline as a spreadsheet, one row per record.

Every write happens on one background thread (see ``submit``), so records keep
the order in which the conversation produced them and logging never blocks the
conversation. Logging must never interrupt a conversation, so every public
function here catches and logs its own errors.

Files live outside the installed package (``~/talk_with_reachy_data`` by
default) so that app updates never touch them. When a OneDrive client ID is
configured, a background uploader copies the data folder to the signed-in
account's Google Drive (see ``cloud_upload`` and ``google_drive_upload``).
"""

from __future__ import annotations
import os
import csv
import json
import time
import uuid
import socket
import logging
import threading
import subprocess
from typing import Any
from pathlib import Path
from datetime import datetime
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version


logger = logging.getLogger(__name__)

LOGGING_ENABLED_ENV = "TALK_WITH_REACHY_LOGGING"
DATA_DIR_ENV = "TALK_WITH_REACHY_DATA_DIR"
ROBOT_ID_ENV = "TALK_WITH_REACHY_ROBOT_ID"
DEFAULT_DATA_DIR = Path.home() / "talk_with_reachy_data"
TRANSCRIPTS_SUBDIR = "transcripts"
AUDIO_SUBDIR = "audio"
SCHEMA_VERSION = 2

# Upstream role names mapped to the labels used in the study data.
SPEAKER_BY_ROLE = {"user": "person", "assistant": "reachy"}

CSV_COLUMNS = [
    "seq",
    "start",
    "end",
    "duration_s",
    "elapsed",
    "kind",
    "speaker",
    "speaker_id",
    "match_score",
    "id_status",
    "flags",
    "text",
    "audio_file",
]

# Utterance fields shown in the CSV ``flags`` column when true.
CSV_FLAGS = ("interrupted", "overlaps_reachy")

# Shared by the writer and the uploader so the uploader never reads a half-written line.
FILE_LOCK = threading.Lock()

CLOCK_CHECK_INTERVAL_S = 600.0


def logging_enabled() -> bool:
    """Return whether study logging is switched on (it is unless the env var is 0)."""
    return os.getenv(LOGGING_ENABLED_ENV, "1").strip() != "0"


def configured_data_dir() -> Path:
    """Return the data folder from the environment, or the default."""
    return Path(os.getenv(DATA_DIR_ENV) or DEFAULT_DATA_DIR).expanduser()


def transcripts_dir(data_dir: Path) -> Path:
    """Return the folder that holds transcript files."""
    return data_dir / TRANSCRIPTS_SUBDIR


def iso(wall_time: float) -> str:
    """Return a wall-clock time as local ISO 8601 with milliseconds and UTC offset."""
    return datetime.fromtimestamp(wall_time).astimezone().isoformat(sep=" ", timespec="milliseconds")


def _now() -> str:
    return datetime.now().astimezone().isoformat(sep=" ", timespec="milliseconds")


def format_elapsed(seconds: float) -> str:
    """Format seconds since the session started as H:MM:SS.s."""
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:04.1f}"


def _app_version() -> str:
    try:
        return version("talk_with_reachy")
    except PackageNotFoundError:
        return "unknown"


def clock_synced() -> bool | None:
    """Return whether the system clock is synchronized with network time, or None if unknown.

    The robot has no battery-backed clock, so until it reaches a time server after
    boot its wall-clock time can be wrong. ``elapsed_s`` is unaffected either way.
    """
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip().lower()
    if value in ("yes", "no"):
        return value == "yes"
    return None


def _event_summary(name: str, fields: dict[str, Any]) -> str:
    parts = [name]
    for key, value in fields.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={text}")
    return " ".join(parts).replace("\n", " / ")


class TranscriptSession:
    """Append-only JSONL log, plus a CSV copy of the timeline, for one run of the app."""

    def __init__(self, data_dir: Path, robot_id: str, extra_start_fields: dict[str, Any] | None = None) -> None:
        """Create the session files and write the session header."""
        self.session_id = uuid.uuid4().hex
        self.data_dir = data_dir
        self.started_mono = time.monotonic()
        self._seq = 0
        self._utterances = 0
        started = datetime.now()
        folder = transcripts_dir(data_dir)
        folder.mkdir(parents=True, exist_ok=True)
        self.stem = f"{robot_id}_{started:%Y%m%d-%H%M%S}_{self.session_id[:6]}"
        self.path = folder / f"{self.stem}.jsonl"
        self.csv_path = folder / f"{self.stem}.csv"
        with FILE_LOCK:
            with self.csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle).writerow(CSV_COLUMNS)
        start_fields: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "robot_id": robot_id,
            "app_version": _app_version(),
            "utc_offset": datetime.now().astimezone().strftime("%z"),
        }
        start_fields.update(extra_start_fields or {})
        self._write({"type": "session_start", "time": _now(), "elapsed_s": 0.0, **start_fields})
        self._csv_row({"kind": "event", "start": _now(), "elapsed": format_elapsed(0.0), "text": "session_start"})

    @property
    def audio_dir(self) -> Path:
        """Folder for this session's utterance audio clips."""
        return self.data_dir / AUDIO_SUBDIR / self.stem

    def elapsed_s(self, mono_time: float) -> float:
        """Seconds between the session start and ``mono_time`` (a ``time.monotonic()`` value)."""
        return round(max(0.0, mono_time - self.started_mono), 3)

    def next_seq(self) -> int:
        """Reserve the next utterance sequence number."""
        self._seq += 1
        return self._seq

    def _write(self, record: dict[str, Any]) -> None:
        record = {"session_id": self.session_id, **record}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with FILE_LOCK:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def _csv_row(self, row: dict[str, Any]) -> None:
        with FILE_LOCK:
            with self.csv_path.open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(["" if row.get(col) is None else row.get(col) for col in CSV_COLUMNS])
                handle.flush()
                os.fsync(handle.fileno())

    def utterance(
        self,
        speaker: str,
        text: str,
        *,
        seq: int,
        start_wall: float,
        end_wall: float,
        start_mono: float,
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Append one finalized utterance with its start and end times."""
        self._utterances += 1
        extra = dict(fields or {})
        duration = round(max(0.0, end_wall - start_wall), 3)
        elapsed = self.elapsed_s(start_mono)
        record: dict[str, Any] = {
            "type": "utterance",
            "seq": seq,
            "time": _now(),
            "start": iso(start_wall),
            "end": iso(end_wall),
            "duration_s": duration,
            "elapsed_s": elapsed,
            "speaker": speaker,
            **extra,
            "text": text,
        }
        self._write(record)
        score = extra.get("match_score")
        self._csv_row(
            {
                "seq": seq,
                "start": record["start"],
                "end": record["end"],
                "duration_s": duration,
                "elapsed": format_elapsed(elapsed),
                "kind": "utterance",
                "speaker": speaker,
                "speaker_id": extra.get("speaker_id"),
                "match_score": f"{score:.2f}" if isinstance(score, float) else None,
                "id_status": extra.get("id_status"),
                "flags": " ".join(flag for flag in CSV_FLAGS if extra.get(flag)) or None,
                "text": text,
                "audio_file": extra.get("audio_file"),
            }
        )

    def event(self, name: str, mono_time: float, fields: dict[str, Any]) -> None:
        """Append one event (tool call, interruption, system status...)."""
        elapsed = self.elapsed_s(mono_time)
        now = _now()
        self._write({"type": "event", "event": name, "time": now, "elapsed_s": elapsed, **fields})
        self._csv_row(
            {"start": now, "elapsed": format_elapsed(elapsed), "kind": "event", "text": _event_summary(name, fields)}
        )

    def close(self, mono_time: float) -> None:
        """Write the session footer."""
        elapsed = self.elapsed_s(mono_time)
        self._write({"type": "session_end", "time": _now(), "elapsed_s": elapsed, "utterances": self._utterances})
        self._csv_row({"kind": "event", "start": _now(), "elapsed": format_elapsed(elapsed), "text": "session_end"})


_session: TranscriptSession | None = None
_worker: ThreadPoolExecutor | None = None
_uploader_stop: threading.Event | None = None
_uploader_thread: threading.Thread | None = None
_last_clock_state: bool | None = None
_last_clock_check: float = 0.0
_state_lock = threading.Lock()


def is_active() -> bool:
    """Return whether a study session is open."""
    return _session is not None


def current_session() -> TranscriptSession | None:
    """Return the open session, if any. Only use it from inside a ``submit`` job."""
    return _session


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any] | None:
    """Run ``fn`` on the study worker thread, after everything submitted before it.

    Returns None when no session is open. Exceptions are logged, never raised.
    """
    worker = _worker
    if worker is None:
        return None

    def job() -> Any:
        try:
            _maybe_check_clock()
            return fn(*args, **kwargs)
        except Exception:
            logger.exception("Study logging job %s failed", getattr(fn, "__name__", fn))
            return None

    try:
        return worker.submit(job)
    except RuntimeError:  # worker already shut down
        return None


def record_event(name: str, mono_time: float | None = None, /, **fields: Any) -> None:
    """Log an event from any thread. ``mono_time`` defaults to now."""
    when = time.monotonic() if mono_time is None else mono_time

    def write() -> None:
        session = _session
        if session is not None:
            session.event(name, when, fields)

    submit(write)


def _maybe_check_clock(force: bool = False) -> None:
    """Log a ``clock_sync`` event whenever the clock's sync state changes."""
    global _last_clock_state, _last_clock_check
    now = time.monotonic()
    if not force and now - _last_clock_check < CLOCK_CHECK_INTERVAL_S:
        return
    _last_clock_check = now
    state = clock_synced()
    if state is not None and state != _last_clock_state and _session is not None:
        _session.event("clock_sync", now, {"synced": state})
    if state is not None:
        _last_clock_state = state


def start() -> None:
    """Open the session files, start the writer thread, voice ID, and the OneDrive uploader."""
    global _session, _worker, _uploader_stop, _uploader_thread, _last_clock_state, _last_clock_check
    if not logging_enabled():
        logger.info("Study logging disabled (%s=0)", LOGGING_ENABLED_ENV)
        return
    data_dir = configured_data_dir()
    try:
        from talk_with_reachy import voice_id

        robot_id = os.getenv(ROBOT_ID_ENV) or socket.gethostname()
        synced = clock_synced()
        with _state_lock:
            _session = TranscriptSession(
                data_dir,
                robot_id,
                {"clock_synced": synced, "voice_id": voice_id.status_for_header()},
            )
            _last_clock_state = synced
            _last_clock_check = time.monotonic()
            _worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="study-log")
        logger.info("Logging transcripts to %s", _session.path)
    except Exception:
        logger.exception("Could not start study logging; the conversation will continue without it")
        _session = None
        return

    try:
        voice_id.start(data_dir)
    except Exception:
        logger.exception("Could not start voice identification; utterances will be logged without speaker IDs")

    try:
        from talk_with_reachy.cloud_upload import uploader_from_env

        uploader = uploader_from_env(data_dir)
        if uploader is None:
            return
        _uploader_stop = threading.Event()
        _uploader_thread = threading.Thread(
            target=uploader.run_until,
            args=(_uploader_stop,),
            daemon=True,
            name="cloud-uploader",
        )
        _uploader_thread.start()
    except Exception:
        logger.exception("Could not start the cloud uploader; study files stay on the robot")


def stop(upload_timeout_s: float = 8.0) -> None:
    """Finish pending writes, close the session, and give the uploader one last chance.

    The daemon kills an app that has not exited 20 s after it was asked to stop, so the
    waits here stay short. Anything not uploaded now is uploaded on the next start.
    """
    global _session, _worker, _uploader_stop, _uploader_thread
    from talk_with_reachy import voice_id

    stopped_at = time.monotonic()
    try:
        voice_id.stop()  # no new commands or model loads; queued utterances still get identified
    except Exception:
        logger.exception("Failed to stop voice identification")

    worker = _worker
    if worker is not None:
        session = _session

        def close() -> None:
            if session is not None:
                session.close(stopped_at)

        submit(close)
        worker.shutdown(wait=True)
    with _state_lock:
        _worker = None
        _session = None
    voice_id.clear()
    if _uploader_stop is not None and _uploader_thread is not None:
        _uploader_stop.set()
        _uploader_thread.join(timeout=upload_timeout_s)
    _uploader_stop = None
    _uploader_thread = None
