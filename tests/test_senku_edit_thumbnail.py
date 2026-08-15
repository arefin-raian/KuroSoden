"""Senku-native Edit-Thumbnail editor — regression tests.

Covers the data + apply logic that runs against the SQLite fixture with a fake
Telegram client (the pure UI wiring is exercised indirectly):

  * the franchise list is sourced from StoragePack, so an anime with packs but
    NO saved ThumbnailSource still appears;
  * distribution entries come from the channel layout;
  * a field edit re-renders, persists (bumps ThumbnailSource) and refreshes the
    right live surface (main for -1, distribution otherwise);
  * genres parse comma-separated → list; an oversized synopsis is truncated;
  * the regenerate session seeds a synthetic code whose franchise blob carries
    anime_doc_id (so persist is not a silent no-op).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text

from bots.senku.handlers.thumbnail_edit_senku import (
    _MAIN_ANILIST,
    _apply_field,
    _distribution_entries,
    _entry_fields,
    _franchises,
)

_aio = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _wipe_tables(sessionmaker):
    """Isolate each test: the engine is session-scoped and these tests commit,
    so wipe every table afterwards (mirrors conftest's ``session`` fixture)."""
    yield
    from nekofetch.infrastructure.database.postgres.base import Base
    async with sessionmaker() as cleanup:
        for t in reversed(Base.metadata.sorted_tables):
            await cleanup.execute(text(f"DELETE FROM {t.name}"))
        await cleanup.commit()


def _container(sessionmaker):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        redis=None,
        config=SimpleNamespace(post_format=SimpleNamespace()),
    )


async def _seed_pack(sessionmaker, *, doc_id, title, resolution="1080p"):
    from nekofetch.domain.enums import AudioType
    from nekofetch.infrastructure.database.postgres.models import StoragePack

    async with sessionmaker() as s:
        s.add(StoragePack(
            anime_doc_id=doc_id, anime_title=title, season=1,
            resolution=resolution, audio=AudioType.SUBBED,
            channel_id=-100123, start_message_id=2, end_message_id=14,
            file_count=12,
        ))
        await s.commit()


async def _seed_thumbnail(sessionmaker, *, doc_id, anilist_id, fields):
    from nekofetch.infrastructure.database.postgres.models import ThumbnailSource

    async with sessionmaker() as s:
        s.add(ThumbnailSource(anime_doc_id=doc_id, anilist_id=anilist_id,
                              fields=fields, image_path="/tmp/old.webp"))
        await s.commit()


# ── franchise list ───────────────────────────────────────────────────────────

@_aio
async def test_franchise_list_includes_pre_feature_anime(sessionmaker):
    # Takopi: has packs but NO ThumbnailSource — must still be listed.
    await _seed_pack(sessionmaker, doc_id="takopi", title="Takopi's Original Sin")
    await _seed_pack(sessionmaker, doc_id="aot", title="Attack on Titan")
    await _seed_thumbnail(sessionmaker, doc_id="aot", anilist_id=-1,
                          fields={"title": "Attack on Titan"})
    rows = await _franchises(_container(sessionmaker))
    docs = {doc for doc, _title in rows}
    assert "takopi" in docs and "aot" in docs  # pre-feature anime present


@_aio
async def test_franchise_list_dedups_multi_pack_franchise(sessionmaker):
    # Two packs (two resolutions) for one franchise → one list entry.
    await _seed_pack(sessionmaker, doc_id="one", title="One Piece", resolution="720p")
    await _seed_pack(sessionmaker, doc_id="one", title="One Piece", resolution="1080p")
    rows = await _franchises(_container(sessionmaker))
    assert [doc for doc, _t in rows].count("one") == 1


# ── distribution entries + saved-field lookup ────────────────────────────────

