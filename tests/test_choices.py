import pytest

from talk_with_reachy import choices


@pytest.mark.parametrize(
    ("heard", "options", "expected"),
    [
        ("Grace with you.", ["breathing", "music"], "breathing"),  # heard on the robot for "breathe with you"
        ("Breathe with you", ["breathe with me", "listen to music"], "breathe with me"),
        ("music please", ["breathing", "music"], "music"),
        ("breath", ["breathing", "music"], "breathing"),
        ("Yes.", ["breathing", "music"], None),
        ("Okay", ["breathing", "music"], None),
        ("Upstairs", ["sick", "upset"], "upset"),  # heard in a test for "upset"
        ("I am feel sick.", ["sick", "upset"], "sick"),
        ("Not good.", ["good", "not good"], "not good"),
        ("Good.", ["good", "not good"], "good"),
        ("I'm not well", ["good", "not good"], "not good"),
        ("Something else?", ["a break", "something else"], "something else"),
        ("What?", ["talk with me", "hear what I can do"], None),
        ("What you can do?", ["talk with me", "hear what I can do"], "hear what I can do"),
        ("Another one", ["one more break", "help from staff"], "one more break"),
        ("help", ["one more break", "help from staff"], "help from staff"),
        ("Slides", ["swings", "slides"], "slides"),
        ("I like both", ["swings", "slides"], None),
    ],
)
def test_answers_are_matched_to_the_choice_they_name_or_sound_like(
    heard: str, options: list[str], expected: str | None
) -> None:
    """One clear choice is found; 'yes', both, or neither gives None."""
    assert choices.match(heard, options) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Breathing, or music?", ["breathing", "music"]),
        ("I am sorry you do not feel well. Sick, or upset?", ["sick", "upset"]),
        ("Do you want to breathe with me, or listen to music?", ["breathe with me", "listen to music"]),
        (
            "Hi, I am Reachy. Do you want to talk with me, or hear what I can do?",
            ["talk with me", "hear what I can do"],
        ),
        ("Okay. We stopped.", []),
    ],
)
def test_the_two_choices_are_found_in_reachys_question(question: str, expected: list[str]) -> None:
    """The last 'A, or B?' question gives its two choices."""
    assert choices.split_choices(question) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("Stop.", True),
        ("stop please", True),
        ("No, stop!", True),
        ("Reachy stop", True),
        ("Can we stop now?", True),
        ("no more music", True),
        ("Don't stop", False),
        ("Oh my god!", False),
        ("Then what's up?", False),
        ("I saw a bus stop near my house and then we walked to the park", False),  # long: not a request
    ],
)
def test_stop_is_heard_only_as_a_short_request(words: str, expected: bool) -> None:
    """Short stop requests count; "don't stop" and long sentences do not."""
    assert choices.is_stop(words) is expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [("More.", "more"), ("Yes.", "more"), ("Okay", "more"), ("Play music", "more"), ("No.", "stop"),
     ("I'm done", "stop"), ("Stop", "stop"), ("Oh my god!", None), ("um", None)],
)  # fmt: skip
def test_answers_to_more_music_or_stop(words: str, expected: str | None) -> None:
    """'Yes' or 'okay' means more and 'no' means stop; other words are left to the model."""
    assert choices.music_answer(words) == expected


def test_repeats_are_counted_per_question_in_a_row() -> None:
    """The same question in other words counts on; another question, or a long gap, starts over."""
    counter = choices.RepeatCounter()

    assert counter.unclear(["breathing", "music"], now=0) == 1
    assert counter.unclear(["breathe with me", "listen to music"], now=5) == 2
    assert counter.unclear(["sick", "upset"], now=6) == 1
    assert counter.unclear(["sick", "upset"], now=6 + choices.REPEAT_WINDOW_S + 1) == 1
