"""Userbot-created channel — admin promotion (feature #41).

When a pooled userbot creates a distribution channel, the operator can only set
the profile picture if they hold admin rights, and they can only be promoted
after they've *joined* (Telegram lets you promote existing members only). So the
factory must:

  • add + promote the Senku and Gojo bots as admins at creation (the owning
    userbot can do this directly), and
  • promote the requesting operator — with ``can_change_info`` (the right that
    lets them set the PFP) — on the channel's owning account, after they join.

The userbot pool + running bot clients are stubbed, so nothing external happens.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.infrastructure.database.postgres.models import DistributionBot
from nekofetch.services.bot_factory import BotFactory

pytestmark = [pytest.mark.asyncio]


class _FakeUserbotClient:
    """Records the promote/add calls the factory issues."""

    def __init__(self):
        self.added: list[tuple[int, object]] = []
        self.promoted: list[tuple[int, object, object]] = []

    async def add_chat_members(self, chat_id, ref):
        self.added.append((chat_id, ref))

    async def promote_chat_member(self, chat_id, ref, privileges=None):
        self.promoted.append((chat_id, ref, privileges))


class _FakePool:
    """Stands in for UserbotPool: runs the callback against a fake client and
    records which account name was targeted by ``execute_on``."""

    def __init__(self, client):
        self.client = client
        self.targeted: list[str] = []

    async def execute_on(self, name, fn):
        self.targeted.append(name)
        return await fn(self.client)


def _container(sessionmaker, *, senku_id=None, gojo_id=None):
    # A pipeline_manager exposing .senku / .gojo running clients with a cached
    # ``me`` (id populated after start), mirroring the real clients.
    def _client(uid):
        if uid is None:
            return None
        return SimpleNamespace(me=SimpleNamespace(id=uid, username=None))

    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        pipeline_manager=SimpleNamespace(senku=_client(senku_id), gojo=_client(gojo_id)),
        config=SimpleNamespace(features=SimpleNamespace(distribution_bots=True)),
    )


async def _make_userbot_channel(sessionmaker, *, chat_id, account):
    async with sessionmaker() as s:
        ch = DistributionBot(
            name="Ch", username="pub", anime_doc_id="a1", encrypted_token="x",
            enabled=True, is_channel=True, chat_id=chat_id,
            creation_scope="userbot", userbot_account=account,
        )
        s.add(ch)
        await s.commit()


async def test_promote_bots_adds_and_promotes_both(sessionmaker, session):
    fake = _FakeUserbotClient()
    pool = _FakePool(fake)
    svc = BotFactory(_container(sessionmaker, senku_id=111, gojo_id=222))
    svc._pool = pool  # inject the stub pool

    await svc._promote_bots("acct-1", -100500)

    # Both bots were added and promoted on the owning account.
    assert pool.targeted == ["acct-1"]
    assert {r for _, r in fake.added} == {111, 222}
    promoted_ids = {r for _, r, _ in fake.promoted}
    assert promoted_ids == {111, 222}
    # Bots get change-info rights (so they can manage the channel).
    for _, _, priv in fake.promoted:
        assert priv.can_change_info is True


async def test_promote_bots_noop_without_pipeline(sessionmaker, session):
    # No pipeline_manager (e.g. a bare context) → no refs → nothing promoted.
    fake = _FakeUserbotClient()
    pool = _FakePool(fake)
    svc = BotFactory(SimpleNamespace(
        pg_sessionmaker=sessionmaker, pipeline_manager=None,
        config=SimpleNamespace(features=SimpleNamespace(distribution_bots=True)),
    ))
    svc._pool = pool
    await svc._promote_bots("acct-1", -100500)
    assert fake.promoted == []


async def test_promote_operator_grants_change_info(sessionmaker, session):
    await _make_userbot_channel(sessionmaker, chat_id=-100777, account="acct-2")
    fake = _FakeUserbotClient()
    pool = _FakePool(fake)
    svc = BotFactory(_container(sessionmaker))
    svc._pool = pool

    ok = await svc.promote_operator(-100777, 99001)

    assert ok is True
    # Promoted the operator on the channel's OWNING account (blind lookup by row).
    assert pool.targeted == ["acct-2"]
    chat_id, ref, priv = fake.promoted[0]
    assert chat_id == -100777 and ref == 99001
    assert priv.can_change_info is True   # the right that lets them set the PFP


async def test_promote_operator_unknown_channel_is_false(sessionmaker, session):
    svc = BotFactory(_container(sessionmaker))
    svc._pool = _FakePool(_FakeUserbotClient())
    # No row for this chat_id → can't resolve an owning account → False.
    assert await svc.promote_operator(-100999, 99001) is False
