"""Tests for the Google Drive uploader and sign-in (Google is faked; no network)."""

import os
import re
import json
import stat
import time
from typing import Any
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from talk_with_reachy import cloud_upload, onedrive_upload, google_drive_upload
from talk_with_reachy.google_drive_upload import GoogleDriveUploader, GoogleTokenProvider


class FakeDrive:
    """Just enough of the Drive v3 API: search, folder create, resumable create and update."""

    def __init__(self) -> None:
        """Start with an empty Drive."""
        self.items: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, tuple[str, str | None, dict[str, Any]]] = {}
        self.calls: list[str] = []
        self._next = 0

    def _new_id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}{self._next}"

    def add(self, name: str, parent: str, folder: bool) -> str:
        """Put an item in Drive, as if another device had created it."""
        item_id = self._new_id("F" if folder else "f")
        self.items[item_id] = {"name": name, "parent": parent, "folder": folder, "content": b""}
        return item_id

    def files(self, name: str) -> list[dict[str, Any]]:
        """All non-folder items with this name."""
        return [i for i in self.items.values() if i["name"] == name and not i["folder"]]

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Route one request."""
        url = str(request.url)
        params = parse_qs(request.url.query.decode())
        self.calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/drive/v3/files":
            return self._search(params["q"][0])
        if request.method == "POST" and request.url.path == "/drive/v3/files":
            meta = json.loads(request.content)
            parent = meta.get("parents", ["root"])[0]
            if parent != "root" and parent not in self.items:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(200, json={"id": self.add(meta["name"], parent, folder=True)})
        if request.url.path.startswith("/upload/drive/v3/files"):
            target = request.url.path.rsplit("/", 1)[-1]
            if request.method == "PATCH" and target not in self.items:
                return httpx.Response(404, json={"error": {"code": 404}})
            meta = json.loads(request.content or b"{}")
            if request.method == "POST" and meta["parents"][0] not in self.items:
                return httpx.Response(404, json={"error": {"code": 404}})
            session = self._new_id("s")
            self.sessions[session] = (request.method, target if request.method == "PATCH" else None, meta)
            return httpx.Response(200, headers={"Location": f"https://upload.fake/session/{session}"})
        if url.startswith("https://upload.fake/session/"):
            method, target, meta = self.sessions.pop(url.rsplit("/", 1)[-1])
            if method == "PATCH":
                assert target is not None
                self.items[target]["content"] = request.content
                return httpx.Response(200, json={"id": target})
            item_id = self.add(meta["name"], meta["parents"][0], folder=False)
            self.items[item_id]["content"] = request.content
            return httpx.Response(200, json={"id": item_id})
        raise AssertionError(f"unexpected request {request.method} {url}")

    def _search(self, query: str) -> httpx.Response:
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", query)
        parent = re.search(r"'([^']+)' in parents", query)
        folder = "mimeType = " in query
        assert name and parent
        wanted = name.group(1).replace("\\'", "'")
        found = [
            {"id": item_id}
            for item_id, i in self.items.items()
            if i["name"] == wanted and i["parent"] == parent.group(1) and i["folder"] == folder
        ]
        return httpx.Response(200, json={"files": found[:1]})

    def client(self) -> httpx.Client:
        """Return an httpx client wired to this fake."""
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _write(data_dir: Path, relative: str, text: str = "x") -> Path:
    path = data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _uploader(data_dir: Path, drive: FakeDrive) -> GoogleDriveUploader:
    return GoogleDriveUploader(data_dir, lambda: "tok", client=drive.client())


def test_files_are_mirrored_into_matching_folders(tmp_path: Path) -> None:
    """Each folder is created once under "Talk with Reachy"; each file lands in its own folder."""
    drive = FakeDrive()
    _write(tmp_path, "transcripts/r_1.jsonl", "{}\n")
    _write(tmp_path, "transcripts/r_1.csv", "seq\n")
    _write(tmp_path, "audio/r_1/0001_P01.wav", "RIFF")

    assert _uploader(tmp_path, drive).upload_pending() == 3

    folders = {i["name"]: (item_id, i["parent"]) for item_id, i in drive.items.items() if i["folder"]}
    top_id, top_parent = folders["Talk with Reachy"]
    assert top_parent == "root"
    assert folders["transcripts"][1] == top_id and folders["audio"][1] == top_id
    assert folders["r_1"][1] == folders["audio"][0]
    (clip,) = drive.files("0001_P01.wav")
    assert clip["parent"] == folders["r_1"][0] and clip["content"] == b"RIFF"
    assert sum(1 for c in drive.calls if c == "POST /drive/v3/files") == 4


def test_a_growing_file_is_updated_in_place(tmp_path: Path) -> None:
    """The second upload of a changed file replaces the same Drive file instead of adding a copy."""
    drive = FakeDrive()
    path = _write(tmp_path, "transcripts/r_1.jsonl", "{}\n")
    uploader = _uploader(tmp_path, drive)
    uploader.upload_pending()

    path.write_text("{}\n{}\n", encoding="utf-8")
    assert uploader.upload_pending() == 1

    (only,) = drive.files("r_1.jsonl")
    assert only["content"] == b"{}\n{}\n"
    assert drive.calls[-2].startswith("PATCH /upload/drive/v3/files/")


def test_a_second_device_reuses_the_existing_folder_and_files(tmp_path: Path) -> None:
    """Without local IDs (another device, or a new data folder), existing items are found, not duplicated."""
    drive = FakeDrive()
    top = drive.add("Talk with Reachy", "root", folder=True)
    transcripts = drive.add("transcripts", top, folder=True)
    drive.add("r_1.jsonl", transcripts, folder=False)
    _write(tmp_path, "transcripts/r_1.jsonl", "new")

    _uploader(tmp_path, drive).upload_pending()

    assert [i["name"] for i in drive.items.values() if i["folder"]] == ["Talk with Reachy", "transcripts"]
    (only,) = drive.files("r_1.jsonl")
    assert only["content"] == b"new"


def test_files_and_folders_deleted_in_drive_are_recreated(tmp_path: Path) -> None:
    """If someone deletes the Drive copy, the next change is uploaded again rather than failing forever."""
    drive = FakeDrive()
    path = _write(tmp_path, "transcripts/r_1.jsonl", "a")
    uploader = _uploader(tmp_path, drive)
    uploader.upload_pending()

    drive.items.clear()  # the whole "Talk with Reachy" folder was deleted
    path.write_text("ab", encoding="utf-8")
    assert uploader.upload_pending() == 1
    (only,) = drive.files("r_1.jsonl")
    assert only["content"] == b"ab"


def test_names_with_quotes_are_escaped_in_searches(tmp_path: Path) -> None:
    """A file name with an apostrophe does not break the Drive query."""
    drive = FakeDrive()
    path = _write(tmp_path, "people/O'Neil/memory.v1.json", "{}")
    _uploader(tmp_path, drive).upload_pending()

    (tmp_path / google_drive_upload.IDS_FILENAME).unlink()  # forget the IDs, so items must be found by name
    path.write_text('{"facts": []}', encoding="utf-8")
    _uploader(tmp_path, drive).upload_pending()

    assert [i["name"] for i in drive.items.values()].count("O'Neil") == 1
    assert len(drive.files("memory.v1.json")) == 1


def test_google_state_is_separate_from_onedrive_state(tmp_path: Path) -> None:
    """Files already sent to OneDrive are still uploaded to Google Drive."""
    path = _write(tmp_path, "transcripts/r_1.jsonl", "x")
    sent_to_onedrive = {"transcripts/r_1.jsonl": [path.stat().st_size, path.stat().st_mtime_ns]}
    (tmp_path / onedrive_upload.STATE_FILENAME).write_text(json.dumps(sent_to_onedrive))
    assert _uploader(tmp_path, FakeDrive()).upload_pending() == 1


# ── Sign-in ──────────────────────────────────────────────────────


class FakeGoogleAuth:
    """Device-code and token endpoints with scripted answers."""

    def __init__(self, polls: list[tuple[int, dict[str, Any]]], refresh: tuple[int, dict[str, Any]] | None = None):
        """Queue the answers to the token endpoint."""
        self.polls = list(polls)
        self.refresh = refresh
        self.requests: list[dict[str, list[str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer device-code, polling, and refresh requests."""
        form = parse_qs(request.content.decode())
        self.requests.append(form)
        if request.url.path == "/device/code":
            assert form["scope"] == [google_drive_upload.SCOPE]
            return httpx.Response(
                200,
                json={
                    "device_code": "dev",
                    "user_code": "ABCD-EFGH",
                    "verification_url": "https://www.google.com/device",
                    "interval": 5,
                    "expires_in": 1800,
                },
            )
        if form["grant_type"] == ["refresh_token"]:
            assert self.refresh is not None
            return httpx.Response(self.refresh[0], json=self.refresh[1])
        status, body = self.polls.pop(0)
        return httpx.Response(status, json=body)

    def client(self) -> httpx.Client:
        """Return an httpx client wired to this fake."""
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_device_sign_in_waits_for_the_user_and_saves_a_private_token(tmp_path: Path) -> None:
    """The code is shown, pending/slow_down are waited out, and the refresh token is saved 0600."""
    auth = FakeGoogleAuth(
        [
            (428, {"error": "authorization_pending"}),
            (403, {"error": "slow_down"}),
            (200, {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600}),
        ]
    )
    token_path = tmp_path / "config" / "google_token.json"
    shown: list[str] = []
    waits: list[float] = []
    provider = GoogleTokenProvider("cid", "secret", token_path, client=auth.client())

    assert provider.sign_in(show=shown.append, sleep=waits.append) == "acc"

    assert "https://www.google.com/device" in shown[0] and "ABCD-EFGH" in shown[0]
    assert waits == [5, 5, 10]
    saved = json.loads(token_path.read_text())
    assert saved["refresh_token"] == "ref"
    assert stat.S_IMODE(os.stat(token_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(token_path.parent).st_mode) == 0o700


def test_a_blocked_sign_in_says_why(tmp_path: Path) -> None:
    """If Google or the university refuses the sign-in, the message says so instead of retrying."""
    auth = FakeGoogleAuth([(403, {"error": "access_denied", "error_description": "denied"})])
    provider = GoogleTokenProvider("cid", "secret", tmp_path / "t.json", client=auth.client())

    with pytest.raises(RuntimeError, match="administrators may not allow this app"):
        provider.sign_in(show=lambda _m: None, sleep=lambda _s: None)


def test_silent_uses_the_cached_token_then_refreshes(tmp_path: Path) -> None:
    """A fresh access token is reused without network; an expired one is refreshed and saved."""
    token_path = tmp_path / "google_token.json"
    token_path.write_text(json.dumps({"access_token": "old", "refresh_token": "ref", "expires_at": time.time() + 600}))
    auth = FakeGoogleAuth([], refresh=(200, {"access_token": "new", "expires_in": 3600}))
    provider = GoogleTokenProvider("cid", "secret", token_path, client=auth.client())

    assert provider.silent() == "old"
    assert auth.requests == []

    data = json.loads(token_path.read_text())
    data["expires_at"] = time.time() - 1
    token_path.write_text(json.dumps(data))
    assert provider.silent() == "new"
    assert auth.requests[0]["refresh_token"] == ["ref"]
    assert json.loads(token_path.read_text())["refresh_token"] == "ref"


def test_silent_reports_signed_out(tmp_path: Path) -> None:
    """No token file, or a revoked refresh token, means not signed in (no exception)."""
    auth = FakeGoogleAuth([], refresh=(400, {"error": "invalid_grant", "error_description": "Token has been expired"}))
    token_path = tmp_path / "google_token.json"
    provider = GoogleTokenProvider("cid", "secret", token_path, client=auth.client())
    assert provider.silent() is None

    token_path.write_text(json.dumps({"refresh_token": "ref", "expires_at": 0}))
    assert provider.silent() is None


def test_upload_target_is_google_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Google Drive wins when its client ID is set; with nothing configured, nothing uploads."""
    monkeypatch.setattr(onedrive_upload, "CLIENT_ID", "")
    monkeypatch.delenv(onedrive_upload.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(google_drive_upload.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(google_drive_upload, "CLIENT_ID", "")
    assert cloud_upload.uploader_from_env(tmp_path) is None

    monkeypatch.setenv(google_drive_upload.CLIENT_ID_ENV, "abc.apps.googleusercontent.com")
    monkeypatch.setenv(onedrive_upload.CLIENT_ID_ENV, "00000000-0000-0000-0000-000000000000")
    assert isinstance(cloud_upload.uploader_from_env(tmp_path), GoogleDriveUploader)


def test_login_without_client_id_exits_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The login command explains what is missing."""
    monkeypatch.delenv(google_drive_upload.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(google_drive_upload, "CLIENT_ID", "")
    assert google_drive_upload.login_main([]) == 2
