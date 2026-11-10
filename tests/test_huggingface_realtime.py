import json
import time
import base64
import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import talk_with_reachy.conversation_handler as conv_mod
import talk_with_reachy.huggingface_realtime as hf_mod
from talk_with_reachy.config import config, get_default_voice
from talk_with_reachy.streaming import AdditionalOutputs
from talk_with_reachy.tools.core_tools import ToolDependencies
from talk_with_reachy.huggingface_realtime import HuggingFaceRealtimeHandler
from talk_with_reachy.tools.background_tool_manager import ToolState, ToolNotification


HF_DEFAULT_VOICE = get_default_voice()


class _FakeEvent:
    """A minimal realtime event: a `type` plus arbitrary attributes."""

    def __init__(self, event_type: str, **fields: Any) -> None:
        """Store the event type and any extra attributes."""
        self.type = event_type
        self.__dict__.update(fields)


def _make_fake_realtime_client(
    *,
    events: tuple[_FakeEvent, ...] = (),
    captured_update: dict[str, Any] | None = None,
    captured_connect: dict[str, Any] | None = None,
) -> Any:
    """Build a fake AsyncOpenAI-shaped client whose realtime session yields `events`.

    When given, `captured_update`/`captured_connect` record the kwargs passed to
    `session.update(...)` / `realtime.connect(...)`.
    """

    class FakeSession:
        async def update(self, **kwargs: Any) -> None:
            if captured_update is not None:
                captured_update.update(kwargs)

    class FakeNoop:
        async def append(self, **_kw: Any) -> None:
            pass

        async def create(self, **_kw: Any) -> None:
            pass

        async def cancel(self, **_kw: Any) -> None:
            pass

    class FakeConversation:
        item = FakeNoop()

    class FakeConn:
        session = FakeSession()
        input_audio_buffer = FakeNoop()
        conversation = FakeConversation()
        response = FakeNoop()

        def __init__(self) -> None:
            self._events = iter(events)

        async def __aenter__(self) -> "FakeConn":
            return self

        async def __aexit__(self, *_args: Any) -> bool:
            return False

        async def close(self) -> None:
            pass

        def __aiter__(self) -> "FakeConn":
            return self

        async def __anext__(self) -> _FakeEvent:
            try:
                return next(self._events)
            except StopIteration:
                raise StopAsyncIteration

    class FakeRealtime:
        def connect(self, **kwargs: Any) -> FakeConn:
            if captured_connect is not None:
                captured_connect.update(kwargs)
            return FakeConn()

    class FakeClient:
        realtime = FakeRealtime()

    return FakeClient()


def _fake_openai_client(captured_kwargs: dict[str, Any]) -> type:
    """Return a fake AsyncOpenAI class that records its constructor kwargs."""

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)

    return FakeClient


def _fake_allocator(
    connect_url: str,
    posts: list[tuple[str, dict[str, str] | None, dict[str, str] | None]],
) -> type:
    """Return a fake httpx.AsyncClient that records allocator requests."""

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return {"session_id": "session-123", "connect_url": connect_url}

    class FakeAsyncClient:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, str] | None = None,
        ) -> FakeResponse:
            posts.append((url, headers, json))
            return FakeResponse()

    return FakeAsyncClient


def _plain_handler() -> HuggingFaceRealtimeHandler:
    return HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))


def _session_handler(monkeypatch: Any, events: tuple[_FakeEvent, ...]) -> HuggingFaceRealtimeHandler:
    """Build a handler wired to a fake realtime client yielding `events`, ready to run a session."""
    monkeypatch.setattr(hf_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: default)
    monkeypatch.setattr(hf_mod, "get_tool_specs", lambda: [])
    monkeypatch.setattr(hf_mod, "get_session_greeting_prompt", lambda: "")
    handler = _plain_handler()
    handler.client = _make_fake_realtime_client(events=events)
    monkeypatch.setattr(type(handler.tool_manager), "start_up", MagicMock())
    monkeypatch.setattr(type(handler.tool_manager), "shutdown", AsyncMock())
    return handler


def _drain(handler: HuggingFaceRealtimeHandler) -> list[Any]:
    items: list[Any] = []
    while not handler.output_queue.empty():
        items.append(handler.output_queue.get_nowait())
    return items


def _messages(items: list[Any]) -> list[dict[str, Any]]:
    return [item.args[0] for item in items if isinstance(item, AdditionalOutputs)]


@pytest.mark.asyncio
async def test_partial_transcription_appends_incremental_deltas(monkeypatch: Any) -> None:
    """Partial transcription deltas for one item should accumulate in arrival order."""
    monkeypatch.setattr(hf_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: "Aiden")
    monkeypatch.setattr(hf_mod, "get_tool_specs", lambda: [])

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.client = _make_fake_realtime_client(
        events=(
            _FakeEvent("conversation.item.input_audio_transcription.delta", item_id="item-1", delta="Hey how are"),
            _FakeEvent("conversation.item.input_audio_transcription.delta", item_id="item-1", delta=" you"),
        )
    )
    monkeypatch.setattr(type(handler.tool_manager), "start_up", MagicMock())
    monkeypatch.setattr(type(handler.tool_manager), "shutdown", AsyncMock())

    await handler._run_realtime_session()

    assert handler.input_transcript_chunks_by_item.item_id == "item-1"
    assert handler.input_transcript_chunks_by_item.deltas == ["Hey how are", " you"]
    assert "".join(handler.input_transcript_chunks_by_item.deltas) == "Hey how are you"


