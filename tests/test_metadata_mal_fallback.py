"""Unit verification for the MAL metadata fallback tier (#105).

Runs offline: patches AcuteBot + live AniList to fail, and feeds a fake
prefetched Jikan blob. Asserts _gather_metadata fills meta from MAL and never
falls through to a live TMDB call (the stub TMDB raises if touched).
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

_kage = types.ModuleType("kurosoden")
_kage.__path__ = [str(HERE)]
sys.modules["kurosoden"] = _kage
for _sub in ("shared", "bots", "nekofetch", "tests"):
    if (HERE / _sub / "__init__.py").is_file():
        _shim = types.ModuleType(f"kurosoden.{_sub}")
        _shim.__path__ = [str(HERE / _sub)]
        sys.modules[f"kurosoden.{_sub}"] = _shim


FAKE_JIKAN = {
    "mal_id": 1,
    "title": "Takopis Original Sin (JP)",
    "title_english": "Takopis Original Sin",
    "synopsis": "A dark comedy about an alien octopus.",
    "score": 8.9,
    "genres": [{"name": "Comedy"}, {"name": "Drama"}],
    "episodes": 12,
    "aired": {"string": "Jul 2025"},
    "duration": "24 min per ep",
    "images": {"jpg": {"large_image_url": "https://cdn.myanimelist.net/x.jpg"}},
}


def _container():
    async def _boom(*_a, **_k):
        raise RuntimeError("anilist down")

    return SimpleNamespace(
        anilist=SimpleNamespace(search=_boom),
        tmdb=SimpleNamespace(
            poster_for=lambda t: (_ for _ in ()).throw(RuntimeError("tmdb touched")),
            search=lambda t: (_ for _ in ()).throw(RuntimeError("tmdb touched")),
        ),
        config=SimpleNamespace(
            features=SimpleNamespace(catbox_image_cache=False),
            post_format=SimpleNamespace(),
            bot=SimpleNamespace(filestore_bots=[], footer_text=""),
        ),
        env=SimpleNamespace(storage_path=Path(".")),
    )


@pytest.mark.asyncio
async def test_mal_tier_fills_gaps_when_acute_and_anilist_fail(monkeypatch):
    from nekofetch.services import bot_content as bc
    from nekofetch.services.bot_content import BotContentService

    # AcuteBot fails (patched at its source module — imported lazily inside
    # _gather_metadata via `from nekofetch.providers.acute_bot import ...`).
    async def _fail_acute(*_a, **_k):
        raise RuntimeError("acutebot down")
    monkeypatch.setattr(
        "nekofetch.providers.acute_bot.fetch_from_acutebot", _fail_acute)
    # UserbotPool.from_env must not touch the network before AcuteBot is called.
    monkeypatch.setattr(
        "nekofetch.sources.telegram.userbot.UserbotPool.from_env",
        staticmethod(lambda *_a, **_k: SimpleNamespace()),
    )

    # Jikan: live search stub (unused when the cache hits) + cache blob.
    async def _fake_jikan(*_a, **_k):
        return FAKE_JIKAN
    monkeypatch.setattr(bc, "_jikan_search", _fake_jikan)

    async def _async_fake_jikan(*_a, **_k):
        return FAKE_JIKAN
    monkeypatch.setattr(
        "nekofetch.services.metadata_prefetch.load_cached_jikan", _async_fake_jikan)

    # load_cached (used by both the AniList prefetch read and the TMDB read)
    # returns nothing, so the chain must rely on the MAL tier.
    async def _no_cache(*_a, **_k):
        return None
    monkeypatch.setattr("nekofetch.services.metadata_prefetch.load_cached", _no_cache)

    svc = BotContentService(_container())
    meta = await svc._gather_metadata("185407", title_hint="Takopis Original Sin")

    assert meta.get("_source") == "myanimelist"
    assert meta.get("title") == "Takopis Original Sin"
    assert "octopus" in (meta.get("synopsis") or "")
    assert meta.get("score") == "8.9"
    assert meta.get("genres") == ["Comedy", "Drama"]
    assert meta.get("runtime") == "24 min/ep"
    assert meta.get("poster_url") == "https://cdn.myanimelist.net/x.jpg"
    # TMDB never touched — the stub would have raised.
