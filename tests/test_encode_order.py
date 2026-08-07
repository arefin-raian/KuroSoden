"""Encode release order — drives the REAL sort contract.

Previously this file re-implemented the sort key locally and asserted against
its own copy, so it stayed green even if ``stages.py`` broke. The sort key is
now a named function (:func:`nekofetch.services.processing.stages.release_key`)
that :class:`EncodeStage` itself uses — this test sorts through it.
"""

from __future__ import annotations

from types import SimpleNamespace

from nekofetch.services.processing.stages import release_key


def test_release_order_sorts_part_then_episode():
    rows = [
        SimpleNamespace(season=1, season_part=None, episode=3),
        SimpleNamespace(season=1, season_part=None, episode=1),
        SimpleNamespace(season=1, season_part=2, episode=2),
        SimpleNamespace(season=1, season_part=None, episode=2),
        SimpleNamespace(season=1, season_part=2, episode=1),
    ]

    ordered = sorted(rows, key=release_key)

    assert [
        (r.season, r.season_part, r.episode) for r in ordered
    ] == [
        (1, None, 1),
        (1, None, 2),
        (1, None, 3),
        (1, 2, 1),
        (1, 2, 2),
    ]


def test_release_order_matches_encodestage_usage():
    """The stage sorts ``ctx.files`` through the same named key this test uses."""
    import inspect

    from nekofetch.services.processing import stages

    source = inspect.getsource(stages.EncodeStage.process)
    assert "ctx.files.sort(key=release_key)" in source
