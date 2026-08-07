"""Work items — admin-marshalled anime the pipeline pulls in, separate from
user *requests*.

A **request** is something a user asks for (rate-limited, one at a time). A
**work item** is something an admin adds directly to the line — a batch job,
backfill, or priority pull. They never count against a user's request limit and
live in their own ``work_items`` table, but they flow into the *same* download
queue Levi drains, so a stalled downstream stage (Senku/Gojo) never blocks the
downloader from pulling the next item.

The ORM model registers on NekoFetch's shared ``Base.metadata`` (imported via
``shared/models.py``) so ``create_all`` and Alembic autogenerate both see it.
Production also gets an explicit migration (20260718_0010_add_work_items).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import BigInteger, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nekofetch.infrastructure.database.postgres.base import (
    Base,
    PKMixin,
    TimestampMixin,
)

WORK_PREFIX = "WRK"

# Pipeline stages a work item can sit at, mirroring the request lifecycle.
STAGE_DOWNLOAD = "download"
STAGE_DISTRIBUTE = "distribute"
STAGE_PUBLISH = "publish"
STAGES = (STAGE_DOWNLOAD, STAGE_DISTRIBUTE, STAGE_PUBLISH)

# Work-item statuses. ``open`` = waiting to be claimed; terminal = done/cancelled.
STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
_OPEN_STATUSES = (STATUS_OPEN, STATUS_CLAIMED)


class WorkItem(Base, PKMixin, TimestampMixin):
    """An admin-added pipeline job, independent of user requests."""

    __tablename__ = "work_items"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    added_by_admin_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    anime_title: Mapped[str] = mapped_column(String(256), nullable=False)
    anime_doc_id: Mapped[str | None] = mapped_column(String(48), index=True)
    franchise_data: Mapped[dict | None] = mapped_column(JSONB)
    stage: Mapped[str] = mapped_column(String(32), default=STAGE_DOWNLOAD,
                                       index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_OPEN,
                                        index=True, nullable=False)
    assigned_admin_id: Mapped[int | None] = mapped_column(BigInteger, index=True)


@dataclass
class WorkItemView:
    """Lightweight, session-detached view for UI callers."""

    code: str
    anime_title: str
    stage: str
    status: str
    assigned_admin_id: int | None


def _view(w: WorkItem) -> WorkItemView:
    return WorkItemView(
        code=w.code, anime_title=w.anime_title, stage=w.stage,
        status=w.status, assigned_admin_id=w.assigned_admin_id,
    )


class WorkService:
    """CRUD + queue operations for admin-marshalled work items."""

    def __init__(self, sessionmaker):
        self._sm = sessionmaker

    def _maybe_session(self, _session=None):
        if _session is not None:
            from contextlib import nullcontext
            return nullcontext(_session)
        return self._sm()

    async def _next_code(self, session) -> str:
        """``WRK-<n>`` where n is the current row count + 1.

        A count-based sequence is fine here: work codes are cosmetic references,
        not foreign keys, and adds are serialized within one admin's batch. If a
        collision ever occurred the unique index would surface it loudly rather
        than corrupt anything.
        """
        total = (await session.execute(
            select(func.count()).select_from(WorkItem)
        )).scalar_one()
        return f"{WORK_PREFIX}-{int(total) + 1}"

    async def add_batch(
        self,
        admin_id: int,
        items: list[dict],
        *,
        _session=None,
    ) -> list[WorkItemView]:
        """Create work items from confirmed batch entries.

        ``items`` is a list of dicts with at least ``anime_title``; optional
        ``anime_doc_id`` and ``franchise_data``. Returns detached views.
        """
        created: list[WorkItemView] = []
        async with self._maybe_session(_session) as session:
            for it in items:
                title = (it.get("anime_title") or it.get("title") or "").strip()
                if not title:
                    continue
                w = WorkItem(
                    code=await self._next_code(session),
                    added_by_admin_id=admin_id,
                    anime_title=title,
                    anime_doc_id=it.get("anime_doc_id"),
                    franchise_data=it.get("franchise_data"),
                    stage=STAGE_DOWNLOAD,
                    status=STATUS_OPEN,
                )
                session.add(w)
                await session.flush()
                created.append(_view(w))
            if _session is None:
                await session.commit()
        return created

    async def count_open(self, *, _session=None) -> int:
        """How many work items are still in the line (open or claimed)."""
        async with self._maybe_session(_session) as session:
            return int((await session.execute(
                select(func.count()).select_from(WorkItem)
                .where(WorkItem.status.in_(_OPEN_STATUSES))
            )).scalar_one())

    async def list_open(self, *, limit: int = 50, _session=None) -> list[WorkItemView]:
        async with self._maybe_session(_session) as session:
            rows = (await session.execute(
                select(WorkItem).where(WorkItem.status.in_(_OPEN_STATUSES))
                .order_by(WorkItem.created_at.asc()).limit(limit)
            )).scalars().all()
            return [_view(w) for w in rows]

    async def next_for_stage(self, stage: str, *, _session=None) -> WorkItemView | None:
        """Oldest open item waiting at ``stage`` — the queue-drain primitive.

        A stalled later stage never starves this: each stage pulls independently,
        so the downloader keeps draining ``download`` even if publishing is down.
        """
        async with self._maybe_session(_session) as session:
            w = (await session.execute(
                select(WorkItem).where(
                    WorkItem.stage == stage,
                    WorkItem.status == STATUS_OPEN,
                ).order_by(WorkItem.created_at.asc()).limit(1)
            )).scalar_one_or_none()
            return _view(w) if w else None

    async def claim(self, code: str, admin_id: int, *, _session=None) -> bool:
        async with self._maybe_session(_session) as session:
            w = (await session.execute(
                select(WorkItem).where(WorkItem.code == code)
            )).scalar_one_or_none()
            if w is None or w.status not in (STATUS_OPEN,):
                return False
            w.status = STATUS_CLAIMED
            w.assigned_admin_id = admin_id
            if _session is None:
                await session.commit()
            return True

    async def advance(self, code: str, stage: str, *, _session=None) -> bool:
        """Move an item to the next stage and reopen it for that stage's pool."""
        async with self._maybe_session(_session) as session:
            w = (await session.execute(
                select(WorkItem).where(WorkItem.code == code)
            )).scalar_one_or_none()
            if w is None:
                return False
            w.stage = stage if stage in STAGES else w.stage
            w.status = STATUS_OPEN
            w.assigned_admin_id = None
            if _session is None:
                await session.commit()
            return True

    async def complete(self, code: str, *, _session=None) -> bool:
        async with self._maybe_session(_session) as session:
            w = (await session.execute(
                select(WorkItem).where(WorkItem.code == code)
            )).scalar_one_or_none()
            if w is None:
                return False
            w.status = STATUS_DONE
            if _session is None:
                await session.commit()
            return True

    async def cancel(self, code: str, *, _session=None) -> bool:
        """Cancel an open work item (removes it from the manage queue).

        The owner's Manage REQ/WRK console routes a WRK row's destructive
        action here (REQ rows go to ``RequestService``). Only open/claimed
        items can be cancelled; returns False for a done/absent one.
        """
        async with self._maybe_session(_session) as session:
            w = (await session.execute(
                select(WorkItem).where(WorkItem.code == code)
            )).scalar_one_or_none()
            if w is None or w.status not in (STATUS_OPEN, STATUS_CLAIMED):
                return False
            w.status = STATUS_CANCELLED
            if _session is None:
                await session.commit()
            return True
