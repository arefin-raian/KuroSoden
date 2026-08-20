"""Franchise filtering: released + canonical only, uniform across sources.

The owner's complaint: Clevatess / The Elusive Samurai showed "2 seasons" when
only ONE is released — a currently-airing (RELEASING) or announced
(NOT_YET_RELEASED) sequel was being counted as a season, and the rating was a
single entry's number instead of the franchise average.

These pin the shared predicate and the AniList ``franchise_totals`` walk (mocked
``_post`` — no network): airing/announced/cancelled/hiatus entries never inflate
the season count, and the average score is computed over the counted entries.
"""

from __future__ import annotations

import pytest

from nekofetch.sources.telegram.anilist import AnilistClient, _is_released


def test_is_released_predicate():
    assert _is_released("FINISHED") is True
    assert _is_released(None) is True          # unknown status kept (sparse sources)
    assert _is_released("RELEASING") is False  # currently airing — the core bug
    assert _is_released("NOT_YET_RELEASED") is False
    assert _is_released("HIATUS") is False
    assert _is_released("CANCELLED") is False


def _media(mid, fmt, status, eps, score, edges=()):
    return {
        "id": mid, "format": fmt, "status": status, "episodes": eps,
        "averageScore": score, "nextAiringEpisode": None,
        "relations": {"edges": [
            {"relationType": rt, "node": {"id": nid, "type": "ANIME",
                                          "format": nfmt, "status": nstatus,
                                          "episodes": neps}}
            for (rt, nid, nfmt, nstatus, neps) in edges
        ]},
    }


@pytest.fixture
def client():
    return AnilistClient()


async def test_airing_sequel_not_counted_as_season(client, monkeypatch):
    # Root S1 FINISHED with a SEQUEL S2 that is RELEASING (Clevatess/Elusive
    # Samurai shape). The airing S2 must NOT be counted → seasons == 1.
    graph = {
        1: _media(1, "TV", "FINISHED", 12, 77,
                  edges=[("SEQUEL", 2, "TV", "RELEASING", 12)]),
        2: _media(2, "TV", "RELEASING", 12, 80),
    }

    async def fake_post(query, variables):
        ids = variables.get("ids") or []
        return {"Page": {"media": [graph[i] for i in ids if i in graph]}}

    monkeypatch.setattr(client, "_post", fake_post)
    client._totals_cache.clear()
    totals = await client.franchise_totals(1)
    assert totals.seasons == 1                      # airing S2 excluded
    # Rating average = only the counted (finished) entry's score, 7.7.
    assert totals.avg_score == pytest.approx(7.7, abs=0.01)


async def test_finished_sequel_is_counted(client, monkeypatch):
    # Control: a FINISHED sequel IS a second season, and the average blends both.
    graph = {
        1: _media(1, "TV", "FINISHED", 12, 70,
                  edges=[("SEQUEL", 2, "TV", "FINISHED", 12)]),
        2: _media(2, "TV", "FINISHED", 12, 90),
    }

    async def fake_post(query, variables):
        ids = variables.get("ids") or []
        return {"Page": {"media": [graph[i] for i in ids if i in graph]}}

    monkeypatch.setattr(client, "_post", fake_post)
    client._totals_cache.clear()
    totals = await client.franchise_totals(1)
    assert totals.seasons == 2
    assert totals.avg_score == pytest.approx(8.0, abs=0.01)  # (7.0 + 9.0) / 2


async def test_announced_and_summary_excluded(client, monkeypatch):
    # A NOT_YET_RELEASED sequel and a SUMMARY recap movie must both be excluded:
    # the announced season isn't counted, and SUMMARY isn't even traversed.
    graph = {
        1: _media(1, "TV", "FINISHED", 24, 85, edges=[
            ("SEQUEL", 2, "TV", "NOT_YET_RELEASED", None),
            ("SUMMARY", 3, "MOVIE", "FINISHED", 1),
        ]),
        2: _media(2, "TV", "NOT_YET_RELEASED", None, None),
        3: _media(3, "MOVIE", "FINISHED", 1, 60),
    }

    async def fake_post(query, variables):
        ids = variables.get("ids") or []
        return {"Page": {"media": [graph[i] for i in ids if i in graph]}}

    monkeypatch.setattr(client, "_post", fake_post)
    client._totals_cache.clear()
    totals = await client.franchise_totals(1)
    assert totals.seasons == 1     # announced S2 not counted
    assert totals.movies == 0      # SUMMARY recap not traversed/counted
    assert totals.avg_score == pytest.approx(8.5, abs=0.01)  # only the root
