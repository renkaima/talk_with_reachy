"""Tests for where the study logging plugs into the rest of the app."""

import json
import asyncio
from typing import Any
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import test_huggingface_realtime as hf_tests
from study_helpers import records

from talk_with_reachy import memory, prompts, voice_id, study_log
from talk_with_reachy.tools.forget import Forget
from talk_with_reachy.tools.remember import Remember
from talk_with_reachy.onedrive_upload import OneDriveUploader, upload_url
from talk_with_reachy.tools.core_tools import ToolDependencies
from talk_with_reachy.tools.background_tool_manager import BackgroundToolManager


def _deps(instance_path: Path | None = None) -> ToolDependencies:
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock(), instance_path=instance_path)


@pytest.mark.asyncio
async def test_realtime_events_reach_the_study_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speech offsets, transcripts, Reachy audio and response boundaries are all forwarded."""
    events = (
        hf_tests._FakeEvent("input_audio_buffer.speech_started", item_id="it1", audio_start_ms=1200),
        hf_tests._FakeEvent("input_audio_buffer.speech_stopped", item_id="it1", audio_end_ms=3400),
        hf_tests._FakeEvent("conversation.item.input_audio_transcription.completed", item_id="it1", transcript="Hi"),
        hf_tests._FakeEvent("response.created"),
        hf_tests._FakeEvent("response.output_audio.delta", delta="AAAAAA=="),
        hf_tests._FakeEvent("response.output_audio_transcript.done", transcript="Hello!"),
        hf_tests._FakeEvent("response.done"),
    )
    handler = hf_tests._session_handler(monkeypatch, events)
    handler.study = MagicMock()

    await handler._run_realtime_session()

    study = handler.study
    study.connection_opened.assert_called_once()
    study.user_speech_started.assert_called_once_with("it1", 1200)
    study.user_speech_stopped.assert_called_once_with("it1", 3400)
    study.user_transcript.assert_called_once_with("it1", "Hi")
    study.response_created.assert_called_once()
    study.assistant_audio.assert_called_once_with(2, handler.SAMPLE_RATE)  # 4 bytes of PCM16
    study.assistant_transcript.assert_called_once_with("Hello!")
    study.response_done.assert_called_once()
    study.connection_closed.assert_called_once_with("closed")


@pytest.mark.asyncio
async def test_only_audio_the_server_received_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frames dropped while reconnecting must not shift the audio offsets."""
    handler = hf_tests._plain_handler()
    handler.study = MagicMock()
    frame = np.zeros(1600, dtype=np.int16)

    await handler.receive((16000, frame))  # no connection yet
    handler.study.mic_audio.assert_not_called()

    handler.connection = MagicMock()
    handler.connection.input_audio_buffer.append = MagicMock(side_effect=_async_none)
    await handler.receive((16000, frame))
    handler.study.mic_audio.assert_called_once()


