"""Franchise season/part grouping — regression tests for the part-labeling fix.

A season-less "… Part 2" title (no explicit "Season N") must be grouped as the
NEXT PART of the previous season, not spawned as its own sequential season. The
Vanitas case ("The Case Study of Vanitas" + "… Part 2") used to mislabel Part 2
as S02P2, which broke the multi-part file split (all files piled onto S1).
"""

from __future__ import annotations

from nekofetch.services.franchise_flow import FranchiseFlowService
from nekofetch.sources.telegram.anilist import FranchiseEntry


def _entry(aid, title, eps, year, fmt="TV"):
    return FranchiseEntry(
        anilist_id=aid, format=fmt, english_title=title,
        episodes=eps, start_date={"year": year, "month": 1, "day": 1},
    )


def _build(entries):
    svc = FranchiseFlowService.__new__(FranchiseFlowService)
    walk = {i: e for i, e in enumerate(entries, start=1)}
    return svc._build_from_franchise_entries(walk, {}, "doc", "Root")


def _seasons(mapping):
    from nekofetch.domain.enums import ContentKind
    return [(e.season_number, e.season_part, e.episodes)
            for e in mapping.entries if e.kind == ContentKind.SEASON]


def test_part_two_without_season_number_groups_as_same_season():
    # Vanitas: base + "Part 2" → S1 P1 + S1 P2 (NOT S1 + S2P2).
    m = _build([
        _entry(1, "The Case Study of Vanitas", 12, 2021),
        _entry(2, "The Case Study of Vanitas Part 2", 12, 2022),
    ])
    assert _seasons(m) == [(1, 1, 12), (1, 2, 12)]


def test_explicit_season_grouping_still_correct():
    # AoT: explicit seasons + parts + Final Season parts.
    m = _build([
        _entry(10, "Attack on Titan", 25, 2013),
        _entry(11, "Attack on Titan Season 2", 12, 2017),
        _entry(12, "Attack on Titan Season 3", 12, 2018),
        _entry(13, "Attack on Titan Season 3 Part 2", 10, 2019),
        _entry(14, "Attack on Titan The Final Season", 16, 2020),
        _entry(15, "Attack on Titan The Final Season Part 2", 12, 2022),
    ])
    assert _seasons(m) == [
        (1, None, 25), (2, None, 12),
        (3, 1, 12), (3, 2, 10),
        (4, 1, 16), (4, 2, 12),
    ]


def test_unrelated_part_two_does_not_merge_across_titles():
    # A "Part 2" whose base title differs from the previous entry must NOT be
    # swallowed into the prior season.
    m = _build([
        _entry(1, "Show Alpha", 12, 2020),
        _entry(2, "Show Beta Part 2", 12, 2021),
    ])
    assert _seasons(m) == [(1, None, 12), (2, 2, 12)]


def test_three_part_season_groups_together():
    m = _build([
        _entry(1, "Split Cour", 12, 2020),
        _entry(2, "Split Cour Part 2", 12, 2021),
        _entry(3, "Split Cour Part 3", 12, 2022),
    ])
    assert _seasons(m) == [(1, 1, 12), (1, 2, 12), (1, 3, 12)]


def test_multi_episode_ona_is_a_season_not_an_extra():
    # Takopi-style: a lone multi-episode ONA is the show itself → Season 1,
    # matching the wizard's watch-order confirm (previously the posted guide
    # said "ONA 1" while the wizard said "Season 1").
    m = _build([
        _entry(100, "Takopi's Original Sin", 12, 2025, fmt="ONA"),
    ])
    assert _seasons(m) == [(1, None, 12)]
    assert len(m.entries) == 1


def test_single_episode_ona_stays_an_extra():
    from nekofetch.domain.enums import ContentKind

    m = _build([
        _entry(101, "One-Shot ONA Special", 1, 2024, fmt="ONA"),
    ])
    assert _seasons(m) == []
    assert all(e.kind != ContentKind.SEASON for e in m.entries)
    assert m.entries[0].kind == ContentKind.SPECIAL


def test_ona_plus_tv_season_orders_ona_first_then_season():
    # A multi-episode ONA alongside a TV season: both are seasons, in air order.
    m = _build([
        _entry(200, "Original Sin ONA", 12, 2025, fmt="ONA"),
        _entry(201, "Show TV Season", 12, 2026, fmt="TV"),
    ])
    assert _seasons(m) == [(1, None, 12), (2, None, 12)]
