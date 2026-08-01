"""Processing pipeline orchestrator.

Runs the enabled stages in order for a completed download job, then moves the request
to READY (awaiting publish approval) or PUBLISHED (if approval isn't required).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.exceptions import ProcessingError
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import JobStatus, RequestStatus
from nekofetch.infrastructure.database.postgres.models import DownloadJob, MediaFile, Request
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.infrastructure.repositories.request_repo import RequestRepository
from nekofetch.services.processing.base import StageContext
from nekofetch.services.processing.stages import default_stages

log = get_logger(__name__)


class _CancelJob(BaseException):
    """Raised when an admin Cancels the job during processing — must be a
    BaseException so per-stage ``except Exception`` guards don't swallow it."""


class ProcessingPipeline:
    def __init__(self, container: Container) -> None:
        self._c = container

    # ── Cancel helpers (mirrors DownloadWorker so both halves honour the same flag) ─
    @staticmethod
    def _cancel_key(job_id: int) -> str:
        return f"nf:job:{job_id}:cancel"

    async def _cancel_requested(self, job_id: int) -> bool:
        if not self._c.redis:
            return False
        try:
            return bool(await self._c.redis.get(self._cancel_key(job_id)))
        except Exception:  # noqa: BLE001
            return False

    async def _finalize_cancelled(self, job_id: int, req: Request | None) -> None:
        """Tear down a job that was cancelled mid-pipeline — identical outcome to the
        download-phase cancel so the admin sees one consistent result regardless of
        which phase the Cancel hit."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            if job is not None:
                job.status = JobStatus.CANCELLED
                job.finished_at = datetime.now(timezone.utc)
            if req is not None:
                db_req = await RequestRepository(session).get(req.id)
                if db_req is not None:
                    db_req.status = RequestStatus.FAILED
        if self._c.progress:
            try:
                await self._c.progress.delete(job_id)
            except Exception:  # noqa: BLE001
                pass
        title = req.anime_title if req else ""
        code = req.code if req else ""
        log.info("processing.cancelled", job_id=job_id, anime=title)
        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(self._c).event(
            "error", "cancelled", job=job_id, anime=title, code=code,
        )

    async def run_for_job(self, job_id: int) -> StageContext:
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            if job is None:
                raise ProcessingError(f"job {job_id} not found")
            req = await RequestRepository(session).get(job.request_id)
            files = list(
                (await session.execute(select(MediaFile).where(MediaFile.job_id == job_id)))
                .scalars()
                .all()
            )
            ctx = StageContext(job_id=job_id, request=req, files=files)

            for stage in default_stages(self._c):
                if not stage.enabled():
                    note = f"{stage.log_name}: skipped (disabled)"
                    ctx.notes.append(note)
                    continue
                # Respect a Cancel that arrived between the download loop and
                # the pipeline — or between stages. Without this an admin's Cancel
                # during a long-running stage (e.g. watermark) is invisible until
                # the stage finishes on its own.
                if await self._cancel_requested(job_id):
                    await self._finalize_cancelled(job_id, req)
                    raise _CancelJob()
                # Use log_name (not stage.value) so Watermark — which shares the
                # BRANDING enum with real Branding — reads "watermarking", not a
                # second "branding" line.
                log.info("processing.stage", job_id=job_id, stage=stage.log_name)
                from nekofetch.services.log_channel_service import LogChannelService

                await LogChannelService(self._c).event(
                    "processing", stage.log_name, job=job_id,
                    anime=req.anime_title if req else None,
                )
                try:
                    await stage.process(ctx)
                    # No per-stage "…done" event — the stage-start line is enough.
                    # Doubling every step with a "done" (carrying only a bare job id)
                    # just clutters the activity stream.
                except Exception as exc:  # noqa: BLE001
                    job.status = JobStatus.FAILED
                    await LogChannelService(self._c).event(
                        "error", f"{stage.log_name}_failed", job=job_id,
                        error=str(exc)[:300],
                    )
                    raise ProcessingError(f"{stage.log_name}: {exc}") from exc

            # Processing done → READY. Uploading the verified packs to the storage
            # (database) channel happens automatically right after (see the worker);
            # going live on the *main* channel ("publish") is a separate action.
            req.status = RequestStatus.READY

        log.info("processing.complete", job_id=job_id, notes=len(ctx.notes))
        return ctx
