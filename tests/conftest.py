"""Pytest configuration for path setup."""

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1].resolve()
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


# Make tests reproducible by ignoring machine-specific profile/tool env config.
# Without this, importing config during test collection can pick up a developer's
# local .env and fail before tests run.
os.environ["REACHY_MINI_SKIP_DOTENV"] = "1"
# Keep test runs from writing transcript files into the real home directory.
os.environ["TALK_WITH_REACHY_LOGGING"] = "0"
os.environ.pop("REACHY_MINI_CUSTOM_PROFILE", None)
os.environ.pop("REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY", None)
os.environ.pop("REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY", None)


import pytest  # noqa: E402


@pytest.fixture
def study_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Turn study logging on into a temp folder, with voice ID and OneDrive upload off."""
    from talk_with_reachy import voice_id, study_log, onedrive_upload, google_drive_upload

    monkeypatch.setenv(study_log.LOGGING_ENABLED_ENV, "1")
    monkeypatch.setenv(study_log.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(study_log.ROBOT_ID_ENV, "robotA")
    monkeypatch.setenv(voice_id.VOICE_ID_ENV, "0")
    monkeypatch.delenv(onedrive_upload.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(onedrive_upload, "CLIENT_ID", "")
    monkeypatch.delenv(google_drive_upload.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(google_drive_upload, "CLIENT_ID", "")
    monkeypatch.setattr(study_log, "clock_synced", lambda: True)
    yield tmp_path
    study_log.stop()
