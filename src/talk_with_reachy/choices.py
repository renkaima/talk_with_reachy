"""Understand short answers to Reachy's two-choice questions in code, and limit how often a question is asked.

The speech service often mishears one-word answers: in tests "upset" came back as "Upstairs" and "breathe with
you" as "Grace with you". The language model did not reliably take such an answer as the choice it sounds like,
and it did not count how often it had asked the same question, so it asked again and again. This module does
both in code: the asked_to_repeat tool uses it to match what was heard against the two choices, and to tell the
model to stop asking after the second try. The realtime handler uses it to hear "stop", "more", and "no" during
a break without waiting for the model.
"""

from __future__ import annotations
import re
import time
import difflib


# Other words for each choice, including mishearings seen in tests. A choice matches when the answer contains
# one of these words or phrases.
ALIASES: dict[str, tuple[str, ...]] = {
    "breathing": ("breathe", "breathing", "breath", "breeze", "brief", "grace", "bread", "breed", "breathe with"),
    "music": ("music", "song", "songs", "listen", "musik", "muse"),
    "move": ("move", "moving", "movement", "game", "dance", "moves"),
    "sick": ("sick", "six", "seek", "thick", "ill"),
    "upset": ("upset", "upstairs", "up set", "a set", "sad", "angry", "mad"),
    "good": ("good", "fine", "great", "better", "well"),
    "not good": ("not good", "bad", "not well", "no good", "not so good", "worse", "not okay", "not fine"),
    "break": ("break", "brake", "a break"),
    "something else": ("something else", "something", "else", "nothing"),
    "staff": ("staff", "help", "helper"),
    "one more break": ("one more", "another", "another break", "more break", "break"),
    "talk": ("talk", "talking", "chat"),
    "what you can do": ("what you can do", "what can you do", "can do", "what you do", "hear"),
    "more": ("more", "more music", "again", "go on", "keep going", "continue", "play"),
    "stop": ("stop", "no more", "enough", "done", "finish", "finished"),
}
# These never pick one of two choices: "yes" to "breathing, or music?" says neither.
NON_ANSWERS = {"yes", "yeah", "yep", "no", "nope", "okay", "ok", "sure", "maybe", "uh", "um", "hmm", "alright"}
# After "More music, or stop?", these mean more.
_YES = {"yes", "yeah", "yep", "okay", "ok", "sure", "alright", "please", "yes please", "okay please"}
# Words left out when a choice is turned into keywords.
STOPWORDS = {
    "do", "you", "want", "to", "the", "a", "an", "me", "with", "or", "feel", "feeling", "are", "is", "it",
    "for", "of", "some", "be", "i", "my", "your", "listen", "like", "would", "which", "one", "from",
    "what", "can", "how", "when", "that", "this",
}  # fmt: skip
_NEGATIONS = {"dont", "don't", "not", "never"}
# A question asked again within this time counts as the same try; after it the count starts over.
REPEAT_WINDOW_S = 180.0
# The second unclear answer ends the asking.
MAX_TRIES = 2


def normalize(text: str) -> str:
    """Lower-case words with no punctuation, single spaces."""
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z']){re.escape(phrase)}(?![a-z'])", text) is not None


def _names(norm: str) -> list[str]:
    """Known choices named in ``norm``, without those inside a longer one ("good" in "not good")."""
    names = [
        name
        for name, aliases in ALIASES.items()
        if name == norm or _contains(norm, name) or any(_contains(norm, alias) for alias in aliases)
    ]
    return [name for name in names if not any(name != other and _contains(other, name) for other in names)]


def canonical(choice: str) -> str:
    """One name for a choice however it is worded ("breathe with me" and "breathing" are both "breathing")."""
    norm = normalize(choice)
    return "+".join(sorted(_names(norm))) or norm


def keywords(choice: str) -> set[str]:
    """Words and phrases that mean ``choice``.

    A choice that names a known choice ("listen to music", "not good") gets that choice's words. A known choice
    inside a longer one ("good" in "not good", "break" in "one more break") is left out, so the two choices of
    a question do not share words. Any other choice ("swings") is its own content words.
    """
    norm = normalize(choice)
    names = _names(norm)
    if not names:
        return {word for word in norm.split() if word not in STOPWORDS}
    found: set[str] = set()
    for name in names:
        found.add(name)
        found.update(ALIASES[name])
    return found


