"""Upload transcript files to OneDrive through Microsoft Graph.

The robot signs in once as a UC account and keeps the resulting token cache
in ``~/.config/talk_with_reachy/``. The token only carries the delegated
permission ``Files.ReadWrite.AppFolder``, so whoever holds it can reach
``OneDrive/Apps/<app registration name>/`` and nothing else in that account.

Uploads are whole-file PUTs. Graph replaces an existing file on PUT, so a
transcript that is still growing is simply re-uploaded until the run ends.
Files that fail to upload stay on the robot and are retried on the next pass.

Run ``talk-with-reachy-onedrive-login`` on the robot (over SSH) to sign in.
"""

from __future__ import annotations
import os
import sys
import json
import socket
import logging
import argparse
import threading
from typing import Any
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
from collections.abc import Callable

import httpx

from talk_with_reachy.study_log import FILE_LOCK, DATA_DIR_ENV, DEFAULT_DATA_DIR, transcripts_dir


logger = logging.getLogger(__name__)

# Fill these in from the lab's Microsoft Entra app registration before publishing the app.
# Neither value is a secret. Environment variables with the names below override them.
CLIENT_ID = ""
TENANT = "organizations"
CLIENT_ID_ENV = "TALK_WITH_REACHY_ONEDRIVE_CLIENT_ID"
TENANT_ENV = "TALK_WITH_REACHY_ONEDRIVE_TENANT"
INTERVAL_ENV = "TALK_WITH_REACHY_UPLOAD_INTERVAL_S"
DEFAULT_INTERVAL_S = 300.0

SCOPES = ["Files.ReadWrite.AppFolder"]
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
REMOTE_TRANSCRIPTS_FOLDER = "transcripts"
STATE_FILENAME = "upload_state.json"
TOKEN_CACHE_PATH = Path.home() / ".config" / "talk_with_reachy" / "onedrive_token_cache.json"


def upload_url(remote_path: str) -> str:
    """Return the Graph URL that writes ``remote_path`` inside the app folder."""
    return f"{GRAPH_ROOT}/me/drive/special/approot:/{quote(remote_path)}:/content"


def _client_settings() -> tuple[str, str]:
    return (os.getenv(CLIENT_ID_ENV) or CLIENT_ID).strip(), (os.getenv(TENANT_ENV) or TENANT).strip()


class TokenProvider:
    """Microsoft sign-in backed by a token cache file that only the robot user can read."""

    def __init__(self, client_id: str, tenant: str, cache_path: Path = TOKEN_CACHE_PATH) -> None:
        """Load the cache and build the MSAL public client."""
        import msal

        self._cache_path = cache_path
        self._cache = msal.SerializableTokenCache()
        self._loaded_mtime_ns = 0
        self._reload_if_changed()
        self._app = msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
            token_cache=self._cache,
        )

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
        accounts = self._app.get_accounts()
        if not accounts:
            self._reload_if_changed()
            accounts = self._app.get_accounts()
        if not accounts:
            return None
        result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
        self._save()
        if result and "access_token" in result:
            return str(result["access_token"])
        return None

    def sign_in(self, use_browser: bool) -> str:
        """Run an interactive sign-in and return an access token."""
        result: dict[str, Any]
        if use_browser:
            result = self._app.acquire_token_interactive(SCOPES)
        else:
            flow = self._app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(flow.get("error_description") or "Could not start device sign-in")
            print(flow["message"], flush=True)
            result = self._app.acquire_token_by_device_flow(flow)
        self._save()
        if "access_token" not in result:
            raise RuntimeError(result.get("error_description") or "Sign-in failed")
        return str(result["access_token"])


class OneDriveUploader:
    """Copies new or changed transcript files to OneDrive on a fixed interval."""

    def __init__(
        self,
        data_dir: Path,
        get_token: Callable[[], str | None],
        interval_s: float = DEFAULT_INTERVAL_S,
        client: httpx.Client | None = None,
    ) -> None:
        """Remember where transcripts live and which files were already uploaded."""
        self._folder = transcripts_dir(data_dir)
        self._state_path = data_dir / STATE_FILENAME
        self._get_token = get_token
        self._interval_s = interval_s
        self._client = client or httpx.Client(timeout=30.0)
        self._state = self._load_state()
        self._warned_signed_out = False

    @classmethod
    def from_env(cls, data_dir: Path) -> OneDriveUploader | None:
        """Build an uploader from settings, or return None when no client ID is configured."""
        client_id, tenant = _client_settings()
        if not client_id:
            logger.info("OneDrive upload is not configured; transcripts stay in %s", transcripts_dir(data_dir))
            return None
        interval_s = float(os.getenv(INTERVAL_ENV) or DEFAULT_INTERVAL_S)
        return cls(data_dir, TokenProvider(client_id, tenant).silent, interval_s)

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

    def upload_pending(self) -> int:
        """Upload every transcript that changed since its last successful upload; return how many."""
        if not self._folder.exists():
            return 0
        pending = [p for p in sorted(self._folder.glob("*.jsonl")) if self._state.get(p.name) != self._fingerprint(p)]
        if not pending:
            return 0

        token = self._get_token()
        if token is None:
            if not self._warned_signed_out:
                logger.warning(
                    "OneDrive: not signed in. Run talk-with-reachy-onedrive-login on the robot. "
                    "Transcripts are kept in %s until then.",
                    self._folder,
                )
                self._warned_signed_out = True
            return 0
        self._warned_signed_out = False

        uploaded = 0
        for path in pending:
            with FILE_LOCK:
                fingerprint = self._fingerprint(path)
                body = path.read_bytes()
            try:
                response = self._client.put(
                    upload_url(f"{REMOTE_TRANSCRIPTS_FOLDER}/{path.name}"),
                    content=body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain; charset=utf-8"},
                )
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.warning("OneDrive upload failed for %s, will retry: %s", path.name, e)
                continue
            self._state[path.name] = fingerprint
            uploaded += 1

        if uploaded:
            self._save_state()
            logger.info("Uploaded %d transcript file(s) to OneDrive", uploaded)
        return uploaded

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
        except Exception:
            logger.exception("OneDrive upload pass failed")


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
