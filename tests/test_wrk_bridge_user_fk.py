"""Regression: the WRK→Request bridge must resolve telegram_id → users.id.

``Request.user_id`` is a foreign key onto ``users.id`` (the internal PK), NOT the
Telegram id. The redo bridge (``RedoService._requeue_as_work``) and the Lelouch
batch bridge both used to write the raw Telegram id straight into ``user_id``.

Under SQLite-with-FKs-off (the default test engine) that INSERT silently
succeeds, so the plumbing tests passed — but production Postgres enforces the FK,
so every bridged INSERT raised ``FOREIGN KEY constraint failed``. That exception
was swallowed by the bridge's broad ``except`` (logged as ``redo.requeue_failed``
/ ``lelouch.batch.bridge_failed``): the WorkItem was created but NO Request and NO
AdminAssignment, so the work never appeared on Levi's ``/tasks`` board. Normal
``/request`` worked because ``RequestService`` resolves ``get_by_telegram_id().id``.

This test pins the fix by running the real redo bridge against an engine with
foreign-key enforcement ON (mirroring Postgres), and asserting a User row is
created for the owner, the Request FKs its internal ``id``, and an assignment
lands so the task is actually visible.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nekofetch.infrastructure.database.postgres.base import Base
from nekofetch.infrastructure.database.postgres.models import Request, User
from kurosoden.shared.admin_assignment import (
    AdminAssignment,
    AdminAssignmentEngine,
    AdminAvailability,
)


@pytest_asyncio.fixture
async def fk_sessionmaker():
    """A private SQLite engine with FK enforcement ON, like production Postgres."""
    import kurosoden.shared.models  # noqa: F401 — register shared tables
    import kurosoden.shared.work_service  # noqa: F401 — register WorkItem

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_con, _rec):  # noqa: ANN001
        dbapi_con.execute("PRAGMA foreign_keys=ON")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    yield sm
    await eng.dispose()


def _container(sm):
    cfg = SimpleNamespace(
        security=SimpleNamespace(owner_id=42),
        log_channel=SimpleNamespace(),
    )
    env = SimpleNamespace(admin_ids=[42], storage_path=None)
    return SimpleNamespace(
        pg_sessionmaker=sm, redis=None, config=cfg, env=env,
        admin_client=None, pipeline_manager=None, progress=None,
    )


@pytest.fixture(autouse=True)
def _silence_and_seq(monkeypatch):
    async def _noop(self, *a, **k):
        return None
    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService.event", _noop)
    # Avoid the Postgres-only request_code_seq.
    from nekofetch.infrastructure.repositories import request_repo
    _seq = [700]

    async def _next(self):
        _seq[0] += 1
        return _seq[0]

    monkeypatch.setattr(request_repo.RequestRepository, "next_sequence", _next)


@pytest.mark.asyncio
async def test_redo_bridge_creates_visible_task_under_fk_enforcement(fk_sessionmaker):
    sm = fk_sessionmaker
    OWNER_TG = 42  # a Telegram id — deliberately different from any users.id

    # An available downloader so the assignment has a real target during the day;
    # during quiet hours the bridge's owner-fallback still lands the row.
    async with sm() as s:
        s.add(AdminAvailability(
            admin_telegram_id=200, admin_name="Worker", is_available=True,
            assigned_bots=["levi"], weight=1, total_tasks_completed=0))
        await s.commit()

    from kurosoden.shared.redo_service import RedoService

    plan = await RedoService(_container(sm)).submit(
        OWNER_TG, "Redone Anime", "anilist:777", {"anilist_id": 777})
    assert plan.state.value == "absent"

    async with sm() as s:
        # A User row was created for the owner; its internal id is NOT the tg id.
        owner = (await s.execute(
            select(User).where(User.telegram_id == OWNER_TG))).scalar_one()
        assert owner.id != OWNER_TG  # sanity: PK differs from telegram id

        req = (await s.execute(select(Request))).scalar_one()
        assert req.user_id == owner.id          # FK points at users.id, not tg id
        assert req.status.value == "queued"

        # An assignment exists → the task is visible on someone's board.
        assignments = (await s.execute(select(AdminAssignment))).scalars().all()
        assert len(assignments) == 1
        assert assignments[0].stage == "levi"


@pytest.mark.asyncio
async def test_bare_telegram_id_would_violate_fk(fk_sessionmaker):
    """Guard: prove the OLD behaviour (user_id = telegram id) really was invalid,
    so this test fails loudly if someone reintroduces it."""
    from nekofetch.domain.enums import DownloadScope, RequestStatus

    sm = fk_sessionmaker
    async with sm() as s:
        s.add(User(telegram_id=42, first_name="Owner"))
        await s.commit()

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with sm() as s:
            s.add(Request(
                code="REQ-BAD", user_id=42,  # telegram id, NOT users.id
                anime_doc_id="a:1", anime_title="X", source="", source_ref="",
                scope=DownloadScope.ENTIRE_SERIES.value, season=None, episodes=None,
                franchise_data={}, status=RequestStatus.QUEUED))
            await s.commit()