@pytest.mark.asyncio
async def test_partial_transcription_resets_on_new_item_id(monkeypatch: Any) -> None:
    """A new item_id should drop fragments from the previous item."""
    monkeypatch.setattr(hf_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: "Aiden")
    monkeypatch.setattr(hf_mod, "get_tool_specs", lambda: [])

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.client = _make_fake_realtime_client(
        events=(
            _FakeEvent("conversation.item.input_audio_transcription.delta", item_id="item-1", delta="old"),
            _FakeEvent("conversation.item.input_audio_transcription.delta", item_id="item-2", delta="new"),
            _FakeEvent("conversation.item.input_audio_transcription.delta", item_id="item-2", delta=" fragment"),
        )
    )
    monkeypatch.setattr(type(handler.tool_manager), "start_up", MagicMock())
    monkeypatch.setattr(type(handler.tool_manager), "shutdown", AsyncMock())

    await handler._run_realtime_session()

    assert handler.input_transcript_chunks_by_item.item_id == "item-2"
    assert handler.input_transcript_chunks_by_item.deltas == ["new", " fragment"]


@pytest.mark.asyncio
async def test_emit_skips_idle_signal_while_response_active(monkeypatch: Any) -> None:
    """Idle tools should not trigger while a response is still active."""
    movement_manager = MagicMock()
    movement_manager.is_idle.return_value = True
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=movement_manager)
    handler = HuggingFaceRealtimeHandler(deps)
    handler.last_activity_time = time.monotonic() - (handler.IDLE_BEHAVIOR_THRESHOLD_S + 10.0)
    handler._response_done_event.clear()

    send_idle_signal = AsyncMock()
    monkeypatch.setattr(handler, "send_idle_signal", send_idle_signal)
    monkeypatch.setattr(conv_mod, "wait_for_item", AsyncMock(return_value=None))

    result = await handler.emit()

    assert result is None
    send_idle_signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_tool_calls_trigger_single_response(monkeypatch: Any) -> None:
    """Parallel tool calls in one turn should yield one response, not one per completed tool."""
    monkeypatch.setattr(hf_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: "Aiden")
    monkeypatch.setattr(hf_mod, "get_tool_specs", lambda: [])

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.connection = AsyncMock()
    handler.output_queue = asyncio.Queue()
    monkeypatch.setattr(handler, "_wait_for_response_done_before_tool_result", AsyncMock(return_value=True))
    create = AsyncMock()
    monkeypatch.setattr(handler, "_safe_response_create", create)

    handler._in_flight_tool_calls = {"call_a", "call_b"}

    def _completed(call_id: str) -> ToolNotification:
        return ToolNotification(
            id=call_id,
            tool_name="test__parallel_probe",
            is_idle_tool_call=False,
            status=ToolState.COMPLETED,
            result={"ok": True},
        )

    await handler._handle_tool_result(_completed("call_a"))
    assert create.await_count == 0

    await handler._handle_tool_result(_completed("call_b"))
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_a_reply_that_is_only_a_no_reply_tool_call_gets_one_spoken_follow_up(monkeypatch: Any) -> None:
    """When Reachy answers unclear speech with only asked_to_repeat and no audio, the app asks it to speak once."""
    monkeypatch.setattr(hf_mod.study_log, "record_event", lambda name, **fields: None)
    monkeypatch.setattr(
        hf_mod.core_tools, "get_tools", lambda: {"asked_to_repeat": SimpleNamespace(needs_response=False)}
    )
    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.connection = AsyncMock()
    monkeypatch.setattr(handler, "_wait_for_response_done_before_tool_result", AsyncMock(return_value=True))
    create = AsyncMock()
    monkeypatch.setattr(handler, "_safe_response_create", create)

    def _completed(call_id: str) -> ToolNotification:
        return ToolNotification(
            id=call_id,
            tool_name="asked_to_repeat",
            is_idle_tool_call=False,
            status=ToolState.COMPLETED,
            result={"saved": True},
        )

    # A reply with speech and the tool call: no follow-up.
    await handler._handle_tool_result(_completed("call_spoken"))
    assert create.await_count == 0
    # Two silent replies in the same turn: one follow-up only, so Reachy cannot loop.
    handler._silent_calls = {"call_silent_1", "call_silent_2"}
    await handler._handle_tool_result(_completed("call_silent_1"))
    await handler._handle_tool_result(_completed("call_silent_2"))
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_a_silent_reply_is_noticed_from_the_realtime_events(monkeypatch: Any) -> None:
    """A response with a tool call and no audio marks that call as silent; one with audio does not."""
    one_second = base64.b64encode(np.zeros(HuggingFaceRealtimeHandler.SAMPLE_RATE, dtype=np.int16)).decode()
    handler = _session_handler(
        monkeypatch,
        (
            _FakeEvent("response.created"),
            _FakeEvent("response.function_call_arguments.done", name="nope", arguments="{}", call_id="call_quiet"),
            _FakeEvent("response.done"),
            _FakeEvent("response.created"),
            _FakeEvent("response.output_audio.delta", delta=one_second),
            _FakeEvent("response.function_call_arguments.done", name="nope", arguments="{}", call_id="call_heard"),
            _FakeEvent("response.done"),
        ),
    )
    monkeypatch.setattr(type(handler.tool_manager), "start_tool", AsyncMock(return_value=SimpleNamespace(tool_id="t")))

    await handler._run_realtime_session()

    assert handler._silent_calls == {"call_quiet"}


