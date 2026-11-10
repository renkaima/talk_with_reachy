from typing import Any
from unittest.mock import MagicMock

import pytest

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools.core_tools import ToolDependencies
from talk_with_reachy.tools.asked_to_repeat import AskedToRepeat


@pytest.mark.asyncio
async def test_asked_to_repeat_logs_what_was_heard_without_scripting_a_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event keeps what Reachy heard; the result gives no fixed "Please say it again" that could loop."""
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: (None, ""))

    result = await AskedToRepeat()(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()), heard="Upstairs."
    )

    assert recorded == [("asked_to_repeat", {"heard": "Upstairs.", "speaker_id": None})]
    assert "say" not in result and "same two choices" in result["next"]
