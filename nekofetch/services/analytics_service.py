"""Analytics service — event recording and the admin dashboard aggregates.

Events are appended to ``analytics_events``; dashboard reads aggregate on demand.
Honors the ``analytics`` feature toggle (recording is a no-op when disabled).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

from nekofetch.core.container import Container
from nekofetch.domain.enums import JobStatus, RequestStatus
from nekofetch.infrastructure.database.postgres.models import (
    AnalyticsEvent,
    DownloadJob,
    Request,
    User,
)
from nekofetch.infrastructure.database.postgres.session import session_scope


@dataclass(slots=True)
class DashboardStats:
    total_users: int
    active_users: int
    total_downloads: int
    queue_size: int
    failed_tasks: int
    published: int
    most_requested: list[tuple[str, int]]


class AnalyticsService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def record(self, event: str, *, user_id: int | None = None,
                     anime_doc_id: str | None = None, data: dict | None = None) -> None:
        if not self._c.config.features.analytics:
            return
        # anime_doc_id is varchar(48) — clamp defensively so a torrent source_ref
        # blob (or any other oversized value) never crashes the analytics insert.
        if anime_doc_id and len(anime_doc_id) > 48:
            anime_doc_id = anime_doc_id[:48]
        async with session_scope(self._c.pg_sessionmaker) as session:
            session.add(
                AnalyticsEvent(
                    ts=datetime.now(timezone.utc),
                    event=event,
                    user_id=user_id,
                    anime_doc_id=anime_doc_id,
                    data=data,
                )
            )

    async def dashboard(self) -> DashboardStats:
        async with session_scope(self._c.pg_sessionmaker) as session:
            total_users = int(
                (await session.execute(select(func.count()).select_from(User))).scalar_one()
            )
            total_downloads = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(DownloadJob)
                        .where(DownloadJob.status == JobStatus.COMPLETED)
                    )
                ).scalar_one()
            )
            queue_size = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(DownloadJob)
                        .where(DownloadJob.status.in_({JobStatus.QUEUED, JobStatus.RUNNING}))
                    )
                ).scalar_one()
            )
            failed = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(DownloadJob)
                        .where(DownloadJob.status == JobStatus.FAILED)
                    )
                ).scalar_one()
            )
            published = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Request)
                        .where(Request.status == RequestStatus.PUBLISHED)
                    )
                ).scalar_one()
            )
            top = (
                await session.execute(
                    select(Request.anime_title, func.count().label("c"))
                    .group_by(Request.anime_title)
                    .order_by(func.count().desc())
                    .limit(5)
                )
            ).all()

            return DashboardStats(
                total_users=total_users,
                active_users=total_users,  # refined with last_seen window in a full build
                total_downloads=total_downloads,
                queue_size=queue_size,
                failed_tasks=failed,
                published=published,
                most_requested=[(row[0], int(row[1])) for row in top],
            )
