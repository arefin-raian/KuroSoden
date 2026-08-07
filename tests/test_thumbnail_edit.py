from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import kurosoden.shared.image_backup as image_backup
from sqlalchemy import select

from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    ChannelLayout,
    ChannelPost,
    ChannelContentBackup,
    DistributionBot,
    PublishedPostBackup,
)
from nekofetch.services.main_channel_service import MainChannelService
from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService


class FakeClient:
    def __init__(self):
        self.media_edits = []
        self.messages = {}

    async def get_messages(self, chat_id, message_id):
        return self.messages.get((chat_id, message_id))

    async def edit_message_media(self, chat_id, message_id, media):
        self.media_edits.append((chat_id, message_id, media))


def _container(sessionmaker, client):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        admin_client=client,
        pipeline_manager=SimpleNamespace(senku=client),
        redis=None,
        config=SimpleNamespace(
            main_channel=SimpleNamespace(enabled=True, channel_id=-1001),
            thumbnail_channel=SimpleNamespace(enabled=True, channel_id=-1002),
            bot=SimpleNamespace(divider_sticker_id=None),
            post_format=SimpleNamespace(),
        ),
    )


@pytest.mark.asyncio
async def test_main_thumbnail_refresh_preserves_caption_and_updates_backup(
    sessionmaker, monkeypatch, tmp_path: Path,
):
    client = FakeClient()
    client.messages[(-1001, 77)] = SimpleNamespace(caption="<b>Old caption</b>")
    image = tmp_path / "new.webp"
    image.write_bytes(b"new-image")
    async with sessionmaker() as session:
        session.add(ChannelPost(
            anime_doc_id="anime-1", main_channel_id=-1001, main_message_id=77,
        ))
        session.add(PublishedPostBackup(
            anime_doc_id="anime-1", caption="<b>Old caption</b>",
            image_source_url="https://old.example/image.webp",
            source_channel_id=-1001, source_message_id=77,
        ))
        await session.commit()

    class Mirror:
        catbox_url = "https://catbox.example/new.webp"
        telegraph_url = None
        imgbb_url = None

    async def mirror_bytes(*args, **kwargs):
        assert kwargs["mime"] == "image/webp"
        return Mirror()

    monkeypatch.setattr(image_backup, "backup_bytes", mirror_bytes)
    svc = MainChannelService(_container(sessionmaker, client))
    assert await svc.refresh_thumbnail("anime-1", str(image)) is True
    assert len(client.media_edits) == 1
    media = client.media_edits[0][2]
    assert media.caption == "<b>Old caption</b>"
    assert media.media == str(image)

    async with sessionmaker() as session:
        backup = (await session.execute(
            select(PublishedPostBackup).where(
                PublishedPostBackup.anime_doc_id == "anime-1"
            )
        )).scalars().one()
        assert backup.image_catbox_url == "https://catbox.example/new.webp"
        assert backup.image_source_url == "https://old.example/image.webp"


@pytest.mark.asyncio
async def test_distribution_thumbnail_refresh_preserves_caption_and_backup(
    sessionmaker, monkeypatch, tmp_path: Path,
):
    client = FakeClient()
    client.messages[(-2001, 88)] = SimpleNamespace(caption="<b>Season caption</b>")
    image = tmp_path / "new.png"
    image.write_bytes(b"new-image")
    async with sessionmaker() as session:
        bot = DistributionBot(
            kind="distribution", name="Senku", encrypted_token="x",
            anime_doc_id="anime-2", enabled=True, is_channel=True, chat_id=-2001,
        )
        session.add(bot)
        await session.flush()
        session.add(ChannelLayout(
            channel_bot_id=bot.id, seq=1, kind="season_card",
            tg_message_id=88, anilist_id=1234,
        ))
        session.add(BotContentPost(
            bot_id=bot.id, post_type="season_card", caption="<b>Season caption</b>",
            image_url="https://old.example/old.png", image_cached_url="https://old.example/old.png",
            anilist_id=1234, tg_message_id=88, order=1,
        ))
        session.add(ChannelContentBackup(
            scope="distribution", channel_key="anime-2",
            cards=[{"kind": "season_card", "anilist_id": 1234,
                    "caption": "<b>Season caption</b>",
                    "image_url": "https://old.example/old.png"}],
        ))
        await session.commit()

    class Mirror:
        catbox_url = None
        telegraph_url = "https://telegraph.example/new.png"
        imgbb_url = None
        @property
        def primary(self):
            return self.telegraph_url

    async def mirror_bytes(*args, **kwargs):
        assert kwargs["mime"] == "image/png"
        return Mirror()

    monkeypatch.setattr(image_backup, "backup_bytes", mirror_bytes)
    svc = ThumbnailChannelService(_container(sessionmaker, client))
    assert await svc.refresh_published_thumbnail("anime-2", 1234, str(image)) is True
    assert len(client.media_edits) == 1
    media = client.media_edits[0][2]
    assert media.caption == "<b>Season caption</b>"
    assert media.media == str(image)

    async with sessionmaker() as session:
        post = (await session.execute(select(BotContentPost))).scalars().one()
        backup = (await session.execute(select(ChannelContentBackup))).scalars().one()
        assert post.image_cached_url == "https://telegraph.example/new.png"
        assert backup.cards[0]["image_url"] == "https://telegraph.example/new.png"
