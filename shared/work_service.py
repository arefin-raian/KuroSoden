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
    # The ``Request.code`` this work was bridged to (REQ-N). Kept null until the
    # batch/redo bridge runs; the durable link lets the pipeline advance the work
    # item's stage/status in step with the request's lifecycle (see
    # ``AdminAssignmentEngine.complete_task``) and lets Levi lazily bridge legacy
    # works that predate the bridge.
    request_code: Mapped[str | None] = mapped_column(String(32), index=True)
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
    request_code: str | None = None


def _view(w: WorkItem) -> WorkItemView:
    return WorkItemView(
        code=w.code, anime_title=w.anime_title, stage=w.stage,
        status=w.status, assigned_admin_id=w.assigned_admin_id,
        request_code=w.request_code,
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

    async def count_open(self, *, stage: str | None = None, _session=None) -> int:
        """How many work items are still in the line (open or claimed).

        ``stage`` narrows the count to one pipeline stage (e.g. the download
        stage Levi drains); ``None`` counts every open/claimed item.
        """
        async with self._maybe_session(_session) as session:
            conds = [WorkItem.status.in_(_OPEN_STATUSES)]
            if stage is not None:
                conds.append(WorkItem.stage == stage)
            return int((await session.execute(
                select(func.count()).select_from(WorkItem).where(*conds)
            )).scalar_one())

    async def list_open(self, *, stage: str | None = None, limit: int = 50,
                        _session=None) -> list[WorkItemView]:
        """Open/claimed work items, oldest first; optionally one pipeline stage."""
        async with self._maybe_session(_session) as session:
            conds = [WorkItem.status.in_(_OPEN_STATUSES)]
            if stage is not None:
                conds.append(WorkItem.stage == stage)
            rows = (await session.execute(
                select(WorkItem).where(*conds)
                .order_by(WorkItem.created_at.asc()).limit(limit)
            )).scalars().all()
            return [_view(w) for w in rows]

    async def get(self, code: str, *, _session=None) -> WorkItem | None:
        """Fetch a work item by its WRK- code, detached from its session."""
        async with self._maybe_session(_session) as session:
            w = (await session.execute(
                select(WorkItem).where(WorkItem.code == code)
            )).scalar_one_or_none()
            if w is None:
                return None
            session.expunge(w)
            return w

    async def link(self, code: str, request_code: str | None, *,
                   _session=None) -> bool:
        """Record the bridged ``Request.code`` (REQ-N) a work item flows into.

        Lets the pipeline advance/complete the work item in step with its request
        (``AdminAssignmentEngine.complete_task``) and lets Levi resolve the work
        on open. Setting ``None`` clears the link.
        """
        async with self._maybe_session(_session) as session:
            w = (await session.execute(
                select(WorkItem).where(WorkItem.code == code)
            )).scalar_one_or_none()
            if w is None:
                return False
            w.request_code = request_code
            if _session is None:
                await session.commit()
            return True

    async def reconcile_links(self, *, _session=None) -> int:
        """Self-heal legacy works + keep stage/status honest from the request side.

        Two fixes, both idempotent and best-effort:

        1. **Link orphaned works** — a ``WorkItem`` without ``request_code`` gets
           linked to the newest non-terminal ``Request`` with the same
           ``anime_doc_id`` (works created before the bridge existed, or where a
           bridge failure silently dropped the request).
        2. **Sync stage/status** — an already-linked work's stage/status is
           re-derived from its request's completed assignments, so a work whose
           request already moved past download stops counting as an open
           download task (the phantom "works left" nudge).

        Returns how many rows were touched. Never raises.
        """
        from nekofetch.domain.enums import RequestStatus
        from nekofetch.infrastructure.database.postgres.models import Request
        from kurosoden.shared.admin_assignment import AdminAssignment

        touched = 0
        async with self._maybe_session(_session) as session:
            works = (await session.execute(
                select(WorkItem).where(WorkItem.status.in_(_OPEN_STATUSES))
            )).scalars().all()
            for w in works:
                changed = False
                if w.request_code is None:
                    doc = w.anime_doc_id
                    if not doc:
                        aid = (w.franchise_data or {}).get("anilist_id")
                        doc = f"{aid}" if aid is not None else None
                    if doc:
                        req = (await session.execute(
                            select(Request).where(
                                Request.anime_doc_id == doc,
                                Request.status.notin_([
                                    RequestStatus.PUBLISHED,
                                    RequestStatus.REJECTED,
                                    RequestStatus.FAILED,
                                ]),
                            ).order_by(Request.id.desc()).limit(1)
                        )).scalars().first()
                        if req is not None:
                            w.request_code = req.code
                            changed = True
                if w.request_code is not None:
                    stage, status = await self._stage_from_assignments(
                        session, w.request_code,
                    )
                    if stage is not None and (w.stage != stage or w.status != status):
                        w.stage, w.status = stage, status
                        changed = True
                if changed:
                    touched += 1
            if touched and _session is None:
                await session.commit()
        return touched

    async def _stage_from_assignments(
        self, session, request_code: str,
    ) -> tuple[str | None, str | None]:
        """Derive the work stage/status a request's stage assignments imply.

        A completed gojo assignment means the whole pipeline finished; senku
        completed means distribution is done and publish awaits; levi completed
        means download is done. An in-progress levi assignment means a downloader
        currently holds it. Returns ``(None, None)`` when the request is unknown
        so the caller leaves the work untouched."""
        from nekofetch.infrastructure.database.postgres.models import Request
        from kurosoden.shared.admin_assignment import AdminAssignment

        req = (await session.execute(
            select(Request).where(Request.code == request_code)
        )).scalar_one_or_none()
        if req is None:
            return None, None
        # A terminal request (published/rejected/failed) means the pipeline is
        # over — the work is DONE no matter what the per-stage assignment rows
        # say. This is essential for the redo-relink path: a redo of a published
        # title auto-publishes in Levi and SKIPS the Senku/Gojo assignments, so
        # there's no `gojo=completed` row to detect — without this the work would
        # stay a phantom open "download" forever (the REQ-1079 stuck-processing
        # symptom on the Manage board).
        from nekofetch.domain.enums import RequestStatus as _RS
        req_status = req.status.value if hasattr(req.status, "value") else str(req.status)
        if req_status in (_RS.PUBLISHED.value, _RS.REJECTED.value, _RS.FAILED.value):
            return STAGE_PUBLISH, STATUS_DONE
        rows = (await session.execute(
            select(AdminAssignment.stage, AdminAssignment.status).where(
                AdminAssignment.request_code == request_code,
            )
        )).all()
        by_stage = {stage: status for stage, status in rows}
        if by_stage.get("gojo") == "completed":
            return STAGE_PUBLISH, STATUS_DONE
        if by_stage.get("senku") == "completed":
            return STAGE_PUBLISH, STATUS_OPEN
        if by_stage.get("levi") == "completed":
            return STAGE_DISTRIBUTE, STATUS_OPEN
        if by_stage.get("levi") == "in_progress":
            return STAGE_DOWNLOAD, STATUS_CLAIMED
        return STAGE_DOWNLOAD, STATUS_OPEN

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
