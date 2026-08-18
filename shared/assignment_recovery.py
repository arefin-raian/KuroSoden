"""Background recovery for offer expiry and quiet-hour assignment deferrals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from kurosoden.shared.admin_assignment import (
    OPEN_STATUSES,
    AdminAssignment,
    AdminAssignmentEngine,
    AssignmentResult,
)
from kurosoden.shared.handoff import notify_stage_assignment
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import RequestStatus
from nekofetch.infrastructure.database.postgres.models import Request
from nekofetch.infrastructure.database.postgres.session import session_scope

log = get_logger(__name__)


@dataclass
class AssignmentRecoveryReport:
    expired_offers: int = 0
    reassigned_offers: int = 0
    recovered_assignments: int = 0
    deferred_assignments: int = 0
    notifications_sent: int = 0


@dataclass
class _NotifyItem:
    stage: str
    result: AssignmentResult
    code: str
    title: str
    requester: str | None = None
    requester_id: int | None = None
    franchise_json: dict | None = None


def make_assignment_recovery_job(container: Container):
    async def _job() -> AssignmentRecoveryReport:
        return await recover_assignment_queue(container)

    return _job


async def recover_assignment_queue(
    container: Container,
    *,
    now: datetime | None = None,
    notify: bool = True,
    limit: int = 100,
) -> AssignmentRecoveryReport:
    """Expire stale offers and assign work that was deferred during quiet hours."""
    clock = _utc(now)
    engine = AdminAssignmentEngine(container.pg_sessionmaker)
    report = AssignmentRecoveryReport()
    notify_items: list[_NotifyItem] = []

    expired = await engine.expire_offers(now=clock, reassign=True)
    report.expired_offers = len(expired)
    report.reassigned_offers = sum(1 for item in expired if item.reassigned_to is not None)
    for item in expired:
        if item.reassigned_to is None:
            continue
        result = await engine.assign(item.request_code, item.stage, now=clock)
        if result is None:
            continue
        request = await _get_request(container, item.request_code)
        if request is not None:
            notify_items.append(_notify_item(item.stage, result, request))

    async with session_scope(container.pg_sessionmaker) as session:
        rows = (
            await session.execute(
                select(Request)
                .where(Request.status.in_([RequestStatus.QUEUED, RequestStatus.READY]))
                .order_by(Request.created_at.asc())
                .limit(limit)
                .options(selectinload(Request.user))
            )
        ).scalars().all()
        for request in rows:
            stage = await _missing_stage(session, request)
            if stage is None:
                continue
            result = await engine.assign(request.code, stage, now=clock, _session=session)
            # First-pass defer usually means the only eligible admin carries a
            # stale ``skipped`` row for this stage (an expired offer) that blocks
            # them for the local day. Escalate to second_pass, which bypasses the
            # skipped-block, so the durable backstop actually re-delivers the task
            # instead of counting it deferred on every 60s tick forever.
            if result is None:
                result = await engine.assign(
                    request.code, stage, now=clock, second_pass=True, _session=session,
                )
            if result is None:
                report.deferred_assignments += 1
                continue
            report.recovered_assignments += 1
            notify_items.append(_notify_item(stage, result, request))

    if notify:
        for item in notify_items:
            report.notifications_sent += await notify_stage_assignment(
                container,
                item.stage,
                item.result,
                item.code,
                item.title,
                requester=item.requester,
                requester_id=item.requester_id,
                franchise_json=item.franchise_json,
            )

    if (
        report.expired_offers
        or report.recovered_assignments
        or report.deferred_assignments
        or report.notifications_sent
    ):
        log.info(
            "assignment_recovery.done",
            expired=report.expired_offers,
            reassigned=report.reassigned_offers,
            recovered=report.recovered_assignments,
            deferred=report.deferred_assignments,
            notified=report.notifications_sent,
        )
    return report


async def _missing_stage(session, request: Request) -> str | None:
    if request.status == RequestStatus.QUEUED:
        if await _has_stage_status(session, request.code, "levi", ("completed",)):
            return None
        if not await _has_stage_status(session, request.code, "levi", OPEN_STATUSES):
            return "levi"
        return None
    if request.status != RequestStatus.READY:
        return None
    if (
        await _has_stage_status(session, request.code, "senku", ("completed",))
        and not await _has_stage_status(
            session, request.code, "gojo", (*OPEN_STATUSES, "completed")
        )
    ):
        return "gojo"
    if (
        await _has_stage_status(session, request.code, "levi", ("completed",))
        and not await _has_stage_status(
            session, request.code, "senku", (*OPEN_STATUSES, "completed")
        )
    ):
        return "senku"
    return None


async def _has_stage_status(
    session, request_code: str, stage: str, statuses: tuple[str, ...]
) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(AdminAssignment)
            .where(
                AdminAssignment.request_code == request_code,
                AdminAssignment.stage == stage,
                AdminAssignment.status.in_(statuses),
            )
        )
    ).scalar()
    return int(count or 0) > 0


async def _get_request(container: Container, code: str) -> Request | None:
    async with session_scope(container.pg_sessionmaker) as session:
        return (
            await session.execute(
                select(Request)
                .where(Request.code == code)
                .options(selectinload(Request.user))
            )
        ).scalar_one_or_none()


def _notify_item(stage: str, result: AssignmentResult, request: Request) -> _NotifyItem:
    user = getattr(request, "user", None)
    requester = getattr(user, "first_name", None) or getattr(user, "username", None)
    requester_id = getattr(user, "telegram_id", None)
    return _NotifyItem(
        stage=stage,
        result=result,
        code=request.code,
        title=request.anime_title,
        requester=requester,
        requester_id=int(requester_id) if requester_id is not None else None,
        franchise_json=request.franchise_data or {},
    )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
