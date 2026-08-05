"""Phase-4 tests: the download-worker coverage gate (_maybe_pause_for_coverage).

The gate sits just before publish: for admin-driven multi-source requests
(ddl / torrent) it pauses the job and asks for more links while the franchise
still has gaps, and otherwise gets out of the way. These tests pin the
pause-vs-proceed decision with a stubbed franchise mapping (so no AniList
network) and the pure coverage diff doing the real work underneath.
"""

from __future__ import annotations

import pytest

from nekofetch.domain.enums import AudioType, ContentKind, JobStatus
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry


def _worker(sessionmaker, *, enabled=True):
    from types import SimpleNamespace
    from nekofetch.services.download_service import DownloadWorker

    cfg = SimpleNamespace(
        downloads=SimpleNamespace(concurrent_downloads=5,
                                  multi_source_coverage=enabled),
    )
    container = SimpleNamespace(config=cfg, pg_sessionmaker=sessionmaker,
                                progress=None, redis=None)
    return DownloadWorker(container)


def _mapping(*entries: MappingEntry) -> FranchiseMapping:
    return FranchiseMapping(anime_doc_id="anilist:12345", root_title="Test",
                            entries=list(entries))


def _season(number: int, episodes: int) -> MappingEntry:
    return MappingEntry(kind=ContentKind.SEASON, season_number=number,
                        episodes=episodes, included=True)


async def _job_for(session, req):
    from nekofetch.infrastructure.database.postgres.models import DownloadJob
    job = DownloadJob(request_id=req.id, status=JobStatus.RUNNING)
    session.add(job)
    await session.commit()
    return job


async def _add_files(session, req, units):
    from nekofetch.infrastructure.database.postgres.models import MediaFile
    for season, episode in units:
        session.add(MediaFile(anime_doc_id=req.anime_doc_id, season=season,
                              episode=episode, resolution="1080p",
                              audio=AudioType.SUBBED, size_bytes=1))
    await session.commit()


@pytest.fixture(autouse=True)
def _stub_card(monkeypatch):
    async def _noop(self, **kw):
        return None
    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService."
        "post_missing_source_card", _noop,
    )


async def _run(worker, monkeypatch, mapping):
    # Stub the franchise reconstruction so no AniList/cache is touched.
    async def _fake(self, req):
        return mapping
    monkeypatch.setattr(
        "nekofetch.services.download_service.DownloadWorker."
        "_reconstruct_franchise_mapping", _fake,
    )


async def test_gate_disabled_returns_false(session, sessionmaker, monkeypatch):
    from tests.helpers import _create_request
    req = await _create_request(session, code="REQ-C1", source="ddl")
    job = await _job_for(session, req)
    worker = _worker(sessionmaker, enabled=False)
    await _run(worker, monkeypatch, _mapping(_season(1, 12), _season(2, 12)))

    paused = await worker._maybe_pause_for_coverage(job.id, req.code, "Test")
    assert paused is False


async def test_gate_skips_non_multi_source(session, sessionmaker, monkeypatch):
    from tests.helpers import _create_request
    req = await _create_request(session, code="REQ-C2", source="anikoto")
    job = await _job_for(session, req)
    worker = _worker(sessionmaker)
    await _run(worker, monkeypatch, _mapping(_season(1, 12), _season(2, 12)))

    paused = await worker._maybe_pause_for_coverage(job.id, req.code, "Test")
    assert paused is False


async def test_gate_proceeds_when_complete(session, sessionmaker, monkeypatch):
    from tests.helpers import _create_request
    req = await _create_request(session, code="REQ-C3", source="ddl")
    job = await _job_for(session, req)
    await _add_files(session, req, [(1, e) for e in range(1, 13)])
    worker = _worker(sessionmaker)
    await _run(worker, monkeypatch, _mapping(_season(1, 12)))

    paused = await worker._maybe_pause_for_coverage(job.id, req.code, "Test")
    assert paused is False


async def test_gate_pauses_when_season_missing(session, sessionmaker, monkeypatch):
    from sqlalchemy import select
    from nekofetch.infrastructure.database.postgres.models import DownloadJob
    from tests.helpers import _create_request

    req = await _create_request(session, code="REQ-C4", source="ddl")
    job = await _job_for(session, req)
    # Have all of S1; S2 entirely missing.
    await _add_files(session, req, [(1, e) for e in range(1, 13)])
    worker = _worker(sessionmaker)
    await _run(worker, monkeypatch, _mapping(_season(1, 12), _season(2, 12)))

    paused = await worker._maybe_pause_for_coverage(job.id, req.code, "Test")
    assert paused is True

    # Job flipped to PAUSED (not finalized/published).
    async with sessionmaker() as s:
        row = (await s.execute(
            select(DownloadJob).where(DownloadJob.id == job.id)
        )).scalar_one()
        assert row.status == JobStatus.PAUSED
