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

    async def edit_message_media(self, chat_id, message_id, media, reply_markup=None):
        self.media_edits.append((chat_id, message_id, media, reply_markup))


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
async def test_main_thumbnail_refresh_preserves_buttons(
    sessionmaker, monkeypatch, tmp_path: Path,
):
    """Replacing the main-post image must KEEP its Index/Download buttons —
    editMessageMedia drops the keyboard unless re-supplied (the bug that stripped
    buttons off 4 live posts). The live markup must be handed back to the edit."""
    kb = SimpleNamespace(inline_keyboard=[["Index", "Download"]])
    client = FakeClient()
    client.messages[(-1001, 88)] = SimpleNamespace(
        caption="<b>Cap</b>", reply_markup=kb)
    image = tmp_path / "n.webp"
    image.write_bytes(b"img")
    async with sessionmaker() as session:
        session.add(ChannelPost(
            anime_doc_id="anime-kb", main_channel_id=-1001, main_message_id=88))
        await session.commit()

    async def mirror_bytes(*a, **k):
        return SimpleNamespace(catbox_url="https://c/x.webp",
                               telegraph_url=None, imgbb_url=None)
    monkeypatch.setattr(image_backup, "backup_bytes", mirror_bytes)

    svc = MainChannelService(_container(sessionmaker, client))
    assert await svc.refresh_thumbnail("anime-kb", str(image)) is True
    # The media edit carried the SAME live keyboard (index 3 = reply_markup).
    assert client.media_edits[0][3] is kb



@pytest.mark.asyncio
async def test_distribution_thumbnail_refresh_sends_jpeg_for_webp_render(
    sessionmaker, monkeypatch, tmp_path: Path,
):
    """A rendered .webp card is converted to JPEG before the live media edit.

    Telegram's media endpoint treats webp as the sticker format, so the preview
    send must carry a JPEG conversion of the render — the stored artifact stays
    webp. This is the "Gallery didn't load" preview fix: the render succeeded
    and the hosts accepted it, only the Telegram send failed.
    """
    import io
    from PIL import Image

    client = FakeClient()
    # Distinct ids from the caption/backup test below so both can run together
    # without the ``.one()`` lookups seeing each other's rows.
    client.messages[(-2003, 99)] = SimpleNamespace(caption="<b>Season caption</b>")
    webp = tmp_path / "card.webp"
    buf = io.BytesIO()
    Image.new("RGBA", (80, 40), (200, 30, 60, 255)).save(buf, format="WEBP")
    webp.write_bytes(buf.getvalue())
    async with sessionmaker() as session:
        bot = DistributionBot(
            kind="distribution", name="Senku", encrypted_token="x",
            anime_doc_id="anime-3", enabled=True, is_channel=True, chat_id=-2003,
        )
        session.add(bot)
        await session.flush()
        session.add(ChannelLayout(
            channel_bot_id=bot.id, seq=1, kind="season_card",
            tg_message_id=99, anilist_id=2222,
        ))
        session.add(BotContentPost(
            bot_id=bot.id, post_type="season_card", caption="<b>Season caption</b>",
            image_url="https://old.example/old.webp",
            image_cached_url="https://old.example/old.webp",
            anilist_id=2222, tg_message_id=99, order=1,
        ))
        await session.commit()

    class Mirror:
        catbox_url = None
        telegraph_url = "https://telegraph.example/card.jpg"
        imgbb_url = None

        @property
        def primary(self):
            return self.telegraph_url

    async def mirror_bytes(*args, **kwargs):
        return Mirror()

    monkeypatch.setattr(image_backup, "backup_bytes", mirror_bytes)
    svc = ThumbnailChannelService(_container(sessionmaker, client))
    assert await svc.refresh_published_thumbnail("anime-3", 2222, str(webp)) is True
    assert len(client.media_edits) == 1
    media = client.media_edits[0][2]
    # The live edit carries the JPEG conversion, not the raw webp.
    assert media.media == str(webp.with_suffix(".jpg"))
    assert webp.with_suffix(".jpg").exists()

    # The test DB is shared across this file's tests, and the caption/backup
    # test below queries BotContentPost/ChannelContentBackup with unfiltered
    # ``.one()`` — remove our rows (children first) so it sees exactly its own.
    async with sessionmaker() as session:
        post = (await session.execute(select(BotContentPost))).scalars().one()
        layout = (await session.execute(select(ChannelLayout))).scalars().one()
        bot = (await session.execute(select(DistributionBot))).scalars().one()
        await session.delete(post)
        await session.delete(layout)
        await session.delete(bot)
        await session.commit()


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
