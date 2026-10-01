"""Upload study files to OneDrive through Microsoft Graph.

Not used at UC: UC's Office of Information Security declined the "Talk with
Reachy" app registration on 2026-10-01, so ``CLIENT_ID`` is empty and the app
uploads to Google Drive instead (see ``google_drive_upload``). The code stays
for an institution that approves the registration.

The robot signs in once and keeps the resulting token cache in
``~/.config/talk_with_reachy/``. The token only carries the delegated
permission ``Files.ReadWrite.AppFolder``, so whoever holds it can reach
``OneDrive/Apps/<app registration name>/`` and nothing else in that account.
Uploads are whole-file PUTs; Graph replaces an existing file on PUT.

Run ``talk-with-reachy-onedrive-login`` on the robot (over SSH) to sign in.
"""

from __future__ import annotations
import os
import sys
import socket
import logging
import argparse
from typing import Any
from pathlib import Path
from datetime import datetime
from urllib.parse import quote

import httpx

from talk_with_reachy.study_log import DATA_DIR_ENV, DEFAULT_DATA_DIR, transcripts_dir
from talk_with_reachy.cloud_upload import (
    CONFIG_DIR,
    INTERVAL_ENV,  # noqa: F401  (re-exported for older imports)
    UPLOADED_FOLDERS,  # noqa: F401
    DEFAULT_INTERVAL_S,  # noqa: F401
    StudyUploader,
    interval_from_env,
)


logger = logging.getLogger(__name__)

# From a Microsoft Entra app registration. Neither value is a secret. Environment
# variables with the names below override them. Empty: OneDrive upload is off.
CLIENT_ID = ""  # UC declined the "Talk with Reachy" registration (07a0d38d-…) on 2026-10-01
TENANT = "f5222e6c-5fc6-48eb-8f03-73db18203b63"  # University of Cincinnati
CLIENT_ID_ENV = "TALK_WITH_REACHY_ONEDRIVE_CLIENT_ID"
TENANT_ENV = "TALK_WITH_REACHY_ONEDRIVE_TENANT"

SCOPES = ["Files.ReadWrite.AppFolder"]
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
STATE_FILENAME = "upload_state.json"
TOKEN_CACHE_PATH = CONFIG_DIR / "onedrive_token_cache.json"


def upload_url(remote_path: str) -> str:
    """Return the Graph URL that writes ``remote_path`` inside the app folder."""
    return f"{GRAPH_ROOT}/me/drive/special/approot:/{quote(remote_path)}:/content"


def _client_settings() -> tuple[str, str]:
    return (os.getenv(CLIENT_ID_ENV) or CLIENT_ID).strip(), (os.getenv(TENANT_ENV) or TENANT).strip()


