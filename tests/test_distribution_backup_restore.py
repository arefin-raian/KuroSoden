"""Distribution-scope backup & restore round-trip (Phase 5, workstream D).

A banned distribution channel must be re-postable **verbatim** on a fresh chat:
captions, Download buttons, dividers, pins, and mirrored images all come from a
wipe-proof :class:`ChannelContentBackup` snapshot — no regeneration, no
re-render. These run against the SQLite fixture + a fake admin client, with the
image mirror stubbed, so no Telegram / catbox / envs.sh calls happen.

Covered:
  • ``record_distribution_channel`` snapshots the live BotContentPost pack in
    send order, mirrors images, and tracks the footer message id.
  • the snapshot survives a ``recreate_bot``-style wipe of the live posts.
  • ``restore_distribution_channel`` replays it in order with dividers before
    every card but the first, rebuilt Download buttons, and the pin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.core.config import PostFormatConfig
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    ChannelContentBackup,
    ChannelLayout,
    DistributionBot,
)
from nekofetch.services.backup_service import BackupService
from kurosoden.shared.image_backup import BackupImage

pytestmark = pytest.mark.asyncio


class _FakeMsg:
    def __init__(self, mid):
        self.id = mid


class _FakeClient:
    """Records the ordered stream of sends so we can assert the choreography."""

    def __init__(self, *, username: str | None = "fresh_channel"):
        self.username = username
        self.events: list[tuple[str, object]] = []
        self.deleted: list[tuple[int, int]] = []
        self.pins: list[int] = []
        self._next_id = 5000

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, username=self.username)

    async def send_sticker(self, chat_id, sticker):
        self._next_id += 1
        self.events.append(("sticker", sticker))
        return _FakeMsg(self._next_id)

    async def send_photo(self, chat_id, image, caption=None, reply_markup=None, parse_mode=None):
        self._next_id += 1
        self.events.append(("photo", {"image": image, "caption": caption,
                                      "markup": reply_markup}))
        return _FakeMsg(self._next_id)

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None,
                           disable_web_page_preview=None):
        self._next_id += 1
        self.events.append(("message", {"caption": text, "markup": reply_markup}))
        return _FakeMsg(self._next_id)

    async def pin_chat_message(self, chat_id, mid, disable_notification=False):
        self.pins.append(mid)

    async def delete_messages(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _container(sessionmaker, client=None):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        admin_client=client,
        config=SimpleNamespace(
            post_format=PostFormatConfig(),
            main_channel=SimpleNamespace(divider_sticker_id=""),
            bot=SimpleNamespace(divider_sticker_id="DIVSTKR"),
        ),
    )


async def _seed_channel(sessionmaker):
    """A channel + its ordered content pack (guide[pinned], card, footer)."""
    async with sessionmaker() as s:
        bot = DistributionBot(
            name="AOT Channel", username="aot", anime_doc_id="anime1",
            encrypted_token="x", enabled=True, is_channel=True, chat_id=-100111,
        )
        s.add(bot)
        await s.commit()
        posts = [
            BotContentPost(
                bot_id=bot.id, post_type="watch_guide", order=0,
                caption="Watch <a>{BOT_QUAL:1080p}</a>", image_url="http://cdn/guide.jpg",
                image_cached_url="http://cat/guide.jpg", is_pinned=True,
                button_data={"type": "flat", "qualities": ["1080p"],
                             "links": {"1080p": "http://dl/1080"}},
            ),
            BotContentPost(
                bot_id=bot.id, post_type="season_card", order=1,
                caption="Season 1", image_url="http://cdn/s1.jpg",
                is_pinned=False, button_data=None,
            ),
            BotContentPost(
                bot_id=bot.id, post_type="footer", order=2,
                caption="Join!", image_url=None, is_pinned=False,
                button_data=None, tg_message_id=999,
            ),
        ]
        for p in posts:
            s.add(p)
        await s.commit()
        return bot.id


def _stub_mirror(monkeypatch):
    """Make image mirroring deterministic + offline: cat/... -> mir/...."""
    import kurosoden.shared.image_backup as ib

    async def fake_backup_image(container, url):
        return BackupImage(source_url=url, catbox_url=f"mir/{url.rsplit('/', 1)[-1]}")

    monkeypatch.setattr(ib, "backup_image", fake_backup_image)


async def test_record_snapshots_pack_in_order(sessionmaker, monkeypatch):
    _stub_mirror(monkeypatch)
    await _seed_channel(sessionmaker)
    svc = BackupService(_container(sessionmaker))

    row = await svc.record_distribution_channel("anime1")

    assert row is not None
    assert row.scope == "distribution" and row.channel_key == "anime1"
    assert row.source_chat_id == -100111
    assert row.footer_message_id == 999
    kinds = [c["kind"] for c in row.cards]
    assert kinds == ["watch_guide", "season_card", "footer"]
    # First card has no preceding divider; the rest do (bot divider configured).
    assert [c["divider_before"] for c in row.cards] == [False, True, True]
    # Cached (catbox) url is preferred as the mirror source, then re-mirrored.
    assert row.cards[0]["image_url"] == "mir/guide.jpg"
    assert row.cards[0]["is_pinned"] is True
    assert row.cards[0]["button_data"]["links"]["1080p"] == "http://dl/1080"


async def test_record_upserts_in_place(sessionmaker, monkeypatch):
    _stub_mirror(monkeypatch)
    await _seed_channel(sessionmaker)
    svc = BackupService(_container(sessionmaker))

    await svc.record_distribution_channel("anime1")
    await svc.record_distribution_channel("anime1")

    async with sessionmaker() as s:
        from sqlalchemy import func, select
        n = (await s.execute(
            select(func.count()).select_from(ChannelContentBackup)
            .where(ChannelContentBackup.channel_key == "anime1")
        )).scalar_one()
    assert n == 1


async def test_snapshot_survives_live_post_wipe(sessionmaker, monkeypatch):
    """The backup outlives a recreate that deletes the live BotContentPost rows."""
    _stub_mirror(monkeypatch)
    bot_id = await _seed_channel(sessionmaker)
    svc = BackupService(_container(sessionmaker))
    await svc.record_distribution_channel("anime1")

    # Simulate recreate_bot's wipe of the live pack.
    async with sessionmaker() as s:
        from sqlalchemy import delete
        await s.execute(delete(BotContentPost).where(BotContentPost.bot_id == bot_id))
        await s.commit()

    async with sessionmaker() as s:
        from sqlalchemy import select
        row = (await s.execute(
            select(ChannelContentBackup)
            .where(ChannelContentBackup.channel_key == "anime1")
        )).scalar_one()
    assert len(row.cards) == 3  # snapshot intact


async def test_restore_replays_verbatim(sessionmaker, monkeypatch):
    _stub_mirror(monkeypatch)
    await _seed_channel(sessionmaker)
    client = _FakeClient(username="fresh")
    svc = BackupService(_container(sessionmaker, client))
    await svc.record_distribution_channel("anime1")

    stats = await svc.restore_distribution_channel("anime1", -100999)

    assert stats.total == 3 and stats.restored == 3 and stats.failed == 0
    # Choreography: guide photo, divider, season photo, divider, footer message.
    kinds = [e[0] for e in client.events]
    assert kinds == ["photo", "sticker", "photo", "sticker", "message"]
    # The pinned guide was pinned on the fresh channel.
    assert len(client.pins) == 1
    # Watch-guide {BOT_QUAL} placeholder resolved to the fresh channel handle.
    guide = client.events[0][1]
    assert 'href="https://t.me/fresh"' in guide["caption"]
    assert "1080p" in guide["caption"]
    # Download button rebuilt from stored links (no regeneration).
    assert guide["markup"] is not None


async def test_restore_no_backup_is_noop(sessionmaker):
    client = _FakeClient()
    svc = BackupService(_container(sessionmaker, client))
    stats = await svc.restore_distribution_channel("missing", -100999)
    assert stats.total == 0 and stats.restored == 0
    assert client.events == []


async def test_failed_recovery_cleanup_deletes_only_tracked_messages(
    sessionmaker,
):
    bot_id = await _seed_channel(sessionmaker)
    async with sessionmaker() as s:
        bot = await s.get(DistributionBot, bot_id)
        s.add(ChannelLayout(
            channel_bot_id=bot_id, seq=10, kind="divider", tg_message_id=1001,
        ))
        await s.commit()

    client = _FakeClient()
    svc = BackupService(_container(sessionmaker, client))
    deleted = await svc.clear_distribution_channel(
        "anime1", -100111, client=client,
    )

    assert deleted == 2
    assert {mid for _chat, mid in client.deleted} == {1001, 999}
    async with sessionmaker() as s:
        from sqlalchemy import select
        assert (await s.execute(
            select(BotContentPost).where(BotContentPost.bot_id == bot_id)
        )).scalars().all() == []
        assert (await s.execute(
            select(ChannelLayout).where(ChannelLayout.channel_bot_id == bot_id)
        )).scalars().all() == []


async def test_cleanup_keeps_failed_message_ids_for_retry(sessionmaker):
    bot_id = await _seed_channel(sessionmaker)

    async with sessionmaker() as s:
        s.add(ChannelLayout(
            channel_bot_id=bot_id, seq=10, kind="divider", tg_message_id=1001,
        ))
        await s.commit()

    class FailingDeleteClient(_FakeClient):
        async def delete_messages(self, chat_id, message_id):
            if message_id == 1001:
                raise RuntimeError("temporary Telegram failure")
            await super().delete_messages(chat_id, message_id)

    client = FailingDeleteClient()
    svc = BackupService(_container(sessionmaker, client))
    with pytest.raises(RuntimeError, match="tracked recovery message"):
        await svc.clear_distribution_channel(
            "anime1", -100111, client=client,
        )

    from sqlalchemy import select

    async with sessionmaker() as s:
        assert (await s.execute(
            select(ChannelLayout).where(
                ChannelLayout.channel_bot_id == bot_id,
                ChannelLayout.tg_message_id == 1001,
            )
        )).scalars().all()
