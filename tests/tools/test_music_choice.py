from typing import Any
from unittest.mock import MagicMock

import pytest

from talk_with_reachy.tools import take_break
from talk_with_reachy.tools.core_tools import ToolDependencies
from talk_with_reachy.tools.music_choice import MusicChoice


@pytest.mark.asyncio
async def test_music_choice_passes_the_answer_to_the_paused_music(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid choice reaches take_break; without paused music or with another word, it is refused."""
    choices: list[str] = []
    paused = {"value": True}

    def choose(choice: str) -> bool:
        choices.append(choice)
        return paused["value"]

    monkeypatch.setattr(take_break, "choose_after_pause", choose)
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())

    result: dict[str, Any] = await MusicChoice()(deps, choice="more")
    assert result == {"saved": "more", "say": "More music. Okay."}
    assert "error" in await MusicChoice()(deps, choice="louder")
    paused["value"] = False
    assert "error" in await MusicChoice()(deps, choice="stop")
    assert choices == ["more", "stop"]