class TokenProvider:
    """Microsoft sign-in backed by a token cache file that only the robot user can read."""

    def __init__(self, client_id: str, tenant: str, cache_path: Path = TOKEN_CACHE_PATH) -> None:
        """Load the token cache. The MSAL client itself is built on first use (see ``_msal_app``)."""
        import msal

        self._client_id = client_id
        self._tenant = tenant
        self._cache_path = cache_path
        self._cache = msal.SerializableTokenCache()
        self._loaded_mtime_ns = 0
        self._app: msal.PublicClientApplication | None = None
        self._reload_if_changed()

    def _msal_app(self) -> Any:
        """Build the MSAL client on first use.

        Building it contacts Microsoft, so it must not happen at app start: the robot may
        still be offline then, and uploads should begin as soon as the network comes up.
        A failure here raises, and the next upload pass simply tries again.
        """
        if self._app is None:
            import msal

            self._app = msal.PublicClientApplication(
                self._client_id,
                authority=f"https://login.microsoftonline.com/{self._tenant}",
                token_cache=self._cache,
            )
        return self._app

    def _reload_if_changed(self) -> None:
        """Pick up a sign-in done by the login command while the app was already running."""
        try:
            mtime_ns = self._cache_path.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if mtime_ns != self._loaded_mtime_ns:
            self._cache.deserialize(self._cache_path.read_text(encoding="utf-8"))
            self._loaded_mtime_ns = mtime_ns

    def _save(self) -> None:
        if not self._cache.has_state_changed:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self._cache.serialize())
        self._cache.has_state_changed = False
        self._loaded_mtime_ns = self._cache_path.stat().st_mtime_ns

    def silent(self) -> str | None:
        """Return an access token from the cache, refreshing it if needed, or None if not signed in."""
        app = self._msal_app()
        accounts = app.get_accounts()
        if not accounts:
            self._reload_if_changed()
            accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        self._save()
        if result and "access_token" in result:
            return str(result["access_token"])
        return None

    def sign_in(self, use_browser: bool) -> str:
        """Run an interactive sign-in and return an access token."""
        app = self._msal_app()
        result: dict[str, Any]
        if use_browser:
            result = app.acquire_token_interactive(SCOPES)
        else:
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(flow.get("error_description") or "Could not start device sign-in")
            print(flow["message"], flush=True)
            result = app.acquire_token_by_device_flow(flow)
        self._save()
        if "access_token" not in result:
            raise RuntimeError(result.get("error_description") or "Sign-in failed")
        return str(result["access_token"])


class OneDriveUploader(StudyUploader):
    """Mirrors the study data folder into the OneDrive app folder."""

    TARGET = "OneDrive"
    LOGIN_COMMAND = "talk-with-reachy-onedrive-login"
    STATE_FILENAME = STATE_FILENAME

    @classmethod
    def from_env(cls, data_dir: Path) -> OneDriveUploader | None:
        """Build an uploader from settings, or return None when no client ID is configured."""
        client_id, tenant = _client_settings()
        if not client_id:
            return None
        return cls(data_dir, TokenProvider(client_id, tenant).silent, interval_from_env())

    def _send(self, relative: str, body: bytes, content_type: str, token: str) -> None:
        """PUT the file to the same path in the app folder; Graph replaces an existing file."""
        response = self._client.put(
            upload_url(relative),
            content=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        )
        response.raise_for_status()


def login_main(argv: list[str] | None = None) -> int:
    """Sign the robot in to OneDrive and write a test file to confirm access."""
    parser = argparse.ArgumentParser(
        prog="talk-with-reachy-onedrive-login",
        description="Sign in to OneDrive so Talk with Reachy can upload transcripts.",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "Sign in through a browser on this computer instead of with a device code. "
            f"Use this on a laptop if device-code sign-in is blocked, then copy {TOKEN_CACHE_PATH.name} "
            "to /home/pollen/.config/talk_with_reachy/ on the robot."
        ),
    )
    args = parser.parse_args(argv)

    client_id, tenant = _client_settings()
    if not client_id:
        print(f"No client ID configured. Set CLIENT_ID in onedrive_upload.py or the {CLIENT_ID_ENV} variable.")
        return 2

    try:
        token = TokenProvider(client_id, tenant).sign_in(use_browser=args.browser)
    except Exception as e:
        print(f"Sign-in failed: {e}")
        return 1

    body = f"Talk with Reachy connection check from {socket.gethostname()} at {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}\n"
    try:
        response = httpx.put(
            upload_url("connection_check.txt"),
            content=body.encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain; charset=utf-8"},
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        print(f"Signed in, but the test upload failed: {e}")
        return 1

    data_dir = Path(os.getenv(DATA_DIR_ENV) or DEFAULT_DATA_DIR).expanduser()
    print("Signed in. Test file written to OneDrive:", response.json().get("webUrl", "(no URL returned)"))
    print("Token cache:", TOKEN_CACHE_PATH)
    print("Transcripts will upload from:", transcripts_dir(data_dir))
    return 0


if __name__ == "__main__":
    sys.exit(login_main())
