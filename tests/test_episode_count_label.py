from __future__ import annotations

from nekofetch.services.main_channel_service import format_episode_summary


def _entries(*rows):
    return [{"kind": kind, "episodes": episodes} for kind, episodes in rows]


def test_season_only():
    assert format_episode_summary(_entries(("season", 25))) == "25"


def test_one_extra_is_singular():
    assert format_episode_summary(_entries(("season", 25), ("ova", 1))) == "25 + 1 extra"


def test_extra_episode_totals_are_summed():
    assert format_episode_summary(_entries(("season", 25), ("ova", 2), ("special", 1))) == "25 + 3 extras"


def test_movies_are_counted_as_entries():
    assert format_episode_summary(_entries(("season", 25), ("movie", 1), ("movie", 1))) == "25 + 2 movies"


def test_composite_summary():
    assert format_episode_summary(_entries(("season", 25), ("ova", 2), ("special", 1), ("movie", 1), ("movie", 1))) == "25 + 3 extras + 2 movies"


def test_tv_special_counts_as_extra():
    assert format_episode_summary(_entries(("season", 25), ("tv_special", 2))) == "25 + 2 extras"


def test_empty_franchise_is_unknown():
    assert format_episode_summary([]) == "—"