@pytest.mark.asyncio
async def test_the_profile_speech_speed_slows_the_played_audio(monkeypatch: Any) -> None:
    """At speech_speed 0.8, one second of speech plays (and is timed) as about 1.25 seconds."""
    tone = (np.sin(np.arange(HuggingFaceRealtimeHandler.SAMPLE_RATE) / 5) * 8000).astype(np.int16)
    handler = _session_handler(
        monkeypatch,
        (
            _FakeEvent("response.created"),
            _FakeEvent("response.output_audio.delta", delta=base64.b64encode(tone.tobytes()).decode()),
            _FakeEvent("response.output_audio.done"),
            _FakeEvent("response.done"),
        ),
    )
    monkeypatch.setattr(hf_mod, "get_session_speech_speed", lambda: 0.8)
    timed: list[int] = []
    monkeypatch.setattr(handler.study, "assistant_audio", lambda samples, rate: timed.append(samples))

    await handler._run_realtime_session()

    played = sum(item[1].size for item in _drain(handler) if isinstance(item, tuple))
    assert 1.21 < played / tone.size < 1.29
    assert sum(timed) == played


def test_handler_uses_hf_startup_voice_at_startup(monkeypatch: Any) -> None:
    """Hugging Face startup should restore persisted HF voices."""
    handler = HuggingFaceRealtimeHandler(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()),
        startup_voice="Aiden",
    )

    assert handler.get_current_voice() == "Aiden"


def test_handler_ignores_unsupported_hf_profile_voice(monkeypatch: Any) -> None:
    """Unsupported profile voices should not be sent to the Hugging Face backend."""
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: "cedar")

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))

    assert handler.get_current_voice() == HF_DEFAULT_VOICE
    session = handler._get_session_config([])
    assert session["audio"]["output"]["voice"] == HF_DEFAULT_VOICE


@pytest.mark.parametrize("turn_silence_ms", [None, 1200])
def test_session_turn_detection_uses_the_profile_silence(monkeypatch: Any, turn_silence_ms: int | None) -> None:
    """A profile's end-of-turn silence is sent to the server; without one the server default applies."""
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: default)
    monkeypatch.setattr(hf_mod, "get_session_turn_silence_ms", lambda: turn_silence_ms)

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    turn_detection = handler._get_session_config([])["audio"]["input"]["turn_detection"]

    assert turn_detection.get("silence_duration_ms") == turn_silence_ms
    assert turn_detection["interrupt_response"] is True


@pytest.mark.asyncio
async def test_tools_can_wait_until_reachy_has_finished_speaking(monkeypatch: Any) -> None:
    """Two seconds of received speech keep Reachy 'speaking' for about two seconds; barge-in ends it at once."""
    two_seconds = base64.b64encode(np.zeros(2 * HuggingFaceRealtimeHandler.SAMPLE_RATE, dtype=np.int16)).decode()
    handler = _session_handler(
        monkeypatch,
        (
            _FakeEvent("response.created"),
            _FakeEvent("response.output_audio.delta", delta=two_seconds),
            _FakeEvent("response.done"),
        ),
    )
    quiet_in = handler.deps.seconds_until_reachy_is_quiet
    assert quiet_in is not None and quiet_in() == 0.0

    await handler._run_realtime_session()

    assert 1.0 < quiet_in() <= 2.0
    handler._clear_queue = MagicMock()
    handler.client = _make_fake_realtime_client(
        events=(_FakeEvent("input_audio_buffer.speech_started", item_id="item-2", audio_start_ms=0),)
    )
    await handler._run_realtime_session()
    assert quiet_in() == 0.0


def test_handler_normalizes_hf_voice_case(monkeypatch: Any) -> None:
    """Lowercase Hugging Face speaker names should resolve to the curated UI value."""
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: "serena")

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))

    assert handler.get_current_voice() == "Serena"


@pytest.mark.asyncio
async def test_run_realtime_session_uses_default_voice_for_lb_allocated_sessions(monkeypatch: Any) -> None:
    """Use the backend default speaker when no profile voice is selected for the hf LB."""
    monkeypatch.setattr(hf_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: default)
    monkeypatch.setattr(hf_mod, "get_tool_specs", lambda: [])
    monkeypatch.setattr(config, "HF_REALTIME_SESSION_URL", "https://lb.example.test/session")

    captured_update: dict[str, Any] = {}
    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.client = _make_fake_realtime_client(captured_update=captured_update)

    await handler._run_realtime_session()

    session = captured_update["session"]
    # HF at 16 kHz passes None so the backend uses its optimal default (16 kHz).
    assert session["audio"]["input"]["format"]["rate"] is None
    assert session["audio"]["output"]["format"]["rate"] is None
    assert session["audio"]["input"]["transcription"]["language"] == "en"
    assert session["audio"]["output"]["voice"] == HF_DEFAULT_VOICE


def test_huggingface_session_uses_configured_transcription_language(monkeypatch: Any) -> None:
    """Hugging Face realtime sessions should forward the configured transcription language."""
    monkeypatch.setattr(config, "REALTIME_TRANSCRIPTION_LANGUAGE", "zh")
    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))

    session = handler._get_session_config([])

    assert session["audio"]["input"]["transcription"]["language"] == "zh"


@pytest.mark.asyncio
async def test_run_realtime_session_passes_allocated_session_query(monkeypatch: Any) -> None:
    """Hugging Face sessions must forward the allocated session token to the websocket connect call."""
    monkeypatch.setattr(hf_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: default)
    monkeypatch.setattr(hf_mod, "get_tool_specs", lambda: [])

    captured_connect: dict[str, Any] = {}
    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.client = _make_fake_realtime_client(captured_connect=captured_connect)
    handler._realtime_connect_query = {"session_token": "abc123"}

    await handler._run_realtime_session()

    assert "model" not in captured_connect
    assert captured_connect["extra_query"] == {"session_token": "abc123"}


