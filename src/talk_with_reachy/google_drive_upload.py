"""Upload study files to Google Drive through the Drive API.

The robot signs in once with Google's device flow: it prints a short code,
and someone enters that code at google.com/device on any phone or laptop and
signs in. The resulting refresh token is kept in
``~/.config/talk_with_reachy/google_token.json``, readable only by the robot user.

The token carries only the ``drive.file`` scope: the app can see and change the
files it created itself, and nothing else in that Drive. Files are mirrored
into a "Talk with Reachy" folder in My Drive, with the same subfolders as the
data folder. A file that is still growing is updated in place, so Drive keeps
one file per study file.

Run ``talk-with-reachy-google-login`` on the robot (over SSH) to sign in.
"""

from __future__ import annotations
import os
import sys
import json
import time
import socket
import logging
import argparse
import posixpath
from typing import Any
from pathlib import Path
from datetime import datetime
from collections.abc import Callable

import httpx

from talk_with_reachy.cloud_upload import CONFIG_DIR, StudyUploader, write_private, interval_from_env


logger = logging.getLogger(__name__)

# From the lab's Google Cloud OAuth client (type "TVs and Limited Input devices").
# The client secret is kept out of the public git repository: it lives in
# deploy/google_client_secret.txt (git-ignored), and deploy/publish_space.py writes
# it into the copy uploaded to the private Space.
# Environment variables with the names below override both. Empty CLIENT_ID: Google Drive upload is off.
CLIENT_ID = "984505342108-dnqjujdrcu94b8s9ctgt2aiqdefh4u0l.apps.googleusercontent.com"  # project talk-with-reachy
CLIENT_SECRET = ""
CLIENT_ID_ENV = "TALK_WITH_REACHY_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "TALK_WITH_REACHY_GOOGLE_CLIENT_SECRET"
FOLDER_ENV = "TALK_WITH_REACHY_GOOGLE_FOLDER"
DEFAULT_FOLDER = "Talk with Reachy"

SCOPE = "https://www.googleapis.com/auth/drive.file"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

TOKEN_PATH = CONFIG_DIR / "google_token.json"
STATE_FILENAME = "upload_state_google.json"
IDS_FILENAME = "upload_ids_google.json"

BLOCKED_ERRORS = ("access_denied", "admin_policy_enforced", "org_internal", "disallowed_useragent")


def client_settings() -> tuple[str, str]:
    """Client ID and secret from the environment, or the constants above."""
    return (
        (os.getenv(CLIENT_ID_ENV) or CLIENT_ID).strip(),
        (os.getenv(CLIENT_SECRET_ENV) or CLIENT_SECRET).strip(),
    )


def folder_name() -> str:
    """Name of the top folder in My Drive."""
    return (os.getenv(FOLDER_ENV) or DEFAULT_FOLDER).strip()


