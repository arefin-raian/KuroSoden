"""Queue service — turns approved requests into download jobs and reports the queue.

Provides the data behind the admin Downloads Queue and the live dashboard. Actual
byte-moving is done by the download worker (``download_service``).
"""

from __future__ import annotations

from dataclasses import dataclass

from nekofetch.core.container import Container
from nekofetch.core.exceptions import NotFound
from nekofetch.core.redis_safe import safe_redis_set
from nekofetch.domain.enums import JobStatus, RequestStatus
from nekofetch.infrastructure.database.postgres.models import DownloadJob
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.infrastructure.repositories.queue_repo import QueueRepository
from nekofetch.infrastructure.repositories.request_repo import RequestRepository


@dataclass(slots=True)
class QueueRow:
    job_id: int
    anime_title: str
    requested_by: str
    status: str
    progress: float
    speed_bps: float
    eta_seconds: int | None
    current_episode: int | None = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    stage: str | None = None
    season: int | None = None
    episode_index: int | None = None
    total_episodes: int | None = None
    label: str | None = None
    resolution: str | None = None
    audio: str | None = None


class QueueService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def enqueue(self, request_code: str, *, priority: int | None = None) -> int:
        async with session_scope(self._c.pg_sessionmaker) as session:
            requests = RequestRepository(session)
            req = await requests.get_by_code(request_code)
            if req is None:
                raise NotFound(request_code)
            # Auto-detect priority from the submitter's identity when not pinned.
            # Owner = 10 (drains first after the current task), everyone else = 100
            # (FIFO within the band). The worker claims one job at a time and never
            # preempts, so an owner request jumps the queue only between tasks.
            if priority is None:
                priority = await self._priority_for(session, req)
            job = DownloadJob(request_id=req.id, status=JobStatus.QUEUED, priority=priority)
            session.add(job)
            req.status = RequestStatus.QUEUED
            await session.flush()
            job_id, title = job.id, req.anime_title

        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(self._c).event(
            "queue", "enqueued", code=request_code, anime=title, job=job_id
        )
        return job_id

    async def dashboard(self, *, limit: int | None = None) -> list[QueueRow]:
        limit = limit or self._c.config.queue.max_visible
        async with session_scope(self._c.pg_sessionmaker) as session:
            jobs = await QueueRepository(session).active()
            rows: list[QueueRow] = []
            for job in jobs[:limit]:
                req = await RequestRepository(session).get(job.request_id)
                # Prefer fast live progress from Redis when present.
                snap = await self._c.progress.get(job.id) if self._c.progress else None
                rows.append(
                    QueueRow(
                        job_id=job.id,
                        anime_title=req.anime_title if req else "—",
                        requested_by=str(req.user_id) if req else "—",
                        status=(snap.status if snap else (job.status.value if isinstance(job.status, JobStatus) else job.status)),
                        progress=(snap.progress if snap else job.progress),
                        speed_bps=(snap.speed_bps if snap else job.speed_bps),
                        eta_seconds=(snap.eta_seconds if snap else job.eta_seconds),
                        current_episode=(snap.current_episode if snap else job.current_episode),
                        downloaded_bytes=(snap.downloaded_bytes if snap else job.downloaded_bytes),
                        total_bytes=(snap.total_bytes if snap else job.total_bytes),
                        stage=(snap.stage if snap else None),
                        season=(snap.season if snap else None),
                        episode_index=(snap.episode_index if snap else None),
                        total_episodes=(snap.total_episodes if snap else None),
                        label=(snap.label if snap else None),
                        resolution=(snap.resolution if snap else None),
                        audio=(snap.audio if snap else None),
                    )
                )
            return rows

    async def _priority_for(self, session, req) -> int:
        """Owner = 10, everyone else = 100."""
        if not req.user_id:
            return 100
        from nekofetch.infrastructure.repositories.user_repo import UserRepository
        from nekofetch.services.auth_service import AuthService

        user = await UserRepository(session).get(req.user_id)
        if user is None:
            return 100
        return 10 if AuthService(self._c).is_owner(user) else 100

    async def cancel(self, job_id: int) -> bool:
        """Cancel a single job entirely: mark it CANCELLED in the DB, signal a
        running worker to abort mid-download, and drop its live progress so it
        leaves ACTIVE TASKS. Works for running, queued, and orphaned/ghost jobs."""
        cancelled = False
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            if job is not None and job.status in {
                JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED,
            }:
                job.status = JobStatus.CANCELLED
                req = await RequestRepository(session).get(job.request_id)
                if req is not None:
                    req.status = RequestStatus.FAILED
                cancelled = True
        if self._c.progress:
            await self._c.progress.delete(job_id)
        if self._c.redis:  # also signal any worker actively running this job to stop
            # Safe ``SET`` with ``ex=600`` — 10-min TTL is critical: a stale
            # flag committed RIGHT BEFORE a worker crash would be honored by
            # ``recover_on_startup`` on the next process boot and immediately
            # ``_finalize_cancelled`` a brand-new run. The TTL bounds the risk.
            await safe_redis_set(self._c.redis, f"nf:job:{job_id}:cancel",
                                  "1", label="queue.cancel.signal", ex=600)
        return cancelled

    async def cancel_all_active(self) -> int:
        """Cancel EVERY active/queued/orphaned job and clear their live progress —
        the 'wipe the download state' button. Returns how many were cancelled.

        Also **deletes** the job rows from the DB and wipes the on-disk work
        directory so no stale files remain after a full clear.
        """
        import shutil
        from pathlib import Path

        async with session_scope(self._c.pg_sessionmaker) as session:
            jobs = await QueueRepository(session).by_status(
                JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED,
            )
            ids = [j.id for j in jobs]
            for job in jobs:
                job.status = JobStatus.CANCELLED
            # Also collect ALL non-active jobs so we can fully purge the table.
            all_jobs = await QueueRepository(session).list(limit=100_000)
            for job in all_jobs:
                await session.delete(job)
        for jid in ids:
            if self._c.progress:
                await self._c.progress.delete(jid)
            if self._c.redis:
                # See ``cancel()`` for the ``ex=600`` rationale — a stale
                # flag committed before a worker crash must NOT survive
                # ``recover_on_startup`` on the next process boot.
                await safe_redis_set(self._c.redis, f"nf:job:{jid}:cancel",
                                      "1", label="queue.cancel_all.signal",
                                      ex=600)
        # Wipe the on-disk work directory to reclaim storage from partial/stale
        # downloads. The directory is recreated on demand by the download service.
        work_dir = Path(self._c.env.storage_path) / "work"
        if work_dir.is_dir():
            shutil.rmtree(work_dir, ignore_errors=True)
        return len(ids)

    async def counts(self) -> dict[str, int]:
        async with session_scope(self._c.pg_sessionmaker) as session:
            repo = QueueRepository(session)
            return {
                "queued": await repo.count_by_status(JobStatus.QUEUED),
                "running": await repo.count_by_status(JobStatus.RUNNING),
                "failed": await repo.count_by_status(JobStatus.FAILED),
            }