@pytest.mark.parametrize(("hf_token", "expected_api_key"), [(None, "DUMMY"), ("hf-secret", "hf-secret")])
@pytest.mark.asyncio
async def test_build_realtime_client_local_uses_explicit_hf_token_only(
    monkeypatch: Any,
    hf_token: str | None,
    expected_api_key: str,
) -> None:
    """Local websocket mode must never forward cached Hugging Face credentials."""
    client_kwargs: dict[str, Any] = {}

    def _no_allocator(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("session allocator should not be called in direct websocket mode")

    monkeypatch.setattr(hf_mod, "AsyncOpenAI", _fake_openai_client(client_kwargs))
    monkeypatch.setattr(hf_mod.httpx, "AsyncClient", _no_allocator)
    monkeypatch.setattr(config, "HF_REALTIME_CONNECTION_MODE", "local")
    monkeypatch.setattr(config, "HF_REALTIME_SESSION_URL", "https://lb.example.test/session")
    monkeypatch.setattr(config, "HF_TOKEN", hf_token)
    monkeypatch.setattr(hf_mod, "get_token", lambda: "hf-cached")
    monkeypatch.setattr(
        config,
        "HF_REALTIME_WS_URL",
        "ws://127.0.0.1:8765/v1/realtime?session_token=abc123&model=ignored-by-sdk",
    )

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))

    client = await handler._build_realtime_client()

    assert client is not None
    assert client_kwargs["api_key"] == expected_api_key
    assert client_kwargs["base_url"] == "http://127.0.0.1:8765/v1"
    assert client_kwargs["websocket_base_url"] == "ws://127.0.0.1:8765/v1"
    assert handler._realtime_connect_query == {"session_token": "abc123"}


@pytest.mark.parametrize(
    (
        "hf_token",
        "cached_token",
        "hardware_id",
        "status_error",
        "expected_header",
        "expected_api_key",
        "expected_payload",
    ),
    [
        (
            "hf-secret",
            "hf-cached",
            "0123456789abcdef",
            None,
            {
                "User-Agent": "reachy-mini-conversation-app",
                "X-Reachy-Mini-Authorization": "Bearer hf-secret",
            },
            "hf-secret",
            {"hardware_id": "0123456789abcdef"},
        ),
        (
            None,
            "hf-cached",
            None,
            None,
            {
                "User-Agent": "reachy-mini-conversation-app",
                "X-Reachy-Mini-Authorization": "Bearer hf-cached",
            },
            "hf-cached",
            {},
        ),
        (None, None, None, None, {"User-Agent": "reachy-mini-conversation-app"}, "DUMMY", {}),
        (
            None,
            None,
            None,
            TimeoutError("status unavailable"),
            {"User-Agent": "reachy-mini-conversation-app"},
            "DUMMY",
            {},
        ),
    ],
)
@pytest.mark.asyncio
async def test_build_realtime_client_deployed_resolves_hf_token(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
    hf_token: str | None,
    cached_token: str | None,
    hardware_id: str | None,
    status_error: Exception | None,
    expected_header: dict[str, str],
    expected_api_key: str,
    expected_payload: dict[str, str],
) -> None:
    """Deployed allocation reports available credentials and robot identity."""
    client_kwargs: dict[str, Any] = {}
    posts: list[tuple[str, dict[str, str] | None, dict[str, str] | None]] = []
    connect_url = "wss://hf.example.test/v1/realtime?session_token=allocated"
    monkeypatch.setattr(hf_mod, "AsyncOpenAI", _fake_openai_client(client_kwargs))
    monkeypatch.setattr(hf_mod.httpx, "AsyncClient", _fake_allocator(connect_url, posts))
    monkeypatch.setattr(config, "HF_REALTIME_CONNECTION_MODE", "deployed")
    monkeypatch.setattr(config, "HF_REALTIME_SESSION_URL", "https://lb.example.test/session")
    # A stale local URL must be ignored in deployed mode.
    monkeypatch.setattr(config, "HF_REALTIME_WS_URL", "ws://127.0.0.1:8765/v1/realtime")
    monkeypatch.setattr(config, "HF_TOKEN", hf_token)
    monkeypatch.setattr(hf_mod, "get_token", lambda: cached_token)

    reachy_mini = MagicMock()
    reachy_mini.client.get_status.return_value.hardware_id = hardware_id
    if status_error:
        reachy_mini.client.get_status.side_effect = status_error
    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=reachy_mini, movement_manager=MagicMock()))

    client = await handler._build_realtime_client()

    assert client is not None
    assert posts == [("https://lb.example.test/session", expected_header, expected_payload)]
    reachy_mini.client.get_status.assert_called_once_with(wait=False)
    if status_error:
        assert "Daemon status unavailable for realtime session allocation" in caplog.text
    assert client_kwargs["api_key"] == expected_api_key
    assert client_kwargs["base_url"] == "https://hf.example.test/v1"
    assert client_kwargs["websocket_base_url"] == "wss://hf.example.test/v1"
    assert handler._realtime_connect_query == {"session_token": "allocated"}


@pytest.mark.asyncio
async def test_apply_personality_uses_selected_voice_for_lb_allocated_sessions(monkeypatch: Any) -> None:
    """Live personality updates should honor the selected Qwen CustomVoice speaker."""
    monkeypatch.setattr(hf_mod, "get_session_instructions", lambda _instance_path=None: "new instructions")
    monkeypatch.setattr(hf_mod, "get_session_voice", lambda default=HF_DEFAULT_VOICE: "Serena")
    monkeypatch.setattr(config, "HF_REALTIME_SESSION_URL", "https://lb.example.test/session")

    captured_update: dict[str, Any] = {}

    class FakeSession:
        async def update(self, **kwargs: Any) -> None:
            captured_update.update(kwargs)

    class FakeConnection:
        session = FakeSession()

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.connection = FakeConnection()
    monkeypatch.setattr(handler, "_restart_session", AsyncMock(return_value=None))

    result = await handler.apply_personality("mars_rover")

    assert "restarted realtime session" in result.lower()
    session = captured_update["session"]
    assert session["instructions"] == "new instructions"
    assert session["audio"]["output"]["voice"] == "Serena"


