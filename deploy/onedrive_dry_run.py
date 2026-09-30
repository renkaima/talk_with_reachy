"""Rehearse the OneDrive half of Talk with Reachy on a laptop, without a robot.

1. Signs in to Microsoft in the browser (the same sign-in the robot needs) and
   writes connection_check.txt to OneDrive/Apps/Talk with Reachy/.
2. Logs a two-line fake conversation with the app's own transcript logger and
   stops it, which triggers the app's own final upload.
3. Confirms the finished file reached OneDrive.

The sign-in is saved in ~/.config/talk_with_reachy/onedrive_token_cache.json, which
copy_login_to_robot.sh can later copy to the robot so it never needs its own sign-in.
Run through onedrive_dry_run.sh.
"""

import os
import sys
import json
import logging
import tempfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from talk_with_reachy import study_log, onedrive_upload  # noqa: E402


DRY_RUN_ROBOT_ID = "DRY-RUN-no-robot"


def main(sign_in: bool = True) -> int:
    """Sign in, log a fake conversation, and check that the app uploaded it."""
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    if sign_in:
        print("Step 1/2: sign in to Microsoft with your UC account (a browser window will open) ...")
        if onedrive_upload.login_main(["--browser"]) != 0:
            return 1

    print("Step 2/2: logging a fake two-line conversation and uploading it with the app's own code ...")
    data_dir = Path(tempfile.mkdtemp(prefix="talk_with_reachy_dry_run_"))
    os.environ[study_log.LOGGING_ENABLED_ENV] = "1"
    os.environ[study_log.DATA_DIR_ENV] = str(data_dir)
    os.environ[study_log.ROBOT_ID_ENV] = DRY_RUN_ROBOT_ID
    study_log.start()
    study_log.record_utterance("user", "Hi Reachy, this is a dry run without a robot.", True)
    study_log.record_utterance("assistant", "Hello! If you can read this in OneDrive, uploads work.", True)
    study_log.stop(upload_timeout_s=60)

    (transcript,) = study_log.transcripts_dir(data_dir).glob("*.jsonl")
    state_path = data_dir / onedrive_upload.STATE_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    stat = transcript.stat()
    if state.get(transcript.name) != [stat.st_size, stat.st_mtime_ns]:
        print("FAILED: the transcript was written but not uploaded. See the messages above.")
        return 1

    print()
    print("Everything works. In OneDrive, open:")
    print(f"  Apps > Talk with Reachy > transcripts > {transcript.name}")
    print("It is a test file; delete it whenever you like.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
