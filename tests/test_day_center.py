from unittest.mock import MagicMock

import pytest

from talk_with_reachy import voice_id, day_center


@pytest.mark.parametrize(("speaker_id", "expected"), [("S01", True), ("P01", False), ("V003", False), (None, False)])
def test_staff_check_only_trusts_a_voice_identified_seconds_ago(
    monkeypatch: pytest.MonkeyPatch, speaker_id: str | None, expected: bool
) -> None:
    """Staff status comes from an S voice ID matched within STAFF_MAX_AGE_S, not the two-minute default."""
    ident = MagicMock()
    ident.current_speaker.return_value = speaker_id
    ident.name_of.return_value = ""
    monkeypatch.setattr(voice_id, "identifier", lambda: ident)

    assert day_center.staff_is_speaking() is expected
    ident.current_speaker.assert_called_once_with(day_center.STAFF_MAX_AGE_S)


def test_nobody_is_staff_when_voice_id_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without voice ID there is no speaker, so staff-only answers stay closed."""
    monkeypatch.setattr(voice_id, "identifier", lambda: None)

    assert day_center.current_speaker() == (None, "")
    assert day_center.staff_is_speaking() is False
