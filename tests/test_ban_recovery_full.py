from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pyrogram import Client
from pyrogram.handlers import CallbackQueryHandler
from pyrogram.handlers.conversation_handler import ConversationHandler
from pyrogram.types import CallbackQuery

from nekofetch.infrastructure.database.postgres.models import DistributionBot


class FakeFSM:
    def __init__(self):
        self.values = {}
        self.cleared = []

    async def set(self, user_id, state, **data):
        self.values[user_id] = (state, data)

    async def get(self, user_id):
        return self.values.get(user_id, (None, {}))

    async def clear(self, user_id):
        self.cleared.append(user_id)
        self.values.pop(user_id, None)


@pytest.mark.asyncio
async def test_human_offer_uses_senku_recovery_card_and_artwork(monkeypatch):
    from bots.gojo.handlers import tasks

    sent = []

    class Admin:
        telegram_id = 4242
        is_available = True
        on_break = False
        active_tasks = 0

    class Management:
        def __init__(self, _sm):
            pass

        async def list_admins(self, *, stage):
            assert stage == "gojo"
            return [Admin()]

    import kurosoden.shared.management_service as management_module
    monkeypatch.setattr(management_module, "ManagementService", Management)
    async def capture_screen(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr(tasks, "send_screen", capture_screen)
    monkeypatch.setattr(tasks, "pick_artwork", lambda bot: f"{bot}-art")

    recipient = await tasks.offer_human_recovery(
        object(),
        SimpleNamespace(pg_sessionmaker=None, env=SimpleNamespace(admin_ids=[])),
        FakeFSM(),
        "anilist:123",
        "Example Anime",
    )

    assert recipient == 4242
    assert sent
    screen = sent[0][0][2]
    assert screen.image == "senku-art"
    assert "Restoring a banned channel" in screen.caption
    callbacks = [button.callback_data for row in screen.keyboard.inline_keyboard for button in row]
    assert any("gojo|recovery|own|anilist:123" == data for data in callbacks)
    assert any("gojo|recovery|auto|anilist:123" == data for data in callbacks)


@pytest.mark.asyncio
async def test_human_handback_verifies_cleans_and_calls_existing_restore_path(monkeypatch):
    from bots.gojo.handlers import tasks

    class FakeChat:
        id = -100999
        title = "Replacement Anime"
        username = "replacement_anime"

    class FakeClient:
        async def get_chat(self, target):
            assert target == "@replacement_anime"
            return FakeChat()

    verified = []
    swept = []
    handed_back = []

    async def verify(_client, _container, chat_id):
        verified.append(chat_id)
        return True, ""

    async def sweep(_client, chat_id):
        swept.append(chat_id)
        return 2

    class FakePublisher:
        _sweep_service_notices = staticmethod(sweep)

    class FakeOrchestrator:
        def __init__(self, _container):
            pass

        async def recover_human_channel(self, anime_doc_id, chat_id, **kwargs):
            handed_back.append((anime_doc_id, chat_id, kwargs))
            return SimpleNamespace(username="replacement_anime", name="Replacement Anime")

    monkeypatch.setattr(tasks, "_verify_recovery_channel", verify)
    import kurosoden.shared.senku_publisher as publisher_module
    monkeypatch.setattr(publisher_module, "SenkuPublisher", FakePublisher)
    monkeypatch.setattr(
        "nekofetch.services.bot_orchestrator.BotOrchestratorService",
        FakeOrchestrator,
    )

    fsm = FakeFSM()
    telegram_client = FakeClient()
    ok, result = await tasks._run_human_recovery(
        telegram_client,
        SimpleNamespace(),
        fsm,
        4242,
        {"anime_doc_id": "anilist:123", "old_name": "Old Anime"},
        "@replacement_anime",
    )

    assert ok is True
    assert "Recovery complete" in result
    assert verified == [-100999]
    assert swept == [-100999]
    assert handed_back == [
        (
            "anilist:123",
            -100999,
            {
                "username": "replacement_anime",
                "name": "Replacement Anime",
                "client": telegram_client,
            },
        )
    ]
    assert fsm.cleared == [4242]


@pytest.mark.asyncio
async def test_recovery_claim_release_is_owner_aware():
    from bots.gojo.handlers import tasks

    class Redis:
        def __init__(self, value):
            self.value = value
            self.deleted = False

        async def eval(self, _script, _numkeys, _key, token):
            if self.value == token:
                self.deleted = True
            return 1 if self.deleted else 0

        async def set(self, *_args, **_kwargs):
            return True

    current = Redis("anilist:new")
    container = SimpleNamespace(redis=current)
    await tasks._release_recovery_admin(container, 4242, "anilist:old")
    assert current.deleted is False

    owned = Redis("anilist:old")
    container.redis = owned
    await tasks._release_recovery_admin(container, 4242, "anilist:old")
    assert owned.deleted is True


def test_recovery_privileges_require_operational_rights():
    from bots.gojo.handlers import tasks

    admin = SimpleNamespace(
        status=SimpleNamespace(value="administrator"),
        privileges=SimpleNamespace(
            can_change_info=True,
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_pin_messages=True,
            can_promote_members=True,
        ),
    )
    limited = SimpleNamespace(
        status=SimpleNamespace(value="administrator"),
        privileges=SimpleNamespace(can_post_messages=True),
    )
    assert tasks._has_recovery_privileges(admin)
    assert not tasks._has_recovery_privileges(limited)


@pytest.mark.asyncio
async def test_real_gojo_registration_routes_recovery_callback():
    from bots.gojo.handlers import tasks

    client = Client(
        "recovery-routing-test",
        api_id=12345,
        api_hash="0" * 32,
        bot_token="1:AAAA",
        in_memory=True,
    )
    container = SimpleNamespace(redis=None)
    tasks.register(client, container)
    for _ in range(20):
        await asyncio.sleep(0)

    handlers = [
        h for group in client.dispatcher.groups.values() for h in group
        if isinstance(h, CallbackQueryHandler)
        and not isinstance(h, ConversationHandler)
        and h.filters is not None
    ]
    query = CallbackQuery(client=client, id="1", from_user=None, chat_instance="x")
    query.data = "gojo|recovery|own|anilist:123"
    matched = False
    for handler in handlers:
        try:
            if await handler.filters(client, query):
                matched = True
                break
        except Exception:
            continue
    assert matched


@pytest.mark.asyncio
async def test_recovery_promotions_use_the_verified_gojo_client():
    from bots.gojo.handlers import tasks

    class PipelineBot:
        def __init__(self, user_id):
            self.user_id = user_id

        async def get_me(self):
            return SimpleNamespace(id=self.user_id)

    class GojoClient:
        def __init__(self):
            self.added = []
            self.promoted = []

        async def get_chat_member(self, _chat_id, _user_id):
            # Force the promotion branch for both pipeline bots.
            return SimpleNamespace(
                status=SimpleNamespace(value="member"), privileges=None,
            )

        async def add_chat_members(self, chat_id, user_id):
            self.added.append((chat_id, user_id))

        async def promote_chat_member(self, chat_id, user_id, *, privileges):
            self.promoted.append((chat_id, user_id, privileges))

    class WrongClient:
        async def add_chat_members(self, *_args, **_kwargs):
            raise AssertionError("recovery must act through Gojo")

        async def promote_chat_member(self, *_args, **_kwargs):
            raise AssertionError("recovery must act through Gojo")

    gojo = GojoClient()
    container = SimpleNamespace(
        pipeline_manager=SimpleNamespace(
            senku=PipelineBot(101), gojo=PipelineBot(202),
        ),
        admin_client=WrongClient(),
    )

    missing = await tasks._promote_recovery_bots(gojo, container, -100123)

    assert missing == []
    assert gojo.added == [(-100123, 101), (-100123, 202)]
    assert [item[:2] for item in gojo.promoted] == [
        (-100123, 101), (-100123, 202),
    ]


@pytest.mark.asyncio
async def test_reused_recovery_row_clears_stale_invite(sessionmaker):
    from nekofetch.services.bot_management_service import BotManagementService

    async with sessionmaker() as session:
        row = DistributionBot(
            name="Old replacement", username="old_replacement",
            anime_doc_id="anilist:123", encrypted_token="enc",
            enabled=False, is_channel=True, chat_id=-100777,
            invite_link="https://t.me/+stale",
            creation_scope="human_recovery",
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    container = SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        cipher=SimpleNamespace(encrypt=lambda value: f"enc:{value}"),
    )
    info = await BotManagementService(container).register_channel(
        -100777,
        name="Fresh replacement",
        username="fresh_replacement",
        anime_doc_id="anilist:123",
        creation_scope="human_recovery",
    )

    assert info.id == row_id
    async with sessionmaker() as session:
        row = await session.get(DistributionBot, row_id)
        assert row.enabled is True
        assert row.invite_link is None
        assert row.username == "fresh_replacement"
