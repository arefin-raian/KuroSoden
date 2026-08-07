"""Tests for kurosoden/shared/distribution_cache.py — the wizard's working set.

Covers the pure Redis-backed read/write/clear surface without touching the DB or
network (``ensure`` resolution is exercised in the wizard/integration tests):
  • entries round-trip through Redis with EntryData fidelity
  • selection read-modify-write: asset picks and done flags persist
  • get_channel / set_channel round-trip
  • all_done reflects per-entry done flags
  • clear() removes every key for the code
  • TTL (``ex``) is passed on every write so an abandoned wizard self-expires
"""

from __future__ import annotations

import json

import pytest

from kurosoden.shared.distribution_cache import (
    DistributionCache,
    EntryData,
    Selection,
    _DEFAULT_TTL,
)


class FakeRedis:
    """In-memory Redis honouring get/set/delete with TTL capture."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


class FakeContainer:
    def __init__(self, redis):
        self.redis = redis


@pytest.fixture
def cache():
    return DistributionCache(FakeContainer(FakeRedis()))


def _entries():
    return [
        EntryData(index=1, label="Season 1", kind="season", season_number=1,
                  episodes=25, anilist_id=101, title="Root"),
        EntryData(index=2, label="Season 3 Part 2", kind="season",
                  season_number=3, season_part=2, anilist_id=102),
        EntryData(index=3, label="Movie: Finale", kind="movie",
                  media_type="movie", title="Finale", anilist_id=103),
    ]


@pytest.mark.asyncio
async def test_entries_round_trip(cache):
    await cache.set_entries("REQ-1", _entries())
    got = await cache.get_entries("REQ-1")
    assert [e.index for e in got] == [1, 2, 3]
    assert got[1].season_part == 2
    assert got[2].kind == "movie" and got[2].media_type == "movie"


@pytest.mark.asyncio
async def test_get_entry_by_index(cache):
    await cache.set_entries("REQ-1", _entries())
    e = await cache.get_entry("REQ-1", 2)
    assert e is not None and e.label == "Season 3 Part 2"
    assert await cache.get_entry("REQ-1", 99) is None


@pytest.mark.asyncio
async def test_selection_asset_picks_persist(cache):
    await cache.set_selection("REQ-1", 1, asset="logo", value="logo.png")
    await cache.set_selection("REQ-1", 1, asset="poster", value="poster.jpg")
    await cache.set_selection("REQ-1", 1, asset="bg", value="bg.jpg")
    sel = await cache.get_selection("REQ-1", 1)
    assert sel.logo_url == "logo.png"
    assert sel.poster_url == "poster.jpg"
    assert sel.backdrop_url == "bg.jpg"
    assert sel.done is False


@pytest.mark.asyncio
async def test_backdrop_alias_maps_to_same_field(cache):
    await cache.set_selection("REQ-1", 1, asset="backdrop", value="b.jpg")
    assert (await cache.get_selection("REQ-1", 1)).backdrop_url == "b.jpg"


@pytest.mark.asyncio
async def test_clear_selection_only_resets_target_entry(cache):
    await cache.set_entries("REQ-1", _entries())
    await cache.set_selection("REQ-1", 1, asset="logo", value="one")
    await cache.set_selection("REQ-1", 1, done=True)
    await cache.set_selection("REQ-1", 2, asset="logo", value="other")

    await cache.clear_selection("REQ-1", 1)

    cleared = await cache.get_selection("REQ-1", 1)
    assert cleared == Selection()
    # A different entry in the same request is untouched by the redo reset.
    assert (await cache.get_selection("REQ-1", 2)).logo_url == "other"


@pytest.mark.asyncio
async def test_done_flag_and_all_done(cache):
    await cache.set_entries("REQ-1", _entries())
    assert await cache.all_done("REQ-1") is False
    for i in (1, 2, 3):
        await cache.set_selection("REQ-1", i, asset="thumbnail",
                                  value=f"t{i}.png", done=True)
    assert await cache.all_done("REQ-1") is True


@pytest.mark.asyncio
async def test_all_done_false_when_no_entries(cache):
    assert await cache.all_done("REQ-empty") is False


@pytest.mark.asyncio
async def test_channel_round_trip(cache):
    await cache.set_channel("REQ-1", handle="@aot_axw", chat_id=-1001234)
    ch = await cache.get_channel("REQ-1")
    assert ch["handle"] == "@aot_axw"
    assert ch["chat_id"] == -1001234


@pytest.mark.asyncio
async def test_clear_removes_all_keys(cache):
    await cache.set_entries("REQ-1", _entries())
    await cache.set_selection("REQ-1", 1, asset="logo", value="x")
    await cache.set_channel("REQ-1", handle="@c")
    await cache.clear("REQ-1")
    assert await cache.get_entries("REQ-1") == []
    assert await cache.get_channel("REQ-1") is None
    assert await cache.get_selections("REQ-1") == {}


@pytest.mark.asyncio
async def test_writes_carry_ttl(cache):
    redis = cache._redis
    await cache.set_entries("REQ-1", _entries())
    await cache.set_selection("REQ-1", 1, asset="logo", value="x")
    await cache.set_channel("REQ-1", handle="@c")
    assert all(ttl == _DEFAULT_TTL for ttl in redis.ttls.values())


@pytest.mark.asyncio
async def test_get_selections_survives_corrupt_blob(cache):
    # A malformed selections blob must not crash reads — returns empty.
    await cache._redis.set("nf:dist:REQ-1:selections", "{not json")
    assert await cache.get_selections("REQ-1") == {}


@pytest.mark.asyncio
async def test_get_entries_survives_corrupt_blob(cache):
    await cache._redis.set("nf:dist:REQ-1:entries", "[not json")
    assert await cache.get_entries("REQ-1") == []


# ── apply_order_correction (watch-order edit) ─────────────────────────────────────

async def _seed_franchise(cache, code):
    """Seed a minimal aggregated franchise so build_mapping has something to map."""
    fr = {"title": "Root", "franchise_seasons": 3, "franchise_episodes": 12,
          "anime_doc_id": "doc1"}
    await cache._redis.set(f"nf:dist:{code}:franchise", json.dumps(fr))
    return fr


@pytest.mark.asyncio
async def test_apply_order_correction_persists_remap(cache):
    from nekofetch.services.franchise_flow import FranchiseFlowService

    await _seed_franchise(cache, "REQ-1")
    # Build the canonical block the admin would edit, then feed it back verbatim.
    svc = FranchiseFlowService(cache._c)
    fr = await cache.get_franchise("REQ-1")
    block = svc.format_mapping_code_block(svc.build_mapping(fr, "doc1"))

    out = await cache.apply_order_correction("REQ-1", block)
    assert out is not None and len(out) == 3
    # Result is persisted as the new canonical entry list.
    stored = await cache.get_entries("REQ-1")
    assert [e.index for e in stored] == [1, 2, 3]


@pytest.mark.asyncio
async def test_apply_order_correction_rejects_garbage(cache):
    await _seed_franchise(cache, "REQ-1")
    assert await cache.apply_order_correction("REQ-1", "") is None


@pytest.mark.asyncio
async def test_apply_order_correction_no_franchise(cache):
    # No franchise seeded and ensure() can't resolve (no DB) → None, no crash.
    assert await cache.apply_order_correction("REQ-missing", "anything") is None
