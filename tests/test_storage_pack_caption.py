"""Storage-pack caption persistence and edit behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.domain.enums import AudioType
from pyrogram.enums import ParseMode
from nekofetch.infrastructure.database.postgres.models import StoragePack
from nekofetch.services.storage_channel_service import StorageChannelService


class FakeClient:
    def __init__(self, *, text_fails=False):
        self.edits = []
        self.text_fails = text_fails

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        if self.text_fails:
            raise RuntimeError("message is media")
        self.edits.append(("text", chat_id, message_id, text, kwargs))

    async def edit_message_caption(self, chat_id, message_id, caption, **kwargs):
        self.edits.append(("caption", chat_id, message_id, caption, kwargs))


def _container(sessionmaker, client):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        redis=None,
        admin_client=client,
        pipeline_manager=None,
        config=SimpleNamespace(
            storage_channel=SimpleNamespace(
                enabled=True,
                channel_id=-100123,
                header_template="",
                movie_header_template="",
                special_header_template="",
                end_sticker_id=None,
            ),
            distribution=SimpleNamespace(protect_content=False),
        ),
    )


@pytest.mark.asyncio
async def test_update_header_caption_persists_and_edits_live_header(sessionmaker):
    client = FakeClient()
    container = _container(sessionmaker, client)
    async with sessionmaker() as session:
        pack = StoragePack(
            anime_doc_id="anime-1", anime_title="Anime", season=1,
            resolution="1080p", audio=AudioType.SUBBED,
            channel_id=-100123, header_message_id=42,
            start_message_id=43, end_message_id=44,
            file_message_ids=[43], file_count=1,
        )
        session.add(pack)
        await session.commit()
        pack_id = pack.id

    updated = await StorageChannelService(container).update_header_caption(
        pack_id, "<b>Edited header</b>",
    )

    assert updated is not None
    assert updated.header_caption == "<b>Edited header</b>"
    assert client.edits == [
        ("text", -100123, 42, "<b>Edited header</b>", {"parse_mode": ParseMode.HTML}),
    ]

    async with sessionmaker() as session:
        row = await session.get(StoragePack, pack_id)
        assert row.header_caption == "<b>Edited header</b>"


@pytest.mark.asyncio
async def test_update_header_caption_rejects_blank(sessionmaker):
    container = _container(sessionmaker, FakeClient())
    with pytest.raises(ValueError):
        await StorageChannelService(container).update_header_caption(1, "  ")


@pytest.mark.asyncio
async def test_update_header_caption_syncs_enabled_sibling_packs(sessionmaker):
    client = FakeClient()
    container = _container(sessionmaker, client)
    async with sessionmaker() as session:
        packs = [
            StoragePack(
                anime_doc_id="anime-group", anime_title="Anime", season=1,
                resolution="1080p", audio=AudioType.SUBBED,
                channel_id=-100123, header_message_id=42,
                start_message_id=43, end_message_id=44,
                file_message_ids=[43], file_count=1,
            ),
            StoragePack(
                anime_doc_id="anime-group", anime_title="Anime", season=1,
                resolution="720p", audio=AudioType.DUBBED,
                channel_id=-100123, header_message_id=52,
                start_message_id=53, end_message_id=54,
                file_message_ids=[53], file_count=1,
            ),
        ]
        session.add_all(packs)
        await session.commit()
        pack_id = packs[0].id

    updated = await StorageChannelService(container).update_header_caption(
        pack_id, "<b>Shared header</b>",
    )

    assert updated is not None
    assert {row[3] for row in client.edits} == {"<b>Shared header</b>"}
    async with sessionmaker() as session:
        rows = (await session.execute(
            select(StoragePack).where(
                StoragePack.anime_doc_id == "anime-group",
            )
        )).scalars().all()
        assert {row.header_caption for row in rows} == {"<b>Shared header</b>"}


@pytest.mark.asyncio
async def test_update_header_caption_falls_back_to_media_caption(sessionmaker):
    client = FakeClient(text_fails=True)
    container = _container(sessionmaker, client)
    async with sessionmaker() as session:
        pack = StoragePack(
            anime_doc_id="anime-media", anime_title="Anime", season=1,
            resolution="1080p", audio=AudioType.SUBBED,
            channel_id=-100123, header_message_id=62,
            start_message_id=63, end_message_id=64,
            file_message_ids=[63], file_count=1,
        )
        session.add(pack)
        await session.commit()
        pack_id = pack.id

    await StorageChannelService(container).update_header_caption(
        pack_id, "<b>Media header</b>",
    )

    assert client.edits == [
        ("caption", -100123, 62, "<b>Media header</b>", {"parse_mode": ParseMode.HTML}),
    ]


@pytest.mark.asyncio
async def test_update_header_caption_rejects_disabled_pack(sessionmaker):
    client = FakeClient()
    container = _container(sessionmaker, client)
    async with sessionmaker() as session:
        pack = StoragePack(
            anime_doc_id="anime-disabled", anime_title="Anime", season=1,
            resolution="1080p", audio=AudioType.SUBBED,
            channel_id=-100123, header_message_id=72,
            start_message_id=73, end_message_id=74,
            file_message_ids=[73], file_count=1, enabled=False,
        )
        session.add(pack)
        await session.commit()
        pack_id = pack.id

    assert await StorageChannelService(container).update_header_caption(
        pack_id, "<b>Ignored</b>",
    ) is None
    assert client.edits == []


def test_storage_pack_has_editable_caption_column():
    assert "header_caption" in {column.name for column in StoragePack.__table__.columns}