@_aio
async def test_distribution_entries_from_layout(sessionmaker):
    from nekofetch.infrastructure.database.postgres.models import (
        ChannelLayout, DistributionBot,
    )

    async with sessionmaker() as s:
        bot = DistributionBot(name="AoT", anime_doc_id="aot", encrypted_token="x",
                              is_channel=True, enabled=True, chat_id=-100999)
        s.add(bot)
        await s.commit()
        s.add(ChannelLayout(channel_bot_id=bot.id, seq=1, kind="season_card",
                            anilist_id=101, tg_message_id=11))
        s.add(ChannelLayout(channel_bot_id=bot.id, seq=2, kind="movie_card",
                            anilist_id=202, tg_message_id=12))
        s.add(ChannelLayout(channel_bot_id=bot.id, seq=3, kind="divider",
                            anilist_id=None, tg_message_id=13))
        await s.commit()
    entries = await _distribution_entries(_container(sessionmaker), "aot")
    aids = [aid for aid, _label in entries]
    assert aids == [101, 202]  # cards only, divider excluded


@_aio
async def test_entry_fields_returns_none_when_unsaved(sessionmaker):
    await _seed_pack(sessionmaker, doc_id="takopi", title="Takopi's Original Sin")
    assert await _entry_fields(_container(sessionmaker), "takopi", -1) is None
    await _seed_thumbnail(sessionmaker, doc_id="takopi", anilist_id=-1,
                          fields={"title": "Takopi", "synopsis": "s"})
    got = await _entry_fields(_container(sessionmaker), "takopi", -1)
    assert got and got["title"] == "Takopi"


# ── apply a field edit ────────────────────────────────────────────────────────

class _FakeRenderer:
    """Stand-in for ThumbnailRenderService that records the kwargs it renders."""
    last_kwargs: dict = {}

    def __init__(self):
        pass

    async def render_thumbnail(self, **kwargs):
        _FakeRenderer.last_kwargs = kwargs
        return "/tmp/new.webp"

    async def close(self):
        pass


@_aio
async def test_apply_field_persists_and_refreshes_distribution(sessionmaker, monkeypatch):
    from sqlalchemy import select
    from nekofetch.infrastructure.database.postgres.models import ThumbnailSource
    import bots.senku.handlers.thumbnail_edit_senku as mod

    await _seed_thumbnail(sessionmaker, doc_id="aot", anilist_id=101,
                          fields={"title": "Attack on Titan", "synopsis": "old"})
    monkeypatch.setattr(mod, "ThumbnailRenderService", _FakeRenderer)
    refreshed: list[tuple] = []

    async def _fake_refresh(container, doc, aid, path):
        refreshed.append((doc, aid, path))
        return "distribution card"

    monkeypatch.setattr(mod, "_refresh_live", _fake_refresh)

    ok, msg, trimmed = await _apply_field(
        _container(sessionmaker), "aot", 101, "synopsis", "a brand new synopsis",
    )
    assert ok and trimmed is None
    async with sessionmaker() as s:
        row = (await s.execute(select(ThumbnailSource).where(
            ThumbnailSource.anime_doc_id == "aot",
            ThumbnailSource.anilist_id == 101,
        ))).scalar_one()
        assert row.fields["synopsis"] == "a brand new synopsis"
    assert refreshed == [("aot", 101, "/tmp/new.webp")]


@_aio
async def test_apply_field_genres_parsed_to_list(sessionmaker, monkeypatch):
    from sqlalchemy import select
    from nekofetch.infrastructure.database.postgres.models import ThumbnailSource
    import bots.senku.handlers.thumbnail_edit_senku as mod

    await _seed_thumbnail(sessionmaker, doc_id="aot", anilist_id=-1,
                          fields={"title": "Attack on Titan"})
    monkeypatch.setattr(mod, "ThumbnailRenderService", _FakeRenderer)
    monkeypatch.setattr(mod, "_refresh_live",
                        lambda *a, **k: _async_return("main-channel post"))

    ok, _msg, _trim = await _apply_field(
        _container(sessionmaker), "aot", _MAIN_ANILIST, "genres",
        "Action, Drama , Fantasy",
    )
    assert ok
    async with sessionmaker() as s:
        row = (await s.execute(select(ThumbnailSource).where(
            ThumbnailSource.anime_doc_id == "aot",
            ThumbnailSource.anilist_id == -1,
        ))).scalar_one()
        assert row.fields["genres"] == ["Action", "Drama", "Fantasy"]


