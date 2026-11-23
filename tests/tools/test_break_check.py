from typing import Any
from unittest.mock import MagicMock

import pytest

from talk_with_reachy import study_log, day_center
from talk_with_reachy.tools import break_check as break_check_module
from talk_with_reachy.tools.core_tools import ToolDependencies
from talk_with_reachy.tools.break_check import BreakCheck


@pytest.fixture(autouse=True)
def _fresh_breaks() -> Any:
    """Each test starts with no saved answers and no break waiting for one."""
    break_check_module.answered.clear()
    break_check_module.awaiting = None
    yield
    break_check_module.answered.clear()
    break_check_module.awaiting = None


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


@pytest.mark.asyncio
async def test_no_answer_is_saved_when_nobody_answers_after_a_break(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished break with no answer in time gets answer "no answer"; an answered one gets nothing more."""
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(break_check_module, "NO_ANSWER_AFTER_S", 0.0)
    break_check_module.answered.add("answered1")

    await break_check_module.record_no_answer_later("silent1")
    await break_check_module.record_no_answer_later("answered1")

    assert recorded == [("break_check", {"break_id": "silent1", "answer": "no answer", "source": "timeout"})]


@pytest.mark.asyncio
async def test_the_answer_the_app_heard_is_saved_once_if_the_model_does_not_save_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After 'Not good.' the model may skip break_check: the app saves what it heard, and a late call is ignored."""
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("P01", "Sam"))
    monkeypatch.setattr(break_check_module, "SAVE_HEARD_ANSWER_AFTER_S", 0.0)
    break_check_module.expect_answer("b1")
    assert break_check_module.waiting_break_id() == "b1"

    await break_check_module.record_heard_answer_later("b1", "not good", "P01")
    late = await BreakCheck()(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()), break_id="b1", answer="not good"
    )

    assert recorded == [
        ("break_check", {"break_id": "b1", "answer": "not good", "source": "app", "speaker_id": "P01"})
    ]
    assert late["say"] == "One more break, or help from staff?"  # Reachy still says the right next sentence
    assert break_check_module.waiting_break_id() is None


@pytest.mark.asyncio
async def test_the_app_does_not_save_an_answer_the_model_already_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the model calls break_check in time, the app's copy is dropped; a missing break_id uses the waiting one."""
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: recorded.append((name, fields)))
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: ("P01", "Sam"))
    monkeypatch.setattr(break_check_module, "SAVE_HEARD_ANSWER_AFTER_S", 0.0)
    break_check_module.expect_answer("b2")

    await BreakCheck()(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()), answer="good")
    await break_check_module.record_heard_answer_later("b2", "good", "P01")

    assert recorded == [("break_check", {"break_id": "b2", "answer": "good", "speaker_id": "P01"})]
