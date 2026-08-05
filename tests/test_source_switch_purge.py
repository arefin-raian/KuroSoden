"""Source-switch purge safeguard (RequestService.retry_episodes).

Regression guard for the Sabikui Bisco 360p incident: switching a request to a
different source must drop the partial packs/files the OLD source left behind
(so the watch guide stops advertising a quality that no longer exists), while
never touching content a surviving (completed / still-running) job produced.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from nekofetch.domain.enums import AudioType, JobStatus, RequestStatus
from nekofetch.infrastructure.database.postgres.models import (
    DownloadJob,
    MediaFile,
    Request,
    StoragePack,
)
from kurosoden.tests.helpers import _create_request


async def _mk_job(session, req_id, status):
    job = DownloadJob(request_id=req_id, status=status)
    session.add(job)
    await session.commit()
    return job


async def _mk_file(session, job_id, doc, res, audio, ep):
    mf = MediaFile(job_id=job_id, anime_doc_id=doc, season=1, episode=ep,
                   resolution=res, audio=audio)
    session.add(mf)
    await session.commit()
    return mf


async def _mk_pack(session, doc, res, audio):
    pack = StoragePack(anime_doc_id=doc, anime_title="Test Anime", season=1,
                       resolution=res, audio=audio, channel_id=-100123,
                       start_message_id=10, end_message_id=20, file_count=6)
    session.add(pack)
    await session.commit()
    return pack


def _container(sessionmaker):
    from types import SimpleNamespace
    # LogChannelService.__init__ reads container.config.log_channel even though
    # our monkeypatched event() short-circuits; give it a stub so construction
    # doesn't blow up.
    cfg = SimpleNamespace(log_channel=SimpleNamespace())
    return SimpleNamespace(pg_sessionmaker=sessionmaker, redis=None,
                           progress=None, admin_client=None, config=cfg)


@pytest.fixture(autouse=True)
def _silence_log(monkeypatch):
    async def _noop(self, *a, **k):
        return None
    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService.event", _noop
    )


async def test_switch_source_purges_old_partial_pack(session, sessionmaker):
    from nekofetch.services.request_service import RequestService

    doc = "anilist:999"
    req = await _create_request(session, code="REQ-9001", user_id=1,
                                anime_doc_id=doc, source="anikoto",
                                status="failed")
    # Old source: a CANCELLED job that uploaded a partial 360p sub pack.
    dead = await _mk_job(session, req.id, JobStatus.CANCELLED)
    await _mk_file(session, dead.id, doc, "360p", AudioType.SUBBED, 1)
    await _mk_file(session, dead.id, doc, "360p", AudioType.SUBBED, 2)
    await _mk_pack(session, doc, "360p", AudioType.SUBBED)
    # Surviving content: a COMPLETED job + its good 1080p pack.
    done = await _mk_job(session, req.id, JobStatus.COMPLETED)
    await _mk_file(session, done.id, doc, "1080p", AudioType.DUAL_AUDIO, 1)
    await _mk_pack(session, doc, "1080p", AudioType.DUAL_AUDIO)

    svc = RequestService(_container(sessionmaker))
    await svc.retry_episodes("REQ-9001", [3, 4], new_source="nyaa")

    async with sessionmaker() as s:
        packs = (await s.execute(
            select(StoragePack).where(StoragePack.anime_doc_id == doc)
        )).scalars().all()
        resolutions = {p.resolution for p in packs}
        assert resolutions == {"1080p"}, "360p pack should be purged, 1080p kept"

        files = (await s.execute(
            select(MediaFile).where(MediaFile.anime_doc_id == doc)
        )).scalars().all()
        assert {f.resolution for f in files} == {"1080p"}

        jobs = (await s.execute(
            select(DownloadJob).where(DownloadJob.request_id == req.id)
        )).scalars().all()
        # Cancelled job removed; completed job survives.
        assert {j.status for j in jobs} == {JobStatus.COMPLETED}

        req_now = (await s.execute(
            select(Request).where(Request.code == "REQ-9001")
        )).scalar_one()
        assert req_now.source == "nyaa"
        assert req_now.status == RequestStatus.QUEUED


async def test_same_source_retry_keeps_everything(session, sessionmaker):
    from nekofetch.services.request_service import RequestService

    doc = "anilist:888"
    req = await _create_request(session, code="REQ-9002", user_id=1,
                                anime_doc_id=doc, source="anikoto",
                                status="failed")
    dead = await _mk_job(session, req.id, JobStatus.CANCELLED)
    await _mk_file(session, dead.id, doc, "360p", AudioType.SUBBED, 1)
    await _mk_pack(session, doc, "360p", AudioType.SUBBED)

    svc = RequestService(_container(sessionmaker))
    # No source change (new_source is None) — nothing should be purged.
    await svc.retry_episodes("REQ-9002", [1], new_source=None)

    async with sessionmaker() as s:
        packs = (await s.execute(
            select(StoragePack).where(StoragePack.anime_doc_id == doc)
        )).scalars().all()
        assert {p.resolution for p in packs} == {"360p"}, "same-source retry must not purge"


async def test_switch_to_same_source_value_keeps_everything(session, sessionmaker):
    from nekofetch.services.request_service import RequestService

    doc = "anilist:777"
    req = await _create_request(session, code="REQ-9003", user_id=1,
                                anime_doc_id=doc, source="nyaa",
                                status="failed")
    dead = await _mk_job(session, req.id, JobStatus.CANCELLED)
    await _mk_file(session, dead.id, doc, "480p", AudioType.DUAL_AUDIO, 1)
    await _mk_pack(session, doc, "480p", AudioType.DUAL_AUDIO)

    svc = RequestService(_container(sessionmaker))
    # new_source equals the current source → not a real switch → keep everything.
    await svc.retry_episodes("REQ-9003", [1], new_source="nyaa")

    async with sessionmaker() as s:
        packs = (await s.execute(
            select(StoragePack).where(StoragePack.anime_doc_id == doc)
        )).scalars().all()
        assert {p.resolution for p in packs} == {"480p"}