def _quote(value: str) -> str:
    """Escape a value for a Drive search query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleTokenProvider:
    """Google sign-in backed by a token file that only the robot user can read."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path = TOKEN_PATH,
        client: httpx.Client | None = None,
    ) -> None:
        """Remember where the token lives. Nothing contacts Google until a token is needed."""
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_path = token_path
        self._client = client or httpx.Client(timeout=30.0)
        self._data: dict[str, Any] = {}
        self._loaded_mtime_ns = 0

    def _reload_if_changed(self) -> None:
        """Pick up a sign-in done by the login command while the app was already running."""
        try:
            mtime_ns = self._token_path.stat().st_mtime_ns
        except FileNotFoundError:
            self._data = {}
            return
        if mtime_ns != self._loaded_mtime_ns:
            try:
                self._data = json.loads(self._token_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}
            self._loaded_mtime_ns = mtime_ns

    def _save(self) -> None:
        write_private(self._token_path, json.dumps(self._data, indent=1))
        self._loaded_mtime_ns = self._token_path.stat().st_mtime_ns

    def _store(self, payload: dict[str, Any]) -> str:
        access_token = str(payload["access_token"])
        self._data["access_token"] = access_token
        self._data["expires_at"] = time.time() + float(payload.get("expires_in", 3600))
        if payload.get("refresh_token"):
            self._data["refresh_token"] = payload["refresh_token"]
        self._save()
        return access_token

    def silent(self) -> str | None:
        """Return a valid access token, refreshing it if needed, or None if not signed in."""
        self._reload_if_changed()
        refresh_token = self._data.get("refresh_token")
        if not refresh_token:
            return None
        if self._data.get("access_token") and float(self._data.get("expires_at", 0)) - 60 > time.time():
            return str(self._data["access_token"])
        response = self._client.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code in (400, 401) and "invalid_grant" in response.text:
            # Revoked, expired, or the password changed: a new sign-in is needed.
            logger.warning("Google sign-in is no longer valid: %s", _describe_error(response))
            return None
        response.raise_for_status()
        return self._store(response.json())

    def sign_in(
        self,
        show: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ) -> str:
        """Run the device flow: show a code, wait until someone enters it, and return an access token."""
        response = self._client.post(DEVICE_CODE_URL, data={"client_id": self._client_id, "scope": SCOPE})
        if response.status_code >= 400:
            raise RuntimeError(_describe_error(response))
        flow = response.json()
        url = flow.get("verification_url") or flow.get("verification_uri")
        show(
            f"On any phone or computer, open {url} and enter the code {flow['user_code']}.\n"
            "Sign in with the Google account whose Drive should receive the study files."
        )
        interval = float(flow.get("interval", 5))
        deadline = time.monotonic() + float(flow.get("expires_in", 1800))
        while time.monotonic() < deadline:
            sleep(interval)
            poll = self._client.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "device_code": flow["device_code"],
                    "grant_type": DEVICE_GRANT,
                },
            )
            if poll.status_code == 200:
                self._data = {}
                return self._store(poll.json())
            try:
                error = poll.json().get("error")
            except json.JSONDecodeError:
                error = None
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise RuntimeError(_describe_error(poll))
        raise RuntimeError("The code expired before anyone signed in. Run the command again.")


def _describe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return f"HTTP {response.status_code}"
    error = str(payload.get("error", f"HTTP {response.status_code}"))
    text = f"{error}: {payload.get('error_description', '')}".strip(": ")
    if error in BLOCKED_ERRORS:
        text += (
            "\nGoogle did not allow this sign-in. If you signed in with a university account, the "
            "university's Google administrators may not allow this app; this cannot be fixed from the robot."
        )
    return text


