from typing import Any
from unittest.mock import MagicMock
from collections.abc import Iterator

import pytest

from talk_with_reachy import choices, study_log, day_center
from talk_with_reachy.tools.core_tools import ToolDependencies
from talk_with_reachy.tools.asked_to_repeat import AskedToRepeat


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Capture study events, with no known speaker and a fresh repeat count."""
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(study_log, "record_event", lambda name, **fields: events.append((name, fields)))
    monkeypatch.setattr(day_center, "current_speaker", lambda *_: (None, ""))
    choices.repeats.reset()
    yield events
    choices.repeats.reset()


async def _ask(**kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = await AskedToRepeat()(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()), **kwargs
    )
    return result


@pytest.mark.asyncio
async def test_a_misheard_answer_is_taken_as_the_choice_it_sounds_like(
    recorded: list[tuple[str, dict[str, Any]]],
) -> None:
    """'Grace with you' (heard on the robot for 'breathe with you') picks breathing; 'Upstairs' picks upset."""
    result = await _ask(heard="Grace with you.", choices=["breathing", "music"])
    assert result["they_chose"] == "breathing" and result["say"] == "Breathing. Okay."

    result = await _ask(heard="Upstairs.", choices=["sick", "upset"])
    assert result["they_chose"] == "upset"
    assert [name for name, _ in recorded] == ["answer_matched", "answer_matched"]


@pytest.mark.asyncio
async def test_the_question_is_asked_once_more_and_then_reachy_waits(
    recorded: list[tuple[str, dict[str, Any]]],
) -> None:
    """'Yes' picks neither choice: one short re-ask, then 'I will wait here', and no third ask."""
    first = await _ask(heard="Yes.", choices=["breathing", "music"])
    second = await _ask(heard="Yes.", choices=["breathe with me", "listen to music"])  # same question, other words
    third = await _ask(heard="Yeah.", choices=["breathing", "music"])

    assert first["say"] == "Which one: breathing, or music?"
    assert second["say"].startswith("I will wait here.") and "breathe with me" in second["say"]
    assert third["say"].startswith("I will wait here.")
    assert "Please say it again" not in str([first, second, third])
    assert [name for name, _ in recorded] == [
        "asked_to_repeat",
        "asked_to_repeat",
        "question_given_up",
        "asked_to_repeat",
        "question_given_up",
    ]


@pytest.mark.asyncio
async def test_without_choices_it_uses_reachys_last_question(recorded: list[tuple[str, dict[str, Any]]]) -> None:
    """When the model leaves out the choices, they come from Reachy's latest question."""
    choices.reachy_said("I am sorry you do not feel well. Sick, or upset?")

    result = await _ask(heard="I am feel sick.")

    assert result["they_chose"] == "sick"


@pytest.mark.asyncio
async def test_with_no_question_to_repeat_reachy_waits(recorded: list[tuple[str, dict[str, Any]]]) -> None:
    """Nonsense with no two-choice question gets 'I will wait here' at once."""
    choices.reachy_said("Okay.")

    result = await _ask(heard="banana tree")

    assert result["say"] == "I will wait here. You can also ask staff."