@pytest.mark.asyncio
async def test_apply_personality_restores_profile_when_tools_fail(monkeypatch: Any) -> None:
    """A failed tool reload should leave the previous profile selected."""
    selected_profiles: list[str | None] = []

    def select_profile(profile: str | None) -> None:
        selected_profiles.append(profile)
        config.REACHY_MINI_CUSTOM_PROFILE = profile

    def fail_tool_reload(*, force: bool = False) -> None:
        raise RuntimeError("tool reload failed")

    monkeypatch.setattr(config, "REACHY_MINI_CUSTOM_PROFILE", "default")
    monkeypatch.setattr(hf_mod, "set_custom_profile", select_profile)
    monkeypatch.setattr(hf_mod, "get_session_instructions", lambda _instance_path=None: "new instructions")
    monkeypatch.setattr(hf_mod.core_tools, "initialize_tools", fail_tool_reload)
    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))

    result = await handler.apply_personality("broken")

    assert result == "Failed to apply personality: tool reload failed"
    assert config.REACHY_MINI_CUSTOM_PROFILE == "default"
    assert selected_profiles == ["broken", "default"]


@pytest.mark.asyncio
async def test_change_voice_updates_live_hf_session_without_restart(monkeypatch: Any) -> None:
    """Changing Hugging Face voice should update the active session in place."""
    captured_update: dict[str, Any] = {}

    class FakeSession:
        async def update(self, **kwargs: Any) -> None:
            captured_update.update(kwargs)

    class FakeConnection:
        session = FakeSession()

    handler = HuggingFaceRealtimeHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.connection = FakeConnection()
    restart = AsyncMock(return_value=None)
    monkeypatch.setattr(handler, "_restart_session", restart)

    result = await handler.change_voice("Serena")

    assert result == "Voice changed to Serena."
    assert handler.get_current_voice() == "Serena"
    restart.assert_not_awaited()
    session = captured_update["session"]
    assert session["audio"]["output"]["voice"] == "Serena"


@pytest.mark.asyncio
async def test_run_session_decodes_audio_delta(monkeypatch: Any) -> None:
    """An audio delta is base64-decoded and enqueued as an (rate, int16) frame."""
    pcm = np.array([1, 2, 3, 4], dtype=np.int16)
    delta = base64.b64encode(pcm.tobytes()).decode("utf-8")
    handler = _session_handler(monkeypatch, (_FakeEvent("response.output_audio.delta", delta=delta),))

    await handler._run_realtime_session()

    frames = [item for item in _drain(handler) if isinstance(item, tuple)]
    assert len(frames) == 1
    rate, array = frames[0]
    assert rate == handler.SAMPLE_RATE
    np.testing.assert_array_equal(array.reshape(-1), pcm)


@pytest.mark.asyncio
async def test_run_session_emits_completed_transcript(monkeypatch: Any) -> None:
    """A non-empty completed transcript is enqueued as a user message and stops listening."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("conversation.item.input_audio_transcription.completed", transcript="Hello there"),),
    )

    await handler._run_realtime_session()

    assert {"role": "user", "content": "Hello there"} in _messages(_drain(handler))
    handler.deps.movement_manager.set_listening.assert_any_call(False)


@pytest.mark.asyncio
async def test_run_session_skips_empty_completed_transcript(monkeypatch: Any) -> None:
    """An empty completed transcript is ignored but still stops listening."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("conversation.item.input_audio_transcription.completed", transcript="   "),),
    )

    await handler._run_realtime_session()

    assert [msg for msg in _messages(_drain(handler)) if msg.get("role") == "user"] == []
    handler.deps.movement_manager.set_listening.assert_any_call(False)


@pytest.mark.asyncio
async def test_run_session_generic_error_is_surfaced(monkeypatch: Any) -> None:
    """A generic realtime error is forwarded to the UI as an assistant message."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("error", error=SimpleNamespace(message="bad", code="server_error")),),
    )

    await handler._run_realtime_session()

    assert {"role": "assistant", "content": "[error] bad"} in _messages(_drain(handler))


@pytest.mark.asyncio
async def test_run_session_commit_empty_error_is_internal(monkeypatch: Any) -> None:
    """A commit-empty error stops listening and is not surfaced to the UI."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("error", error=SimpleNamespace(message="empty", code="input_audio_buffer_commit_empty")),),
    )

    await handler._run_realtime_session()

    assert _messages(_drain(handler)) == []
    handler.deps.movement_manager.set_listening.assert_any_call(False)


@pytest.mark.asyncio
async def test_run_session_active_response_error_flags_rejection(monkeypatch: Any) -> None:
    """A duplicate-response error is recorded for the sender worker, not surfaced."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("error", error=SimpleNamespace(message="x", code="conversation_already_has_active_response")),),
    )

    await handler._run_realtime_session()

    assert handler._last_response_rejected is True
    assert _messages(_drain(handler)) == []


@pytest.mark.asyncio
async def test_run_session_dispatches_valid_tool_call(monkeypatch: Any) -> None:
    """A valid function call starts a background tool and announces it."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("response.function_call_arguments.done", name="dance", arguments="{}", call_id="c1"),),
    )
    start_tool = AsyncMock(return_value=SimpleNamespace(tool_id="dance-1"))
    monkeypatch.setattr(type(handler.tool_manager), "start_tool", start_tool)

    await handler._run_realtime_session()

    start_tool.assert_awaited_once()
    assert any("Used tool dance" in msg["content"] for msg in _messages(_drain(handler)))


