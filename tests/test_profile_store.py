from pathlib import Path

import pytest

from talk_with_reachy.config import DEFAULT_PROFILES_DIRECTORY
from talk_with_reachy.profile_store import (
    ProfileFormatError,
    write_profile,
    list_profile_names,
    read_profile_from_directory,
)


def test_profile_document_round_trip(tmp_path: Path) -> None:
    """Profile metadata and Markdown instructions should round-trip losslessly."""
    profile_directory = tmp_path / "guide"

    write_profile(
        "guide",
        profile_directory,
        '# Role\n\nBe concise and say "hello".',
        ["dance", "dance", "", "# disabled", "move_head"],
        voice="Ono_Anna",
        greeting="Open with a \\tiny greeting.",
    )

    profile = read_profile_from_directory("guide", profile_directory)

    assert profile.instructions == '# Role\n\nBe concise and say "hello".'
    assert profile.default_tools == ("dance", "move_head")
    assert profile.voice == "Ono_Anna"
    assert profile.greeting == "Open with a \\tiny greeting."
    assert profile.hidden is False


@pytest.mark.parametrize(
    ("document", "error"),
    [
        ("Legacy instructions only.\n", "expected TOML front matter"),
        ("+++\nschema_version = 1\ndefault_tools = []\n", "missing closing"),
        ("+++\nschema_version = 2\ndefault_tools = []\n+++\nPrompt\n", "Unsupported profile schema"),
        ("+++\nschema_version = 1\ndefault_tools = 'dance'\n+++\nPrompt\n", "expected a list of strings"),
        ("+++\nschema_version = 1\ndefault_tools = []\nvoiec = 'Aiden'\n+++\nPrompt\n", "Unknown profile metadata"),
        ("+++\nschema_version = 1\ndefault_tools = []\n+++\n", "empty instruction body"),
        ("+++\nschema_version = 1\ndefault_tools = []\nturn_silence_ms = 20\n+++\nPrompt\n", "turn_silence_ms"),
        ("+++\nschema_version = 1\ndefault_tools = []\nturn_silence_ms = '1s'\n+++\nPrompt\n", "turn_silence_ms"),
        ("+++\nschema_version = 1\ndefault_tools = []\nspeech_speed = 0.3\n+++\nPrompt\n", "speech_speed"),
        ("+++\nschema_version = 1\ndefault_tools = []\nspeech_speed = 1.5\n+++\nPrompt\n", "speech_speed"),
        ("+++\nschema_version = 1\ndefault_tools = []\nspeech_speed = 'slow'\n+++\nPrompt\n", "speech_speed"),
    ],
)
def test_invalid_profile_document_is_rejected(tmp_path: Path, document: str, error: str) -> None:
    """Malformed or legacy profile documents should fail with a useful error."""
    profile_directory = tmp_path / "invalid"
    profile_directory.mkdir()
    (profile_directory / "profile.md").write_text(document, encoding="utf-8")

    with pytest.raises(ProfileFormatError, match=error):
        read_profile_from_directory("invalid", profile_directory)


def test_day_center_profile_waits_longer_for_the_end_of_a_turn() -> None:
    """The Day Center profile sets a longer end-of-turn silence; profiles that omit it keep the server default."""
    day_center = read_profile_from_directory("day_center", DEFAULT_PROFILES_DIRECTORY / "day_center")
    default = read_profile_from_directory("default", DEFAULT_PROFILES_DIRECTORY / "default")

    assert day_center.turn_silence_ms == 1200
    assert default.turn_silence_ms is None


def test_day_center_profile_speaks_more_slowly_in_the_default_voice() -> None:
    """The Day Center profile plays the service's default voice at 55% speed; other profiles keep the default speed."""
    day_center = read_profile_from_directory("day_center", DEFAULT_PROFILES_DIRECTORY / "day_center")
    default = read_profile_from_directory("default", DEFAULT_PROFILES_DIRECTORY / "default")

    assert (day_center.speech_speed, day_center.voice) == (0.55, None)
    assert (default.speech_speed, default.voice) == (1.0, None)


def test_day_center_profile_has_no_random_head_movements() -> None:
    """Without move_head, the only idle action left in the Day Center profile is staying still."""
    day_center = read_profile_from_directory("day_center", DEFAULT_PROFILES_DIRECTORY / "day_center")

    assert "move_head" not in day_center.default_tools
    assert not {"dance", "play_emotion"} & set(day_center.default_tools)


def test_profile_listing_requires_profile_document(tmp_path: Path) -> None:
    """Directories without profile.md should not be treated as profiles."""
    write_profile("visible", tmp_path / "visible", "Hello.", [])
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    (legacy_directory / "instructions.txt").write_text("Legacy.\n", encoding="utf-8")

    assert list_profile_names(tmp_path) == ["visible"]


def test_bundled_profiles_enable_head_tracking_by_default() -> None:
    """Every bundled personality should start with head tracking available."""
    for profile_name in list_profile_names(DEFAULT_PROFILES_DIRECTORY):
        profile = read_profile_from_directory(profile_name, DEFAULT_PROFILES_DIRECTORY / profile_name)

        assert "head_tracking" in profile.default_tools, profile_name
