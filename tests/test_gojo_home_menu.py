"""Gojo home-menu consistency.

Regression for the reported bug: the ``/start`` menu was missing Index / Change
Index / Stats, which only appeared after bouncing through Settings and back
(the ``gojo|home`` callback built a *different*, fuller keyboard). Both surfaces
now render from the single ``_home_rows`` source of truth, so this pins that the
full owner menu is complete and that non-owners lose the owner-only buttons
(Settings + the post editor).
"""

from __future__ import annotations

import pytest


# Every action the owner must see on the Gojo home menu (both /start and home).
# The old direct "index" slot editor was replaced by the "pe" post editor
# (owner-only), which also covers index editing.
_EXPECTED_ACTIONS = [
    "tasks", "publish", "schedule", "recover", "backup", "change_main",
    "check_updates", "check_banned", "pe", "change_index", "stats",
    "edit_footer", "settings", "help",
]

# Buttons only the owner may see (stripped for staff by _home_rows).
_OWNER_ONLY = ["settings", "pe"]


def _flat(rows):
    return [data for row in rows for (_label, data) in row]


def test_owner_home_menu_is_complete(monkeypatch):
    import kurosoden.bots.gojo.app as gojo_app
    import kurosoden.shared.access_gate as gate

    monkeypatch.setattr(gate, "is_owner", lambda c, o: True)
    flat = _flat(gojo_app._home_rows(object(), object()))
    for action in _EXPECTED_ACTIONS:
        assert any(d == f"gojo|{action}" for d in flat), f"home menu missing '{action}'"


def test_non_owner_loses_owner_only_buttons(monkeypatch):
    import kurosoden.bots.gojo.app as gojo_app
    import kurosoden.shared.access_gate as gate

    monkeypatch.setattr(gate, "is_owner", lambda c, o: False)
    flat = _flat(gojo_app._home_rows(object(), object()))
    for owner_action in _OWNER_ONLY:
        assert not any(d == f"gojo|{owner_action}" for d in flat), \
            f"'{owner_action}' must be owner-only"
    # Everything else still present for staff.
    for action in [a for a in _EXPECTED_ACTIONS if a not in _OWNER_ONLY]:
        assert any(d == f"gojo|{action}" for d in flat), f"home menu missing '{action}'"


def test_schedule_button_targets_real_schedule_view():
    """The Schedule button must route to gojo|schedule, which schedule.py owns
    (matching the /schedule command) rather than the generic tool-panel."""
    import kurosoden.bots.gojo.app as gojo_app

    import kurosoden.shared.access_gate as gate
    orig = gate.is_owner
    gate.is_owner = lambda c, o: True
    try:
        flat = _flat(gojo_app._home_rows(object(), object()))
    finally:
        gate.is_owner = orig
    assert any(d == "gojo|schedule" for d in flat), \
        "Schedule button must emit exactly gojo|schedule"
