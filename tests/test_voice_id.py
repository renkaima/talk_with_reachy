"""Tests for voice identification: matching, automatic voices, enrollment, and commands."""

import json
import time
from pathlib import Path

import numpy as np
import pytest
from study_helpers import SR, FakeEmbedder, tone, basis

from talk_with_reachy import memory, voice_id, voices_cli
from talk_with_reachy.voice_files import people_dir, person_dir, requests_dir


A, B, C, MIX = 1, 2, 3, 9


@pytest.fixture
def ident(tmp_path: Path) -> voice_id.VoiceIdentifier:
    """Build an identifier with three orthogonal voices and one scoring 0.35 against A."""
    mix = list(0.35 * np.array(basis(0)) + np.sqrt(1 - 0.35**2) * np.array(basis(5)))
    embedder = FakeEmbedder({A: basis(0), B: basis(1), C: basis(2), MIX: mix})
    return voice_id.VoiceIdentifier(tmp_path, embedder)


def _request(data_dir: Path, **request: object) -> None:
    folder = requests_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{time.time_ns()}_{request['action']}.json").write_text(json.dumps(request), encoding="utf-8")


def test_without_a_model_every_utterance_is_unknown(tmp_path: Path) -> None:
    """Before the model loads, nothing is identified and the library is untouched."""
    result = voice_id.VoiceIdentifier(tmp_path).assign(tone(A, 3), SR)
    assert (result.speaker_id, result.status) == ("unknown", "model_not_ready")


def test_short_utterances_are_not_identified(ident: voice_id.VoiceIdentifier) -> None:
    """Under a second of speech is too little for a voiceprint."""
    result = ident.assign(tone(A, 0.8), SR)
    assert (result.speaker_id, result.status) == ("unknown", "too_short")
    assert ident.library.voices == {}


def test_a_new_voice_gets_a_label_and_is_recognized_later(ident: voice_id.VoiceIdentifier) -> None:
    """V001 is created once, then matched; a second person becomes V002."""
    first = ident.assign(tone(A, 2.5), SR)
    again = ident.assign(tone(A, 1.2), SR)
    other = ident.assign(tone(B, 2.5), SR)

    assert (first.speaker_id, first.status) == ("V001", "new_voice")
    assert (again.speaker_id, again.status) == ("V001", "matched")
    assert again.match_score == pytest.approx(1.0)
    assert (other.speaker_id, other.status) == ("V002", "new_voice")
    assert ident.library.voices["V001"].count == 2
    saved = json.loads((people_dir(ident.data_dir) / "library.json").read_text())
    assert [v["id"] for v in saved["voices"]] == ["V001", "V002"]


def test_unmatched_but_short_speech_does_not_create_a_voice(ident: voice_id.VoiceIdentifier) -> None:
    """A new voice needs at least two seconds of speech."""
    result = ident.assign(tone(A, 1.5), SR)
    assert (result.speaker_id, result.status) == ("unknown", "uncertain")
    assert ident.library.voices == {}


def test_ambiguous_scores_stay_unknown(ident: voice_id.VoiceIdentifier) -> None:
    """Between the new-voice and match thresholds, the utterance is labeled unknown."""
    ident.assign(tone(A, 2.5), SR)
    result = ident.assign(tone(MIX, 3.0), SR)

    assert (result.speaker_id, result.status) == ("unknown", "uncertain")
    assert result.match_score == pytest.approx(0.35, abs=0.01)
    assert list(ident.library.voices) == ["V001"]


def test_enrollment_registers_a_participant(ident: voice_id.VoiceIdentifier) -> None:
    """After an enroll command, the next speech up to the target becomes P01."""
    _request(ident.data_dir, action="enroll", id="P01", name="Mary", seconds=5)
    assert ident.process_requests() == 1

    statuses = [ident.assign(tone(A, 3), SR).status for _ in range(2)]
    after = ident.assign(tone(A, 2), SR)

    assert statuses == ["enrolling", "enrolled"]
    assert (after.speaker_id, after.status) == ("P01", "matched")
    voice = ident.library.voices["P01"]
    assert (voice.kind, voice.name) == ("enrolled", "Mary")


def test_enrollment_drops_a_clip_from_someone_else(ident: voice_id.VoiceIdentifier) -> None:
    """A minority clip that disagrees with the rest is left out of the voiceprint."""
    _request(ident.data_dir, action="enroll", id="P01", seconds=8)
    ident.process_requests()
    statuses = [ident.assign(tone(key, 3), SR).status for key in (A, A, B)]

    assert statuses == ["enrolling", "enrolling", "enrolled"]

    assert "P01" in ident.library.voices
    assert float(ident.library.voices["P01"].embedding @ np.asarray(basis(0))) == pytest.approx(1.0)


def test_enrolled_people_are_not_absorbed_by_a_new_enrollment(ident: voice_id.VoiceIdentifier) -> None:
    """While P02 is enrolling, a researcher already enrolled as R01 is still recognized as R01."""
    ident.library.register("R01", ident._embedder.embed(tone(B, 1), SR), "Researcher")  # type: ignore[union-attr]
    _request(ident.data_dir, action="enroll", id="P02", seconds=5)
    ident.process_requests()

    researcher = ident.assign(tone(B, 3), SR)
    participant = ident.assign(tone(A, 3), SR)

    assert (researcher.speaker_id, researcher.status) == ("R01", "matched")
    assert (participant.speaker_id, participant.status) == ("P02", "enrolling")


def test_enrollment_expires(ident: voice_id.VoiceIdentifier) -> None:
    """An enrollment nobody completes stops listening."""
    _request(ident.data_dir, action="enroll", id="P01", seconds=30)
    ident.process_requests()
    assert ident._enrollment is not None
    ident._enrollment.expires_mono = time.monotonic() - 1

    result = ident.assign(tone(A, 3), SR)
    assert result.speaker_id == "V001"
    assert "P01" not in ident.library.voices


