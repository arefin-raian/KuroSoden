"""Senku per-entry thumbnail FSM — drives the REAL state store.

Previously this file re-implemented the approve/redo transition locally and
asserted against its own copy. It now drives the exact functions the wizard's
Approve/Redo handlers call — :meth:`DistributionCache.set_selection(done=True)`
and :meth:`DistributionCache.clear_selection` — and advances via
:meth:`SenkuThumbnailAdapter.next_pending` against the real Redis-backed
selection blob (in-memory fake Redis, same as ``test_distribution_cache.py``).
"""

from __future__ import annotations

from kurosoden.shared.distribution_cache import DistributionCache, EntryData
from kurosoden.shared.senku_thumbnail_adapter import SenkuThumbnailAdapter


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, **kw):
        self.store[key] = value
        self.ttls[key] = kw.get("ex")

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


class _Container:
    def __init__(self, redis):
        self.redis = redis
        self.config = None


async def _seed(code: str, *, done: set[int] | None = None) -> tuple[DistributionCache, SenkuThumbnailAdapter]:
    """Two rendered-but-unapproved entries; ``done`` marks approved indices."""
    container = _Container(FakeRedis())
    cache = DistributionCache(container)
    adapter = SenkuThumbnailAdapter(container)
    await cache.set_entries(code, [
        EntryData(index=1, label="Season 01", anilist_id=101),
        EntryData(index=2, label="Season 02", anilist_id=102),
    ])
    for idx in (1, 2):
        await cache.set_selection(code, idx, asset="thumbnail",
                                  value="file:///tmp/thumb.webp")
    for idx in done or set():
        await cache.set_selection(code, idx, done=True)
    return cache, adapter


async def test_approve_advances_to_next_pending_entry():
    code = "REQ-1"
    cache, adapter = await _seed(code)

    # The wizard's Approve handler marks exactly this entry done…
    await cache.set_selection(code, 1, done=True)
    # …and the advance step (next_pending) lands on the next not-done entry.
    nxt = await adapter.next_pending(code)
    assert nxt is not None and nxt.index == 2
    assert (await cache.get_selection(code, 1)).done is True


async def test_redo_keeps_target_entry_pending_and_preserves_previous_done_entry():
    code = "REQ-2"
    cache, adapter = await _seed(code, done={1, 2})

    # Redo on entry 2 clears only ITS picks; entry 1 stays approved.
    await cache.clear_selection(code, 2)
    nxt = await adapter.next_pending(code)
    assert nxt is not None and nxt.index == 2
    assert (await cache.get_selection(code, 1)).done is True
    assert (await cache.get_selection(code, 2)).done is False


async def test_all_done_gates_completion():
    code = "REQ-3"
    cache, adapter = await _seed(code, done={1})

    assert await adapter.is_complete(code) is False
    assert (await adapter.next_pending(code)).index == 2

    await cache.set_selection(code, 2, done=True)
    assert await adapter.next_pending(code) is None
    assert await adapter.is_complete(code) is True