@_aio
async def test_apply_field_truncates_oversized_synopsis(sessionmaker, monkeypatch):
    import bots.senku.handlers.thumbnail_edit_senku as mod

    await _seed_thumbnail(sessionmaker, doc_id="aot", anilist_id=-1,
                          fields={"title": "Attack on Titan"})
    monkeypatch.setattr(mod, "ThumbnailRenderService", _FakeRenderer)
    monkeypatch.setattr(mod, "_refresh_live",
                        lambda *a, **k: _async_return("main-channel post"))

    ok, _msg, trimmed = await _apply_field(
        _container(sessionmaker), "aot", _MAIN_ANILIST, "synopsis", "x" * 800,
    )
    assert ok and trimmed is not None
    assert trimmed.endswith("…") and len(trimmed) <= mod._SYNOPSIS_MAX_CHARS


def _async_return(value):
    async def _coro():
        return value
    return _coro()


# ── link parsing reuse (from post_caption_edit) ──────────────────────────────

def test_editor_reuses_post_link_parser():
    from bots.senku.handlers.post_caption_edit import _parse_post_link
    assert _parse_post_link("https://t.me/c/1699000000/42") == (-1001699000000, 42)


# ── regenerate synthetic-code seeding ────────────────────────────────────────

def test_regen_code_roundtrip():
    from bots.senku.handlers.thumbnail_regen import _code_for, _split_code
    code = _code_for("takopi", 101)
    assert code.startswith("THUMBEDIT-")
    doc, aid = _split_code(code)
    assert doc == "takopi" and aid == 101
    # Main-card sentinel survives the round trip too.
    assert _split_code(_code_for("aot", -1)) == ("aot", -1)


def test_regen_code_roundtrip_with_hyphenated_doc():
    # anime_doc_id can contain hyphens; the '#<tag>' delimiter must still split
    # cleanly (the old '-<aid>' scheme broke on both hyphens and the -1 sentinel).
    from bots.senku.handlers.thumbnail_regen import _code_for, _split_code
    assert _split_code(_code_for("doc-with-hyphen", -1)) == ("doc-with-hyphen", -1)
    assert _split_code(_code_for("a-b-c", 42)) == ("a-b-c", 42)


@_aio
async def test_multi_entry_franchise_shows_surface_choice(sessionmaker, monkeypatch):
    """A franchise with >1 distribution entry routes to the main-vs-dist choice,
    NOT straight into one entry's editor."""
    import bots.senku.handlers.thumbnail_edit_senku as mod
    from nekofetch.infrastructure.database.postgres.models import (
        ChannelLayout, DistributionBot,
    )

    async with sessionmaker() as s:
        bot = DistributionBot(name="AoT", anime_doc_id="aot", encrypted_token="x",
                              is_channel=True, enabled=True, chat_id=-100999)
        s.add(bot)
        await s.commit()
        s.add(ChannelLayout(channel_bot_id=bot.id, seq=1, kind="season_card",
                            anilist_id=101, tg_message_id=11))
        s.add(ChannelLayout(channel_bot_id=bot.id, seq=2, kind="season_card",
                            anilist_id=102, tg_message_id=12))
        await s.commit()
    await _seed_pack(sessionmaker, doc_id="aot", title="Attack on Titan")

    # Capture which path _open_franchise takes by stubbing _open_edit_page +
    # send_screen.
    edit_calls: list = []
    surface_captions: list = []

    async def _fake_edit(client, container, q, doc, aid, title):
        edit_calls.append((doc, aid))

    async def _fake_send(client, chat_id, screen, old_msg=None):
        surface_captions.append(screen.caption)

    monkeypatch.setattr(mod, "_open_edit_page", _fake_edit)
    monkeypatch.setattr(mod, "send_screen", _fake_send)

    q = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=1)))
    await mod._open_franchise(None, _container(sessionmaker), q, "aot")
    # Two entries → surface choice card shown, edit page NOT opened directly.
    assert not edit_calls
    assert surface_captions and "several entries" in surface_captions[0]
