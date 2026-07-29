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

from nekofetch.services.backup_service import _markup_to_rows, _rows_to_markup
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
    both = BackupImage(source_url="http://cdn/x.jpg",
                       catbox_url="http://cat/x.jpg",
                       telegraph_url="http://tel/x.jpg",
                       imgbb_url="http://imgbb/x.jpg")
    assert both.primary == "http://cat/x.jpg"

    tel_only = BackupImage(source_url="http://cdn/x.jpg",
                           telegraph_url="http://tel/x.jpg",
                           imgbb_url="http://imgbb/x.jpg")
    assert tel_only.primary == "http://tel/x.jpg"

    # ImgBB is the third mirror, preferred over the (possibly-dead) source CDN.
    imgbb_only = BackupImage(source_url="http://cdn/x.jpg",
                             imgbb_url="http://imgbb/x.jpg")
    assert imgbb_only.primary == "http://imgbb/x.jpg"

    src_only = BackupImage(source_url="http://cdn/x.jpg")
    assert src_only.primary == "http://cdn/x.jpg"

    empty = BackupImage(source_url="")
    assert empty.primary is None
