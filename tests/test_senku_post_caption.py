"""Senku post-caption editor regression (Phase 11).

Staff edit the captions of published distribution posts (info card / season
card / movie card / watch guide / footer). The editor must:

  • list only published channels that actually have posts,
  • list only editable post kinds (dividers excluded),
  • persist the new caption on the ``BotContentPost`` row AND bump
    ``content_revision`` (returning /start users get the new text),
  • attempt the live Telegram edit (best-effort).

The picker screens are pure functions; the persistence path is exercised
against the SQLite fixture with a fake Telegram client.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bots.senku.handlers.post_caption_edit import (
    _channel_screen,
    _edit_buttons,
    _edit_caption,
    _kind_label,
    _parse_button_lines,
    _posts_for,
    _posts_screen,
    _published_channels,
)

pytestmark = pytest.mark.asyncio


# ── pure screen builders ────────────────────────────────────────────────────────

def test_kind_label_maps_all_editable_kinds():
    assert _kind_label("info_card") == "Info card"
    assert _kind_label("season_card") == "Season card"
    assert _kind_label("movie_card") == "Movie card"
    assert _kind_label("watch_guide") == "Watch guide"
    assert _kind_label("footer") == "Footer"
    # Unknown kinds degrade to a readable title, never crash.
    assert _kind_label("divider") == "Divider"


def test_channel_screen_lists_channels_and_back():
    channels = [
        SimpleNamespace(id=1, name="Vanitas Channel", anime_doc_id="doc1", chat_id=-1001),
        SimpleNamespace(id=2, name=None, anime_doc_id="doc2", chat_id=-1002),
    ]
    caption, rows = _channel_screen(channels)
    # Channel names are the BUTTON labels (the caption is a fixed prompt).
    assert rows[0][0][0] == "Vanitas Channel"
    assert rows[1][0][0] == "doc2"  # name-less channel falls back to doc id
    assert len(rows) == len(channels) + 1  # one row per channel + Back
    assert rows[-1] == [("⬅ Back", "senku|home")]
    # Empty state is friendly, not an error.
    empty_caption, empty_rows = _channel_screen([])
    assert "No published" in empty_caption
    assert empty_rows == [[("⬅ Back", "senku|home")]]


def test_posts_screen_lists_editable_posts_with_back():
    posts = [
        {"kind": "info_card", "tg_message_id": 101, "caption": "Old caption"},
        {"kind": "divider", "tg_message_id": 102, "caption": ""},
        {"kind": "footer", "tg_message_id": 103, "caption": ""},
    ]
    caption, rows = _posts_screen("Vanitas", posts)
    assert "Vanitas" in caption
    assert "Info card" in caption and "Old caption" in caption
    # Every post gets its own row, then the Back-to-channels row.
    assert len(rows) == len(posts) + 1
    assert rows[-1] == [("⬅ Channels", "senku|capedit|channels")]


# ── picker + persistence against the DB ────────────────────────────────────────

def _container(sessionmaker):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        redis=None,
        config=SimpleNamespace(post_format=SimpleNamespace()),
    )


async def _seed_channel(sessionmaker):
    from nekofetch.infrastructure.database.postgres.models import (
        BotContentPost,
        ChannelLayout,
        DistributionBot,
    )

    async with sessionmaker() as s:
        bot = DistributionBot(
            name="Vanitas Channel", anime_doc_id="doc-vanitas",
            encrypted_token="x", is_channel=True, enabled=True,
            chat_id=-1001234567890,
        )
        s.add(bot)
        await s.commit()
        s.add(ChannelLayout(channel_bot_id=bot.id, seq=1, kind="info_card",
                            tg_message_id=101))
        s.add(ChannelLayout(channel_bot_id=bot.id, seq=2, kind="divider",
                            tg_message_id=102))
        s.add(BotContentPost(bot_id=bot.id, post_type="info_card", order=1,
                             caption="Old caption", tg_message_id=101))
        s.add(BotContentPost(bot_id=bot.id, post_type="watch_guide", order=2,
                             caption="Watch order", tg_message_id=103))
        await s.commit()
        return bot.id


async def test_published_channels_requires_layout(sessionmaker):
    bot_id = await _seed_channel(sessionmaker)
    channels = await _published_channels(_container(sessionmaker))
    assert [c.id for c in channels] == [bot_id]

    # A channel with no layout/posts is NOT offered (nothing to edit).
    from nekofetch.infrastructure.database.postgres.models import DistributionBot

    async with sessionmaker() as s:
        s.add(DistributionBot(name="Empty", encrypted_token="y", is_channel=True,
                              enabled=True, chat_id=-10099))
        await s.commit()
    channels = await _published_channels(_container(sessionmaker))
    assert len(channels) == 1  # still only the seeded one


async def test_posts_for_prefers_durable_rows_and_skips_dividers(sessionmaker):
    bot_id = await _seed_channel(sessionmaker)
    posts = await _posts_for(_container(sessionmaker), bot_id)
    kinds = {p["kind"] for p in posts}
    # info_card + watch_guide are editable; divider is not a content row.
    assert kinds == {"info_card", "watch_guide"}
    info = next(p for p in posts if p["kind"] == "info_card")
    assert info["tg_message_id"] == 101 and info["caption"] == "Old caption"


async def test_edit_caption_persists_bumps_revision_and_edits_live(
    sessionmaker, monkeypatch,
):
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import (
        BotContentPost,
        DistributionBot,
    )

    bot_id = await _seed_channel(sessionmaker)
    edited: list[tuple[int, int, str]] = []

    class _Client:
        async def get_messages(self, chat_id, mid):
            return SimpleNamespace(photo=False)

        async def edit_message_text(self, chat_id, mid, caption, **kw):
            edited.append((chat_id, mid, caption))

        async def edit_message_caption(self, chat_id, mid, caption, **kw):
            edited.append((chat_id, mid, caption))

    backups: list[str] = []

    class _FakeBackup:
        def __init__(self, _c):
            pass

        async def record_distribution_channel(self, anime_doc_id):
            backups.append(anime_doc_id)

    monkeypatch.setattr("nekofetch.services.backup_service.BackupService",
                        _FakeBackup)

    ok, result = await _edit_caption(
        _Client(), _container(sessionmaker), None,
        bot_id=bot_id, tg_message_id=101, new_caption="<b>New caption</b>",
    )
    assert ok and "Caption updated" in result

    async with sessionmaker() as s:
        row = (await s.execute(
            select(BotContentPost).where(
                BotContentPost.bot_id == bot_id,
                BotContentPost.tg_message_id == 101,
            )
        )).scalar_one()
        assert row.caption == "<b>New caption</b>"
        bot = await s.get(DistributionBot, bot_id)
        assert (bot.content_revision or 0) == 1
    # Live Telegram edit attempted (photo=False → edit_message_text).
    assert len(edited) == 1 and edited[0][2] == "<b>New caption</b>"
    # Wipe-proof backup refreshed with the channel's anime doc id.
    assert backups == ["doc-vanitas"]


def test_button_lines_parse_as_custom_payload():
    assert _parse_button_lines("Open | https://example.test\nChat | tg://resolve?domain=x") == {
        "type": "custom",
        "buttons": [
            {"text": "Open", "url": "https://example.test"},
            {"text": "Chat", "url": "tg://resolve?domain=x"},
        ],
    }
    assert _parse_button_lines("none") == {"type": "custom", "buttons": []}
    with pytest.raises(ValueError):
        _parse_button_lines("missing separator")


async def test_edit_buttons_persists_and_edits_live(sessionmaker, monkeypatch):
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import (
        BotContentPost,
        DistributionBot,
    )

    bot_id = await _seed_channel(sessionmaker)
    edited = []

    class _Client:
        async def edit_message_reply_markup(self, chat_id, mid, reply_markup):
            edited.append((chat_id, mid, reply_markup))

    backups = []

    class _FakeBackup:
        def __init__(self, _c):
            pass

        async def record_distribution_channel(self, anime_doc_id):
            backups.append(anime_doc_id)

    monkeypatch.setattr("nekofetch.services.backup_service.BackupService", _FakeBackup)
    ok, result = await _edit_buttons(
        _Client(), _container(sessionmaker), bot_id=bot_id,
        tg_message_id=101,
        button_data={"type": "custom", "buttons": [{
            "text": "Open", "url": "https://example.test",
        }]},
    )
    assert ok and "Buttons updated" in result
    assert len(edited) == 1
    assert edited[0][2].inline_keyboard[0][0].url == "https://example.test"
    assert backups == ["doc-vanitas"]

    async with sessionmaker() as s:
        row = (await s.execute(
            select(BotContentPost).where(
                BotContentPost.bot_id == bot_id,
                BotContentPost.tg_message_id == 101,
            )
        )).scalar_one()
        assert row.button_data["buttons"][0]["text"] == "Open"
        bot = await s.get(DistributionBot, bot_id)
        assert (bot.content_revision or 0) == 1


async def test_edit_caption_rejects_oversized(sessionmaker):
    from bots.senku.handlers.post_caption_edit import _TEXT_LIMIT

    bot_id = await _seed_channel(sessionmaker)
    ok, result = await _edit_caption(
        SimpleNamespace(get_messages=None), _container(sessionmaker), None,
        bot_id=bot_id, tg_message_id=101, new_caption="x" * (_TEXT_LIMIT + 1),
    )
    assert not ok and "too long" in result


async def test_failed_live_caption_edit_does_not_overwrite_database(sessionmaker):
    from sqlalchemy import select
    from nekofetch.infrastructure.database.postgres.models import BotContentPost

    bot_id = await _seed_channel(sessionmaker)

    class _FailingClient:
        async def get_messages(self, chat_id, mid):
            return SimpleNamespace(photo=False)

        async def edit_message_text(self, *args, **kwargs):
            raise RuntimeError("MESSAGE_ID_INVALID")

    ok, result = await _edit_caption(
        _FailingClient(), _container(sessionmaker), None,
        bot_id=bot_id, tg_message_id=101, new_caption="Should not persist",
    )
    assert not ok and "database was left unchanged" in result

    async with sessionmaker() as s:
        row = (await s.execute(
            select(BotContentPost).where(
                BotContentPost.bot_id == bot_id,
                BotContentPost.tg_message_id == 101,
            )
        )).scalar_one()
        assert row.caption == "Old caption"
