"""Mirror the study data folder to a cloud drive, shared by the OneDrive and Google Drive uploaders.

Everything under the data folder's ``transcripts/``, ``people/`` and ``audio/``
folders is copied to the same relative path in the cloud. The speaker model
(``models/``), the upload bookkeeping files, and hidden temp files are not.

A pass uploads every file whose size or modification time changed since its
last successful upload, so a file that is still growing is uploaded again and
replaced until the run ends. Files that fail to upload stay on the robot and
are retried on the next pass.
"""

from __future__ import annotations
import os
import json
import logging
import mimetypes
import threading
from pathlib import Path
from collections.abc import Callable

import httpx

from talk_with_reachy import study_log
from talk_with_reachy.study_log import FILE_LOCK


logger = logging.getLogger(__name__)

INTERVAL_ENV = "TALK_WITH_REACHY_UPLOAD_INTERVAL_S"
DEFAULT_INTERVAL_S = 300.0
CONFIG_DIR = Path.home() / ".config" / "talk_with_reachy"
# Uploaded in this order, so transcripts reach the cloud before the bulkier audio.
UPLOADED_FOLDERS = ("transcripts", "people", "audio")


def interval_from_env() -> float:
    """Seconds between upload passes."""
    return float(os.getenv(INTERVAL_ENV) or DEFAULT_INTERVAL_S)


def content_type_for(path: Path) -> str:
    """MIME type to upload ``path`` with; text files are marked UTF-8."""
    if path.suffix == ".csv":
        return "text/csv; charset=utf-8"
    if path.suffix in (".jsonl", ".json", ".txt"):
        return "text/plain; charset=utf-8"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def write_private(path: Path, text: str) -> None:
    """Write a file only the current user can read, in a folder only they can open."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)


class StudyUploader:
    """Copies new or changed study files to a cloud drive on a fixed interval.

    Subclasses set ``TARGET`` and ``LOGIN_COMMAND`` and implement ``_send``.
    """

    TARGET = "cloud"
    LOGIN_COMMAND = ""
    STATE_FILENAME = "upload_state.json"

    def __init__(
        self,
        data_dir: Path,
        get_token: Callable[[], str | None],
        interval_s: float = DEFAULT_INTERVAL_S,
        client: httpx.Client | None = None,
    ) -> None:
        """Remember where study files live and which files were already uploaded."""
        self._data_dir = data_dir
        self._state_path = data_dir / self.STATE_FILENAME
        self._get_token = get_token
        self._interval_s = interval_s
        self._client = client or httpx.Client(timeout=60.0)
        self._state = self._load_state()
        self._warned_signed_out = False
        self._failing = False

    def _send(self, relative: str, body: bytes, content_type: str, token: str) -> None:
        """Upload one file; raise ``httpx.HTTPError`` on failure."""
        raise NotImplementedError

    def _load_state(self) -> dict[str, list[int]]:
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save_state(self) -> None:
        tmp_path = self._state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        tmp_path.replace(self._state_path)

    @staticmethod
    def _fingerprint(path: Path) -> list[int]:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def study_files(self) -> list[tuple[str, Path]]:
        """Every file to mirror, as (path relative to the data folder, local path)."""
        files: list[tuple[str, Path]] = []
        for folder in UPLOADED_FOLDERS:
            root = self._data_dir / folder
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(self._data_dir)
                hidden = any(part.startswith(".") for part in relative.parts)
                if path.is_file() and not hidden and path.suffix not in (".tmp", ".part"):
                    files.append((relative.as_posix(), path))
        return files

    def upload_pending(self) -> int:
        """Upload every study file that changed since its last successful upload; return how many."""
        pending = [(rel, p) for rel, p in self.study_files() if self._state.get(rel) != self._fingerprint(p)]
        if not pending:
            return 0

        token = self._get_token()
        if token is None:
            if not self._warned_signed_out:
                logger.warning(
                    "%s: not signed in. Run %s on the robot. Study files are kept in %s until then.",
                    self.TARGET,
                    self.LOGIN_COMMAND,
                    self._data_dir,
                )
                study_log.record_event("upload_signed_out", target=self.TARGET)
                self._warned_signed_out = True
            return 0
        self._warned_signed_out = False

        uploaded = 0
        failures: list[str] = []
        for relative, path in pending:
            with FILE_LOCK:
                fingerprint = self._fingerprint(path)
                body = path.read_bytes()
            try:
                self._send(relative, body, content_type_for(path), token)
            except httpx.HTTPError as e:
                logger.warning("%s upload failed for %s, will retry: %s", self.TARGET, relative, e)
                failures.append(f"{relative}: {e}")
                continue
            self._state[relative] = fingerprint
            uploaded += 1

        if uploaded:
            self._save_state()
            logger.info("Uploaded %d study file(s) to %s", uploaded, self.TARGET)
        self._note_failures(failures)
        return uploaded

    def _note_failures(self, failures: list[str]) -> None:
        """Log upload trouble to the study log only when it starts or ends, not on every pass."""
        if failures and not self._failing:
            study_log.record_event(
                "upload_failing", target=self.TARGET, files=len(failures), first_error=failures[0][:300]
            )
        elif not failures and self._failing:
            study_log.record_event("upload_recovered", target=self.TARGET)
        self._failing = bool(failures)

    def run_until(self, stop_event: threading.Event) -> None:
        """Upload on every interval until ``stop_event`` is set, then make one final pass."""
        while True:
            self._safe_upload()
            if stop_event.wait(self._interval_s):
                break
        self._safe_upload()

    def _safe_upload(self) -> None:
        try:
            self.upload_pending()
        except Exception as e:
            # Usually no network yet; keep the log short because this repeats every pass.
            logger.warning("%s upload pass failed, will retry: %s: %s", self.TARGET, type(e).__name__, e)
            self._note_failures([f"{type(e).__name__}: {e}"])


def uploader_from_env(data_dir: Path) -> StudyUploader | None:
    """Return the configured uploader: Google Drive if set up, else OneDrive, else None."""
    from talk_with_reachy.onedrive_upload import OneDriveUploader
    from talk_with_reachy.google_drive_upload import GoogleDriveUploader

    for build in (GoogleDriveUploader.from_env, OneDriveUploader.from_env):
        uploader = build(data_dir)
        if uploader is not None:
            logger.info("Study files will upload to %s", uploader.TARGET)
            return uploader
    logger.info("Cloud upload is not configured; study files stay in %s", data_dir)
    return None
