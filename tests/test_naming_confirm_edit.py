"""Regression: the filename/caption confirm gate must actually receive edits,
and Edit must offer a Cancel button.

Two bugs this pins:

1. **Group collision (edits silently lost).** The text consumer was registered
   in ``group=13`` — but the reused review flow ALSO registers a broad
   ``filters.text`` handler in group=13 (the DDL-link reply), and review is
   registered first. Pyrogram runs only the first matching handler per group, so
   review's handler ran, found no URL, returned, and the naming consumer never
   fired. Sending an edited filename did nothing. It now lives in group=16.

2. **No Cancel affordance.** Tapping Edit put the card into "awaiting a value"
   mode but left the Use it / Edit buttons; there was no way to back out. Edit
   now swaps to a single Cancel button, and Cancel restores the choice row.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyrogram.types import CallbackQuery

from kurosoden.bots.levi.handlers import naming_confirm_handler as nch


class _Capture:
    """Records what register() wires, without a real Pyrogram client."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, object]] = []      # (group, callback)
        self.callbacks: list[tuple[object, object]] = []  # (filter, callback)

    def on_message(self, flt=None, group=0):
        def deco(fn):
            self.messages.append((group, fn))
            return fn
        return deco

    def on_callback_query(self, flt=None, group=0):
        def deco(fn):
            self.callbacks.append((flt, fn))
            return fn
        return deco


def _register():
    cap = _Capture()
    nch.register(cap, SimpleNamespace(redis=None))
    return cap


def test_text_consumer_is_off_the_review_collision_group():
    """The single text consumer must NOT sit in group 13 (review owns it)."""
    cap = _register()
    text_groups = [g for g, _ in cap.messages]
    assert text_groups == [16], (
        f"expected exactly one text consumer in group 16, got groups {text_groups}"
    )
    assert 13 not in text_groups, "group 13 collides with review's DDL-link handler"


@pytest.mark.asyncio
async def test_all_three_callbacks_route_including_cancel():
    """nmuse / nmedit / nmcancel must each be matched by a registered filter —
    the new Cancel button must not be a dead tap."""
    cap = _register()

    async def _matches(data: str) -> bool:
        cq = CallbackQuery(client=None, id="1", from_user=None, chat_instance="x")
        cq.data = data
        for flt, _fn in cap.callbacks:
            cq.matches = None
            try:
                if await flt(None, cq):
                    return True
            except Exception:
                continue
        return False

    assert await _matches("levi|nmuse|42|name")
    assert await _matches("levi|nmedit|42|name")
    assert await _matches("levi|nmcancel|42|name"), "Cancel button is a dead tap"
    # A foreign namespace must NOT match — proves the filters aren't rubber stamps.
    assert not await _matches("staff|rsource|REQ-1|ddl")


def test_keyboards_carry_the_expected_callbacks():
    """The choice row offers Use it + Edit; the editing row offers only Cancel."""
    choice = nch._choice_kb(42, "name")
    labels = [b.text for row in choice.inline_keyboard for b in row]
    datas = [b.callback_data for row in choice.inline_keyboard for b in row]
    assert any("Use it" in x for x in labels)
    assert any("Edit" in x for x in labels)
    assert "levi|nmuse|42|name" in datas
    assert "levi|nmedit|42|name" in datas

    cancel = nch._cancel_kb(42, "name")
    cdatas = [b.callback_data for row in cancel.inline_keyboard for b in row]
    assert cdatas == ["levi|nmcancel|42|name"], "editing row must be a lone Cancel"
