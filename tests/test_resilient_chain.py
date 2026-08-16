"""Resilient metadata chain — tier fall-through semantics.

``ResilientMetadataClient`` chains AniList → Jikan/MAL → Kitsu (then @acutebot
for search). These tests pin the ordering and, crucially, the "empty result is
a MISS" rule for collection methods: a tier that returns an empty dict/list or
an all-zero ``FranchiseTotals`` (e.g. a cross-tier id that doesn't resolve on
that provider) must NOT short-circuit the chain — the next tier is tried.

Pure unit tests with fake tier clients; no network.
"""

from __future__ import annotations

import pytest

from nekofetch.sources.telegram.anilist import FranchiseTotals
from nekofetch.sources.telegram.resilient_client import ResilientMetadataClient

_aio = pytest.mark.asyncio


class _FakeTier:
    """A stand-in metadata tier whose per-method results (or raises) are set."""

    def __init__(self, name, **results):
        self._name = name
        self._results = results
        self.calls: list[str] = []

    def _resolve(self, method, *a, **k):
        self.calls.append(method)
        val = self._results.get(method, None)
        if isinstance(val, Exception):
            raise val
        return val

    async def search(self, query):
        return self._resolve("search", query)

    async def search_candidates(self, query, *, limit=25):
        return self._resolve("search_candidates", query)

    async def _fetch_full(self, media_id):
        return self._resolve("_fetch_full", media_id)

    async def franchise_totals(self, root_id, *, max_nodes=120):
        return self._resolve("franchise_totals", root_id)

    async def walk_franchise_full(self, root_id, *, max_nodes=120):
        return self._resolve("walk_franchise_full", root_id)

    async def title_variants(self, query):
        return self._resolve("title_variants", query)

    async def close(self):
        pass


def _client(anilist, mal, kitsu, dataset=None, kaggle=None) -> ResilientMetadataClient:
    c = ResilientMetadataClient()
    c.anilist, c.mal, c.kitsu = anilist, mal, kitsu
    # Stub the local dataset tiers too (default: miss-everything fakes) so unit
    # tests never touch the network — the real dataset clients would download and
    # index the live CSV snapshots.
    c.dataset = dataset if dataset is not None else _FakeTier("dataset")
    c.kaggle = kaggle if kaggle is not None else _FakeTier("kaggle")
    return c


@_aio
async def test_search_falls_through_to_kitsu_when_anilist_and_mal_miss():
    anilist = _FakeTier("anilist", search=RuntimeError("403 down"))
    mal = _FakeTier("mal", search=None)          # Jikan search 504 → None
    kitsu = _FakeTier("kitsu", search="KITSU_MEDIA")
    c = _client(anilist, mal, kitsu)

    assert await c.search("Shadows House") == "KITSU_MEDIA"
    # Chain tried every tier in order.
    assert anilist.calls == ["search"]
    assert mal.calls == ["search"]
    assert kitsu.calls == ["search"]


@_aio
async def test_first_tier_hit_short_circuits():
    anilist = _FakeTier("anilist", search="ANILIST_MEDIA")
    mal = _FakeTier("mal", search="MAL_MEDIA")
    kitsu = _FakeTier("kitsu", search="KITSU_MEDIA")
    c = _client(anilist, mal, kitsu)

    assert await c.search("Frieren") == "ANILIST_MEDIA"
    assert mal.calls == [] and kitsu.calls == []  # never consulted


@_aio
async def test_dataset_tier_sits_right_after_anilist():
    # AniList misses → the local dataset is tried BEFORE the REST APIs; a dataset
    # hit short-circuits Jikan/Kitsu (the fast local path the owner wants).
    anilist = _FakeTier("anilist", search=None)
    dataset = _FakeTier("dataset", search="DATASET_MEDIA")
    mal = _FakeTier("mal", search="MAL_MEDIA")
    kitsu = _FakeTier("kitsu", search="KITSU_MEDIA")
    c = _client(anilist, mal, kitsu, dataset=dataset)

    assert await c.search("Solo Leveling") == "DATASET_MEDIA"
    assert dataset.calls == ["search"]
    assert mal.calls == [] and kitsu.calls == []  # APIs skipped on a dataset hit