async def _async_none(**_kw: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_system_note_is_sent_as_a_system_message() -> None:
    """Who-is-speaking notes are system items that do not ask for a reply."""
    handler = hf_tests._plain_handler()
    created: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> None:
        created.append(kwargs)

    handler.connection = MagicMock()
    handler.connection.conversation.item.create = create
    await handler._send_system_note("Voice ID: the person speaking now is probably Mary (ID P01).")

    assert created == [
        {
            "item": {
                "type": "message",
                "role": "system",
                "content": [
                    {"type": "input_text", "text": "Voice ID: the person speaking now is probably Mary (ID P01)."}
                ],
            }
        }
    ]


@pytest.mark.asyncio
async def test_tool_calls_are_logged_with_their_outcome(study_dir: Path) -> None:
    """Each tool run produces a tool_started and a tool_finished event."""
    study_log.start()
    manager = BackgroundToolManager()
    routine = MagicMock()
    routine.tool_name = "dance"
    routine.args_json_str = '{"move": "happy"}'

    async def run(_manager: BackgroundToolManager) -> dict[str, Any]:
        return {"ok": True}

    routine.side_effect = run
    await manager.start_tool("call_1", routine, is_idle_tool_call=False)
    await asyncio.sleep(0.05)
    study_log.stop()

    events = [r for r in records(study_dir) if r["type"] == "event" and str(r["event"]).startswith("tool_")]
    assert [(e["event"], e["tool"]) for e in events] == [("tool_started", "dance"), ("tool_finished", "dance")]
    assert events[0]["args"] == '{"move": "happy"}' and events[0]["idle"] is False
    assert events[1]["status"] == "completed"


def test_uploader_mirrors_every_study_folder(tmp_path: Path) -> None:
    """Transcripts, people and audio go up under the same relative paths; models and temp files do not."""
    files = {
        "transcripts/r_1.jsonl": b"{}\n",
        "transcripts/r_1.csv": b"seq\n",
        "people/library.json": b"{}",
        "people/P01/memory.v1.json": b"{}",
        "audio/r_1/0001_P01.wav": b"RIFF",
        "audio/r_1/.0002_V001.wav.tmp": b"partial",
        "audio/.DS_Store": b"mac",
        "models/nemo_en_titanet_small.onnx": b"model",
        "upload_state.json": b"{}",
    }
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    sent: dict[str, str] = {}

    def graph(request: Any) -> Any:
        import httpx

        sent[str(request.url)] = request.headers["Content-Type"]
        return httpx.Response(201, json={})

    import httpx

    uploader = OneDriveUploader(tmp_path, lambda: "tok", client=httpx.Client(transport=httpx.MockTransport(graph)))
    assert uploader.upload_pending() == 5
    expected = [
        "transcripts/r_1.csv",
        "transcripts/r_1.jsonl",
        "people/P01/memory.v1.json",
        "people/library.json",
        "audio/r_1/0001_P01.wav",
    ]
    assert list(sent) == [upload_url(p) for p in expected]
    assert sent[upload_url("audio/r_1/0001_P01.wav")].startswith("audio/")
    assert sent[upload_url("transcripts/r_1.jsonl")] == "text/plain; charset=utf-8"


@pytest.mark.asyncio
async def test_memories_belong_to_the_person_speaking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With voice ID on, remember and forget act on the current speaker's own memory."""
    monkeypatch.setattr(voice_id, "enabled", lambda: True)
    monkeypatch.setattr(voice_id, "current_speaker", lambda: "P01")
    monkeypatch.setattr(voice_id, "current_person_dir", lambda: tmp_path / "people" / "P01")

    saved = await Remember()(_deps(tmp_path), fact="Likes gardening")
    assert saved["about"] == "P01"
    assert [f.text for f in memory.list_memory_facts(tmp_path / "people" / "P01")] == ["Likes gardening"]
    assert memory.list_memory_facts(tmp_path) == []

    removed = await Forget()(_deps(tmp_path), query="gardening")
    assert removed["removed"] == "Likes gardening"


@pytest.mark.asyncio
async def test_nothing_is_remembered_when_the_speaker_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fact is not filed under the wrong person, or under everyone."""
    monkeypatch.setattr(voice_id, "enabled", lambda: True)
    monkeypatch.setattr(voice_id, "current_speaker", lambda: None)
    monkeypatch.setattr(voice_id, "current_person_dir", lambda: None)

    result = await Remember()(_deps(tmp_path), fact="Likes gardening")
    assert "error" in result
    assert memory.list_memory_facts(tmp_path) == []


def test_voice_id_replaces_the_shared_memory_in_the_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With voice ID on, nobody's memories are in the session instructions."""
    memory.add_memory_fact(tmp_path, "Shared fact")
    monkeypatch.setattr(voice_id, "enabled", lambda: True)
    with_voice_id = prompts.get_session_instructions(tmp_path)
    monkeypatch.setattr(voice_id, "enabled", lambda: False)
    without = prompts.get_session_instructions(tmp_path)

    assert prompts.VOICE_ID_GUIDANCE in with_voice_id and "Shared fact" not in with_voice_id
    assert "Shared fact" in without and prompts.VOICE_ID_GUIDANCE not in without


def test_session_header_records_voice_id_settings(study_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first record says whether voice ID was on and with which model and threshold."""
    monkeypatch.setenv(voice_id.VOICE_ID_ENV, "1")
    monkeypatch.setattr(voice_id, "start", lambda data_dir: None)
    study_log.start()
    study_log.stop()

    header = records(study_dir)[0]
    assert header["voice_id"] == {
        "enabled": True,
        "model": voice_id.MODEL_NAME,
        "match_threshold": voice_id.DEFAULT_MATCH_THRESHOLD,
    }
    assert json.dumps(header)  # serializable
