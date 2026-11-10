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
    "not good": (
        "not good",
        "bad",
        "not well",
        "no good",
        "not so good",
        "worse",
        "not okay",
        "not fine",
        "not great",
        "sad",
        "tired",
        "don't feel good",
        "don't feel well",
        "dont feel good",
        "dont feel well",
        "not feeling good",
        "not feeling well",
    ),  # fmt: skip
    "break": ("break", "brake", "a break"),
    "something else": ("something else", "something", "else", "nothing"),
    "staff": ("staff", "help", "helper"),
    "one more break": ("one more", "another", "another break", "more break", "break"),
    "talk": ("talk", "talking", "chat"),
    "what you can do": ("what you can do", "what can you do", "can do", "what you do", "hear"),
    "more": ("more", "more music", "again", "go on", "keep going", "continue", "play"),
    "stop": ("stop", "no more", "enough", "done", "finish", "finished"),
}
# A hit right after one of these ("I don't feel good", "not music") does not pick that choice.
NEGATIONS = {"not", "no", "don't", "dont", "never", "isn't", "can't", "cannot", "won't", "didn't", "doesn't", "hate"}
# These never pick one of two choices: "yes" to "breathing, or music?" says neither.
NON_ANSWERS = {"yes", "yeah", "yep", "no", "nope", "okay", "ok", "sure", "maybe", "uh", "um", "hmm", "alright"}
# After "More music, or stop?", these whole answers (without "reachy", "please", "the", "some") mean more.
_MORE = {
    "more", "more music", "yes", "yeah", "yep", "okay", "ok", "sure", "alright", "please", "yes more", "go on",
    "keep going", "continue", "play", "music", "listen to music", "play music", "play more", "more song",
}  # fmt: skip
# Words left out when a choice is turned into keywords.
STOPWORDS = {
    "do", "you", "want", "to", "the", "a", "an", "me", "with", "or", "feel", "feeling", "are", "is", "it",
    "for", "of", "some", "be", "i", "my", "your", "listen", "like", "would", "which", "one", "from",
    "what", "can", "how", "when", "that", "this",
}  # fmt: skip
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
        r"^(do you (want|like|feel|need)( to)?|are you( feeling)?|would you like( to)?|is it)\s+",
        "",
        match.group(1).rsplit(":", 1)[-1].strip(),  # "Which one: breathing", "How do you feel now: good"
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
    hits: dict[str, str] = {}
    for option in options:
        for phrase in sorted(keywords(option), key=len, reverse=True):
            if _contains(text, phrase) and not _negated(text, phrase):
                hits[option] = phrase
                break
    if not hits:
        # Close spellings of a longer keyword ("breath" for "breathe", "musics" for "music"); short words such as
        # "six" for "sick" are listed as aliases instead.
        words = text.split()
        for option in options:
            for i, word in enumerate(words):
                if _negation_before(words, i):
                    continue
                if len(word) >= 5 and any(
                    difflib.SequenceMatcher(None, word, key).ratio() >= 0.8
                    for key in keywords(option)
                    if " " not in key and len(key) >= 5
                ):
                    hits[option] = word
    if len(hits) == 1:
        return next(iter(hits))
    if len(hits) == 2:
        # "not good" contains "good": the longer phrase wins only when it contains the other one.
        (a, pa), (b, pb) = sorted(hits.items(), key=lambda item: -len(item[1]))
        return a if pa != pb and _contains(pa, pb) else None
    return None


def _negation_before(words: list[str], i: int) -> bool:
    """Tell whether one of the four words before ``words[i]`` negates it ("I don't want to breathe").

    "Don't know" is not a negation of what follows: "I don't know, I feel sick" picks sick.
    """
    for j in range(max(0, i - 4), i):
        if words[j] in NEGATIONS and not (j + 1 < len(words) and words[j + 1] in ("know", "think")):
            return True
    return False


def _negated(text: str, phrase: str) -> bool:
    """Tell whether ``phrase`` comes right after a negation, unless the phrase is itself negative."""
    if any(word in NEGATIONS for word in phrase.split()):
        return False
    words = text.split()
    size = len(phrase.split())
    for i in range(len(words) - size + 1):
        if " ".join(words[i : i + size]) == phrase and _negation_before(words, i):
            return True
    return False


# Words that may come before "stop" in a request to stop ("Reachy, can we stop?"). Any other word before it
# ("you can say stop any time", "I don't want to stop", "when does it stop") means it is not a request.
_STOP_LEAD = {
    "please", "reachy", "okay", "ok", "oh", "can", "could", "we", "you", "i", "want", "to", "just", "now", "lets",
    "let's", "and", "so", "yes", "no", "hey",
}  # fmt: skip
# Whole utterances that ask to stop without the word "stop".
_STOP_PHRASES = {
    "no more", "no more music", "no more please", "enough", "that's enough", "thats enough", "enough please",
    "turn it off", "turn off", "turn the music off", "turn off the music", "music off", "i'm done", "im done",
    "i am done", "done", "finished", "i'm finished", "im finished", "all done",
}  # fmt: skip