class GoogleDriveUploader(StudyUploader):
    """Mirrors the study data folder into a "Talk with Reachy" folder in My Drive."""

    TARGET = "Google Drive"
    LOGIN_COMMAND = "talk-with-reachy-google-login"
    STATE_FILENAME = STATE_FILENAME

    def __init__(
        self,
        data_dir: Path,
        get_token: Callable[[], str | None],
        interval_s: float = 300.0,
        client: httpx.Client | None = None,
        top_folder: str = DEFAULT_FOLDER,
    ) -> None:
        """Load the Drive IDs of the folders and files uploaded before."""
        super().__init__(data_dir, get_token, interval_s, client)
        self._top_folder = top_folder
        self._ids_path = data_dir / IDS_FILENAME
        self._ids: dict[str, dict[str, str]] = {"folders": {}, "files": {}}
        try:
            loaded = json.loads(self._ids_path.read_text(encoding="utf-8"))
            self._ids["folders"].update(loaded.get("folders", {}))
            self._ids["files"].update(loaded.get("files", {}))
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            pass

    @classmethod
    def from_env(cls, data_dir: Path) -> GoogleDriveUploader | None:
        """Build an uploader from settings, or return None when no client ID is configured."""
        client_id, client_secret = client_settings()
        if not client_id:
            return None
        provider = GoogleTokenProvider(client_id, client_secret)
        return cls(data_dir, provider.silent, interval_from_env(), top_folder=folder_name())

    def _save_ids(self) -> None:
        tmp = self._ids_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._ids, indent=1), encoding="utf-8")
        tmp.replace(self._ids_path)

    def _auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _find(self, name: str, parent: str | None, folder: bool, token: str) -> str | None:
        """ID of a file or folder this app created with that name and parent, if any."""
        query = [f"name = '{_quote(name)}'", "trashed = false"]
        query.append(f"mimeType {'=' if folder else '!='} '{FOLDER_MIME}'")
        query.append(f"'{parent}' in parents" if parent else "'root' in parents")
        response = self._client.get(
            FILES_URL,
            params={"q": " and ".join(query), "fields": "files(id)", "pageSize": "1", "spaces": "drive"},
            headers=self._auth(token),
        )
        response.raise_for_status()
        files = response.json().get("files", [])
        return str(files[0]["id"]) if files else None

    def folder_id(self, folder: str, token: str) -> str:
        """Drive ID for a data-folder path ("" is the top folder), creating folders as needed."""
        cached = self._ids["folders"].get(folder)
        if cached:
            return cached
        if folder == "":
            parent, name = None, self._top_folder
        else:
            parent, name = self.folder_id(posixpath.dirname(folder), token), posixpath.basename(folder)
        found = self._find(name, parent, folder=True, token=token)
        if found is None:
            metadata: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
            if parent:
                metadata["parents"] = [parent]
            response = self._client.post(FILES_URL, params={"fields": "id"}, json=metadata, headers=self._auth(token))
            response.raise_for_status()
            found = str(response.json()["id"])
        self._ids["folders"][folder] = found
        self._save_ids()
        return found

    def _resumable(
        self, method: str, url: str, metadata: dict[str, Any], body: bytes, content_type: str, token: str
    ) -> str:
        """Upload ``body`` in a resumable session and return the file ID."""
        start = self._client.request(
            method,
            url,
            params={"uploadType": "resumable", "fields": "id"},
            json=metadata,
            headers={
                **self._auth(token),
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(len(body)),
            },
        )
        start.raise_for_status()
        finish = self._client.put(
            start.headers["Location"], content=body, headers={**self._auth(token), "Content-Type": content_type}
        )
        finish.raise_for_status()
        return str(finish.json()["id"])

    def _send(self, relative: str, body: bytes, content_type: str, token: str) -> None:
        """Update the file uploaded earlier, or create it in the matching folder."""
        file_id = self._ids["files"].get(relative)
        if file_id:
            try:
                self._resumable("PATCH", f"{UPLOAD_URL}/{file_id}", {}, body, content_type, token)
                return
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
                # Deleted in Drive: forget it and upload it again.
                del self._ids["files"][relative]

        folder, name = posixpath.dirname(relative), posixpath.basename(relative)
        for attempt in (1, 2):
            parent = self.folder_id(folder, token)
            try:
                existing = self._find(name, parent, folder=False, token=token)
                if existing:
                    new_id = self._resumable("PATCH", f"{UPLOAD_URL}/{existing}", {}, body, content_type, token)
                else:
                    metadata = {"name": name, "parents": [parent]}
                    new_id = self._resumable("POST", UPLOAD_URL, metadata, body, content_type, token)
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404 or attempt == 2:
                    raise
                # A folder was deleted in Drive: look the folders up again.
                self._ids["folders"].clear()
        self._ids["files"][relative] = new_id
        self._save_ids()


def login_main(argv: list[str] | None = None) -> int:
    """Sign the robot in to Google Drive and write a test file to confirm access."""
    parser = argparse.ArgumentParser(
        prog="talk-with-reachy-google-login",
        description="Sign in to Google Drive so Talk with Reachy can upload study files.",
    )
    parser.parse_args(argv)

    client_id, client_secret = client_settings()
    if not client_id:
        print(f"No Google client ID configured. Set CLIENT_ID in google_drive_upload.py or {CLIENT_ID_ENV}.")
        return 2
    if not client_secret:
        print(
            "No Google client secret configured. Install the app from the private Space published with "
            f"deploy/google_client_secret.txt, or set {CLIENT_SECRET_ENV}."
        )
        return 2

    try:
        token = GoogleTokenProvider(client_id, client_secret).sign_in()
    except Exception as e:
        print(f"Sign-in failed: {e}")
        return 1

    from talk_with_reachy.study_log import configured_data_dir

    data_dir = configured_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    uploader = GoogleDriveUploader(data_dir, lambda: token, top_folder=folder_name())
    body = f"Talk with Reachy connection check from {socket.gethostname()} at {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}\n"
    try:
        uploader._send("connection_check.txt", body.encode("utf-8"), "text/plain; charset=utf-8", token)
    except httpx.HTTPError as e:
        print(f"Signed in, but the test upload failed: {e}")
        return 1

    print(f"Signed in. Test file written to Google Drive: My Drive > {folder_name()} > connection_check.txt")
    print("Token file:", TOKEN_PATH)
    print("Study files will upload from:", data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(login_main())
