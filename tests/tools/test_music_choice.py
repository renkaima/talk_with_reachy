from typing import Any
from unittest.mock import MagicMock

import pytest

from talk_with_reachy.tools import take_break
from talk_with_reachy.tools.core_tools import ToolDependencies
from talk_with_reachy.tools.stop_break import StopBreak
from talk_with_reachy.tools.music_choice import MusicChoice


@pytest.mark.asyncio
async def test_music_choice_passes_the_answer_on_and_is_harmless_when_late(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass "more" to the paused music and stop the break on "stop"; a late call (the app already acted) is fine."""
    calls: list[str] = []
    monkeypatch.setattr(take_break, "choose_after_pause", lambda choice: calls.append(choice) or False)
    monkeypatch.setattr(take_break, "stop_running_break", lambda source, words="": calls.append(source) or False)
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())

    result: dict[str, Any] = await MusicChoice()(deps, choice="more")
    assert result == {"saved": "more", "say": "More music. Okay."}
    assert (await MusicChoice()(deps, choice="stop"))["say"] == "Okay. We stopped."
    assert "error" in await MusicChoice()(deps, choice="louder")
    assert calls == ["more", "tool"]


@pytest.mark.asyncio
async def test_stop_break_stops_whatever_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop_break stops the running break and always answers the same way."""
    calls: list[str] = []
    monkeypatch.setattr(take_break, "stop_running_break", lambda source, words="": calls.append(source) or True)

    result = await StopBreak()(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))

    assert result == {"stopped": True, "say": "Okay. We stopped."} and calls == ["tool"]
