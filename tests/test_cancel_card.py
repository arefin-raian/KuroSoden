"""Cancel/delete → Levi card finalization.

When a request Levi is actively downloading is cancelled or deleted, the live
Levi progress card must be EDITED IN PLACE to a "cancelled" card (not left frozen
while a different bot DMs a stray message), then the downloader is auto-advanced
to their next task. Covers the shared ``finalize_cancelled_card`` helper both
cancel paths call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bots.levi.handlers import progress_monitor as pm
from kurosoden.shared import levi_voice as V


def test_task_cancelled_voice_has_title_and_house_style():
    text = V.task_cancelled("Akudama Drive")
    assert "Cancelled" in text
    assert "Akudama Drive" in text
    assert V.ICON in text  # house-style icon prefix


@pytest.mark.asyncio
async def test_finalize_cancelled_card_edits_levi_card_and_advances(monkeypatch):
    edited: dict = {}

    class _Levi:
        async def edit_message_text(self, chat, mid, text, **k):
            edited.update(chat=chat, mid=mid, text=text)

    class _Redis:
        def __init__(self):
            self.store = {"nf:job:9:progressmsg": json.dumps({"chat": 555, "msg": 42})}

        async def get(self, key):
            return self.store.get(key)

    advanced: dict = {}

    async def fake_advance(container, admin_id, code):
        advanced.update(admin_id=admin_id, code=code)

    import kurosoden.shared.handoff as handoff
    monkeypatch.setattr(handoff, "_advance_to_next_task", fake_advance)

    container = SimpleNamespace(
        redis=_Redis(),
        pipeline_manager=SimpleNamespace(levi=_Levi()),
    )
    await pm.finalize_cancelled_card(container, 9, title="Akudama Drive", code="REQ-1081")

    # The live card was edited IN PLACE to a cancelled card...
    assert edited["chat"] == 555 and edited["mid"] == 42
    assert "Cancelled" in edited["text"] and "Akudama Drive" in edited["text"]
    # ...and the downloader auto-advanced (admin_id derived from the card's DM chat).
    assert advanced["admin_id"] == 555 and advanced["code"] == "REQ-1081"


@pytest.mark.asyncio
async def test_finalize_cancelled_card_no_ref_still_advances(monkeypatch):
    """No stored card ref (already wiped / never sent) → skip the edit but still
    auto-advance, and never raise."""
    class _Redis:
        async def get(self, key):
            return None

    advanced: dict = {}

    async def fake_advance(container, admin_id, code):
        advanced.update(admin_id=admin_id, code=code)

    import kurosoden.shared.handoff as handoff
    monkeypatch.setattr(handoff, "_advance_to_next_task", fake_advance)

    container = SimpleNamespace(redis=_Redis(), pipeline_manager=SimpleNamespace(levi=None))
    await pm.finalize_cancelled_card(
        container, 9, title="X", code="REQ-1", admin_id=777,
    )
    assert advanced["admin_id"] == 777 and advanced["code"] == "REQ-1"