@pytest.mark.asyncio
async def test_run_session_ignores_invalid_tool_call(monkeypatch: Any) -> None:
    """A function call with a non-string name is ignored."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("response.function_call_arguments.done", name=None, arguments="{}", call_id="c1"),),
    )
    start_tool = AsyncMock()
    monkeypatch.setattr(type(handler.tool_manager), "start_tool", start_tool)

    await handler._run_realtime_session()

    start_tool.assert_not_awaited()
    assert not any("Used tool" in msg["content"] for msg in _messages(_drain(handler)))


def test_sanitize_tool_result_strips_camera_image() -> None:
    """Camera results drop the raw image bytes and flag that an image was attached."""
    sanitized = HuggingFaceRealtimeHandler._sanitize_tool_result_for_model("camera", {"b64_im": "x", "seen": "cat"})
    assert sanitized == {"seen": "cat", "image_attached": True}


def test_sanitize_tool_result_passthrough_for_non_camera() -> None:
    """Non-camera results are returned unchanged."""
    result = {"status": "ok"}
    assert HuggingFaceRealtimeHandler._sanitize_tool_result_for_model("dance", result) == result


class _FakeConnection:
    def __init__(self) -> None:
        self.conversation = SimpleNamespace(item=SimpleNamespace(create=AsyncMock()))


@pytest.mark.asyncio
async def test_handle_tool_result_error_is_reported_and_prompts_response() -> None:
    """A failed tool reports its error to the UI and requests a spoken follow-up."""
    handler = _plain_handler()
    handler.connection = _FakeConnection()
    note = ToolNotification(id="c1", tool_name="dance", is_idle_tool_call=False, status=ToolState.FAILED, error="boom")

    await handler._handle_tool_result(note)

    assert {"role": "assistant", "content": json.dumps({"error": "boom"})} in _messages(_drain(handler))
    assert not handler._pending_responses.empty()
    handler.connection.conversation.item.create.assert_awaited()


@pytest.mark.asyncio
async def test_handle_tool_result_no_connection_is_dropped() -> None:
    """With no connection the tool result is dropped without enqueuing anything."""
    handler = _plain_handler()
    handler.connection = None
    note = ToolNotification(
        id="c1", tool_name="dance", is_idle_tool_call=False, status=ToolState.COMPLETED, result={"ok": 1}
    )

    await handler._handle_tool_result(note)

    assert handler.output_queue.empty()


@pytest.mark.asyncio
async def test_handle_tool_result_missing_result_reports_error() -> None:
    """A tool that returns neither result nor error is reported as a failure."""
    handler = _plain_handler()
    handler.connection = _FakeConnection()
    note = ToolNotification(id="c1", tool_name="dance", is_idle_tool_call=False, status=ToolState.COMPLETED)

    await handler._handle_tool_result(note)

    expected = json.dumps({"error": "No result returned from tool execution"})
    assert {"role": "assistant", "content": expected} in _messages(_drain(handler))


@pytest.mark.asyncio
async def test_wait_for_response_done_returns_true_when_set() -> None:
    """The tool-result gate passes immediately when no response is active."""
    handler = _plain_handler()
    handler._response_done_event.set()
    assert await handler._wait_for_response_done_before_tool_result() is True


@pytest.mark.asyncio
async def test_wait_for_response_done_times_out(monkeypatch: Any) -> None:
    """The tool-result gate fails when the active response never finishes."""
    monkeypatch.setattr(hf_mod, "_RESPONSE_DONE_TIMEOUT", 0.01)
    handler = _plain_handler()
    handler._response_done_event.clear()
    assert await handler._wait_for_response_done_before_tool_result() is False


@pytest.mark.asyncio
async def test_emit_debounced_partial_emits_current_snapshot() -> None:
    """A partial transcript is emitted when it is still the latest fragment sequence."""
    handler = _plain_handler()
    handler.partial_debounce_delay = 0
    handler.input_transcript_chunks_by_item.item_id = "item-1"
    handler.input_transcript_chunks_by_item.deltas = ["hi"]

    await handler._emit_debounced_partial("hi", "item-1", 0)

    assert {"role": "user_partial", "content": "hi"} in _messages(_drain(handler))


@pytest.mark.asyncio
async def test_emit_debounced_partial_skips_stale_snapshot() -> None:
    """A partial transcript is dropped once a newer delta has arrived."""
    handler = _plain_handler()
    handler.partial_debounce_delay = 0
    handler.input_transcript_chunks_by_item.item_id = "item-1"
    handler.input_transcript_chunks_by_item.deltas = ["hi", "hi there"]

    await handler._emit_debounced_partial("hi", "item-1", 0)

    assert handler.output_queue.empty()


@pytest.mark.asyncio
async def test_emit_runs_idle_behavior_when_due(monkeypatch: Any) -> None:
    """Emit runs the idle behavior once the idle thresholds are exceeded and the robot is idle."""
    handler = _plain_handler()
    handler.last_activity_time = time.monotonic() - (handler.IDLE_BEHAVIOR_THRESHOLD_S + 10.0)
    handler.last_idle_behavior_time = handler.last_activity_time
    handler.deps.movement_manager.is_idle.return_value = True
    handler._response_done_event.set()
    send_idle_signal = AsyncMock()
    monkeypatch.setattr(handler, "send_idle_signal", send_idle_signal)
    monkeypatch.setattr(conv_mod, "wait_for_item", AsyncMock(return_value=None))

    before = time.monotonic()
    await handler.emit()

    send_idle_signal.assert_awaited_once()
    assert handler.last_idle_behavior_time >= before


@pytest.mark.asyncio
async def test_emit_keeps_idle_timer_when_idle_behavior_fails(monkeypatch: Any) -> None:
    """A failing idle behavior returns None and does not advance the idle-behavior timer."""
    handler = _plain_handler()
    handler.last_activity_time = time.monotonic() - (handler.IDLE_BEHAVIOR_THRESHOLD_S + 10.0)
    handler.last_idle_behavior_time = handler.last_activity_time
    handler.deps.movement_manager.is_idle.return_value = True
    handler._response_done_event.set()
    monkeypatch.setattr(handler, "send_idle_signal", AsyncMock(side_effect=RuntimeError("closed")))

    previous = handler.last_idle_behavior_time
    result = await handler.emit()

    assert result is None
    assert handler.last_idle_behavior_time == previous


def test_mark_activity_swallows_observer_errors() -> None:
    """The activity observer is notified, and a raising observer is ignored."""
    handler = _plain_handler()
    seen: list[str] = []
    handler.set_activity_observer(seen.append)
    handler._mark_activity("user_speech_started")
    assert seen == ["user_speech_started"]

    def _raise(_reason: str) -> None:
        raise RuntimeError("observer boom")

    handler.set_activity_observer(_raise)
    handler._mark_activity("later")  # must not raise


@pytest.mark.asyncio
async def test_send_idle_signal_noop_without_connection(monkeypatch: Any) -> None:
    """No idle tool runs when there is no active connection."""
    handler = _plain_handler()
    handler.connection = None
    start_idle = AsyncMock()
    monkeypatch.setattr(conv_mod, "start_idle_tool_call", start_idle)

    await handler.send_idle_signal(200.0)

    start_idle.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_idle_signal_dispatches_idle_tool(monkeypatch: Any) -> None:
    """With an active connection the locally selected idle tool is dispatched."""
    handler = _plain_handler()
    handler.connection = _FakeConnection()
    monkeypatch.setattr(conv_mod, "get_tool_specs", lambda: [{"name": "dance"}])
    start_idle = AsyncMock()
    monkeypatch.setattr(conv_mod, "start_idle_tool_call", start_idle)

    await handler.send_idle_signal(200.0)

    start_idle.assert_awaited_once()


@pytest.mark.asyncio
async def test_receive_without_connection_is_dropped() -> None:
    """A frame received before the connection opens is dropped."""
    handler = _plain_handler()
    handler.connection = None
    await handler.receive((16000, np.zeros(4, dtype=np.int16)))  # must not raise


@pytest.mark.asyncio
async def test_receive_ignores_empty_frame() -> None:
    """An empty audio frame is not forwarded to the realtime buffer."""
    handler = _plain_handler()
    append = AsyncMock()
    handler.connection = SimpleNamespace(input_audio_buffer=SimpleNamespace(append=append))
    await handler.receive((16000, np.array([], dtype=np.int16)))
    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_receive_downmixes_stereo_and_forwards() -> None:
    """A stereo frame is reduced to mono and appended to the realtime buffer."""
    handler = _plain_handler()
    append = AsyncMock()
    handler.connection = SimpleNamespace(input_audio_buffer=SimpleNamespace(append=append))
    stereo = np.array([[1, 10], [2, 20], [3, 30], [4, 40]], dtype=np.int16)
    await handler.receive((16000, stereo))
    append.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_session_speech_started_clears_queue_and_listens(monkeypatch: Any) -> None:
    """User speech start clears the playback queue and enters listening mode."""
    handler = _session_handler(monkeypatch, (_FakeEvent("input_audio_buffer.speech_started"),))
    clear_queue = MagicMock()
    handler._clear_queue = clear_queue

    await handler._run_realtime_session()

    clear_queue.assert_called_once()
    handler.deps.movement_manager.set_listening.assert_any_call(True)


@pytest.mark.asyncio
async def test_run_session_response_lifecycle_toggles_done_event(monkeypatch: Any) -> None:
    """response.created clears the done gate and response.done sets it again."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("response.created"), _FakeEvent("response.done")),
    )

    await handler._run_realtime_session()

    assert handler._response_done_event.is_set()
    handler.deps.movement_manager.set_speaking.assert_any_call(True)
    handler.deps.movement_manager.set_speaking.assert_any_call(False)


