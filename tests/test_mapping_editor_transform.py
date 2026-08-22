"""Mapping-editor save-transform: editor layout → build_torrent_mapping inputs.

Covers layout_to_overrides (drag position → episode number, season assignment,
exclude → junk), the new episode_overrides path in build_torrent_mapping, and the
end-to-end apply_layout round-trip.
"""

from __future__ import annotations

from nekofetch.domain.enums import ContentKind
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry
from nekofetch.services.torrent_mapping import TorrentMapping, build_torrent_mapping
from nekofetch.web.mapping_session import apply_layout, layout_to_overrides


def _franchise_s1_s2() -> FranchiseMapping:
    return FranchiseMapping(anime_doc_id="1", root_title="Show", entries=[
        MappingEntry(anilist_id=10, kind=ContentKind.SEASON, season_number=1,
                     season_part=None, title="", episodes=2, included=True),
        MappingEntry(anilist_id=20, kind=ContentKind.SEASON, season_number=2,
                     season_part=None, title="", episodes=2, included=True),
    ])


def _ordered_s1_s2() -> list[dict]:
    of = []
    idx = 0
    for season in (1, 2):
        for ep in (1, 2):
            of.append({
                "index": idx, "name": f"Show S{season:02d}E{ep:02d}",
                "season": season, "episode": ep, "kind": "episode",
                "season_explicit": True, "seq": idx + 1,
                "resolutions": ["1080p"],
            })
            idx += 1
    return of


def _working_set() -> dict:
    m = build_torrent_mapping(_ordered_s1_s2(), _franchise_s1_s2())
    return {"mapping": m.to_dict(), "ordered_files": _ordered_s1_s2(),
            "episode_titles": {}}


# ── layout_to_overrides (pure) ────────────────────────────────────────────────

def test_layout_positions_become_episode_numbers_per_season():
    layout = {"files": [
        {"index": 0, "season": 1, "position": 2},
        {"index": 1, "season": 1, "position": 1},   # dragged ABOVE index 0
        {"index": 2, "season": 2, "position": 1},
        {"index": 3, "season": 2, "position": 2},
    ], "excluded": []}
    season_ov, episode_ov, junk = layout_to_overrides(layout)
    assert junk == set()
    assert season_ov == {0: 1, 1: 1, 2: 2, 3: 2}
    # Within S1, index 1 (position 1) → ep 1; index 0 (position 2) → ep 2.
    assert episode_ov == {1: 1, 0: 2, 2: 1, 3: 2}


def test_excluded_files_become_junk_and_drop_out():
    layout = {"files": [
        {"index": 0, "season": 1, "position": 1},
        {"index": 1, "season": 1, "position": 2},
    ], "excluded": [1]}
    season_ov, episode_ov, junk = layout_to_overrides(layout)
    assert junk == {1}
    # Excluded index 1 gets no season/episode override.
    assert 1 not in season_ov and 1 not in episode_ov
    assert season_ov == {0: 1} and episode_ov == {0: 1}


def test_gaps_and_dupe_positions_are_renumbered_1_to_n():
    layout = {"files": [
        {"index": 0, "season": 1, "position": 5},
        {"index": 1, "season": 1, "position": 5},   # dup
        {"index": 2, "season": 1, "position": 99},
    ], "excluded": []}
    _s, episode_ov, _j = layout_to_overrides(layout)
    # Stable order by (position, index) → 0,1,2 → renumbered 1,2,3 (no collisions).
    assert sorted(episode_ov.values()) == [1, 2, 3]
    assert episode_ov[2] == 3


# ── episode_overrides in build_torrent_mapping ────────────────────────────────

def test_episode_overrides_win_over_filename_number():
    # Reverse S1: file S01E01 (index 0) → ep 2, S01E02 (index 1) → ep 1.
    m = build_torrent_mapping(
        _ordered_s1_s2(), _franchise_s1_s2(),
        episode_overrides={0: 2, 1: 1},
    )
    s1 = next(e for e in m.entries if e.franchise_entry.season_number == 1)
    # The entry still has 2 episodes; the override drove their numbering.
    assert s1.actual == 2


# ── apply_layout (end-to-end) ─────────────────────────────────────────────────

def test_apply_layout_reassign_season_and_exclude():
    ws = _working_set()
    # Move index 2 (originally S02E01) into S01 as ep 3, and exclude index 3.
    layout = {"files": [
        {"index": 0, "season": 1, "position": 1},
        {"index": 1, "season": 1, "position": 2},
        {"index": 2, "season": 1, "position": 3},
    ], "excluded": [3]}
    out = apply_layout(ws, layout)
    m = TorrentMapping.from_dict(out)
    by_season = {e.franchise_entry.season_number: e for e in m.entries
                 if e.franchise_entry.kind == ContentKind.SEASON}
    # S1 now holds 3 files; the excluded one is not an episode anywhere.
    assert by_season[1].actual == 3
    assert by_season[2].actual == 0
