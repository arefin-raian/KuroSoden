from __future__ import annotations

from bots.lelouch import screens


def _buttons(screen):
    return [
        (button.text, button.callback_data)
        for row in screen.keyboard.inline_keyboard
        for button in row
    ]


def test_owner_panel_has_redo_and_clear_database_row():
    screen = screens.admin_panel(
        mode="normal", requests_open=True, total=2, working=1, is_owner=True,
    )
    buttons = _buttons(screen)
    assert ("🗂 Manage REQ/WRK", "mg|reqs|0") in buttons
    assert ("🔁 Redo", "redo|new") in buttons
    assert ("🧨 Clear Database", "lelouch|dbclear") in buttons


def test_non_owner_panel_hides_redo_and_clear_database():
    screen = screens.admin_panel(
        mode="normal", requests_open=True, total=2, working=1, is_owner=False,
    )
    buttons = _buttons(screen)
    assert ("🗂 Manage REQ/WRK", "mg|reqs|0") in buttons
    assert not any(data in {"redo|new", "lelouch|dbclear"} for _, data in buttons)
