"""Senku post editor regression (link-based flow).

Staff run ``/editpost``, paste a post LINK, and — once the bot confirms it
administers that channel — rewrite the caption or replace the buttons. The
editor must:

  • parse ``t.me/c/<internal>/<msg>`` and ``t.me/<username>/<msg>`` links,
  • only proceed when the bot is an admin (with edit rights) of the channel,
  • live-edit the Telegram message first, then (when tracked) persist the new
    caption/buttons on ``BotContentPost`` and bump ``content_revision``,
  • never overwrite the DB when the live edit fails.

Link parsing is a pure function; the persistence path is exercised against the
SQLite fixture with a fake Telegram client.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bots.senku.handlers.post_caption_edit import (
    _edit_buttons,
    _edit_caption,
    _parse_button_lines,
    _parse_post_link,
    _resolve_editable_channel,
)

# Only the DB/async tests need the event loop; the pure parsers stay sync.
_aio = pytest.mark.asyncio


# ── link parsing (pure) ──────────────────────────────────────────────────────

def test_parse_private_channel_link():
    # t.me/c/<internal>/<msg> → chat id is int("-100" + internal).
    assert _parse_post_link("https://t.me/c/1699000000/42") == (-1001699000000, 42)
    # forum/topic link keeps the LAST segment as the message id.
    assert _parse_post_link("https://t.me/c/1699000000/7/42") == (-1001699000000, 42)
    # scheme-less and trailing junk tolerated.
    assert _parse_post_link("t.me/c/1699000000/42?single") == (-1001699000000, 42)


def test_parse_public_channel_link():
    assert _parse_post_link("https://t.me/AnimeWeebs/123") == ("@AnimeWeebs", 123)
    assert _parse_post_link("t.me/AnimeWeebs/123") == ("@AnimeWeebs", 123)


def test_parse_rejects_non_links():
    assert _parse_post_link("") is None
    assert _parse_post_link("just some text") is None
    assert _parse_post_link("https://example.com/foo/1") is None
    assert _parse_post_link("https://t.me/AnimeWeebs") is None  # no message id
    assert _parse_post_link("https://t.me/c/notanumber/1") is None
    assert _parse_post_link("https://t.me/c/1699000000/notanumber") is None


# ── channel resolution (edit-rights gate) ────────────────────────────────────

def _container(sessionmaker):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        redis=None,
        config=SimpleNamespace(post_format=SimpleNamespace()),
    )


def _member(status, *, can_edit=True):
    priv = SimpleNamespace(can_edit_messages=can_edit)
    return SimpleNamespace(status=SimpleNamespace(value=status), privileges=priv)


class _ChatClient:
    """Fake client whose membership status/rights are configurable."""

    def __init__(self, chat_id, member, *, title="A Channel", username=None):
        self._chat = SimpleNamespace(id=chat_id, title=title, username=username)
        self._member = member

    async def get_chat(self, ref):
        return self._chat

    async def get_chat_member(self, chat_id, who):
        if self._member is None:
            raise RuntimeError("USER_NOT_PARTICIPANT")
        return self._member


@_aio
async def test_resolve_requires_admin_with_edit_rights(sessionmaker):
    # Admin WITH edit rights → resolved (bot_id None: channel not tracked).
    ok = await _resolve_editable_channel(
        _ChatClient(-1001, _member("administrator", can_edit=True)),
        _container(sessionmaker), -1001,
    )
    assert ok is not None
    chat_id, bot_id, title = ok
    assert chat_id == -1001 and bot_id is None

    # Admin WITHOUT edit rights → rejected.
    assert await _resolve_editable_channel(
        _ChatClient(-1002, _member("administrator", can_edit=False)),
        _container(sessionmaker), -1002,
    ) is None

    # Plain member → rejected.
    assert await _resolve_editable_channel(
        _ChatClient(-1003, _member("member")),
        _container(sessionmaker), -1003,
    ) is None

    # Not a participant at all → rejected (no crash).
    assert await _resolve_editable_channel(
        _ChatClient(-1004, None), _container(sessionmaker), -1004,
    ) is None


@_aio
async def test_resolve_maps_tracked_channel_to_bot_id(sessionmaker):
    from nekofetch.infrastructure.database.postgres.models import DistributionBot

    async with sessionmaker() as s:
        bot = DistributionBot(
            name="Vanitas Channel", anime_doc_id="doc-vanitas",
            encrypted_token="x", is_channel=True, enabled=True,
            chat_id=-1001234567890,
        )
        s.add(bot)
        await s.commit()
        bot_id = bot.id

    ok = await _resolve_editable_channel(
        _ChatClient(-1001234567890, _member("creator")),
        _container(sessionmaker), -1001234567890,
    )
    assert ok is not None
    chat_id, resolved_bot_id, title = ok
    assert chat_id == -1001234567890 and resolved_bot_id == bot_id


# ── button parsing (pure) ────────────────────────────────────────────────────

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


# ── persistence against the DB ───────────────────────────────────────────────

async def _seed_channel(sessionmaker):
    from nekofetch.infrastructure.database.postgres.models import (
        BotContentPost,
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
        s.add(BotContentPost(bot_id=bot.id, post_type="info_card", order=1,
                             caption="Old caption", tg_message_id=101))
        s.add(BotContentPost(bot_id=bot.id, post_type="watch_guide", order=2,
                             caption="Watch order", tg_message_id=103))
        await s.commit()
        return bot.id, -1001234567890


@_aio
async def test_edit_caption_persists_bumps_revision_and_edits_live(
    sessionmaker, monkeypatch,
):
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import (
        BotContentPost,
        DistributionBot,
    )

    bot_id, chat_id = await _seed_channel(sessionmaker)
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
        chat_id=chat_id, bot_id=bot_id, tg_message_id=101,
        new_caption="<b>New caption</b>",
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


@_aio
async def test_edit_caption_untracked_link_is_live_only(sessionmaker, monkeypatch):
    """A link-edit of an untracked post (bot_id None) live-edits without DB writes."""
    edited: list[tuple[int, int, str]] = []

    class _Client:
        async def get_messages(self, chat_id, mid):
            return SimpleNamespace(photo=True)

        async def edit_message_caption(self, chat_id, mid, caption, **kw):
            edited.append((chat_id, mid, caption))

    ok, result = await _edit_caption(
        _Client(), _container(sessionmaker), None,
        chat_id=-100999, bot_id=None, tg_message_id=7,
        new_caption="live only",
    )
    assert ok and "Caption updated in the channel." == result
    assert edited == [(-100999, 7, "live only")]


@_aio
async def test_edit_buttons_persists_and_edits_live(sessionmaker, monkeypatch):
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import (
        BotContentPost,
        DistributionBot,
    )

    bot_id, chat_id = await _seed_channel(sessionmaker)
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
        _Client(), _container(sessionmaker), chat_id=chat_id, bot_id=bot_id,
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


@_aio
async def test_edit_caption_rejects_oversized(sessionmaker):
    from bots.senku.handlers.post_caption_edit import _TEXT_LIMIT

    bot_id, chat_id = await _seed_channel(sessionmaker)
    ok, result = await _edit_caption(
        SimpleNamespace(get_messages=None), _container(sessionmaker), None,
        chat_id=chat_id, bot_id=bot_id, tg_message_id=101,
        new_caption="x" * (_TEXT_LIMIT + 1),
    )
    assert not ok and "too long" in result


@_aio
async def test_failed_live_caption_edit_does_not_overwrite_database(sessionmaker):
    from sqlalchemy import select
    from nekofetch.infrastructure.database.postgres.models import BotContentPost

    bot_id, chat_id = await _seed_channel(sessionmaker)

    class _FailingClient:
        async def get_messages(self, chat_id, mid):
            return SimpleNamespace(photo=False)

        async def edit_message_text(self, *args, **kwargs):
            raise RuntimeError("MESSAGE_ID_INVALID")

    ok, result = await _edit_caption(
        _FailingClient(), _container(sessionmaker), None,
        chat_id=chat_id, bot_id=bot_id, tg_message_id=101,
        new_caption="Should not persist",
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
