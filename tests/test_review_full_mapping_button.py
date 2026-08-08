"""The torrent-mapping card's Full Mapping control is a URL button, not a link.

The operator asked for the Telegraph full-mapping page to open from a BUTTON —
one extra row at the top of the confirm card — instead of a hyperlink buried in
the caption. ``_torrent_map_kb`` builds that keyboard: Full Mapping (URL) first,
then Confirm / Details / Toggle (/ Fix), then Back.
"""

from __future__ import annotations

from nekofetch.bots.admin.handlers.review import _torrent_map_kb


def _L(key):
    """Stand-in localizer — labels don't matter, structure does."""
    return key


def _flat(kb):
    return [(b.text, b.callback_data, b.url)
            for row in kb.inline_keyboard for b in row]


def test_full_mapping_is_the_first_row_url_button():
    kb = _torrent_map_kb(_L, "REQ-1", "https://telegra.ph/vanitas-map")
    rows = kb.inline_keyboard
    assert len(rows) == 4

    # Row 1: the external mapping page as a real URL button (the ask).
    assert len(rows[0]) == 1
    assert rows[0][0].url == "https://telegra.ph/vanitas-map"
    assert rows[0][0].callback_data is None

    # Rows below: the usual action buttons, unchanged.
    assert [b.callback_data for b in rows[1]] == [
        "staff|rtmapok|REQ-1", "staff|rtmapdet|REQ-1|0",
    ]
    assert [b.callback_data for b in rows[2]] == [
        "staff|rtmaptgl|REQ-1|0", "staff|rtmapfix|REQ-1",
    ]
    assert [b.callback_data for b in rows[3]] == ["staff|rdetail|REQ-1"]


def test_no_mapping_url_means_no_full_mapping_row():
    kb = _torrent_map_kb(_L, "REQ-1", "")
    rows = kb.inline_keyboard
    assert len(rows) == 3  # Confirm/Details, Toggle/Fix, Back — no URL row
    assert all(b.url is None for row in rows for b in row)


def test_overview_keyboard_drops_the_fix_button():
    """The overview return (rtmapov) never offers Fix — only the first card does."""
    kb = _torrent_map_kb(_L, "REQ-1", "https://telegra.ph/map", with_fix=False)
    rows = kb.inline_keyboard
    assert [b.callback_data for b in rows[2]] == ["staff|rtmaptgl|REQ-1|0"]
    # … and the URL button is still there when a map exists.
    assert rows[0][0].url == "https://telegra.ph/map"


def test_keyboard_is_never_empty():
    kb = _torrent_map_kb(_L, "REQ-1", "")
    assert len(kb.inline_keyboard) == 3
    assert _flat(kb)  # every row has buttons
