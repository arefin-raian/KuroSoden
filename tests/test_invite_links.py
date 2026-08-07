"""Bot-minted private invite links (feature #39).

The public catalog surfaces route inbound traffic through a *private* invite
link we own (``t.me/+…``), not the channel's public ``t.me/<username>`` link, so
a banned-and-recreated channel can swap the link everywhere. These pin:

  • ``InviteLinkService`` durable store + ``ensure_for_bot`` (mint-once, reuse,
    no-op for bots),
  • the index caption hyperlinks each title to its channel's invite link, and
  • the link resolver maps title → invite link across the letter's packs.

The userbot mint is stubbed, so no Telegram calls happen.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.core.config import IndexChannelConfig
from nekofetch.infrastructure.database.postgres.models import (
    DistributionBot,
    StoragePack,
)
from nekofetch.services.index_channel_service import IndexChannelService, _letter_caption
from nekofetch.services.invite_link_service import InviteLinkService


def _container(sessionmaker, client=None):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        admin_client=client,
        config=SimpleNamespace(
            index_channel=IndexChannelConfig(enabled=True, channel_id=-100500),
        ),
    )


async def _make_channel(sessionmaker, *, chat_id, anime_doc_id, invite_link=None):
    async with sessionmaker() as s:
        ch = DistributionBot(
            name="Ch", username="pub_handle", anime_doc_id=anime_doc_id,
            encrypted_token="x", enabled=True, is_channel=True, chat_id=chat_id,
            invite_link=invite_link,
        )
        s.add(ch)
        await s.commit()
        return ch.id


# ── InviteLinkService ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_store_and_reuse(sessionmaker, session, monkeypatch):
    bot_id = await _make_channel(sessionmaker, chat_id=-100111, anime_doc_id="a1")
    svc = InviteLinkService(_container(sessionmaker))

    minted: list[int] = []

    async def fake_mint(chat_id):
        minted.append(chat_id)
        return "https://t.me/+FIRSTlink"

    monkeypatch.setattr(svc, "mint_for_channel", fake_mint)

    first = await svc.ensure_for_bot(bot_id)
    assert first == "https://t.me/+FIRSTlink"
    assert minted == [-100111]

    # Second call reuses the stored link — no re-mint.
    again = await svc.ensure_for_bot(bot_id)
    assert again == "https://t.me/+FIRSTlink"
    assert minted == [-100111]


@pytest.mark.asyncio
async def test_store_overwrites(sessionmaker, session):
    bot_id = await _make_channel(sessionmaker, chat_id=-100111, anime_doc_id="a1",
                                 invite_link="https://t.me/+OLD")
    svc = InviteLinkService(_container(sessionmaker))
    await svc.store(bot_id, "https://t.me/+NEW")

    from sqlalchemy import select
    async with sessionmaker() as s:
        row = (await s.execute(
            select(DistributionBot).where(DistributionBot.id == bot_id)
        )).scalar_one()
    assert row.invite_link == "https://t.me/+NEW"


@pytest.mark.asyncio
async def test_ensure_noop_for_bot(sessionmaker, session, monkeypatch):
    async with sessionmaker() as s:
        bot = DistributionBot(
            name="Bot", username="a_bot", anime_doc_id="a1",
            encrypted_token="x", enabled=True, is_channel=False, chat_id=None,
        )
        s.add(bot)
        await s.commit()
        bot_id = bot.id
    svc = InviteLinkService(_container(sessionmaker))

    async def boom(chat_id):
        raise AssertionError("bots must never mint an invite link")

    monkeypatch.setattr(svc, "mint_for_channel", boom)
    assert await svc.ensure_for_bot(bot_id) is None


# ── index hyperlink ─────────────────────────────────────────────────────────

def test_letter_caption_hyperlinks_titles():
    links = {"Attack on Titan": "https://t.me/+aot"}
    cap = _letter_caption("A", ["Attack on Titan", "Akira"], links)
    # Titled entry becomes an <a href> to its private invite link …
    assert '<a href="https://t.me/+aot">' in cap
    assert "Attack on Titan" in cap
    # … while a title with no channel link stays plain bold text.
    assert "Akira" in cap
    assert cap.count("<a href=") == 1


def test_letter_caption_no_links_is_plain():
    cap = _letter_caption("A", ["Attack on Titan"], None)
    assert "<a href=" not in cap
    assert "Attack on Titan" in cap


@pytest.mark.asyncio
async def test_link_map_resolves_title_to_invite(sessionmaker, session):
    await _make_channel(sessionmaker, chat_id=-100111, anime_doc_id="a1",
                        invite_link="https://t.me/+aot")
    async with sessionmaker() as s:
        s.add(StoragePack(
            anime_title="Attack on Titan", anime_doc_id="a1", season=1,
            resolution="1080p", audio="subbed", channel_id=-100500,
            start_message_id=1, end_message_id=2,
        ))
        await s.commit()

    svc = IndexChannelService(_container(sessionmaker))
    links = await svc._links_for_titles(["Attack on Titan", "Unknown Show"])
    assert links == {"Attack on Titan": "https://t.me/+aot"}


@pytest.mark.asyncio
async def test_revoke_for_bot_revokes_and_clears_link(sessionmaker):
    bot_id = await _make_channel(
        sessionmaker, chat_id=-100111, anime_doc_id="a1",
        invite_link="https://t.me/+stale",
    )

    class Client:
        def __init__(self):
            self.calls = []

        async def revoke_chat_invite_link(self, chat_id, invite_link):
            self.calls.append((chat_id, invite_link))

    client = Client()
    svc = InviteLinkService(_container(sessionmaker))
    assert await svc.revoke_for_bot(bot_id, client=client) is True
    assert client.calls == [(-100111, "https://t.me/+stale")]

    async with sessionmaker() as s:
        row = await s.get(DistributionBot, bot_id)
        assert row.invite_link is None
