from __future__ import annotations

from types import SimpleNamespace


def _release_key(row):
    return (
        row.season if row.season is not None else 10_000,
        row.season_part if row.season_part is not None else 0,
        row.episode if row.episode is not None else 0,
    )


def test_release_order_sorts_part_then_episode():
    rows = [
        SimpleNamespace(season=1, season_part=None, episode=3),
        SimpleNamespace(season=1, season_part=None, episode=1),
        SimpleNamespace(season=1, season_part=2, episode=2),
        SimpleNamespace(season=1, season_part=None, episode=2),
        SimpleNamespace(season=1, season_part=2, episode=1),
    ]

    ordered = sorted(rows, key=_release_key)

    assert [
        (r.season, r.season_part, r.episode) for r in ordered
    ] == [
        (1, None, 1),
        (1, None, 2),
        (1, None, 3),
        (1, 2, 1),
        (1, 2, 2),
    ]
