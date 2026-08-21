"""Bugs A + E: resilient AniList fallback wherever data is missing.

Shadows House was stored with ``anilist_id=None`` and ``romaji=None`` (AniList
never resolved for it), which produced a bare channel title (E) and a shared
TMDB poster instead of per-season covers (A). The fix routes both through the
resilient metadata chain (``container.anilist``: AniList → Kaggle → Jikan →
Kitsu) by TITLE, so a null id is fine as long as SOME tier carries the data.

These pin the two new helpers with fakes — no network, no DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.services.bot_factory import BotFactory
from nekofetch.services.publishing_service import PublishingService


# ── E: romaji fallback in the title path ──────────────────────────────────────

@pytest.mark.asyncio
async def test_romaji_fallback_uses_prefetch_cache_first(monkeypatch):
    import nekofetch.services.metadata_prefetch as mp

    async def fake_load_cached(container, code, kind, *, anime_doc_id=None):
        return {"search": {"romaji": "Cached Romaji"}}
    monkeypatch.setattr(mp, "load_cached", fake_load_cached)

    class _Boom:  # live must NOT run when the cache hits
        async def search(self, *_a, **_k):  # pragma: no cover
            raise AssertionError("live search should not run on a cache hit")

    bf = BotFactory(SimpleNamespace(anilist=_Boom()))
    assert await bf._resolve_romaji_fallback("REQ-1", "Shadows House") == "Cached Romaji"


@pytest.mark.asyncio
async def test_romaji_fallback_goes_live_when_cache_empty(monkeypatch):
    import nekofetch.services.metadata_prefetch as mp

    async def fake_load_cached(container, code, kind, *, anime_doc_id=None):
        return None                              # nothing cached
    monkeypatch.setattr(mp, "load_cached", fake_load_cached)

    class _Resilient:
        async def search(self, title):
            assert title == "Shadows House"
            return SimpleNamespace(romaji="Shadows House")   # from some tier

    bf = BotFactory(SimpleNamespace(anilist=_Resilient()))
    assert await bf._resolve_romaji_fallback("REQ-1", "Shadows House") == "Shadows House"


@pytest.mark.asyncio
async def test_romaji_fallback_returns_empty_on_total_miss(monkeypatch):
    import nekofetch.services.metadata_prefetch as mp

    async def fake_load_cached(container, code, kind, *, anime_doc_id=None):
        return None
    monkeypatch.setattr(mp, "load_cached", fake_load_cached)

    class _Miss:
        async def search(self, title):
            return None

    bf = BotFactory(SimpleNamespace(anilist=_Miss()))
    # A bare title is still better than raising inside channel creation.
    assert await bf._resolve_romaji_fallback("REQ-1", "Nonexistent") == ""


# ── A: resilient franchise walk for per-season posters ────────────────────────

@pytest.mark.asyncio
async def test_resilient_walk_serializes_live_entries():
    from nekofetch.sources.telegram.anilist import FranchiseEntry

    class _Resilient:
        async def search(self, title):
            assert title == "Shadows House"
            return SimpleNamespace(id=101177)
        async def walk_franchise_full(self, root_id):
            assert root_id == 101177
            return {
                101177: FranchiseEntry(
                    anilist_id=101177, format="TV", english_title="Shadows House",
                    cover_url="https://img/s1.jpg", start_date={"year": 2021}),
                111888: FranchiseEntry(
                    anilist_id=111888, format="TV", english_title="Shadows House 2nd",
                    cover_url="https://img/s2.jpg", start_date={"year": 2022}),
            }

    svc = PublishingService(SimpleNamespace(anilist=_Resilient()))
    walk = await svc._resilient_franchise_walk("Shadows House")
    # Serialized to the same {str(id): {...}} shape the cache uses, with per-entry
    # covers so each season resolves its OWN poster (never the shared TMDB one).
    assert set(walk) == {"101177", "111888"}
    assert walk["111888"]["cover_url"] == "https://img/s2.jpg"
    assert walk["101177"]["anilist_id"] == 101177


@pytest.mark.asyncio
async def test_resilient_walk_empty_when_title_unresolved():
    class _Miss:
        async def search(self, title):
            return None                    # no tier knows the title

    svc = PublishingService(SimpleNamespace(anilist=_Miss()))
    assert await svc._resilient_franchise_walk("Nope") == {}


@pytest.mark.asyncio
async def test_resilient_walk_empty_without_title():
    svc = PublishingService(SimpleNamespace(anilist=object()))
    assert await svc._resilient_franchise_walk(None) == {}
    assert await svc._resilient_franchise_walk("") == {}
