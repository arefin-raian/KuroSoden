"""Offline tests for metadata fallback and AcuteBot title resolution."""

from __future__ import annotations

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
    "duration": "24 min per ep",
    "images": {"jpg": {"large_image_url": "https://cdn.myanimelist.net/x.jpg"}},
}


def _container(**overrides):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("anilist down")

    values = {
        "anilist": SimpleNamespace(search=_boom),
        "tmdb": SimpleNamespace(
            poster_for=lambda _title: (_ for _ in ()).throw(RuntimeError("tmdb touched")),
            search=lambda _title: (_ for _ in ()).throw(RuntimeError("tmdb touched")),
        ),
        "config": SimpleNamespace(
            features=SimpleNamespace(catbox_image_cache=False),
            post_format=SimpleNamespace(),
            bot=SimpleNamespace(filestore_bots=[], footer_text=""),
        ),
        "env": SimpleNamespace(
            storage_path=Path("."),
            telegram_api_id=12345,
            telegram_api_hash="test-api-hash",
            session_path=Path("."),
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _pool_for_test(monkeypatch):
    from nekofetch.sources.telegram.userbot import Account, UserbotPool

    pool = UserbotPool(12345, "test-api-hash", [Account("primary")])

    async def _execute(fn, **_kwargs):
        return await fn(SimpleNamespace())

    pool.execute = _execute
    monkeypatch.setattr(
        "nekofetch.sources.telegram.userbot.UserbotPool.from_env",
        staticmethod(lambda *_args, **_kwargs: pool),
    )


@pytest.mark.asyncio
async def test_mal_tier_fills_gaps_when_acute_and_anilist_fail(monkeypatch):
    from nekofetch.services import bot_content as bc
    from nekofetch.services.bot_content import BotContentService

    async def _fail_acute(*_args, **_kwargs):
        raise RuntimeError("acutebot down")

    monkeypatch.setattr("nekofetch.providers.acute_bot.fetch_from_acutebot", _fail_acute)
    monkeypatch.setattr(
        "nekofetch.sources.telegram.userbot.UserbotPool.from_env",
        staticmethod(lambda *_args, **_kwargs: SimpleNamespace()),
    )
    async def _fake_jikan(*_args, **_kwargs):
        return FAKE_JIKAN

    async def _fake_jikan_cache(*_args, **_kwargs):
        return FAKE_JIKAN

    async def _no_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bc, "_jikan_search", _fake_jikan)
    monkeypatch.setattr(
        "nekofetch.services.metadata_prefetch.load_cached_jikan", _fake_jikan_cache,
    )
    monkeypatch.setattr("nekofetch.services.metadata_prefetch.load_cached", _no_cache)

    svc = BotContentService(_container())
    meta = await svc._gather_metadata("185407", title_hint="Takopis Original Sin")

    assert meta["_source"] == "myanimelist"
    assert meta["title"] == "Takopis Original Sin"
    assert "octopus" in (meta["synopsis"] or "")
    assert meta["score"] == "8.9"
    assert meta["genres"] == ["Comedy", "Drama"]
    assert meta["runtime"] == "24 min/ep"
    assert meta["poster_url"] == "https://cdn.myanimelist.net/x.jpg"


@pytest.mark.asyncio
async def test_numeric_anilist_doc_uses_cached_title_for_acutebot(monkeypatch):
    from nekofetch.services.bot_content import BotContentService

    calls = []

    async def _acute(title, *_args, **_kwargs):
        calls.append(title)
        return {"title": title, "romaji": title, "_source": "acutebot"}

    monkeypatch.setattr("nekofetch.providers.acute_bot.fetch_from_acutebot", _acute)
    await _pool_for_test(monkeypatch)
    async def _cached(*_args, **_kwargs):
        return {"search": {
            "english": "The Case Study of Vanitas",
            "romaji": "Vanitas no Carte",
            "titles": ["Vanitas no Carte"],
        }}

    monkeypatch.setattr("nekofetch.services.metadata_prefetch.load_cached", _cached)

    meta = await BotContentService(_container())._gather_metadata("anilist:131646")

    assert calls == ["The Case Study of Vanitas"]
    assert meta["_source"] == "acutebot"
    assert meta["title"] == "The Case Study of Vanitas"


@pytest.mark.asyncio
async def test_numeric_anilist_doc_uses_persisted_title_when_cache_missing(monkeypatch):
    from nekofetch.services.bot_content import BotContentService

    calls = []

    async def _acute(title, *_args, **_kwargs):
        calls.append(title)
        return {"title": title, "romaji": title, "_source": "acutebot"}

    monkeypatch.setattr("nekofetch.providers.acute_bot.fetch_from_acutebot", _acute)
    await _pool_for_test(monkeypatch)
    async def _no_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr("nekofetch.services.metadata_prefetch.load_cached", _no_cache)

    class _Result:
        def scalar_one_or_none(self):
            return "The Case Study of Vanitas"

    class _Session:
        async def execute(self, _statement):
            return _Result()

    class _Scope:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "nekofetch.infrastructure.database.postgres.session.session_scope",
        lambda *_args, **_kwargs: _Scope(),
    )
    container = _container(pg_sessionmaker=object())
    meta = await BotContentService(container)._gather_metadata("131646")

    assert calls == ["The Case Study of Vanitas"]
    assert meta["_source"] == "acutebot"


@pytest.mark.asyncio
async def test_numeric_anilist_doc_uses_direct_id_title_when_other_sources_missing(monkeypatch):
    from nekofetch.services.bot_content import BotContentService

    calls = []

    async def _acute(title, *_args, **_kwargs):
        calls.append(title)
        return {"title": title, "romaji": title, "_source": "acutebot"}

    async def _fetch_full(_media_id):
        return SimpleNamespace(
            english="The Case Study of Vanitas",
            romaji="Vanitas no Carte",
            titles=[],
        )

    monkeypatch.setattr("nekofetch.providers.acute_bot.fetch_from_acutebot", _acute)
    await _pool_for_test(monkeypatch)
    async def _no_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr("nekofetch.services.metadata_prefetch.load_cached", _no_cache)
    container = _container(
        anilist=SimpleNamespace(_fetch_full=_fetch_full),
        pg_sessionmaker=None,
    )
    meta = await BotContentService(container)._gather_metadata("131646")

    assert calls == ["The Case Study of Vanitas"]
    assert meta["_source"] == "acutebot"
