"""send_screen must never crash on bad artwork — it falls back to a text card.

The MEDIA_EMPTY crash: a Levi task card's stored artwork URL (franchise
backdrop/banner) resolved to a dead/unfetchable image, Telegram rejected the
``send_photo`` with ``[400 MEDIA_EMPTY]`` (a ``BadRequest`` subclass), and the
uncaught exception took down the whole callback handler
(``_task_cb → _render_detail → send_screen → _send_photo``).

Fix (screens.py): the send-new photo path catches ``BadRequest`` and re-sends the
caption as a plain text card, so a bad image degrades gracefully instead of
crashing the flow. These tests pin that behaviour for both a fresh send and the
edit→send-new fallback path.
"""

from __future__ import annotations

import pytest
from pyrogram.errors import MediaEmpty

from nekofetch.ui import screens
from nekofetch.ui.screens import Screen, send_screen


class _Msg:
    """Stand-in for a sent Pyrogram Message."""

    def __init__(self, kind: str, **kw):
        self.kind = kind
        self.photo = kw.get("photo")
        self.text = kw.get("text")
        self.id = 1

    async def delete(self):
        return None


class _FakeClient:
    """Records send_photo / send_message calls; send_photo raises MEDIA_EMPTY."""

    def __init__(self, *, photo_raises=True):
        self.photo_raises = photo_raises
        self.photo_calls: list[dict] = []
        self.text_calls: list[dict] = []

    async def send_photo(self, chat_id, **kw):
        self.photo_calls.append({"chat_id": chat_id, **kw})
        if self.photo_raises:
            # Construct the real error type without going through RPCError.__init__
            # (which requires a live query object).
            raise MediaEmpty.__new__(MediaEmpty)
        return _Msg("photo", photo=None)

    async def send_message(self, chat_id, text, **kw):
        self.text_calls.append({"chat_id": chat_id, "text": text, **kw})
        return _Msg("text", text=text)


@pytest.mark.asyncio
async def test_media_empty_on_fresh_send_falls_back_to_text():
    client = _FakeClient(photo_raises=True)
    screen = Screen(caption="<b>Frieren</b> — Episode 1", image="https://dead.example/x.jpg")

    msg = await send_screen(client, 555, screen)  # no old_msg → fresh send path

    # It TRIED the photo (with the artwork)...
    assert len(client.photo_calls) == 1
    # ...then, on MEDIA_EMPTY, fell back to a text card carrying the caption.
    assert len(client.text_calls) == 1
    assert "Frieren" in client.text_calls[0]["text"]
    assert msg.kind == "text"  # returned a real message, never raised


@pytest.mark.asyncio
async def test_media_empty_via_edit_then_send_new_falls_back(monkeypatch):
    """When an in-place edit is impossible and the send-new photo is also rejected,
    the whole path still resolves to a text card (and deletes the stale card)."""
    # Force the edit-in-place attempt to report "can't edit" so we reach send-new.
    async def _no_edit(client, old_msg, screen, photo_arg, fitted):
        return None
    monkeypatch.setattr(screens, "_try_edit_in_place", _no_edit)

    client = _FakeClient(photo_raises=True)
    deleted = {"n": 0}

    class _Old(_Msg):
        async def delete(self):
            deleted["n"] += 1

    old = _Old("photo")
    screen = Screen(caption="Vinland Saga", image="https://dead.example/y.jpg")

    msg = await send_screen(client, 777, screen, old_msg=old)

    assert msg.kind == "text"
    assert len(client.text_calls) == 1 and "Vinland Saga" in client.text_calls[0]["text"]
    assert deleted["n"] == 1  # the stale photo card was cleaned up


@pytest.mark.asyncio
async def test_good_artwork_still_sends_photo():
    """Sanity: when the image is fine, we still send a photo (no needless fallback)."""
    client = _FakeClient(photo_raises=False)
    screen = Screen(caption="Monster", image="https://ok.example/z.jpg")

    msg = await send_screen(client, 999, screen)

    assert len(client.photo_calls) == 1 and len(client.text_calls) == 0
    assert msg.kind == "photo"