def is_stop(text: str) -> bool:
    """Tell whether an utterance asks to stop ("stop", "Reachy, stop the music", "I'm done"), not "don't stop"."""
    norm = normalize(text)
    words = norm.split()
    if not words or len(words) > 8:
        return False
    if norm in _STOP_PHRASES:
        return True
    for i, word in enumerate(words):
        if word == "stop":
            return all(lead in _STOP_LEAD for lead in words[:i])
    return False


# Whole requests that do what they say even right after another question (after "reachy", "please", "i want",
# "can i have", "let's" are left out). Only these exact words count: room talk such as "Grace, come here" or
# "we played a game" must not start an activity.
REQUESTS = {
    "staff": {"help", "help me", "i need help", "staff", "help from staff", "i need staff", "get staff", "i need a staff"},
    "music": {"music", "play music", "listen to music", "music please", "some music", "the music"},
    "breathe": {"breathe", "breathing", "breathe with me", "let's breathe", "breathe with you"},
    "move": {"move", "move with me", "the game", "play the game", "moving"},
}  # fmt: skip


def request_in(text: str) -> str | None:
    """Return "staff", "music", "breathe", or "move" when the whole utterance is that request, else None."""
    norm = normalize(text)
    for lead in ("reachy ", "please ", "i want ", "i want to ", "can i have ", "can we ", "let's ", "lets "):
        if norm.startswith(lead):
            norm = norm[len(lead) :]
    norm = norm.removesuffix(" please").removesuffix(" reachy").strip()
    for name, phrases in REQUESTS.items():
        if norm in phrases:
            return name
    return None


# Words that send someone to staff at once (CALM AND SAFE), wherever they come in a conversation.
HEALTH_WORDS = ("sick", "ill", "hurt", "hurts", "pain", "scared", "afraid", "bleeding")


def says_unwell(text: str) -> bool:
    """Tell whether someone says they are hurt, sick, or scared ("I feel sick"), not "I am not sick"."""
    norm = normalize(text)
    return any(_contains(norm, word) and not _negated(norm, word) for word in HEALTH_WORDS)


def is_decline(text: str) -> bool:
    """Tell whether a short answer turns down both choices ("no", "neither", "nothing", "no thanks")."""
    return normalize(text) in {"no", "nope", "no thanks", "no thank you", "neither", "nothing", "none", "neither one"}


def music_answer(text: str) -> str | None:
    """Return "more" or "stop" for a short answer to "More music, or stop?", or None if it is neither."""
    norm = normalize(text)
    if not norm or len(norm.split()) > 6:
        return None
    if is_stop(norm) or norm in {"no", "nope", "no thanks", "no thank you"}:
        return "stop"
    # Only these exact answers mean more: "The music is too loud" or "I don't like this music" are left to the model.
    trimmed = " ".join(word for word in norm.split() if word not in {"reachy", "please", "the", "some"})
    if trimmed in _MORE:
        return "more"
    return None


class RepeatCounter:
    """Count the unclear answers to the same question in a row."""

    def __init__(self) -> None:
        """Start with no question asked."""
        self._key = ""
        self._count = 0
        self._last = 0.0

    @staticmethod
    def key_for(options: list[str]) -> str:
        """Return one key for a question's choices, whatever their order and wording."""
        return "|".join(sorted(canonical(option) for option in options))

    @property
    def current_key(self) -> str:
        """Return the key of the question being counted ("" if none)."""
        return self._key

    def unclear(self, options: list[str], now: float | None = None) -> int:
        """Note one more unclear answer to the question with ``options``; return how many in a row."""
        now = time.monotonic() if now is None else now
        key = self.key_for(options)
        if key != self._key or now - self._last > REPEAT_WINDOW_S:
            self._key, self._count = key, 0
        self._count += 1
        self._last = now
        return self._count

    def reset(self) -> None:
        """Forget the question (it was answered)."""
        self._key, self._count = "", 0

    def counted_recently(self, seconds: float = 20.0) -> bool:
        """Tell whether an unclear answer was counted in the last ``seconds`` (asked_to_repeat just ran)."""
        return bool(self._count) and time.monotonic() - self._last < seconds


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
        key = repeats.key_for(pair)
        if repeats.current_key and key != repeats.current_key:
            repeats.reset()  # another question: its count starts over
        elif len(previous) == 2 and repeats.key_for(previous) == key:
            if "which one" not in text.lower() and not repeats.counted_recently():
                # The model asked the same question again on its own ("Which one" comes from asked_to_repeat,
                # which has already counted that try).
                repeats.unclear(pair)
        elif not _last_reachy_words.lower().startswith(("i will wait here", "i am here")):
            repeats.reset()  # asked again after other talk: a fresh start (not right after giving up)
    _last_reachy_words = text.strip()


def last_question_choices() -> list[str]:
    """Return the two choices of Reachy's latest question, or [] if its latest words had none."""
    return split_choices(_last_reachy_words)
