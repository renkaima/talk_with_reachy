"""Check how closely Reachy's own words and timing followed the Day Center rules, from the study transcripts.

For each transcript file, and for all files together, it reports Reachy's sentence length, the share of
sentences over 12 words, the Flesch-Kincaid grade level (textstat), how many of Reachy's questions were
two-choice questions versus yes/no questions, the hard words Reachy used (words of two or more syllables
that are not on the Dale-Chall list of about 3,000 familiar words), and how often Reachy said "Please say it again" (a count of
recognition failures that does not depend on the model logging asked_to_repeat). It also reports the median
time from the end of a person's speech to the start of Reachy's reply, to check how long Reachy really waits,
and Reachy's median speaking rate in words per minute (utterances that someone interrupted are left out).
Questions that look like yes/no questions are written to plain_language_flags.csv in the current folder, for
a person to review. Run it on a Mac, with uv:

    uv run --no-project --with "textstat==0.7.8" python deploy/plain_language_report.py ~/talk_with_reachy_data/transcripts

(textstat 0.7.8 counts syllables with the cmudict package; later versions need an extra NLTK download.)

The yes/no check is a simple heuristic (a question that starts with a verb such as "do" or "are" and has no
"or"); read the flagged lines before reporting them.
"""

import re
import csv
import sys
import json
import statistics
from pathlib import Path
from datetime import datetime

import textstat


MAX_WORDS = 12
YES_NO_START = re.compile(
    r"^(do|does|did|are|is|was|were|can|could|will|would|should|have|has|shall|may)\b", re.IGNORECASE
)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def reachy_lines(path: Path) -> list[str]:
    """Reachy's utterances in one transcript file, in order."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") == "utterance" and record.get("speaker") == "reachy" and record.get("text"):
            lines.append(str(record["text"]))
    return lines


def reply_gaps(path: Path) -> list[float]:
    """Seconds from the end of each person utterance to the start of the Reachy utterance right after it."""
    utterances = [
        record
        for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if record.get("type") == "utterance"
    ]
    return [
        (datetime.fromisoformat(after["start"]) - datetime.fromisoformat(before["end"])).total_seconds()
        for before, after in zip(utterances, utterances[1:])
        if before.get("speaker") != "reachy"
        and after.get("speaker") == "reachy"
        and before.get("end")
        and after.get("start")
    ]


def speaking_rates(path: Path) -> list[float]:
    """Words per minute of each Reachy utterance that played to the end."""
    rates = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") != "utterance" or record.get("speaker") != "reachy" or record.get("interrupted"):
            continue
        if not (record.get("start") and record.get("end") and record.get("text")):
            continue
        seconds = (datetime.fromisoformat(record["end"]) - datetime.fromisoformat(record["start"])).total_seconds()
        if seconds > 0:
            rates.append(len(str(record["text"]).split()) / seconds * 60)
    return rates


def median_seconds(values: list[float]) -> float | None:
    """Median, rounded, or None when the transcripts have no start and end times."""
    return round(statistics.median(values), 2) if values else None


def summarize(texts: list[str]) -> tuple[dict[str, float | int | list[str]], list[str]]:
    """Language measures for a list of Reachy utterances, and the questions that look like yes/no questions."""
    sentences = [sentence for text in texts for sentence in SENTENCE_SPLIT.split(text.strip()) if sentence]
    questions = [sentence for sentence in sentences if sentence.endswith("?")]
    yes_no = [q for q in questions if YES_NO_START.match(q) and not re.search(r"\bor\b", q, re.IGNORECASE)]
    words = [len(sentence.split()) for sentence in sentences]
    measures: dict[str, float | int | list[str]] = {
        "utterances": len(texts),
        "sentences": len(sentences),
        "mean_words_per_sentence": round(sum(words) / len(words), 1) if words else 0,
        "share_over_12_words": round(sum(count > MAX_WORDS for count in words) / len(words), 3) if words else 0,
        "flesch_kincaid_grade": round(textstat.flesch_kincaid_grade(" ".join(texts)), 1) if texts else 0,
        "questions": len(questions),
        "two_choice_questions": sum(bool(re.search(r"\bor\b", q, re.IGNORECASE)) for q in questions),
        "yes_no_questions": len(yes_no),
        "said_please_say_it_again": sum("say it again" in text.lower() for text in texts),
        "hard_words": sorted({word.lower() for word in textstat.difficult_words_list(" ".join(texts))}),
    }
    return measures, yes_no


def main() -> int:
    """Print the measures per transcript and overall, and write the flagged questions to a CSV file."""
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "~/talk_with_reachy_data/transcripts").expanduser()
    files = sorted(folder.glob("*.jsonl"))
    if not files:
        print(f"No transcript files (*.jsonl) in {folder}")
        return 1
    every_text: list[str] = []
    every_gap: list[float] = []
    every_rate: list[float] = []
    flags: list[tuple[str, str]] = []
    for path in files:
        texts = reachy_lines(path)
        gaps = reply_gaps(path)
        rates = speaking_rates(path)
        measures, yes_no = summarize(texts)
        timing = {
            "median_seconds_before_reply": median_seconds(gaps),
            "median_words_per_minute": median_seconds(rates),
        }
        print(path.name, json.dumps({**measures, **timing}))
        every_text.extend(texts)
        every_gap.extend(gaps)
        every_rate.extend(rates)
        flags.extend((path.name, question) for question in yes_no)
    overall, _ = summarize(every_text)
    timing = {
        "median_seconds_before_reply": median_seconds(every_gap),
        "median_words_per_minute": median_seconds(every_rate),
    }
    print("ALL", json.dumps({**overall, **timing}))
    # Written here, not next to the transcripts, so it never joins the study data that uploads to Drive.
    output = Path.cwd() / "plain_language_flags.csv"
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["transcript", "question"])
        writer.writerows(flags)
    print(f"{len(flags)} possible yes/no questions written to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
