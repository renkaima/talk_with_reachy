from typing import Any
from unittest.mock import MagicMock
from collections.abc import Iterator

import pytest

from talk_with_reachy import choices, study_log, day_center
from talk_with_reachy.tools import take_break
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
    assert result["they_chose"] == "breathe" and result["say"].startswith("Breathing. Okay. Watch my head")
    assert "call take_break with activity 'breathe'" in result["next"]  # the step goes on, not just the echo

    result = await _ask(heard="Upstairs.", choices=["sick", "upset"])
    assert result["they_chose"] == "upset"
    assert result["say"] == "Upset. Okay. I am sorry you are upset. Breathing, or music?"
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
    assert third["say"] == "I am here."  # not the whole wait sentence again
    assert "Please say it again" not in str([first, second, third])
    assert [name for name, _ in recorded] == [
        "asked_to_repeat",
        "asked_to_repeat",
        "question_given_up",
        "asked_to_repeat",
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


@pytest.mark.asyncio
async def test_no_turns_down_both_and_yes_means_more_music(
    recorded: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """'No' to 'Breathing, or music?' ends the asking; 'Yes' to 'More music, or stop?' plays more."""
    picked: list[str] = []
    monkeypatch.setattr(take_break, "choose_after_pause", lambda choice: picked.append(choice) or True)

    declined = await _ask(heard="No.", choices=["breathing", "music"])
    more = await _ask(heard="Yes.", choices=["more music", "stop"])

    assert declined["say"] == "Okay. I am here if you need me."
    assert more == {"they_chose": "more", "say": "More music. Okay."} and picked == ["more"]


@pytest.mark.asyncio
async def test_a_question_asked_again_without_the_tool_still_counts(
    recorded: list[tuple[str, dict[str, Any]]],
) -> None:
    """If the model re-asks on its own, the next unclear answer already ends the asking."""
    choices.reachy_said("Breathing, or music?")
    choices.reachy_said("Breathing, or music?")

    result = await _ask(heard="Yes.", choices=["breathing", "music"])

    assert result["say"].startswith("I will wait here.")


@pytest.mark.asyncio
async def test_help_or_an_activity_named_after_another_question_does_what_it_says(
    recorded: list[tuple[str, dict[str, Any]]],
) -> None:
    """'I need help' or 'music please' after 'Sick, or upset?' is not asked again."""
    staff = await _ask(heard="I need help.", choices=["sick", "upset"])
    music = await _ask(heard="music please", choices=["a break", "talk"])
    unsure = await _ask(heard="I don't know.", choices=["sick", "upset"])

    assert staff["say"] == "Okay. Please go to a staff member, or wave to one."
    assert music["they_chose"] == "music" and music["say"].startswith("Music. Okay.")
    assert unsure["say"].startswith("Okay. I will wait here.") and "asked_to_repeat" not in [n for n, _ in recorded]


@pytest.mark.asyncio
async def test_hurt_sick_or_scared_always_goes_to_staff(
    recorded: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """'I feel sick' after 'Breathing, or music?' gets the staff sentence and stops any running break."""
    stops: list[str] = []
    monkeypatch.setattr(take_break, "stop_running_break", lambda source, words="": stops.append(words) or True)

    result = await _ask(heard="I feel sick.", choices=["breathing", "music"])

    assert result["say"] == "Please tell a staff member now. This is important." and stops == ["I feel sick."]


@pytest.mark.asyncio
async def test_asking_the_question_again_later_starts_a_fresh_count(
    recorded: list[tuple[str, dict[str, Any]]],
) -> None:
    """After giving up, the same question asked again after other talk gets one re-ask again, not 'I am here.'."""
    choices.reachy_said("Breathing, or music?")
    await _ask(heard="Yes.", choices=["breathing", "music"])
    choices.reachy_said("Which one: breathing, or music?")
    await _ask(heard="Yes.", choices=["breathing", "music"])  # gives up
    choices.reachy_said("I will wait here. You can say breathing, or music, any time. You can also ask staff.")
    choices.reachy_said("Okay. Tell me about your day, or ask me a question.")
    choices.reachy_said("Breathing, or music?")

    result = await _ask(heard="Yes.", choices=["breathing", "music"])

    assert result["say"] == "Which one: breathing, or music?"


@pytest.mark.asyncio
async def test_yes_to_the_greeting_means_talk(recorded: list[tuple[str, dict[str, Any]]]) -> None:
    """'Yes' to 'Do you want to talk with me, or hear what I can do?' starts a chat, with no re-ask."""
    result = await _ask(heard="Yes.", choices=["talk with me", "hear what I can do"])

    assert result["they_chose"] == "talk"
    assert result["say"] == "Okay. Tell me about your day, or ask me a question."
    assert [name for name, _ in recorded] == ["answer_matched"]
