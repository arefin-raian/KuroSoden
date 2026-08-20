"""The torrent-mapping card keyboard is a single, STABLE button set.

The rebuilt card (owner request) drops the four overlapping buttons and the
``with_fix`` branching that corrupted the keyboard on a Back round-trip. It now
renders the SAME rows every paint: Full Mapping, Toggle Entry + Edit Mapping,
Confirm, Back. Full Mapping is a URL button when a Telegraph page exists, else an
inline callback — so it is ALWAYS a real button, never a caption hyperlink.
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

    # Row 1: the external mapping page as a real URL button.
    assert len(rows[0]) == 1
    assert rows[0][0].url == "https://telegra.ph/vanitas-map"
    assert rows[0][0].callback_data is None

    # Row 2: Toggle Entry (render-only 'list' sentinel — never auto-toggles) + Edit.
    assert [b.callback_data for b in rows[1]] == [
        "staff|rtmaptgl|REQ-1|list", "staff|rtmapedit|REQ-1",
    ]
    # Row 3: Confirm. Row 4: Back.
    assert [b.callback_data for b in rows[2]] == ["staff|rtmapok|REQ-1"]
    assert [b.callback_data for b in rows[3]] == ["staff|rdetail|REQ-1"]


def test_no_mapping_url_still_gives_a_full_mapping_button():
    # Without a Telegraph page, Full Mapping becomes an INLINE callback button —
    # it must never disappear / collapse into a caption hyperlink.
    kb = _torrent_map_kb(_L, "REQ-1", "")
    rows = kb.inline_keyboard
    assert len(rows) == 4
    assert rows[0][0].url is None
    assert rows[0][0].callback_data == "staff|rtmapfull|REQ-1|0"


def test_keyboard_is_stable_regardless_of_with_fix():
    # The Back round-trip bug: with_fix is now ignored, so the keyboard is
    # identical whether or not it's passed — a round-trip reproduces the card.
    a = _torrent_map_kb(_L, "REQ-1", "https://telegra.ph/map", with_fix=True)
    b = _torrent_map_kb(_L, "REQ-1", "https://telegra.ph/map", with_fix=False)
    assert _flat(a) == _flat(b)


def test_toggle_entry_uses_list_sentinel_not_index_zero():
    # Opening Toggle Entry must NOT carry idx=0 (which used to flip Season 1 on
    # open). It carries the render-only 'list' sentinel.
    kb = _torrent_map_kb(_L, "REQ-1", "")
    toggle = next(b for b in _flat(kb) if b[1] and "rtmaptgl" in b[1])
    assert toggle[1].endswith("|list")


def test_keyboard_is_never_empty():
    kb = _torrent_map_kb(_L, "REQ-1", "")
    assert len(kb.inline_keyboard) == 4
    assert _flat(kb)  # every row has buttons
