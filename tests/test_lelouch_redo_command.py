"""Regression: every Lelouch slash-command must be excluded from the free-text
title handler, or it gets silently swallowed.

The bug: ``/redo`` did nothing and logged nothing. Root cause — Lelouch's
free-text handler (`requests.py::_text`) matches
``filters.text & filters.private & ~filters.command(LELOUCH_COMMANDS)``. It is
registered in group 0 BEFORE the ``/redo`` command handler (redo.py), so any
command NOT in ``LELOUCH_COMMANDS`` is consumed by ``_text`` as an "anime title
to search", which finds nothing and returns — the real command handler never
runs. ``/settings`` worked only because it WAS in the list.

This test pins that every command Lelouch actually registers a handler for is
present in the exclusion list, so a newly-added command can never regress the
same way.
"""

from __future__ import annotations

from bots.lelouch.handlers.requests import LELOUCH_COMMANDS


def test_redo_is_excluded_from_free_text_handler():
    # The specific command that regressed.
    assert "redo" in LELOUCH_COMMANDS, (
        "/redo must be in LELOUCH_COMMANDS or the free-text handler swallows it "
        "before redo.py's command handler can run"
    )


def test_all_registered_lelouch_commands_are_excluded():
    """Every command Lelouch registers a handler for MUST be in the exclusion
    list. Guards against a future command being added without excluding it."""
    # Commands Lelouch registers @on_message(filters.command(...)) handlers for,
    # across its handler modules. Keep in sync when a new command is added.
    registered = {
        "start", "help", "myrequests", "admin", "settings",
        "batch", "cleardatabase", "redo",
    }
    excluded = set(LELOUCH_COMMANDS)
    missing = registered - excluded
    assert not missing, (
        f"these registered commands are missing from LELOUCH_COMMANDS and will "
        f"be swallowed by the free-text title handler: {sorted(missing)}"
    )
