"""Per-userbot channel-quota tracking (feature #41, two-scope creation).

When the operator opts to have a userbot create a distribution channel, we must
pick a session that still has a free public-channel slot — and never reveal which
one. A session's used slots = channels we created through it (recorded on the
bots table) + public channels it already owned before us (seeded in Redis).

These pin the quota math and the blind picker: full accounts are excluded, and
``pick_available`` returns ``None`` when every account is capped so the flow can
fall back to admin-creates-own.

The userbot account list and Redis are stubbed, so nothing external is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from nekofetch.services.channel_quota_service import ChannelQuotaService

from nekofetch.infrastructure.database.postgres.models import DistributionBot

pytestmark = [pytest.mark.asyncio]


class _FakeRedis:
    """Minimal async get/set over an in-memory dict."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _container(sessionmaker, *, accounts, cap=10, redis=None):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        redis=redis or _FakeRedis(),
        env=SimpleNamespace(
            telegram_api_id=1, telegram_api_hash="h", session_path="s",
        ),
        config=SimpleNamespace(bot=SimpleNamespace(max_channels_per_account=cap)),
        _accounts=accounts,
    )


def _svc(container):
    svc = ChannelQuotaService(container)
    # Stub the userbot-pool account discovery with the container's fake list.
    svc._account_names = lambda: list(container._accounts)  # type: ignore[assignment]
    return svc


async def _make_userbot_channel(sessionmaker, *, chat_id, account):
    async with sessionmaker() as s:
        s.add(DistributionBot(
            name=f"Ch{chat_id}", anime_doc_id=f"a{chat_id}", encrypted_token="x",
            enabled=True, is_channel=True, chat_id=chat_id,
            creation_scope="userbot", userbot_account=account,
        ))
        await s.commit()


async def test_used_slots_counts_created_and_preexisting(sessionmaker, session):
    c = _container(sessionmaker, accounts=["acc1"])
    await _make_userbot_channel(sessionmaker, chat_id=-101, account="acc1")
    await _make_userbot_channel(sessionmaker, chat_id=-102, account="acc1")
    svc = _svc(c)
    await svc.seed_preexisting("acc1", 3)

    # 2 created + 3 pre-existing = 5 used, cap 10 → 5 free.
    assert await svc.used_slots("acc1") == 5
    assert await svc.free_slots("acc1") == 5


async def test_availability_per_account(sessionmaker, session):
    c = _container(sessionmaker, accounts=["acc1", "acc2"], cap=10)
    await _make_userbot_channel(sessionmaker, chat_id=-201, account="acc1")
    svc = _svc(c)
    await svc.seed_preexisting("acc2", 10)  # acc2 already full

    avail = await svc.availability()
    assert avail == {"acc1": 9, "acc2": 0}


async def test_pick_excludes_full_accounts(sessionmaker, session):
    c = _container(sessionmaker, accounts=["acc1", "acc2"], cap=10)
    svc = _svc(c)
    await svc.seed_preexisting("acc1", 10)  # full
    # acc2 has all 10 free → the only viable pick.
    for _ in range(8):
        assert await svc.pick_available() == "acc2"


async def test_pick_returns_none_when_all_full(sessionmaker, session):
    c = _container(sessionmaker, accounts=["acc1", "acc2"], cap=5)
    svc = _svc(c)
    await svc.seed_preexisting("acc1", 5)
    await svc.seed_preexisting("acc2", 5)
    assert await svc.pick_available() is None


async def test_created_channels_alone_can_fill(sessionmaker, session):
    c = _container(sessionmaker, accounts=["acc1"], cap=2)
    await _make_userbot_channel(sessionmaker, chat_id=-301, account="acc1")
    await _make_userbot_channel(sessionmaker, chat_id=-302, account="acc1")
    svc = _svc(c)
    assert await svc.free_slots("acc1") == 0
    assert await svc.pick_available() is None
