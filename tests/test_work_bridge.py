"""Lazy WRK → Request bridge — opening a work item always yields a REQ.

Covers (shared/work_bridge.py):
  • Fresh bridge: an unlinked work with no matching request gets a QUEUED
    Request, a WorkItem link, a claim for the opener, and a Levi assignment.
  • Legacy reuse: an unlinked work whose anime already has a live request is
    linked to it instead of creating a duplicate.
  • Unknown codes return None; published requests are never reused.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.domain.enums import RequestStatus
from nekofetch.infrastructure.database.postgres.models import Request, User
from kurosoden.shared.admin_assignment import AdminAssignment, AdminAvailability
from kurosoden.shared.work_service import WorkItem, WorkService


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
    from nekofetch.infrastructure.repositories import request_repo

    _seq = [700]

    async def _next(self):
        _seq[0] += 1
        return _seq[0]

    monkeypatch.setattr(request_repo.RequestRepository, "next_sequence", _next)


async def _seed_downloader(session, admin_id=200):
    session.add(AdminAvailability(
        admin_telegram_id=admin_id, admin_name="Worker", is_available=True,
        assigned_bots=["levi"], weight=1, total_tasks_completed=0))
    await session.commit()


class TestBridge:
    async def test_fresh_bridge_creates_request_and_assignment(
        self, sessionmaker, session,
    ):
        from kurosoden.shared.work_bridge import ensure_work_request

        await _seed_downloader(session)
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{
            "anime_title": "Bridged Fresh",
            "franchise_data": {"anilist_id": 12345, "title": "Bridged Fresh"},
        }])
        work_code = out[0].code
        assert out[0].request_code is None

        req = await ensure_work_request(_container(sessionmaker), work_code, 200)

        assert req is not None
        assert req.anime_title == "Bridged Fresh"
        async with sessionmaker() as s:
            w = (await s.execute(select(WorkItem).where(
                WorkItem.code == work_code))).scalar_one()
            assert w.request_code == req.code
            assert w.assigned_admin_id == 200          # claimed for the opener
            assert w.status == "claimed"
            assigns = (await s.execute(select(AdminAssignment).where(
                AdminAssignment.request_code == req.code))).scalars().all()
            assert any(a.stage == "levi" and a.status in ("assigned",)
                       for a in assigns)
            # The request FK points at users.id, never the raw telegram id.
            owner = (await s.execute(select(User).where(
                User.telegram_id == 200))).scalar_one()
            stored = (await s.execute(select(Request).where(
                Request.code == req.code))).scalar_one()
            assert stored.user_id == owner.id

    async def test_legacy_work_reuses_live_request(self, sessionmaker, session):
        """An unlinked work for an anime with a live request links to it — the
        exact WRK-5 → REQ-1079 case (redo created the request; the work row was
        never linked) — instead of duplicating the bridge."""
        from nekofetch.infrastructure.database.postgres.models import (
            Request,
            User,
        )
        from nekofetch.domain.enums import DownloadScope
        from kurosoden.shared.work_bridge import ensure_work_request

        await _seed_downloader(session)
        # The live request (already past download: levi completed, senku assigned).
        session.add(User(id=1, telegram_id=999, first_name="Reuser"))
        session.add(Request(
            code="REQ-1079", user_id=1, anime_doc_id="151514",
            anime_title="Orb: On the Movements of the Earth", source="",
            scope=DownloadScope.ENTIRE_SERIES.value,
            status=RequestStatus.PROCESSING,
        ))
        session.add(AdminAssignment(admin_telegram_id=200, request_code="REQ-1079",
                                    stage="levi", status="completed"))
        await session.commit()

        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{
            "anime_title": "Orb: On the Movements of the Earth",
            "anime_doc_id": "151514",
            "franchise_data": {"anilist_id": 151514},
        }])
        work_code = out[0].code

        req = await ensure_work_request(_container(sessionmaker), work_code, 200)

        assert req is not None
        assert req.code == "REQ-1079"          # reused, not duplicated
        async with sessionmaker() as s:
            reqs = (await s.execute(select(Request).where(
                Request.anime_doc_id == "151514"))).scalars().all()
            assert len(reqs) == 1               # no duplicate request created
            w = (await s.execute(select(WorkItem).where(
                WorkItem.code == work_code))).scalar_one()
            assert w.request_code == "REQ-1079"

    async def test_unknown_work_code_returns_none(self, sessionmaker, session):
        from kurosoden.shared.work_bridge import ensure_work_request

        assert await ensure_work_request(
            _container(sessionmaker), "WRK-nope", 200) is None

    async def test_published_request_not_reused(self, sessionmaker, session):
        """A work whose anime is already PUBLISHED must not link to that request
        — the title's pipeline is done, so a leftover work is stale, not a redo."""
        from nekofetch.infrastructure.database.postgres.models import Request, User
        from nekofetch.domain.enums import DownloadScope
        from kurosoden.shared.work_bridge import ensure_work_request

        await _seed_downloader(session)
        session.add(User(id=1, telegram_id=999, first_name="Reuser"))
        session.add(Request(
            code="REQ-1078", user_id=1, anime_doc_id="131646",
            anime_title="The Case Study of Vanitas", source="",
            scope=DownloadScope.ENTIRE_SERIES.value,
            status=RequestStatus.PUBLISHED,
        ))
        await session.commit()

        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{
            "anime_title": "The Case Study of Vanitas",
            "anime_doc_id": "131646",
            "franchise_data": {"anilist_id": 131646},
        }])

        req = await ensure_work_request(_container(sessionmaker), out[0].code, 200)

        # No reuse → a fresh bridge was created for this leftover work.
        assert req is not None
        assert req.code != "REQ-1078"
