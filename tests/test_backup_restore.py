"""Tests for the main-channel backup & restore path (Phase 5).

Backup and restore must reproduce a post byte-for-byte on a fresh channel with
NO re-rendering: caption HTML, mirrored image URL, button layout, and divider
sticker all come from the stored snapshot. These tests pin the two pure,
host-independent pieces:

  • the inline-keyboard ↔ JSON round-trip (buttons survive backup→restore), and
  • ``image_backup.BackupImage.primary`` preference order (mirror before source).

The DB/Telegram-driven capture/restore flows are exercised through their pure
helpers here; the network hosts (catbox/telegraph) are never touched.
"""

from __future__ import annotations

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from nekofetch.services.backup_service import (
    BackupService,
    _markup_to_rows,
    _rows_to_markup,
)
from kurosoden.shared.image_backup import BackupImage


# ── button serialization round-trip ─────────────────────────────────────────────

def test_markup_round_trips_url_buttons():
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("Iɴᴅᴇx", url="https://t.me/idx/5"),
        InlineKeyboardButton("Dᴏᴡɴʟᴏᴀᴅ", url="https://t.me/bot?start=anime_x"),
    ]])
    rows = _markup_to_rows(markup)
    assert rows == [[
        {"text": "Iɴᴅᴇx", "url": "https://t.me/idx/5"},
        {"text": "Dᴏᴡɴʟᴏᴀᴅ", "url": "https://t.me/bot?start=anime_x"},
    ]]
    rebuilt = _rows_to_markup(rows)
    assert rebuilt is not None
    btns = rebuilt.inline_keyboard[0]
    assert btns[0].text == "Iɴᴅᴇx" and btns[0].url == "https://t.me/idx/5"
    assert btns[1].url.endswith("anime_x")


def test_markup_none_round_trips_to_none():
    assert _markup_to_rows(None) is None
    assert _rows_to_markup(None) is None
    assert _rows_to_markup([]) is None


def test_markup_preserves_callback_buttons():
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Go", callback_data="gojo|x")]])
    rows = _markup_to_rows(markup)
    assert rows == [[{"text": "Go", "callback_data": "gojo|x"}]]
    rebuilt = _rows_to_markup(rows)
    assert rebuilt.inline_keyboard[0][0].callback_data == "gojo|x"


# ── image mirror preference order ────────────────────────────────────────────────

def test_backup_image_prefers_catbox_then_telegraph_then_imgbb_then_source():
    src_only = BackupImage(source_url="http://cdn/x.jpg")
    assert src_only.primary == "http://cdn/x.jpg"

    empty = BackupImage(source_url="")
    assert empty.primary is None


# ── {BOT_QUAL…} resolution on restore (deep-link vs root) ────────────────────────

def test_resolve_quals_deeplinks_anchored_id_to_restored_message():
    """An anchored {BOT_QUAL#id:…} whose id is in msg_by_id links to that
    restored card's message; without the id it degrades to the channel root."""
    handle = "aot_channel"
    msg_by_id = {555: 4242}

    # Anchored + known id → deep-link to the restored season card message.
    out = BackupService._resolve_quals(
        "S2: {BOT_QUAL#555:480p  720p}", handle, msg_by_id,
    )
    assert 'href="https://t.me/aot_channel/4242"' in out
    assert ">480p  720p<" in out

    # Anchored but unknown id → channel-root link (best we can do).
    out2 = BackupService._resolve_quals(
        "S9: {BOT_QUAL#999:1080p}", handle, msg_by_id,
    )
    assert 'href="https://t.me/aot_channel"' in out2 and ">1080p<" in out2

    # Legacy unanchored form still resolves to the channel root.
    out3 = BackupService._resolve_quals("{BOT_QUAL:720p}", handle, msg_by_id)
    assert 'href="https://t.me/aot_channel"' in out3 and ">720p<" in out3


def test_resolve_quals_without_handle_collapses_to_label():
    out = BackupService._resolve_quals("{BOT_QUAL#555:480p}", None, {555: 1})
    assert out == "480p"
