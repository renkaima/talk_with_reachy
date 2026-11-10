from typing import Any
from unittest.mock import MagicMock

import pytest

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools.core_tools import ToolDependencies
from talk_with_reachy.tools.break_check import BreakCheck


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "say"),
    [
        ("good", "Come back any time you need me."),
        ("don't know", "Come back any time you need me."),
        ("not good", "One more break, or help from staff?"),
    ],
)
async def test_break_check_saves_the_answer_and_offers_more_help_after_not_good(
    monkeypatch: pytest.MonkeyPatch, answer: str, say: str
) -> None:
    """Every answer is logged with the break; after "not good", Reachy offers one more break or staff."""
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("P01", "Sam"))
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())

    result = await BreakCheck()(deps, break_id="a1", answer=answer)

    assert result == {"saved": answer, "say": say}
    assert recorded == [("break_check", {"break_id": "a1", "answer": answer, "speaker_id": "P01"})]
