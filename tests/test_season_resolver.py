"""Unit tests for the season-mapping cascade (auto → titles → absolute → manual)."""

from __future__ import annotations

from nekofetch.domain.enums import ContentKind
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry
from nekofetch.services.season_resolver import (
    resolve_seasons,
    _absolute_to_season,
    _title_from_name,
)


def _franchise(*season_counts: tuple[int, int]) -> FranchiseMapping:
    """Build a franchise from (season_number, episodes) pairs."""
    entries = [
        MappingEntry(
            anilist_id=1000 + sn, kind=ContentKind.SEASON,
            season_number=sn, title=f"Season {sn}", episodes=eps,
        )
        for sn, eps in season_counts
    ]
    return FranchiseMapping(anime_doc_id="doc", root_title="Show", entries=entries)


def _file(index: int, name: str, season: int, episode, explicit: bool, kind="episode"):
    return {
        "index": index, "name": name, "season": season, "episode": episode,
        "season_explicit": explicit, "kind": kind,
    }


def test_explicit_season_is_trusted_verbatim():
    fr = _franchise((1, 12), (2, 12))
    files = [_file(0, "Show S02E03 [1080p].mkv", 2, 3, True)]
    res = resolve_seasons(files, fr)
    assert res.all_resolved
    a = res.assignments[0]
    assert (a.season, a.episode, a.method) == (2, 3, "explicit")


def test_absolute_number_folds_across_seasons():
    # Seasons of 25, 25, 22. Absolute ep 60 → S3E10.
    assert _absolute_to_season(60, {1: 25, 2: 25, 3: 22}) == (3, 10)
    assert _absolute_to_season(25, {1: 25, 2: 25, 3: 22}) == (1, 25)
    assert _absolute_to_season(26, {1: 25, 2: 25, 3: 22}) == (2, 1)


def test_absolute_fold_unknown_boundary_returns_none():
    assert _absolute_to_season(60, {1: 0, 2: 25}) is None
    # overruns known total
    assert _absolute_to_season(99, {1: 25, 2: 25}) is None


def test_flat_release_uses_absolute_fold_for_multiseason():
    fr = _franchise((1, 25), (2, 25), (3, 22))
    files = [_file(0, "Attack on Titan - 60 [1080p].mkv", 1, 60, False)]
    res = resolve_seasons(files, fr)
    a = res.assignments[0]
    assert (a.season, a.episode, a.method) == (3, 10, "absolute")


def test_title_match_wins_when_no_explicit_season():
    fr = _franchise((1, 12), (2, 12))
    titles = {
        1: [{"number": 1, "title": "The Beginning"}],
        2: [{"number": 5, "title": "To You, in 2000 Years"}],
    }
    files = [_file(0, "[Grp] Show - 05 - To You, in 2000 Years [1080p].mkv", 1, 5, False)]
    res = resolve_seasons(files, fr, titles_by_season=titles)
    a = res.assignments[0]
    assert (a.season, a.episode, a.method) == (2, 5, "title")


def test_manual_override_beats_everything():
    fr = _franchise((1, 25), (2, 25), (3, 22))
    files = [_file(0, "Attack on Titan - 60 [1080p].mkv", 1, 60, False)]
    res = resolve_seasons(files, fr, overrides={0: 2})
    a = res.assignments[0]
    assert (a.season, a.method) == (2, "manual")


def test_single_season_shortcut():
    fr = _franchise((1, 12))
    files = [_file(0, "Show - 07.mkv", 1, 7, False)]
    res = resolve_seasons(files, fr)
    a = res.assignments[0]
    assert (a.season, a.method) == (1, "single")


def test_unresolved_when_multiseason_and_no_signal():
    # Multi-season, no explicit season, no titles, and an episode number that
    # can't fold (unknown season counts).
    fr = _franchise((1, 0), (2, 0))
    files = [_file(0, "Show - 03.mkv", 1, 3, False)]
    res = resolve_seasons(files, fr)
    assert not res.all_resolved
    assert res.unresolved[0].method == "unresolved"


def test_extras_pass_through():
    fr = _franchise((1, 12))
    files = [_file(0, "Show Movie.mkv", 0, None, False, kind="movie")]
    res = resolve_seasons(files, fr)
    assert res.assignments[0].season == 0
    assert res.all_resolved


def test_title_extraction():
    assert _title_from_name("[Grp] Show - 05 - To You, in 2000 Years [1080p].mkv") \
        == "To You, in 2000 Years"
    assert _title_from_name("Show - 05 [1080p].mkv") == ""
    assert _title_from_name("Show.S01E05.1080p.mkv") == ""
