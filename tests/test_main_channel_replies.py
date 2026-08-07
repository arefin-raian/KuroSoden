from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.services.main_channel_service import MainChannelService, PublicationFacts


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace(id=900)


def _container(sessionmaker, client):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        admin_client=client,
        config=SimpleNamespace(
            main_channel=SimpleNamespace(enabled=True, channel_id=-100500),
        ),
    )


@pytest.mark.asyncio
async def test_reply_update_targets_main_post_and_has_no_keyboard(sessionmaker, session, monkeypatch):
    from nekofetch.infrastructure.database.postgres.models import ChannelPost

    session.add(ChannelPost(
        anime_doc_id="anilist:123", main_channel_id=-100500, main_message_id=77,
    ))
    await session.commit()
    client = FakeClient()
    svc = MainChannelService(_container(sessionmaker, client))
    monkeypatch.setattr(
        svc, "gather_facts",
        lambda _doc: _facts(),
    )

    assert await svc.reply_update(
        "anilist:123", "Season 2", 12, "720p, 1080p", "https://t.me/+fresh",
    ) is True

    args, kwargs = client.sent[0]
    assert args[0] == -100500
    assert "Season 2" in args[1]
    assert "https://t.me/+fresh" in args[1]
    assert kwargs["reply_to_message_id"] == 77
    assert kwargs.get("reply_markup") is None


def _facts():
    return PublicationFacts(anime_doc_id="anilist:123", title="Example Anime")


@pytest.mark.asyncio
async def test_reply_recovery_is_localized_and_buttonless(sessionmaker, session):
    from nekofetch.infrastructure.database.postgres.models import ChannelPost

    session.add(ChannelPost(
        anime_doc_id="anilist:123", main_channel_id=-100500, main_message_id=77,
    ))
    await session.commit()
    client = FakeClient()
    svc = MainChannelService(_container(sessionmaker, client))

    assert await svc.reply_recovery(
        "anilist:123", "Example Anime", "https://t.me/+new-channel",
    ) is True
    _args, kwargs = client.sent[0]
    assert kwargs["reply_to_message_id"] == 77
    assert kwargs.get("reply_markup") is None
    assert "restored" in _args[1]
    assert "https://t.me/+new-channel" in _args[1]
