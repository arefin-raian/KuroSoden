"""Lelouch batch commit → requests only (NO work items) + task-card handoff.

Two owner-reported fixes, both in ``_commit_batch_requests`` (the extracted core
of Lelouch's ``batch|commit`` handler):

  • #106 — Submitting a batch used to create a ``WorkItem`` (WRK-N) *and* a bridged
    ``Request`` (REQ-N), so a single batch entry showed up twice on the manage
    board. A batch entry is a request and nothing more: the commit must create
    ONLY the Request (Levi's board reads AdminAssignment rows keyed on
    ``Request.code``, so the request alone surfaces the work).

  • #107 — On assignment the downloader must RECEIVE the "New Download Task" card
    (via ``notify_stage_assignment``), the same handoff the single-request path
    sends — not a silent "added to the line" summary.

Uses the real in-memory DB (so the request rows are genuinely persisted and the
FK to ``users`` is exercised) but stubs ``AdminAssignmentEngine.assign`` to a
deterministic result and counts ``notify_stage_assignment`` calls, so the test
never depends on availability-row timing (the documented assignment-cluster
flakiness).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from kurosoden.bots.lelouch.handlers.batch import _commit_batch_requests
from nekofetch.infrastructure.database.postgres.models import Request
from kurosoden.shared.work_service import WorkItem


_SUBMITTER_TG = 123456  # a telegram id with no pre-existing User row


@pytest.fixture(autouse=True)
def _sqlite_request_seq(monkeypatch):
    """SQLite has no ``request_code_seq`` (Postgres-only); hand out monotonic
    sequence numbers so ``next_sequence`` works on the in-memory test DB."""
    from nekofetch.infrastructure.repositories import request_repo
    counter = [900]

    async def _next(self):
        counter[0] += 1
        return counter[0]

    monkeypatch.setattr(request_repo.RequestRepository, "next_sequence", _next)


def _container(sessionmaker):
    """Minimal container surface ``_commit_batch_requests`` reaches."""
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        config=SimpleNamespace(security=SimpleNamespace(owner_id=_SUBMITTER_TG)),
        env=SimpleNamespace(owner_id=_SUBMITTER_TG, admin_ids=[_SUBMITTER_TG]),
        pipeline_manager=SimpleNamespace(levi=object(), lelouch=object()),
    )


def _stub_assign(monkeypatch, *, to_admin=_SUBMITTER_TG):
    """Make every assign() land on ``to_admin`` deterministically (no DB / slots)."""
    from kurosoden.shared.admin_assignment import AdminAssignmentEngine, AssignmentResult

    async def _fake_assign(self, code, stage, **kw):
        return AssignmentResult(
            admin_telegram_id=to_admin, admin_name="owner",
            tasks_active=1, tasks_completed=0,
        )

    monkeypatch.setattr(AdminAssignmentEngine, "assign", _fake_assign)


def _capture_notify(monkeypatch):
    """Replace notify_stage_assignment with a call recorder; returns the list."""
    calls: list[dict] = []

    async def _fake_notify(container, stage, assignment, code, title, **kw):
        calls.append({
            "stage": stage, "code": code, "title": title,
            "admin": assignment.admin_telegram_id, "franchise": kw.get("franchise_json"),
        })
        return 1

    import kurosoden.shared.handoff as handoff
    monkeypatch.setattr(handoff, "notify_stage_assignment", _fake_notify)
    return calls


def _keep(*titles):
    return [
        {"anime_title": t, "franchise_data": {"anilist_id": 100 + i, "title": t}}
        for i, t in enumerate(titles)
    ]


@pytest.mark.asyncio
async def test_commit_creates_requests_not_workitems(session, sessionmaker, monkeypatch):
    _stub_assign(monkeypatch)
    _capture_notify(monkeypatch)

    keep = _keep("Frieren", "Vinland Saga", "Monster")
    bridged = await _commit_batch_requests(_container(sessionmaker), _SUBMITTER_TG, keep)

    # Returns one aligned (code, title, franchise) tuple per accepted entry.
    assert [t for _c, t, _f in bridged] == ["Frieren", "Vinland Saga", "Monster"]
    assert all(c.startswith("REQ-") for c, _t, _f in bridged)
    assert [f.get("anilist_id") for _c, _t, f in bridged] == [100, 101, 102]

    async with sessionmaker() as s:
        # #106 — three real requests exist...
        req_rows = (await s.execute(select(Request))).scalars().all()
        assert {r.code for r in req_rows} == {c for c, _t, _f in bridged}
        assert all(r.status == "queued" for r in req_rows)
        # ...each keyed by AniList id (so downstream stages hit the prefetch cache),
        # and FK-valid (user_id resolves to the created submitter, not the tg id).
        assert {r.anime_doc_id for r in req_rows} == {"100", "101", "102"}
        assert all(r.user_id is not None and r.user_id != _SUBMITTER_TG for r in req_rows)

        # #106 — and ZERO work items were created.
        work_count = (await s.execute(select(func.count()).select_from(WorkItem))).scalar_one()
        assert work_count == 0


@pytest.mark.asyncio
async def test_commit_sends_task_card_per_request(session, sessionmaker, monkeypatch):
    _stub_assign(monkeypatch)
    calls = _capture_notify(monkeypatch)

    keep = _keep("Frieren", "Vinland Saga")
    bridged = await _commit_batch_requests(_container(sessionmaker), _SUBMITTER_TG, keep)

    # #107 — exactly one Levi "New Download Task" handoff per created request,
    # carrying the request code, title, and franchise (the same call the
    # single-request path makes).
    assert len(calls) == len(bridged) == 2
    assert all(c["stage"] == "levi" for c in calls)
    assert {c["code"] for c in calls} == {code for code, _t, _f in bridged}
    assert {c["title"] for c in calls} == {"Frieren", "Vinland Saga"}
    assert all(c["admin"] == _SUBMITTER_TG for c in calls)
    assert all(isinstance(c["franchise"], dict) for c in calls)


@pytest.mark.asyncio
async def test_commit_falls_back_to_owner_when_no_admin(session, sessionmaker, monkeypatch):
    """assign() → None (off-hours / no candidate) must not silently drop the task:
    it reassigns to the owner and still DMs the task card."""
    from kurosoden.shared.admin_assignment import AdminAssignmentEngine
    from kurosoden.shared.management_service import ManagementService

    async def _no_admin(self, code, stage, **kw):
        return None

    reassigned: list[tuple[str, str, int]] = []

    async def _fake_reassign(self, code, stage, to_admin, **kw):
        reassigned.append((code, stage, to_admin))
        return True

    monkeypatch.setattr(AdminAssignmentEngine, "assign", _no_admin)
    monkeypatch.setattr(ManagementService, "reassign", _fake_reassign)
    calls = _capture_notify(monkeypatch)

    bridged = await _commit_batch_requests(
        _container(sessionmaker), _SUBMITTER_TG, _keep("Frieren"))

    assert len(bridged) == 1
    # Reassigned to the owner...
    assert reassigned == [(bridged[0][0], "levi", _SUBMITTER_TG)]
    # ...and the task card still went out, addressed to the owner.
    assert len(calls) == 1
    assert calls[0]["admin"] == _SUBMITTER_TG and calls[0]["stage"] == "levi"


@pytest.mark.asyncio
async def test_commit_skips_blank_titles(session, sessionmaker, monkeypatch):
    """A blank-title entry is skipped so codes/titles/franchise stay aligned and no
    empty request is created."""
    _stub_assign(monkeypatch)
    calls = _capture_notify(monkeypatch)

    keep = [
        {"anime_title": "Frieren", "franchise_data": {"anilist_id": 1}},
        {"anime_title": "   ", "franchise_data": {"anilist_id": 2}},  # blank → skipped
        {"anime_title": "Monster", "franchise_data": {"anilist_id": 3}},
    ]
    bridged = await _commit_batch_requests(_container(sessionmaker), _SUBMITTER_TG, keep)

    assert [t for _c, t, _f in bridged] == ["Frieren", "Monster"]
    assert len(calls) == 2
    async with sessionmaker() as s:
        count = (await s.execute(select(func.count()).select_from(Request))).scalar_one()
        assert count == 2
