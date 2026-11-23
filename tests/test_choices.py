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


@pytest.mark.parametrize(
    ("heard", "options", "expected"),
    [
        ("I don't want to breathe", ["breathing", "music"], None),
        ("I don't know, I feel sick", ["sick", "upset"], "sick"),
        ("I don't feel good", ["good", "not good"], "not good"),
    ],
)
def test_negation_is_read_in_context(heard: str, options: list[str], expected: str | None) -> None:
    """A negated choice is not taken; "don't know" does not negate what follows."""
    assert choices.match(heard, options) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("I need help.", "staff"),
        ("Reachy, play music please", "music"),
        ("Breathe with me.", "breathe"),
        ("Grace, come here.", None),  # room talk must not start breathing
        ("We played a game.", None),
        ("helpful", None),
        ("Reachy?", None),
    ],
)
def test_only_whole_requests_start_something_after_another_question(words: str, expected: str | None) -> None:
    """Requests must be the whole utterance, so mishearings and chat do not start an activity."""
    assert choices.request_in(words) == expected


def test_a_name_alone_does_not_resume_the_music() -> None:
    """'Reachy?' to get attention leaves the song paused."""
    assert choices.music_answer("Reachy?") is None


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("I feel sick.", True),
        ("My arm hurts.", True),
        ("I am scared.", True),
        ("I am not sick.", False),
        ("I am fine.", False),
    ],
)
def test_hurt_sick_or_scared_is_heard_unless_negated(words: str, expected: bool) -> None:
    """'I feel sick' goes to staff; 'I am not sick' does not."""
    assert choices.says_unwell(words) is expected


@pytest.mark.parametrize(
    ("words", "level"),
    [
        ("I hurt myself.", "urgent"),
        ("I am scared.", "urgent"),
        ("My head hurts.", "minor"),
        ("I got caught with cold and my nose is runny.", "minor"),
        ("I have a headache and my arm hurts.", "urgent"),  # anything urgent makes it urgent
        ("I am not sick.", None),
        ("I am tired.", None),  # tired stays a reason for a break, not a health complaint
    ],
)
def test_urgent_and_minor_health_words_are_told_apart(words: str, level: str | None) -> None:
    """A cold or a headache gets the calm staff sentence; hurt, sick, or scared gets the urgent one."""
    assert choices.unwell_level(words) == level


@pytest.mark.parametrize(
    ("words", "answer"),
    [
        ("Not good.", "not good"),
        ("Good.", "good"),
        ("Bad.", "not good"),
        ("I don't know.", "don't know"),
        ("Yes.", "unclear"),
    ],
)
def test_answers_to_the_question_after_a_break(words: str, answer: str) -> None:
    """The app reads the answer to 'How do you feel now: good, or not good?' the way the model should."""
    assert choices.after_break_answer(words) == answer