def test_link_gives_an_automatic_voice_a_participant_id(ident: voice_id.VoiceIdentifier) -> None:
    """V001 becomes P05, keeps its history as an alias, and keeps its memories."""
    ident.assign(tone(A, 3), SR)
    memory.add_memory_fact(person_dir(ident.data_dir, "V001"), "Likes gardening")
    _request(ident.data_dir, action="link", source="V001", target="P05", name="Sam")
    ident.process_requests()

    voice = ident.library.voices["P05"]
    assert (voice.kind, voice.name, voice.aliases) == ("enrolled", "Sam", ["V001"])
    assert [f.text for f in memory.list_memory_facts(person_dir(ident.data_dir, "P05"))] == ["Likes gardening"]
    assert not person_dir(ident.data_dir, "V001").exists()
    assert ident.assign(tone(A, 2), SR).speaker_id == "P05"


def test_link_merges_two_voices_and_never_reuses_a_label(ident: voice_id.VoiceIdentifier) -> None:
    """Merging V002 into V001 combines memories; the next new voice is V003, not V002."""
    ident.assign(tone(A, 3), SR)
    ident.assign(tone(B, 3), SR)
    memory.add_memory_fact(person_dir(ident.data_dir, "V001"), "Has a cat")
    memory.add_memory_fact(person_dir(ident.data_dir, "V002"), "Plays piano")
    _request(ident.data_dir, action="link", source="V002", target="V001")
    ident.process_requests()

    assert list(ident.library.voices) == ["V001"]
    facts = {f.text for f in memory.list_memory_facts(person_dir(ident.data_dir, "V001"))}
    assert facts == {"Has a cat", "Plays piano"}
    assert ident.assign(tone(C, 3), SR).speaker_id == "V003"


def test_delete_removes_voiceprint_and_memories(ident: voice_id.VoiceIdentifier) -> None:
    """Withdrawal: the voice and its memory folder are gone; the command is logged."""
    ident.assign(tone(A, 3), SR)
    memory.add_memory_fact(person_dir(ident.data_dir, "V001"), "Has a cat")
    _request(ident.data_dir, action="delete", id="V001")
    ident.process_requests()

    assert ident.library.voices == {}
    assert not person_dir(ident.data_dir, "V001").exists()
    assert ident.current_speaker() is None
    log = (people_dir(ident.data_dir) / "requests_log.jsonl").read_text().splitlines()
    assert json.loads(log[-1])["result"] == "deleted V001 and its memory"


def test_bad_commands_are_logged_and_cleared(ident: voice_id.VoiceIdentifier) -> None:
    """A broken or invalid command does not block the queue."""
    folder = requests_dir(ident.data_dir)
    folder.mkdir(parents=True)
    (folder / "1_broken.json").write_text("{not json", encoding="utf-8")
    _request(ident.data_dir, action="enroll", id="V009")
    _request(ident.data_dir, action="rename", id="nobody", name="X")

    assert ident.process_requests() == 3
    assert list(folder.glob("*.json")) == []
    results = [
        json.loads(line)["result"]
        for line in (people_dir(ident.data_dir) / "requests_log.jsonl").read_text().splitlines()
    ]
    assert all(r.startswith("error") or "automatic label" in r or "no voice" in r for r in results)


def test_identity_note_names_the_person_and_lists_their_memories(ident: voice_id.VoiceIdentifier) -> None:
    """What Reachy is told depends on whether the voice is enrolled and what it remembers."""
    ident.library.register("P01", ident._embedder.embed(tone(A, 1), SR), "Mary")  # type: ignore[union-attr]
    memory.add_memory_fact(person_dir(ident.data_dir, "P01"), "Likes gardening")
    ident.assign(tone(B, 3), SR)

    named = ident.identity_note("P01")
    new_voice = ident.identity_note("V001")

    assert named is not None and "Mary (ID P01)" in named and "- Likes gardening" in named
    assert new_voice is not None and "not heard before" in new_voice and "no saved memories" in new_voice
    assert ident.identity_note("unknown") is None


def test_current_speaker_expires(ident: voice_id.VoiceIdentifier) -> None:
    """The current speaker is only trusted for a while after they were last heard."""
    ident.assign(tone(A, 3), SR)
    assert ident.current_speaker() == "V001"
    assert ident.current_speaker(max_age_s=-1) is None


def test_cli_queues_a_command_that_the_app_applies(
    ident: voice_id.VoiceIdentifier, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command line only drops a request; the identifier applies it and logs the result."""
    monkeypatch.setenv("TALK_WITH_REACHY_DATA_DIR", str(ident.data_dir))
    monkeypatch.setattr(voices_cli, "WAIT_FOR_APP_S", 0.0)

    assert voices_cli.main(["enroll", "P01", "--name", "Mary", "--seconds", "5"]) == 0
    assert "Queued" in capsys.readouterr().out
    ident.process_requests()
    assert voices_cli._last_result(ident.data_dir) == "listening for 5 s of P01's speech"

    assert voices_cli.main(["enroll", "V001"]) == 2
    assert "automatic label" in capsys.readouterr().out


def test_cli_lists_voices(
    ident: voice_id.VoiceIdentifier, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``list`` shows enrolled voices first."""
    monkeypatch.setenv("TALK_WITH_REACHY_DATA_DIR", str(ident.data_dir))
    ident.assign(tone(B, 3), SR)
    ident.library.register("P01", ident._embedder.embed(tone(A, 1), SR), "Mary")  # type: ignore[union-attr]

    voices_cli.main(["list"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("P01") and "Mary" in lines[1]
    assert lines[2].startswith("V001")
