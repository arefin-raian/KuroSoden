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

from bots.lelouch.handlers.redo import _REDO_RESERVED
from bots.lelouch.handlers.requests import LELOUCH_COMMANDS


def test_redo_is_excluded_from_free_text_handler():
    # The specific command that regressed.
    assert "redo" in LELOUCH_COMMANDS, (
        "/redo must be in LELOUCH_COMMANDS or the free-text handler swallows it "
        "before redo.py's command handler can run"
    )


def test_cancel_is_excluded_from_free_text_handlers():
    """``/cancel`` (the redo abort command) must be excluded from BOTH the
    request free-text intake (so it reaches the group-0 /cancel handler instead
    of being eaten as a title) AND redo's own group-3 title intake (so a mid-
    /redo ``/cancel`` isn't searched on AniList — the owner's "it keeps asking
    for a value / it's searching for commands even" bug)."""
    assert "cancel" in LELOUCH_COMMANDS
    assert "cancel" in _REDO_RESERVED


def test_redo_text_intake_excludes_its_own_commands():
    """redo.py's group-3 free-text handler must exclude the commands the owner
    is most likely to type mid-flow, or they get searched as a title."""
    for cmd in ("redo", "cancel", "start", "batch"):
        assert cmd in _REDO_RESERVED, (
            f"/{cmd} missing from _REDO_RESERVED → redo's text handler would "
            f"swallow it as a search title"
        )


def test_all_registered_lelouch_commands_are_excluded():
    """Every command Lelouch registers a handler for MUST be in the exclusion
    list. Guards against a future command being added without excluding it."""
    # Commands Lelouch registers @on_message(filters.command(...)) handlers for,
    # across its handler modules. Keep in sync when a new command is added.
    registered = {
        "start", "help", "myrequests", "admin", "settings",
        "batch", "cleardatabase", "redo", "cancel",
    }
    excluded = set(LELOUCH_COMMANDS)
    missing = registered - excluded
    assert not missing, (
        f"these registered commands are missing from LELOUCH_COMMANDS and will "
        f"be swallowed by the free-text title handler: {sorted(missing)}"
    )
