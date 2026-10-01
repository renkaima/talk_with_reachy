"""Rehearse the Google Drive half of Talk with Reachy on a Mac, without a robot.

1. Signs in to Google with a device code (the same sign-in the robot uses) and
   writes connection_check.txt to My Drive > Talk with Reachy.
2. Logs a two-line fake conversation with the app's own study logger and stops
   it, which triggers the app's own final upload.
3. Confirms the transcript and its CSV timeline reached Google Drive.

The sign-in is saved in ~/.config/talk_with_reachy/google_token.json on this Mac,
which is what the app uses when it runs in the Reachy Mini Control simulation.
Run through google_dry_run.sh.
"""

import os
import sys
import json
import time
import logging
import tempfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from talk_with_reachy import voice_id, study_log, google_drive_upload  # noqa: E402


DRY_RUN_ROBOT_ID = "DRY-RUN-no-robot"


def _log_line(speaker: str, text: str, seconds: float) -> None:
    """Write one utterance that ended just now and lasted ``seconds``."""

    def write() -> None:
        session = study_log.current_session()
        if session is not None:
            now_wall, now_mono = time.time(), time.monotonic()
            session.utterance(
                speaker,
                text,
                seq=session.next_seq(),
                start_wall=now_wall - seconds,
                end_wall=now_wall,
                start_mono=now_mono - seconds,
                fields={"speaker_id": "DRY-RUN" if speaker == "person" else "reachy"},
            )

    study_log.submit(write)


def main() -> int:
    """Sign in, log a fake conversation, and check that the app uploaded it."""
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too much here

    print("Step 1/2: sign in to Google ...")
    if google_drive_upload.login_main([]) != 0:
        return 1

    print("Step 2/2: logging a fake two-line conversation and uploading it with the app's own code ...")
    data_dir = Path(tempfile.mkdtemp(prefix="talk_with_reachy_dry_run_"))
    os.environ[study_log.LOGGING_ENABLED_ENV] = "1"
    os.environ[study_log.DATA_DIR_ENV] = str(data_dir)
    os.environ[study_log.ROBOT_ID_ENV] = DRY_RUN_ROBOT_ID
    os.environ[voice_id.VOICE_ID_ENV] = "0"  # no speaker model needed for this check
    study_log.start()
    _log_line("person", "Hi Reachy, this is a dry run without a robot.", 2.5)
    _log_line("reachy", "Hello! If you can read this in Google Drive, uploads work.", 3.0)
    study_log.stop(upload_timeout_s=60)

    (transcript,) = study_log.transcripts_dir(data_dir).glob("*.jsonl")
    state_path = data_dir / google_drive_upload.STATE_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    for path in (transcript, transcript.with_suffix(".csv")):
        stat = path.stat()
        if state.get(f"transcripts/{path.name}") != [stat.st_size, stat.st_mtime_ns]:
            print(f"FAILED: {path.name} was written but not uploaded. See the messages above.")
            return 1

    print()
    print("Everything works. In Google Drive, open:")
    print(f"  My Drive > {google_drive_upload.folder_name()} > transcripts > {transcript.name}")
    print(f"  (and {transcript.with_suffix('.csv').name}, the same timeline as a spreadsheet)")
    print("They are test files; delete them whenever you like.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