@_aio
async def test_walk_treats_empty_dict_as_miss_and_continues():
    # MAL returns {} for a Kitsu id that doesn't resolve on Jikan — must NOT
    # short-circuit; the chain continues to Kitsu which has the real walk.
    anilist = _FakeTier("anilist", walk_franchise_full=RuntimeError("down"))
    mal = _FakeTier("mal", walk_franchise_full={})
    kitsu = _FakeTier("kitsu", walk_franchise_full={1: "root", 2: "sequel"})
    # Kaggle sits in the walk chain before MAL — make it miss so we reach Kitsu.
    kaggle = _FakeTier("kaggle", walk_franchise_full={})
    c = _client(anilist, mal, kitsu, kaggle=kaggle)

    result = await c.walk_franchise_full(43820)
    assert result == {1: "root", 2: "sequel"}
    assert kitsu.calls == ["walk_franchise_full"]


@_aio
async def test_kaggle_walk_hit_short_circuits_before_apis():
    # Kaggle carries offline relations — a Kaggle walk hit must be used before
    # ever calling the Jikan/Kitsu APIs.
    anilist = _FakeTier("anilist", walk_franchise_full=RuntimeError("down"))
    kaggle = _FakeTier("kaggle", walk_franchise_full={125038: "root", 139093: "s2"})
    mal = _FakeTier("mal", walk_franchise_full={1: "wrong"})
    kitsu = _FakeTier("kitsu", walk_franchise_full={2: "wrong"})
    c = _client(anilist, mal, kitsu, kaggle=kaggle)

    result = await c.walk_franchise_full(125038)
    assert result == {125038: "root", 139093: "s2"}
    assert mal.calls == [] and kitsu.calls == []


@_aio
async def test_totals_treats_all_zero_as_miss_and_continues():
    anilist = _FakeTier("anilist", franchise_totals=RuntimeError("down"))
    mal = _FakeTier("mal", franchise_totals=FranchiseTotals())  # all-zero → miss
    kitsu = _FakeTier("kitsu",
                      franchise_totals=FranchiseTotals(seasons=2, episodes=25, nodes=2))
    c = _client(anilist, mal, kitsu)

    totals = await c.franchise_totals(43820)
    assert totals.seasons == 2 and totals.episodes == 25
    assert kitsu.calls == ["franchise_totals"]


@_aio
async def test_totals_empty_everywhere_returns_empty_not_none():
    anilist = _FakeTier("anilist", franchise_totals=FranchiseTotals())
    mal = _FakeTier("mal", franchise_totals=FranchiseTotals())
    kitsu = _FakeTier("kitsu", franchise_totals=FranchiseTotals())
    c = _client(anilist, mal, kitsu)

    totals = await c.franchise_totals(1)
    assert isinstance(totals, FranchiseTotals) and totals.nodes == 0


@_aio
async def test_search_candidates_falls_through_to_kitsu_on_empty_anilist():
    # AniList returns [] (down/miss) → must fall to Kitsu, not return [].
    anilist = _FakeTier("anilist", search_candidates=[])
    mal = _FakeTier("mal")  # no candidate page — must be skipped, never called
    kitsu = _FakeTier("kitsu",
                      search_candidates=[{"id": 43820, "title": "Shadows House",
                                          "format": "TV", "popularity": 1}])
    c = _client(anilist, mal, kitsu)

    cands = await c.search_candidates("Shadows House")
    assert len(cands) == 1 and cands[0]["id"] == 43820
    assert mal.calls == []  # MAL has no candidate page; skipped entirely


@_aio
async def test_search_candidates_empty_everywhere_returns_empty_list():
    anilist = _FakeTier("anilist", search_candidates=[])
    mal = _FakeTier("mal")
    kitsu = _FakeTier("kitsu", search_candidates=[])
    c = _client(anilist, mal, kitsu)

    assert await c.search_candidates("nope") == []
