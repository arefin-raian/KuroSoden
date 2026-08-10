"""Regression: two entries that share a title must not clobber each other's
thumbnail render output.

The bug: a cour split (e.g. "The Case Study of Vanitas" S1P1 + S1P2) produces two
EntryData with the SAME franchise title. ``render_thumbnail`` derived its asset
dir + output file from the title alone (``assets_<title>/`` + ``thumb_<title>.webp``),
so the second entry overwrote the first's logo/poster/webp — both entry cards then
showed a single, identical thumbnail. ``variant_key`` disambiguates the paths.
"""

from __future__ import annotations

from types import SimpleNamespace

from kurosoden.shared.senku_thumbnail_adapter import _entry_variant_key


def _entry(index, season, part):
    return SimpleNamespace(index=index, season_number=season, season_part=part,
                           anilist_id=170087)  # same id on purpose (cour split)


def test_cour_split_entries_get_distinct_variant_keys():
    """S1P1 and S1P2 of one franchise (same title, same anilist id) must differ."""
    e1 = _entry(1, 1, None)   # Season 1 (cour 1, no part marker)
    e2 = _entry(2, 1, 2)      # Season 1 Part 2
    k1 = _entry_variant_key(e1)
    k2 = _entry_variant_key(e2)
    assert k1 != k2, f"cour split collided: {k1!r} == {k2!r}"


def test_variant_key_is_stable_for_same_entry():
    e = _entry(2, 1, 2)
    assert _entry_variant_key(e) == _entry_variant_key(e)


def test_variant_key_filesystem_safe():
    """The key is spliced into a path, so it must contain no separators/spaces."""
    k = _entry_variant_key(_entry(3, 2, 1))
    assert "/" not in k and "\\" not in k and " " not in k and k


def test_render_path_suffix_separates_same_title():
    """The safe_name suffix logic (mirrored from thumbnail_service) yields a
    different asset dir per variant even when the title is identical."""
    def safe(title, variant_key):
        safe_name = "".join(
            c if c.isalnum() or c in " -_" else "_" for c in title)[:40]
        if variant_key not in (None, ""):
            vk = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in str(variant_key))[:24]
            safe_name = f"{safe_name}_{vk}" if safe_name else vk
        return f"assets_{safe_name}"

    title = "The Case Study of Vanitas"
    d1 = safe(title, _entry_variant_key(_entry(1, 1, None)))
    d2 = safe(title, _entry_variant_key(_entry(2, 1, 2)))
    assert d1 != d2, f"same asset dir for both cours: {d1}"
