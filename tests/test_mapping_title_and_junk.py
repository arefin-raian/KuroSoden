"""Supplementary title-match tier + admin junk marking in build_torrent_mapping.

Two owner-requested mapping improvements:
  • When a filename embeds the episode TITLE but no clean number, recover the
    episode number by matching the title (from Jikan) — never overriding a
    numeric match, only filling blanks, and only on an unambiguous hit.
  • A file the admin marks "not an episode" (junk) is forced out of the episode
    stream into the unmatched bucket (the "Season 1 Trailer 1 became E01" fix).
"""

from __future__ import annotations

from nekofetch.domain.enums import ContentKind
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry
from nekofetch.services.torrent_mapping import (
    _match_episodes_by_title,
    build_torrent_mapping,
)


def _season(n, episodes, *, anilist_id=111):
    return MappingEntry(kind=ContentKind.SEASON, season_number=n,
                        episodes=episodes, anilist_id=anilist_id, included=True)


def _franchise(*entries):
    return FranchiseMapping(anime_doc_id="anilist:1", root_title="Show",
                            entries=list(entries))


# ── title-match tier (unit) ─────────────────────────────────────────────────────

def test_title_match_fills_missing_episode_number():
    files = [
        {"index": 1, "name": "Show - The Long Awaited Duel.mkv",
         "episode": None, "kind": "episode"},
        {"index": 2, "name": "Show - A Quiet Morning Alone.mkv",
         "episode": None, "kind": "episode"},
    ]
    titles = {111: [
        {"number": 5, "title": "The Long Awaited Duel"},
        {"number": 6, "title": "A Quiet Morning Alone"},
    ]}
    filled = _match_episodes_by_title(files, titles)
    assert filled == 2
    assert files[0]["episode"] == 5
    assert files[1]["episode"] == 6


def test_title_match_never_overrides_a_numeric_episode():
    files = [{"index": 1, "name": "Show E01 The Long Awaited Duel.mkv",
              "episode": 1, "kind": "episode"}]
    titles = {111: [{"number": 5, "title": "The Long Awaited Duel"}]}
    _match_episodes_by_title(files, titles)
    assert files[0]["episode"] == 1  # numeric match preserved


def test_title_match_skips_ambiguous_and_short_titles():
    files = [
        {"index": 1, "name": "Show - Home.mkv", "episode": None, "kind": "episode"},
        {"index": 2, "name": "Show - Reunion.mkv", "episode": None, "kind": "episode"},
    ]
    titles = {111: [
        {"number": 1, "title": "Home"},        # too short (<6 chars) → skipped
        {"number": 2, "title": "Reunion"},     # unique + long enough
        {"number": 9, "title": "Reunion"},     # duplicate title → ambiguous → dropped
    ]}
    _match_episodes_by_title(files, titles)
    assert files[0]["episode"] is None   # "Home" too short to match
    assert files[1]["episode"] is None   # "Reunion" ambiguous (appears twice)


# ── title-match integrated into build_torrent_mapping ───────────────────────────

def test_build_mapping_uses_title_tier_for_unnumbered_files():
    files = [
        {"index": 1, "name": "Show - Whispers of the Forgotten.mkv",
         "path": "Show/f1.mkv", "episode": None, "season": 1, "kind": "episode",
         "season_explicit": False, "seq": 1},
        {"index": 2, "name": "Show - Embers in the Rain.mkv",
         "path": "Show/f2.mkv", "episode": None, "season": 1, "kind": "episode",
         "season_explicit": False, "seq": 2},
    ]
    franchise = _franchise(_season(1, 2))
    titles = {111: [
        {"number": 1, "title": "Whispers of the Forgotten"},
        {"number": 2, "title": "Embers in the Rain"},
    ]}
    mapping = build_torrent_mapping(files, franchise, episode_titles=titles)
    entry = mapping.entries[0]
    assigned = {fa.episode_number for fa in entry.files}
    assert assigned == {1, 2}


# ── junk marking ────────────────────────────────────────────────────────────────

def test_junk_index_moves_file_to_unmatched():
    files = [
        {"index": 1, "name": "Show S01E01.mkv", "path": "Show/e1.mkv",
         "episode": 1, "season": 1, "kind": "episode", "season_explicit": True, "seq": 1},
        {"index": 2, "name": "Show S01E02.mkv", "path": "Show/e2.mkv",
         "episode": 2, "season": 1, "kind": "episode", "season_explicit": True, "seq": 2},
    ]
    franchise = _franchise(_season(1, 2))

    # Without junk: both are episodes.
    base = build_torrent_mapping([dict(f) for f in files], franchise)
    assert base.entries[0].actual == 2
    assert not base.unmatched

    # Mark file #2 as junk → it leaves the episode stream, lands in unmatched.
    marked = build_torrent_mapping([dict(f) for f in files], franchise,
                                   junk_indices={2})
    assert marked.entries[0].actual == 1
    assert [u.file_index for u in marked.unmatched] == [2]
