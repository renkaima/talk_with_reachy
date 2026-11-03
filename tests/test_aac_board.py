import json
from pathlib import Path


BOARD = Path(__file__).parents[1] / "deploy" / "aac" / "talk_with_reachy.obf"


def test_aac_board_is_a_complete_open_board_format_grid() -> None:
    """Every grid cell points at a button, and every button has a label and a phrase to speak."""
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    buttons = {button["id"]: button for button in board["buttons"]}
    order = board["grid"]["order"]

    assert board["format"] == "open-board-0.1"
    assert len(order) == board["grid"]["rows"] and all(len(row) == board["grid"]["columns"] for row in order)
    assert sorted(cell for row in order for cell in row) == sorted(buttons)
    assert all(button["label"] and button["vocalization"] for button in buttons.values())
