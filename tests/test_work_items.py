"""Tests for kurosoden/shared/work_service.py — admin-marshalled work items.

Covers (per PLAN §4):
  • add_batch → N work_items, correct fields, blank titles skipped
  • WorkItemView shape + code sequence
  • Work never counts against a user's request limit (isolation)
  • Queue-drain: next_for_stage pulls independently per stage, so a stalled
    downstream stage never starves the downloader
  • claim / advance / complete lifecycle + count_open / list_open
"""

from __future__ import annotations

import pytest

from kurosoden.shared.work_service import (
    WorkService, WorkItem, WorkItemView,
    STAGE_DOWNLOAD, STAGE_DISTRIBUTE, STAGE_PUBLISH,
    STATUS_OPEN, STATUS_CLAIMED, STATUS_DONE,
)


# ── add_batch ──────────────────────────────────────────────────────────────────

class TestAddBatch:
    async def test_creates_one_per_title(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(42, [
            {"anime_title": "Frieren"},
            {"anime_title": "Vinland Saga"},
            {"anime_title": "Dandadan"},
        ])
        assert len(out) == 3
        assert all(isinstance(v, WorkItemView) for v in out)
        assert {v.anime_title for v in out} == {"Frieren", "Vinland Saga", "Dandadan"}
        assert all(v.stage == STAGE_DOWNLOAD for v in out)
        assert all(v.status == STATUS_OPEN for v in out)

    async def test_skips_blank_titles(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [
            {"anime_title": "Real"},
            {"anime_title": "   "},
            {"anime_title": ""},
            {"title": "AltKey"},  # accepts 'title' alias
        ])
        titles = {v.anime_title for v in out}
        assert titles == {"Real", "AltKey"}

    async def test_carries_franchise_and_doc(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(7, [{
            "anime_title": "Bleach",
            "anime_doc_id": "doc-99",
            "franchise_data": {"mal_id": 269, "seasons": 3},
        }])
        assert len(out) == 1
        from sqlalchemy import select
        async with sessionmaker() as s:
            row = (await s.execute(select(WorkItem).where(
                WorkItem.code == out[0].code))).scalar_one()
            assert row.anime_doc_id == "doc-99"
            assert row.franchise_data == {"mal_id": 269, "seasons": 3}
            assert row.added_by_admin_id == 7

    async def test_codes_are_sequential_and_unique(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": f"T{i}"} for i in range(5)])
        codes = [v.code for v in out]
        assert len(set(codes)) == 5
        assert all(c.startswith("WRK-") for c in codes)

    async def test_empty_batch(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        assert await svc.add_batch(1, []) == []


# ── Request-limit isolation ─────────────────────────────────────────────────────

class TestLimitIsolation:
    """Work items must not touch the user request-limit accounting."""

    async def test_work_items_do_not_populate_requests(self, sessionmaker, session):
        from kurosoden.shared.work_service import WorkItem
        from nekofetch.infrastructure.database.postgres.models import Request
        from sqlalchemy import func, select

        svc = WorkService(sessionmaker)
        await svc.add_batch(1, [{"anime_title": f"T{i}"} for i in range(4)])

        async with sessionmaker() as s:
            work_count = (await s.execute(
                select(func.count()).select_from(WorkItem))).scalar_one()
            req_count = (await s.execute(
                select(func.count()).select_from(Request))).scalar_one()
        assert work_count == 4
        assert req_count == 0  # zero user requests created

    async def test_user_limit_respected_alongside_work(self, sessionmaker, session):
        """A user with an active request is still blocked from a 2nd, regardless
        of how many work items exist in the line."""
        from kurosoden.tests.helpers import _create_user, _create_request

        svc = WorkService(sessionmaker)
        await svc.add_batch(1, [{"anime_title": f"W{i}"} for i in range(3)])

        user = await _create_user(session, telegram_id=555)
        await _create_request(session, user_id=user.id, anime_title="UserPick")

        # The user's active-request count is 1 (their own), unaffected by 3 work items.
        from nekofetch.infrastructure.database.postgres.models import Request
        from sqlalchemy import func, select
        async with sessionmaker() as s:
            active = (await s.execute(
                select(func.count()).select_from(Request).where(
                    Request.user_id == user.id))).scalar_one()
        assert active == 1


# ── Queue-drain guarantee ───────────────────────────────────────────────────────

class TestQueueDrain:
    async def test_next_for_stage_is_fifo(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        await svc.add_batch(1, [{"anime_title": "First"}, {"anime_title": "Second"}])
        nxt = await svc.next_for_stage(STAGE_DOWNLOAD)
        assert nxt is not None and nxt.anime_title == "First"

    async def test_stalled_downstream_never_starves_downloader(self, sessionmaker, session):
        """Publish stage down (items stuck there) must not stop download draining."""
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [
            {"anime_title": "Stuck"}, {"anime_title": "Fresh"}])
        # Push 'Stuck' all the way to publish and leave it (downstream is "down").
        await svc.advance(out[0].code, STAGE_PUBLISH)
        # Download stage still yields the other item.
        nxt = await svc.next_for_stage(STAGE_DOWNLOAD)
        assert nxt is not None and nxt.anime_title == "Fresh"
        # Publish stage independently still shows the stuck one.
        pub = await svc.next_for_stage(STAGE_PUBLISH)
        assert pub is not None and pub.anime_title == "Stuck"

    async def test_next_for_stage_empty(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        assert await svc.next_for_stage(STAGE_DOWNLOAD) is None


# ── Lifecycle ───────────────────────────────────────────────────────────────────

class TestLifecycle:
    async def test_claim_then_complete(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": "One"}])
        code = out[0].code
        assert await svc.claim(code, 900) is True
        assert await svc.claim(code, 901) is False  # already claimed
        assert await svc.complete(code) is True
        assert await svc.count_open() == 0

    async def test_advance_reopens_for_next_stage(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": "Two"}])
        code = out[0].code
        await svc.claim(code, 900)
        assert await svc.advance(code, STAGE_DISTRIBUTE) is True
        nxt = await svc.next_for_stage(STAGE_DISTRIBUTE)
        assert nxt is not None and nxt.code == code
        assert nxt.status == STATUS_OPEN
        assert nxt.assigned_admin_id is None

    async def test_count_and_list_open(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": f"T{i}"} for i in range(3)])
        assert await svc.count_open() == 3
        await svc.complete(out[0].code)
        assert await svc.count_open() == 2
        assert len(await svc.list_open()) == 2

    async def test_missing_code_ops_return_false(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        assert await svc.claim("WRK-nope", 1) is False
        assert await svc.advance("WRK-nope", STAGE_PUBLISH) is False
        assert await svc.complete("WRK-nope") is False


# ── Request link (WRK ↔ REQ) ─────────────────────────────────────────────────

class TestRequestLink:
    async def test_link_sets_and_clears(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": "Linked"}])
        code = out[0].code

        assert await svc.link(code, "REQ-42") is True
        w = await svc.get(code)
        assert w is not None and w.request_code == "REQ-42"
        assert w.code == code

        assert await svc.link(code, None) is True
        assert (await svc.get(code)).request_code is None

    async def test_link_missing_code(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        assert await svc.link("WRK-nope", "REQ-1") is False
        assert await svc.get("WRK-nope") is None

    async def test_count_and_list_filter_by_stage(self, sessionmaker, session):
        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": f"T{i}"} for i in range(3)])
        # Push one item to the publish pool — it must stop counting as download.
        await svc.advance(out[0].code, STAGE_PUBLISH)

        assert await svc.count_open() == 3                      # all stages
        assert await svc.count_open(stage=STAGE_DOWNLOAD) == 2  # Levi's pool
        assert await svc.count_open(stage=STAGE_PUBLISH) == 1
        assert len(await svc.list_open(stage=STAGE_DOWNLOAD)) == 2
        assert {w.code for w in await svc.list_open(stage=STAGE_PUBLISH)} == {
            out[0].code}

    async def test_reconcile_links_orphaned_work_to_live_request(
        self, sessionmaker, session,
    ):
        """A work whose bridge never wrote the link gets linked to the anime's
        live request (legacy reconciliation), and its stage is synced from the
        request's completed assignments so it stops counting as download work."""
        from nekofetch.infrastructure.database.postgres.models import Request
        from nekofetch.domain.enums import RequestStatus, DownloadScope

        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{
            "anime_title": "Legacy",
            "anime_doc_id": "151514",
            "franchise_data": {"anilist_id": 151514},
        }])

        # The bridged request already moved past download (senku is assigned).
        async with sessionmaker() as s:
            s.add(Request(code="REQ-1079", user_id=1, anime_doc_id="151514",
                          anime_title="Legacy", source="", scope=DownloadScope.ENTIRE_SERIES.value,
                          status=RequestStatus.PROCESSING))
            await s.commit()

        assert (await svc.get(out[0].code)).request_code is None
        touched = await svc.reconcile_links()
        assert touched >= 1

        w = await svc.get(out[0].code)
        assert w.request_code == "REQ-1079"
        # No completed levi assignment → still download/open (sync is additive).
        assert w.stage == STAGE_DOWNLOAD
        assert w.status == STATUS_OPEN

    async def test_reconcile_syncs_stage_from_completed_assignments(
        self, sessionmaker, session,
    ):
        """A linked work whose request already finished levi is reopened at the
        distribute stage — it must no longer count as an open download task."""
        from kurosoden.shared.admin_assignment import AdminAssignment
        from nekofetch.infrastructure.database.postgres.models import Request
        from nekofetch.domain.enums import RequestStatus, DownloadScope

        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": "Moving",
                                       "anime_doc_id": "doc-x"}])
        await svc.link(out[0].code, "REQ-500")

        async with sessionmaker() as s:
            s.add(Request(code="REQ-500", user_id=1, anime_doc_id="doc-x",
                          anime_title="Moving", source="",
                          scope=DownloadScope.ENTIRE_SERIES.value,
                          status=RequestStatus.PROCESSING))
            s.add(AdminAssignment(admin_telegram_id=1, request_code="REQ-500",
                                  stage="levi", status="completed"))
            s.add(AdminAssignment(admin_telegram_id=1, request_code="REQ-500",
                                  stage="senku", status="assigned"))
            await s.commit()

        await svc.reconcile_links()
        w = await svc.get(out[0].code)
        assert w.stage == STAGE_DISTRIBUTE
        assert w.status == STATUS_OPEN
        assert await svc.count_open(stage=STAGE_DOWNLOAD) == 0

    async def test_reconcile_ignores_published_requests(self, sessionmaker, session):
        """A completed/published request is terminal — a leftover work must NOT
        be linked to it (the anime is done; the work is stale)."""
        from nekofetch.infrastructure.database.postgres.models import Request
        from nekofetch.domain.enums import RequestStatus, DownloadScope

        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": "Done",
                                       "anime_doc_id": "doc-done"}])
        async with sessionmaker() as s:
            s.add(Request(code="REQ-999", user_id=1, anime_doc_id="doc-done",
                          anime_title="Done", source="",
                          scope=DownloadScope.ENTIRE_SERIES.value,
                          status=RequestStatus.PUBLISHED))
            await s.commit()

        await svc.reconcile_links()
        assert (await svc.get(out[0].code)).request_code is None

    async def test_reconcile_closes_work_when_request_published_no_assignment(
        self, sessionmaker,
    ):
        """A redo-relink of a published title auto-publishes in Levi and SKIPS the
        Senku/Gojo assignments, so there's no `gojo=completed` row. The linked
        work must still close (status=done) off the terminal Request.status —
        otherwise it stays a phantom open 'download' (the REQ-1079 stuck-on-board
        symptom). Regression for the _stage_from_assignments terminal-status path.
        """
        from nekofetch.infrastructure.database.postgres.models import Request
        from nekofetch.domain.enums import RequestStatus, DownloadScope

        svc = WorkService(sessionmaker)
        out = await svc.add_batch(1, [{"anime_title": "Relinked",
                                       "anime_doc_id": "151514"}])
        await svc.link(out[0].code, "REQ-1079")
        async with sessionmaker() as s:
            s.add(Request(code="REQ-1079", user_id=1, anime_doc_id="151514",
                          anime_title="Relinked", source="",
                          scope=DownloadScope.ENTIRE_SERIES.value,
                          status=RequestStatus.PUBLISHED))
            await s.commit()

        # No AdminAssignment rows at all — mirrors the relink path.
        await svc.reconcile_links()

        w = await svc.get(out[0].code)
        assert w.status == STATUS_DONE, "published request must close its work"
        # And it must no longer appear as open work on the board.
        assert all(v.code != out[0].code for v in await svc.list_open(limit=200))