@pytest.mark.asyncio
async def test_talking_during_break_music_pauses_it_and_a_sound_lets_it_go_on(monkeypatch: Any) -> None:
    """Speech start pauses the music; an empty transcript lets it go on; no note joins the person's words."""
    handler = _session_handler(
        monkeypatch,
        (
            _FakeEvent("input_audio_buffer.speech_started"),
            _FakeEvent("conversation.item.input_audio_transcription.completed", item_id="item-1", transcript="  "),
        ),
    )
    monkeypatch.setattr(hf_mod.take_break, "pause_music_for_speech", lambda: True)
    chosen: list[str] = []
    monkeypatch.setattr(hf_mod.take_break, "choose_after_pause", lambda choice: chosen.append(choice) or True)
    notes: list[str] = []

    async def note(text: str) -> None:
        notes.append(text)

    monkeypatch.setattr(handler, "_send_app_note", note)

    await handler._run_realtime_session()

    assert notes == []
    assert chosen == ["more_now"]


@pytest.mark.parametrize(
    ("words", "waiting", "stopped", "chosen"),
    [
        ("Stop.", False, ["Stop."], []),  # breathing or the game: stop at once
        ("Don't stop.", False, [], []),
        ("Oh my god!", True, [], []),  # the music stays paused; the model asks "More music, or stop?"
        ("More.", True, [], ["more"]),
        ("Yes.", True, [], ["more"]),
        ("No.", True, ["No."], []),
    ],
)
def test_words_during_a_break_act_in_code(
    monkeypatch: Any, words: str, waiting: bool, stopped: list[str], chosen: list[str]
) -> None:
    """During a break, the app stops it on "stop" and passes "more" or "no" to the paused music itself."""
    monkeypatch.setattr(hf_mod.take_break, "running_activity", lambda: "music" if waiting else "breathe")
    monkeypatch.setattr(hf_mod.take_break, "music_waiting_for_choice", lambda: waiting)
    stops: list[str] = []
    picks: list[str] = []
    monkeypatch.setattr(hf_mod.take_break, "stop_running_break", lambda source, text="": stops.append(text) or True)
    monkeypatch.setattr(hf_mod.take_break, "choose_after_pause", lambda choice: picks.append(choice) or True)

    hf_mod.HuggingFaceRealtimeHandler._apply_break_words(words)

    assert stops == stopped
    assert picks == chosen


