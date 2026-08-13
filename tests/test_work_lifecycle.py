"""WorkItem lifecycle sync — a work must advance/complete with its request.

The batch/redo flows bridge each ``WorkItem`` (WRK-N) to a ``Request`` (REQ-N)
via ``WorkItem.request_code``. As the request moves levi → senku → gojo, the
linked work must follow: levi done → reopen at ``distribute``, senku done →
reopen at ``publish``, gojo done → ``done``. Without this a work whose request
is already mid-pipeline keeps phantom-counting as an open *download* task, which
is exactly the "the board says works left but Levi's tasks are empty" report.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from kurosoden.shared.admin_assignment import AdminAssignmentEngine, AdminAvailability
from kurosoden.shared.work_service import (
    STAGE_DISTRIBUTE,
    STAGE_DOWNLOAD,
    STAGE_PUBLISH,
    STATUS_DONE,
    STATUS_OPEN,
    WorkItem,
    WorkService,
)


@pytest.fixture(autouse=True)
def _silence_log(monkeypatch):
    async def _noop(self, *a, **k):
        return None
    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService.event", _noop)


async def _linked_work(sessionmaker, *, work_code="WRK-1", request_code="REQ-1",
                       title="Work Anime"):
    svc = WorkService(sessionmaker)
    out = await svc.add_batch(1, [{"anime_title": title}])
    await svc.link(out[0].code, request_code)
    async with sessionmaker() as s:
        row = (await s.execute(select(WorkItem).where(
            WorkItem.code == out[0].code))).scalars().first()
        assert row is not None
    return out[0].code


async def _seed_admin(session, admin_id=1, stage="levi"):
    session.add(AdminAvailability(
        admin_telegram_id=admin_id, admin_name="Worker", is_available=True,
        assigned_bots=["levi"], weight=1, total_tasks_completed=0))
    await session.commit()


async def _assignment(session, request_code, stage, status="assigned",
                      admin_id=1):
    from kurosoden.shared.admin_assignment import AdminAssignment

    session.add(AdminAssignment(admin_telegram_id=admin_id,
                                request_code=request_code, stage=stage,
                                status=status))
    await session.commit()


class TestCompleteTaskSync:
    async def test_levi_complete_reopens_at_distribute(self, sessionmaker, session):
        await _seed_admin(session)
        code = await _linked_work(sessionmaker)
        await _assignment(session, "REQ-1", "levi")

        await AdminAssignmentEngine(sessionmaker).complete_task("REQ-1", "levi")

        async with sessionmaker() as s:
            w = (await s.execute(select(WorkItem).where(
                WorkItem.code == code))).scalar_one()
            assert w.stage == STAGE_DISTRIBUTE
            assert w.status == STATUS_OPEN
            assert w.assigned_admin_id is None
        svc = WorkService(sessionmaker)
        assert await svc.count_open(stage=STAGE_DOWNLOAD) == 0

    async def test_senku_complete_reopens_at_publish(self, sessionmaker, session):
        await _seed_admin(session)
        code = await _linked_work(sessionmaker)
        await _assignment(session, "REQ-1", "senku")

        await AdminAssignmentEngine(sessionmaker).complete_task("REQ-1", "senku")

        async with sessionmaker() as s:
            w = (await s.execute(select(WorkItem).where(
                WorkItem.code == code))).scalar_one()
            assert w.stage == STAGE_PUBLISH
            assert w.status == STATUS_OPEN

    async def test_gojo_complete_marks_done(self, sessionmaker, session):
        await _seed_admin(session)
        code = await _linked_work(sessionmaker)
        await _assignment(session, "REQ-1", "gojo")

        await AdminAssignmentEngine(sessionmaker).complete_task("REQ-1", "gojo")

        async with sessionmaker() as s:
            w = (await s.execute(select(WorkItem).where(
                WorkItem.code == code))).scalar_one()
            assert w.status == STATUS_DONE
        assert await WorkService(sessionmaker).count_open() == 0

    async def test_completes_work_even_without_open_assignment(
        self, sessionmaker, session,
    ):
        """A force-published request (assignment row already completed/absent)
        must still finish its linked work item — no dangling open work."""
        code = await _linked_work(sessionmaker)

        await AdminAssignmentEngine(sessionmaker).complete_task("REQ-1", "gojo")

        async with sessionmaker() as s:
            w = (await s.execute(select(WorkItem).where(
                WorkItem.code == code))).scalar_one()
            assert w.status == STATUS_DONE

    async def test_unlinked_work_untouched(self, sessionmaker, session):
        """No link → complete_task leaves work items alone (no crash, no change)."""
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": "Unlinked"}])

        await AdminAssignmentEngine(sessionmaker).complete_task("REQ-NOPE", "levi")

        async with sessionmaker() as s:
            w = (await s.execute(select(WorkItem).where(
                WorkItem.code == out[0].code))).scalar_one()
            assert w.stage == STAGE_DOWNLOAD
            assert w.status == STATUS_OPEN
