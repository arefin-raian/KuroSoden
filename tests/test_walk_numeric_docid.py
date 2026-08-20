"""Regression: _walk_franchise must resolve a NUMERIC anime_doc_id by id, not by
title text-search, and fall back to storage packs when the walk is empty.

The bug: on a prefetch-cache miss, _walk_franchise did
``anilist.search("162896")`` — a TITLE search for the numeric string, which
AniList can't match → empty franchise → the season card silently vanished from
the distribution channel (The Elusive Samurai). A numeric doc id IS the AniList
id, so it must be walked by id. And if the walk still yields nothing but packs
exist, season entries are synthesized so a card always renders.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from nekofetch.services.bot_content import BotContentService
from nekofetch.sources.telegram.anilist import FranchiseEntry


@dataclass
class _Pack:
    anime_title: str = "The Elusive Samurai"
    season: int | None = 1
    season_part: int | None = None
    resolution: str = "1080p"
    entry_id: int | None = None


class _FakeAnilist:
    """Mimics the real client: text-search for a numeric string misses; a walk by
    the numeric id resolves. Records what was called so the test can assert the
    id path was taken (never the doomed search path)."""

    def __init__(self):
        self.search_calls: list[str] = []
        self.walk_calls: list[int] = []

    async def search(self, q):
        self.search_calls.append(q)
        return None  # AniList can't match a bare numeric string to a title

    async def walk_franchise_full(self, root_id, *, max_nodes: int = 120):
        self.walk_calls.append(int(root_id))
        return {
            162896: FranchiseEntry(
                anilist_id=162896, format="TV",
                english_title="The Elusive Samurai",
                episodes=12, relation="ROOT", status="FINISHED",
                start_date={"year": 2024, "month": 7, "day": 6},
            )
        }


class _Container:
    def __init__(self, anilist):
        self.anilist = anilist
        # metadata_prefetch.load_cached reads container.env.storage_path — point it
        # at a nonexistent dir so the cache-first read misses (forces live walk).
        from types import SimpleNamespace
        from pathlib import Path
        self.env = SimpleNamespace(storage_path=Path("/nonexistent-kuro-test"))


async def test_walk_resolves_numeric_docid_by_id_not_search():
    anilist = _FakeAnilist()
    svc = BotContentService(_Container(anilist))
    out = await svc._walk_franchise("162896", meta={})
    # Resolved by id, and the doomed title-search for "162896" was NOT used.
    assert anilist.walk_calls == [162896]
    assert "162896" not in anilist.search_calls
    assert [e.anilist_id for e in out["tv"]] == [162896]


async def test_walk_handles_anilist_prefixed_numeric():
    anilist = _FakeAnilist()
    svc = BotContentService(_Container(anilist))
    out = await svc._walk_franchise("anilist:162896", meta={})
    assert anilist.walk_calls == [162896]
    assert out["tv"] and out["tv"][0].anilist_id == 162896


def test_seasons_from_packs_synthesizes_tv_entries():
    # When the walk returns nothing but packs exist, synthesize one TV entry per
    # distinct (season, part) so a season card still renders.
    svc = BotContentService(_Container(_FakeAnilist()))
    packs = [
        _Pack(season=1, resolution="1080p"),
        _Pack(season=1, resolution="720p"),   # same season → one entry
        _Pack(season=1, resolution="480p"),
    ]
    entries = svc._seasons_from_packs(packs, meta={}, title_hint="The Elusive Samurai")
    assert len(entries) == 1
    assert entries[0].english_title == "The Elusive Samurai"
    assert entries[0].format == "TV"
    assert entries[0].season_part is None


def test_seasons_from_packs_skips_extras_and_dedups_parts():
    svc = BotContentService(_Container(_FakeAnilist()))
    packs = [
        _Pack(season=1),
        _Pack(season=2, season_part=1),
        _Pack(season=2, season_part=2),
        _Pack(season=90),   # extra (movie/OVA slot) → skipped
        _Pack(season=None),  # no season → skipped
    ]
    entries = svc._seasons_from_packs(packs, meta={}, title_hint="X")
    keys = {(e.english_title, e.season_part) for e in entries}
    # S1, S2P1, S2P2 → 3 entries; season 90 + None dropped.
    assert len(entries) == 3
    assert entries[0].relation == "ROOT"  # season 1 is the root