def test_words_do_nothing_when_no_break_runs(monkeypatch: Any) -> None:
    """Outside a break, "stop" is left to the model."""
    monkeypatch.setattr(hf_mod.take_break, "running_activity", lambda: None)
    stop = MagicMock()
    monkeypatch.setattr(hf_mod.take_break, "stop_running_break", stop)

    hf_mod.HuggingFaceRealtimeHandler._apply_break_words("Stop.")

    stop.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("respond", "replies"), [(True, 1), (False, 0)])
async def test_break_end_adds_a_note_and_asks_for_a_reply_unless_stopped(respond: bool, replies: int) -> None:
    """A finished break makes Reachy speak; after a stop, the person's own "stop" already got a reply."""
    handler = _plain_handler()
    create = AsyncMock()
    handler.connection = SimpleNamespace(conversation=SimpleNamespace(item=SimpleNamespace(create=create)))

    await handler._on_break_ended({"note": "The music is over. Say: 'All done.'", "respond": respond})

    item = create.await_args.kwargs["item"]
    assert item["role"] == "user"
    assert item["content"][0]["text"].startswith("(App note, not said by the person: The music is over.")
    assert handler._pending_responses.qsize() == replies


@pytest.mark.asyncio
async def test_paused_music_goes_on_when_no_words_arrive(monkeypatch: Any) -> None:
    """A sound that pauses the music but never gets a transcript does not leave the music paused."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("input_audio_buffer.speech_started"), _FakeEvent("input_audio_buffer.speech_stopped")),
    )
    monkeypatch.setattr(hf_mod, "MUSIC_RESUME_WITHOUT_WORDS_S", 0.0)
    monkeypatch.setattr(hf_mod.take_break, "pause_music_for_speech", lambda: True)
    monkeypatch.setattr(hf_mod.take_break, "music_waiting_for_choice", lambda: True)
    chosen: list[str] = []
    monkeypatch.setattr(hf_mod.take_break, "choose_after_pause", lambda choice: chosen.append(choice) or True)
    monkeypatch.setattr(handler, "_send_app_note", AsyncMock())

    await handler._run_realtime_session()
    assert handler._music_resume_task is not None
    await handler._music_resume_task

    assert chosen == ["more_now"]


@pytest.mark.asyncio
async def test_break_end_reply_waits_while_someone_is_talking() -> None:
    """While a person talks, the reply is held; their words get the server's own reply, a wordless sound gets ours."""
    handler = _plain_handler()
    handler.connection = SimpleNamespace(conversation=SimpleNamespace(item=SimpleNamespace(create=AsyncMock())))
    handler._user_turn_open = True

    await handler._on_break_ended({"note": "The breathing is over.", "respond": True})
    assert handler._pending_responses.qsize() == 0

    await handler._release_held_break_end_reply(person_said_words=False)
    assert handler._pending_responses.get_nowait() == {"_tag": "break_end"}


@pytest.mark.asyncio
async def test_a_silent_reply_to_the_end_of_a_break_is_asked_for_once_more(monkeypatch: Any) -> None:
    """The speech server's rules can leave the reply after a break empty; the app asks once more, out loud."""
    handler = _session_handler(monkeypatch, (_FakeEvent("response.created"), _FakeEvent("response.done")))
    handler._sent_tag = "break_end"  # the response.create just sent was the one asked for at the end of a break
    notes: list[str] = []
    requests: list[dict[str, Any]] = []

    async def note(text: str) -> None:
        notes.append(text)

    async def create(**kwargs: Any) -> None:
        requests.append(kwargs)

    monkeypatch.setattr(handler, "_send_app_note", note)
    monkeypatch.setattr(handler, "_safe_response_create", create)

    await handler._run_realtime_session()

    assert notes == ["Say your reply out loud now."] and requests == [{"_tag": "break_end_retry"}]


@pytest.mark.asyncio
async def test_break_end_reply_after_someone_talked_comes_after_their_reply() -> None:
    """If the person said words, their reply comes first; the held reply is asked for after it, not dropped."""
    handler = _plain_handler()
    handler.connection = SimpleNamespace(conversation=SimpleNamespace(item=SimpleNamespace(create=AsyncMock())))
    handler._user_turn_open = True
    await handler._on_break_ended({"note": "The breathing is over.", "respond": True})

    await handler._release_held_break_end_reply(person_said_words=True)

    assert handler._pending_responses.qsize() == 0 and handler._break_end_after_reply is True


@pytest.mark.asyncio
async def test_a_cancelled_break_end_reply_waits_for_the_person(monkeypatch: Any) -> None:
    """The server cancels the reply before it reports the new speech; the app holds it instead of retrying."""
    handler = _session_handler(
        monkeypatch,
        (_FakeEvent("response.created"), _FakeEvent("response.done", response=SimpleNamespace(status="cancelled"))),
    )
    handler._sent_tag = "break_end"
    requests: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> None:
        requests.append(kwargs)

    monkeypatch.setattr(handler, "_safe_response_create", create)
    monkeypatch.setattr(hf_mod, "BREAK_END_HOLD_MAX_S", 60.0)

    await handler._run_realtime_session()

    assert requests == [] and handler._break_end_reply_held is True
    assert handler._hold_timer is not None
    handler._hold_timer.cancel()
