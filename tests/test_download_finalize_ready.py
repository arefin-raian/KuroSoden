"""_finalize_complete sets the request READY so a deferred senku task is
recoverable.

The SK8 / God-of-High-School bug: download+processing+upload finished, but the
download→senku handoff's assign deferred (no eligible admin that instant) and the
request stayed at PROCESSING. The assignment-recovery job only re-offers READY
requests, so a PROCESSING request with no senku task is stranded forever.

_finalize_complete now asserts READY on completion for a NORMAL work (so recovery
can rescue a deferred senku task), while:
  * a redo/update work is left alone (it SKIPS senku + auto-publishes; flipping it
    READY could let recovery inject a bogus senku task before publish), and
  * a terminal status (published/failed/cancelled) is never downgraded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.domain.enums import JobStatus, RequestStatus
from nekofetch.infrastructure.database.postgres.models import DownloadJob, Request


def _worker(sessionmaker):
    from nekofetch.services.download_service import DownloadWorker

    cfg = SimpleNamespace(downloads=SimpleNamespace(concurrent_downloads=5))
    container = SimpleNamespace(config=cfg, pg_sessionmaker=sessionmaker,
                                progress=None, redis=None)
    return DownloadWorker(container)


async def _seed(session, *, code, status, fd=None):
    from tests.helpers import _create_request

    req = await _create_request(session, code=code, source="ddl",
                                status=status.value if hasattr(status, "value") else status)
    req.franchise_data = fd or {}
    job = DownloadJob(request_id=req.id, status=JobStatus.RUNNING)
    session.add(job)
    await session.commit()
    return req, job


async def _status_of(sessionmaker, code):
    async with sessionmaker() as s:
        req = (await s.execute(select(Request).where(Request.code == code))).scalar_one()
        return req.status


@pytest.mark.asyncio
async def test_finalize_sets_processing_to_ready(session, sessionmaker):
    req, job = await _seed(session, code="REQ-F1", status=RequestStatus.PROCESSING)
    await _worker(sessionmaker)._finalize_complete(job.id)

    assert await _status_of(sessionmaker, "REQ-F1") == RequestStatus.READY
    async with sessionmaker() as s:
        j = (await s.execute(select(DownloadJob).where(DownloadJob.id == job.id))).scalar_one()
        assert j.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_finalize_leaves_redo_work_alone(session, sessionmaker):
    # A redo skips senku + auto-publishes → must NOT be flipped to READY.
    _req, job = await _seed(session, code="REQ-F2", status=RequestStatus.PROCESSING,
                            fd={"redo_relink": True})
    await _worker(sessionmaker)._finalize_complete(job.id)
    assert await _status_of(sessionmaker, "REQ-F2") == RequestStatus.PROCESSING


@pytest.mark.asyncio
async def test_finalize_leaves_update_entry_alone(session, sessionmaker):
    _req, job = await _seed(session, code="REQ-F3", status=RequestStatus.PROCESSING,
                            fd={"update_entry": True})
    await _worker(sessionmaker)._finalize_complete(job.id)
    assert await _status_of(sessionmaker, "REQ-F3") == RequestStatus.PROCESSING


@pytest.mark.asyncio
async def test_finalize_never_downgrades_a_terminal_status(session, sessionmaker):
    # An already-published request must not be dragged back to READY.
    _req, job = await _seed(session, code="REQ-F4", status=RequestStatus.PUBLISHED)
    await _worker(sessionmaker)._finalize_complete(job.id)
    assert await _status_of(sessionmaker, "REQ-F4") == RequestStatus.PUBLISHED