def split_choices(question: str) -> list[str]:
    """Return the two choices of an "A, or B?" question (the last one in ``question``), or [] if there is none."""
    sentences = re.findall(r"[^.?!]*\?", question)
    if not sentences:
        return []
    match = re.search(r"^(.*?),?\s+or\s+(.+?)\?$", sentences[-1].strip(), flags=re.IGNORECASE)
    if not match:
        return []
    first = re.sub(
        r"^(do you (want|like|feel|need)( to)?|are you( feeling)?|would you like( to)?|which one:?|is it)\s+",
        "",
        match.group(1).strip(),
        flags=re.IGNORECASE,
    )
    found = [first.strip(" ,:"), match.group(2).strip(" ,:")]
    # "Sick, or upset?" gives "sick": the choices are said back in the middle of a sentence.
    return [choice[:1].lower() + choice[1:] if not choice.startswith("I ") else choice for choice in found]


def match(heard: str, options: list[str]) -> str | None:
    """Return the one option that ``heard`` names or sounds like, or None if it names neither or both."""
    text = normalize(heard)
    if not text or text in NON_ANSWERS:
        return None
    hits: dict[str, int] = {}
    for option in options:
        for phrase in keywords(option):
            if _contains(text, phrase):
                hits[option] = max(hits.get(option, 0), len(phrase))
    if not hits:
        # Close spellings of a keyword ("breath" for "breathe", "musics" for "music").
        for option in options:
            for word in text.split():
                if any(
                    difflib.SequenceMatcher(None, word, key).ratio() >= 0.8
                    for key in keywords(option)
                    if " " not in key
                ):
                    hits[option] = 1
    if len(hits) == 1:
        return next(iter(hits))
    if len(hits) == 2:
        # "not good" also contains "good": the longer phrase wins.
        (a, la), (b, lb) = sorted(hits.items(), key=lambda item: -item[1])
        return a if la > lb else None
    return None


def is_stop(text: str) -> bool:
    """Tell whether a short utterance asks to stop ("stop", "stop please", "no more", "enough"), not "don't stop"."""
    norm = normalize(text)
    words = norm.split()
    if not words or len(words) > 8:
        return False
    for phrase in ("no more", "that's enough", "thats enough", "enough", "turn it off"):
        if _contains(norm, phrase):
            return True
    for i, word in enumerate(words):
        if word in ("stop", "stopped"):
            before = " ".join(words[max(0, i - 2) : i])
            return not any(_contains(before, neg) for neg in _NEGATIONS)
    return False


def is_decline(text: str) -> bool:
    """Tell whether a short answer turns down both choices ("no", "neither", "nothing", "no thanks")."""
    return normalize(text) in {"no", "nope", "no thanks", "no thank you", "neither", "nothing", "none", "neither one"}


def music_answer(text: str) -> str | None:
    """Return "more" or "stop" for a short answer to "More music, or stop?", or None if it is neither."""
    norm = normalize(text)
    if not norm or len(norm.split()) > 6:
        return None
    if is_stop(norm) or norm in {"no", "nope", "no thanks", "no thank you", "done", "i'm done", "im done"}:
        return "stop"
    if norm in _YES or match(norm, ["more"]) == "more" or _contains(norm, "music"):
        return "more"
    return None


class RepeatCounter:
    """Count the unclear answers to the same question in a row."""

    def __init__(self) -> None:
        """Start with no question asked."""
        self._key = ""
        self._count = 0
        self._last = 0.0

    def unclear(self, options: list[str], now: float | None = None) -> int:
        """Note one more unclear answer to the question with ``options``; return how many in a row."""
        now = time.monotonic() if now is None else now
        key = "|".join(sorted(canonical(option) for option in options))
        if key != self._key or now - self._last > REPEAT_WINDOW_S:
            self._key, self._count = key, 0
        self._count += 1
        self._last = now
        return self._count

    def reset(self) -> None:
        """Forget the question (it was answered)."""
        self._key, self._count = "", 0


# What Reachy said last, so asked_to_repeat can find the choices when the model does not pass them.
_last_reachy_words = ""
repeats = RepeatCounter()


def reachy_said(text: str) -> None:
    """Remember Reachy's latest words (called by the realtime handler).

    Asking the same two choices again right away counts as a try, even when the model did not call
    asked_to_repeat, so the asking still ends after the second try. A new question starts a new count.
    """
    global _last_reachy_words
    if not text.strip():
        return
    pair, previous = split_choices(text), split_choices(_last_reachy_words)
    if len(pair) == 2:
        if len(previous) == 2 and {canonical(c) for c in pair} == {canonical(c) for c in previous}:
            repeats.unclear(pair)
        else:
            repeats.reset()
    _last_reachy_words = text.strip()


def last_question_choices() -> list[str]:
    """Return the two choices of Reachy's latest question, or [] if its latest words had none."""
    return split_choices(_last_reachy_words)
