"""Tests for the OneDrive transcript uploader (Graph is mocked; no network)."""

import os
import stat
import threading
from pathlib import Path
from unittest.mock import MagicMock

import msal
import httpx
import pytest

from talk_with_reachy import study_log, onedrive_upload
from talk_with_reachy.onedrive_upload import TokenProvider, OneDriveUploader, upload_url


class FakeGraph:
    """Records PUT requests and replies with queued status codes (201 once the queue is empty)."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        """Queue the status codes to return, in order."""
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses or [])

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Record the request and return the next queued status."""
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 201
        return httpx.Response(status, json={"webUrl": "https://example.invalid/file"})

    def client(self) -> httpx.Client:
        """Return an httpx client wired to this fake."""
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _transcript(data_dir: Path, name: str = "robotA_20261001-090000_abc123.jsonl", text: str = "{}\n") -> Path:
    folder = study_log.transcripts_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_upload_url_targets_app_folder_and_escapes_names() -> None:
    """Upload url targets app folder and escapes names."""
    url = upload_url("transcripts/a b.jsonl")
    assert url == "https://graph.microsoft.com/v1.0/me/drive/special/approot:/transcripts/a%20b.jsonl:/content"


def test_uploads_new_file_with_bearer_token(tmp_path: Path) -> None:
    """Uploads new file with bearer token."""
    path = _transcript(tmp_path, text='{"type": "session_start"}\n')
    graph = FakeGraph()
    uploader = OneDriveUploader(tmp_path, lambda: "tok", client=graph.client())

    assert uploader.upload_pending() == 1
    (request,) = graph.requests
    assert request.method == "PUT"
    assert str(request.url) == upload_url(f"transcripts/{path.name}")
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.content == path.read_bytes()


def test_only_changed_files_are_uploaded_again(tmp_path: Path) -> None:
    """Only changed files are uploaded again."""
    path = _transcript(tmp_path)
    graph = FakeGraph()
    uploader = OneDriveUploader(tmp_path, lambda: "tok", client=graph.client())

    assert uploader.upload_pending() == 1
    assert uploader.upload_pending() == 0

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "utterance"}\n')
    assert uploader.upload_pending() == 1


def test_upload_state_survives_restart(tmp_path: Path) -> None:
    """Upload state survives restart."""
    _transcript(tmp_path)
    graph = FakeGraph()
    OneDriveUploader(tmp_path, lambda: "tok", client=graph.client()).upload_pending()

    restarted = OneDriveUploader(tmp_path, lambda: "tok", client=graph.client())
    assert restarted.upload_pending() == 0
    assert len(graph.requests) == 1


def test_not_signed_in_keeps_files_for_later(tmp_path: Path) -> None:
    """Not signed in keeps files for later."""
    _transcript(tmp_path)
    graph = FakeGraph()
    token: list[str | None] = [None]
    uploader = OneDriveUploader(tmp_path, lambda: token[0], client=graph.client())

    assert uploader.upload_pending() == 0
    assert graph.requests == []

    token[0] = "tok"
    assert uploader.upload_pending() == 1


def test_failed_upload_is_retried_next_pass(tmp_path: Path) -> None:
    """Failed upload is retried next pass."""
    _transcript(tmp_path)
    graph = FakeGraph(statuses=[503])
    uploader = OneDriveUploader(tmp_path, lambda: "tok", client=graph.client())

    assert uploader.upload_pending() == 0
    assert uploader.upload_pending() == 1
    assert len(graph.requests) == 2


def test_run_until_makes_a_final_pass_after_stop(tmp_path: Path) -> None:
    """Run until makes a final pass after stop."""
    graph = FakeGraph()
    uploader = OneDriveUploader(tmp_path, lambda: "tok", interval_s=3600, client=graph.client())
    stop = threading.Event()
    thread = threading.Thread(target=uploader.run_until, args=(stop,))
    thread.start()

    _transcript(tmp_path)  # written while the uploader is waiting out its interval
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(graph.requests) == 1


def test_from_env_returns_none_without_client_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """From env returns none without client id."""
    monkeypatch.delenv(onedrive_upload.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(onedrive_upload, "CLIENT_ID", "")
    assert OneDriveUploader.from_env(tmp_path) is None


def test_login_without_client_id_exits_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Login without client id exits with error."""
    monkeypatch.delenv(onedrive_upload.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(onedrive_upload, "CLIENT_ID", "")
    assert onedrive_upload.login_main([]) == 2


def test_token_cache_is_readable_by_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Token cache is readable by owner only."""
    monkeypatch.setattr(msal, "PublicClientApplication", MagicMock())
    cache_path = tmp_path / "config" / "onedrive_token_cache.json"
    provider = TokenProvider("client", "tenant", cache_path=cache_path)

    provider._cache.has_state_changed = True
    provider._save()

    assert stat.S_IMODE(os.stat(cache_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cache_path.parent).st_mode) == 0o700


def test_silent_picks_up_a_login_done_while_the_app_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Silent picks up a login done while the app runs."""
    app = MagicMock()
    app.get_accounts.side_effect = [[], [{"username": "lab@uc.edu"}]]
    app.acquire_token_silent.return_value = {"access_token": "fresh"}
    monkeypatch.setattr(msal, "PublicClientApplication", MagicMock(return_value=app))
    cache_path = tmp_path / "onedrive_token_cache.json"
    provider = TokenProvider("client", "tenant", cache_path=cache_path)

    cache_path.write_text(msal.SerializableTokenCache().serialize(), encoding="utf-8")  # the login command ran

    assert provider.silent() == "fresh"
    assert provider._loaded_mtime_ns == cache_path.stat().st_mtime_ns


def test_offline_at_start_does_not_stop_later_uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The robot may boot before Wi-Fi is up; uploads must start once Microsoft is reachable."""
    app = MagicMock()
    app.get_accounts.return_value = [{"username": "lab@uc.edu"}]
    app.acquire_token_silent.return_value = {"access_token": "tok"}
    build = MagicMock(side_effect=[ConnectionError("no network"), app])
    monkeypatch.setattr(msal, "PublicClientApplication", build)
    provider = TokenProvider("client", "tenant", cache_path=tmp_path / "cache.json")  # must not touch the network
    _transcript(tmp_path)
    graph = FakeGraph()
    uploader = OneDriveUploader(tmp_path, provider.silent, client=graph.client())

    uploader._safe_upload()  # offline: logged, nothing uploaded
    assert graph.requests == []

    uploader._safe_upload()  # back online
    assert len(graph.requests) == 1
