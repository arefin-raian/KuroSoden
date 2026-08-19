"""Admin assignment engine - balanced, slot-aware workload distribution.

Pipeline stages use a single routing ladder:

1. Admins inside their preferred local slot receive a duty assignment.
2. Admins outside slot but recently active in-bot receive a one-hour offer.
3. If nobody can be offered live, the closest upcoming slot receives fallback duty.

Profile-less admins keep the original always-on behavior. Existing active task
queries still only return ``assigned`` and ``in_progress`` rows; ``offered`` rows
stay separate until accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from nekofetch.infrastructure.database.postgres.base import Base, PKMixin, TimestampMixin

ACTIVE_STATUSES = ("assigned", "in_progress")
OPEN_STATUSES = ("assigned", "in_progress", "offered")
OFFER_TIMEOUT_MINUTES = 60
RECENT_ACTIVITY_MINUTES = 15
QUIET_START_MINUTE = 4 * 60
QUIET_END_MINUTE = 8 * 60
_DAY_MINUTES = 24 * 60


class AdminAssignment(Base, PKMixin, TimestampMixin):
    """Tracks which admin owns, has been offered, or closed a pipeline task."""

    __tablename__ = "admin_assignments"
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    request_code: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="assigned", nullable=False)
    # "offered" | "assigned" | "in_progress" | "completed" | "skipped" | "rejected"
    assignment_mode: Mapped[str] = mapped_column(String(16), default="duty", nullable=False)
    # "duty" | "offer" | "fallback"
    offer_attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(String(64))
    task_count_at_assignment: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index(
            "uq_admin_assignments_open_request_stage",
            "request_code",
            "stage",
            unique=True,
            postgresql_where=status.in_(OPEN_STATUSES),
            sqlite_where=status.in_(OPEN_STATUSES),
        ),
    )


class AdminAvailability(Base, PKMixin, TimestampMixin):
    """Tracks admin availability, breaks, bot assignments, and profile slots."""

    __tablename__ = "admin_availability"

    admin_telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    admin_name: Mapped[str | None] = mapped_column(String(128))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    assigned_bots: Mapped[list[str] | None] = mapped_column(JSONB)
    scheduled_breaks: Mapped[list | None] = mapped_column(JSONB)
    total_tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    working_hours: Mapped[dict | None] = mapped_column(JSONB)
    timezone: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(64))
    max_hours_per_day: Mapped[int | None] = mapped_column(Integer)
    slots_weekday: Mapped[list | None] = mapped_column(JSONB)
    slots_weekend: Mapped[list | None] = mapped_column(JSONB)


@dataclass
class AssignmentResult:
    admin_telegram_id: int
    admin_name: str | None
    tasks_active: int
    tasks_completed: int
    status: str = "assigned"
    assignment_mode: str = "duty"
    expires_at: datetime | None = None


@dataclass
class OfferExpiry:
    request_code: str
    stage: str
    admin_telegram_id: int
    final_status: str
    decision_reason: str
    reassigned_to: int | None = None


class AdminAssignmentEngine:
    """Picks the best admin for a pipeline task using the slot-aware ladder."""

    def __init__(self, sessionmaker):
        self._sm = sessionmaker

    def _maybe_session(self, _session=None):
        """Context manager: yields ``_session`` if provided, else a new session."""
        if _session is not None:
            from contextlib import nullcontext

            return nullcontext(_session)
        return self._sm()

    async def assign(
        self,
        request_code: str,
        stage: str,
        *,
        preferred_admin: int | None = None,
        now: datetime | None = None,
        second_pass: bool = False,
        excluded_admin_ids: set[int] | None = None,
        _session=None,
    ) -> AssignmentResult | None:
        """Assign or offer a request to the best available admin for the stage."""
        clock = self._utc(now)
        excluded = set(excluded_admin_ids or set())
        async with self._maybe_session(_session) as session:
            if _session is None:
                async with session.begin():
                    return await self._assign_impl(
                        session,
                        request_code,
                        stage,
                        preferred_admin,
                        clock,
                        second_pass=second_pass,
                        excluded_admin_ids=excluded,
                    )
            return await self._assign_impl(
                session,
                request_code,
                stage,
                preferred_admin,
                clock,
                second_pass=second_pass,
                excluded_admin_ids=excluded,
            )

    async def _assign_impl(
        self,
        session,
        request_code: str,
        stage: str,
        preferred_admin: int | None,
        now: datetime,
        *,
        second_pass: bool,
        excluded_admin_ids: set[int],
    ) -> AssignmentResult | None:
        existing = await self._existing_open_result(session, request_code, stage)
        if existing is not None:
            return existing

        if preferred_admin is not None:
            forced = (
                await session.execute(
                    select(AdminAvailability).where(
                        AdminAvailability.admin_telegram_id == preferred_admin
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                forced is not None
                and forced.is_available
                and forced.admin_telegram_id not in excluded_admin_ids
                and self._stage_enabled(forced, stage)
                and not self._is_on_break(forced, now)
                and self._within_hours(forced, now)
                and not self._in_quiet_hours(forced, now)
            ):
                return await self._create_assignment(
                    session,
                    forced.admin_telegram_id,
                    request_code,
                    stage,
                    forced,
                    status="assigned",
                    assignment_mode="duty",
                    now=now,
                )

        candidates = await self._stage_candidates(
            session, stage, now, request_code, second_pass, excluded_admin_ids
        )
        if not candidates:
            return None

        duty = await self._best_by_load(
            session, [a for a in candidates if self._in_duty_slot(a, now)]
        )
        if duty is not None:
            return await self._create_assignment(
                session,
                duty.admin_telegram_id,
                request_code,
                stage,
                duty,
                status="assigned",
                assignment_mode="duty",
                now=now,
            )

        users = await self._users_by_telegram_id(
            session, [a.admin_telegram_id for a in candidates]
        )
        offerable = [
            a for a in candidates
            if self._recently_active(users.get(a.admin_telegram_id), now)
        ]
        offer = await self._best_by_load(session, offerable, slot_distance=True, now=now)
        if offer is not None:
            attempt = await self._next_offer_attempt(
                session, request_code, stage, offer.admin_telegram_id
            )
            return await self._create_assignment(
                session,
                offer.admin_telegram_id,
                request_code,
                stage,
                offer,
                status="offered",
                assignment_mode="offer",
                offer_attempt=attempt,
                now=now,
                expires_at=now + timedelta(minutes=OFFER_TIMEOUT_MINUTES),
            )

        fallback_candidates = [a for a in candidates if not self._in_quiet_hours(a, now)]
        fallback = await self._best_by_load(
            session, fallback_candidates, slot_distance=True, now=now
        )
        if fallback is None:
            return None
        return await self._create_assignment(
            session,
            fallback.admin_telegram_id,
            request_code,
            stage,
            fallback,
            status="assigned",
            assignment_mode="fallback",
            now=now,
        )

    async def _find_best_admin(
        self, session, stage: str, now: datetime | None = None
    ) -> AdminAvailability | None:
        """Compatibility helper: return the best in-slot/always-on admin."""
        clock = self._utc(now)
        candidates = await self._stage_candidates(session, stage, clock, None, False, set())
        return await self._best_by_load(
            session, [a for a in candidates if self._in_duty_slot(a, clock)]
        )

    async def has_quiet_candidates(self, stage: str, *, now: datetime | None = None) -> bool:
        """True when qualified admins exist but are inside their local quiet window."""
        clock = self._utc(now)
        async with self._maybe_session() as session:
            result = await session.execute(
                select(AdminAvailability).where(AdminAvailability.is_available.is_(True))
            )
            rows = list(result.scalars().all())
            return any(
                self._stage_enabled(a, stage)
                and not self._is_on_break(a, clock)
                and self._within_hours(a, clock)
                and self._in_quiet_hours(a, clock)
                for a in rows
            )

    async def _stage_candidates(
        self,
        session,
        stage: str,
        now: datetime,
        request_code: str | None,
        second_pass: bool,
        excluded_admin_ids: set[int],
    ) -> list[AdminAvailability]:
        result = await session.execute(
            select(AdminAvailability).where(
                AdminAvailability.is_available.is_(True),
            ).with_for_update()
        )
        rows = list(result.scalars().all())
        availability_by_id = {int(a.admin_telegram_id): a for a in rows}
        blocked = await self._blocked_admin_ids(
            session, stage, now, second_pass, availability_by_id
        )
        if request_code is not None:
            blocked |= await self._request_blocked_admin_ids(
                session, request_code, stage, now, second_pass, availability_by_id
            )
        blocked |= excluded_admin_ids
        return [
            a for a in rows
            if self._stage_enabled(a, stage)
            and a.admin_telegram_id not in blocked
            and not self._is_on_break(a, now)
            and self._within_hours(a, now)
        ]

    async def _best_by_load(
        self,
        session,
        candidates: list[AdminAvailability],
        *,
        slot_distance: bool = False,
        now: datetime | None = None,
    ) -> AdminAvailability | None:
        from sqlalchemy import func

        if not candidates:
            return None
        scored: list[tuple[float, int, int, AdminAvailability]] = []
        for a in candidates:
            active_count = (
                await session.execute(
                    select(func.count(AdminAssignment.id)).where(
                        AdminAssignment.admin_telegram_id == a.admin_telegram_id,
                        AdminAssignment.status.in_(ACTIVE_STATUSES),
                    )
                )
            ).scalar() or 0
            weight = max(1, a.weight or 1)
            distance = self._minutes_until_next_slot(a, now) if slot_distance else 0
            scored.append((active_count / weight, distance, a.total_tasks_completed, a))
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        return scored[0][3]

    async def _create_assignment(
        self,
        session,
        admin_id: int,
        request_code: str,
        stage: str,
        avail: AdminAvailability,
        *,
        status: str,
        assignment_mode: str,
        now: datetime,
        offer_attempt: int = 0,
        expires_at: datetime | None = None,
    ) -> AssignmentResult:
        from sqlalchemy import func

        active_count = (
            await session.execute(
                select(func.count(AdminAssignment.id)).where(
                    AdminAssignment.admin_telegram_id == admin_id,
                    AdminAssignment.status.in_(ACTIVE_STATUSES),
                )
            )
        ).scalar() or 0

        assignment = AdminAssignment(
            admin_telegram_id=admin_id,
            request_code=request_code,
            stage=stage,
            status=status,
            assignment_mode=assignment_mode,
            offer_attempt=offer_attempt,
            offered_at=now if status == "offered" else None,
            expires_at=expires_at,
            decision_reason=(
                "quiet_offer"
                if status == "offered" and self._in_quiet_hours(avail, now)
                else "slot_offer" if status == "offered" else None
            ),
            task_count_at_assignment=active_count,
        )
        try:
            # Insert inside a SAVEPOINT so a lost race doesn't poison the outer
            # transaction. The application-level guard (_existing_open_result at
            # the top of _assign_impl) is a bare SELECT and is NOT atomic: the
            # 60s recovery job and live bot handlers call assign() concurrently
            # in SEPARATE transactions, so two can both read "no open row" and
            # both INSERT, the second violating the partial-unique index
            # uq_admin_assignments_open_request_stage (request_code, stage).
            # add() goes INSIDE the savepoint so its rollback also detaches the
            # pending row (no manual expunge — that would double-detach and raise).
            async with session.begin_nested():
                session.add(assignment)
                await session.flush()
        except IntegrityError:
            # The other transaction won and committed its open row. The savepoint
            # rollback (on begin_nested exit) already undid our INSERT and
            # detached the row, leaving the outer/shared recover_assignment_queue
            # transaction usable — return the winner's assignment, don't crash.
            existing = await self._existing_open_result(session, request_code, stage)
            if existing is not None:
                return existing
            raise

        result_active = active_count + (1 if status in ACTIVE_STATUSES else 0)
        return AssignmentResult(
            admin_telegram_id=admin_id,
            admin_name=avail.admin_name,
            tasks_active=result_active,
            tasks_completed=avail.total_tasks_completed,
            status=status,
            assignment_mode=assignment_mode,
            expires_at=expires_at,
        )

    async def _existing_open_result(
        self, session, request_code: str, stage: str
    ) -> AssignmentResult | None:
        row = (
            await session.execute(
                select(AdminAssignment).where(
                    AdminAssignment.request_code == request_code,
                    AdminAssignment.stage == stage,
                    AdminAssignment.status.in_(OPEN_STATUSES),
                ).order_by(AdminAssignment.created_at.desc())
            )
        ).scalars().first()
        if row is None:
            return None
        avail = (
            await session.execute(
                select(AdminAvailability).where(
                    AdminAvailability.admin_telegram_id == row.admin_telegram_id
                )
            )
        ).scalar_one_or_none()
        active = await self._active_count(session, row.admin_telegram_id)
        return AssignmentResult(
            admin_telegram_id=row.admin_telegram_id,
            admin_name=avail.admin_name if avail else None,
            tasks_active=active,
            tasks_completed=avail.total_tasks_completed if avail else 0,
            status=row.status,
            assignment_mode=row.assignment_mode,
            expires_at=row.expires_at,
        )

    async def _active_count(self, session, admin_id: int) -> int:
        from sqlalchemy import func

        return int((
            await session.execute(
                select(func.count(AdminAssignment.id)).where(
                    AdminAssignment.admin_telegram_id == admin_id,
                    AdminAssignment.status.in_(ACTIVE_STATUSES),
                )
            )
        ).scalar() or 0)

    async def _next_offer_attempt(
        self, session, request_code: str, stage: str, admin_id: int
    ) -> int:
        from sqlalchemy import func

        prior = (
            await session.execute(
                select(func.count(AdminAssignment.id)).where(
                    AdminAssignment.request_code == request_code,
                    AdminAssignment.stage == stage,
                    AdminAssignment.admin_telegram_id == admin_id,
                    AdminAssignment.status.in_(["offered", "skipped", "rejected"]),
                )
            )
        ).scalar() or 0
        return int(prior) + 1

    async def _blocked_admin_ids(
        self,
        session,
        stage: str,
        now: datetime,
        second_pass: bool,
        availability_by_id: dict[int, AdminAvailability],
    ) -> set[int]:
        oldest_local_day = now - timedelta(days=2)
        rows = (
            await session.execute(
                select(
                    AdminAssignment.admin_telegram_id,
                    AdminAssignment.status,
                    AdminAssignment.decision_reason,
                    AdminAssignment.created_at,
                ).where(
                    AdminAssignment.stage == stage,
                    AdminAssignment.status.in_(["skipped", "rejected"]),
                    AdminAssignment.created_at >= oldest_local_day,
                )
            )
        ).all()
        blocked: set[int] = set()
        for admin_id, status, reason, created_at in rows:
            admin_id = int(admin_id)
            avail = availability_by_id.get(admin_id)
            if avail is not None and created_at is not None:
                created_at = self._utc(created_at)
                if created_at < self._local_day_start_utc(avail, now):
                    continue
            if status == "skipped" and not second_pass:
                blocked.add(admin_id)
            elif status == "rejected":
                if reason == "quiet_reject":
                    if avail is not None and self._in_quiet_hours(avail, now):
                        blocked.add(admin_id)
                else:
                    blocked.add(admin_id)
        return blocked

    async def _request_blocked_admin_ids(
        self,
        session,
        request_code: str,
        stage: str,
        now: datetime,
        second_pass: bool,
        availability_by_id: dict[int, AdminAvailability],
    ) -> set[int]:
        rows = (
            await session.execute(
                select(
                    AdminAssignment.admin_telegram_id,
                    AdminAssignment.status,
                    AdminAssignment.decision_reason,
                    AdminAssignment.created_at,
                ).where(
                    AdminAssignment.request_code == request_code,
                    AdminAssignment.stage == stage,
                    AdminAssignment.status.in_(["skipped", "rejected"]),
                )
            )
        ).all()
        blocked: set[int] = set()
        for admin_id, status, reason, created_at in rows:
            admin_id = int(admin_id)
            avail = availability_by_id.get(admin_id)
            if avail is not None and created_at is not None:
                created_at = self._utc(created_at)
                if created_at < self._local_day_start_utc(avail, now):
                    continue
            if status == "skipped" and not second_pass:
                blocked.add(admin_id)
            elif status == "rejected":
                if reason == "quiet_reject":
                    if avail is not None and self._in_quiet_hours(avail, now):
                        blocked.add(admin_id)
                else:
                    blocked.add(admin_id)
        return blocked

    async def _users_by_telegram_id(self, session, telegram_ids: list[int]) -> dict[int, object]:
        if not telegram_ids:
            return {}
        from nekofetch.infrastructure.database.postgres.models import User

        rows = (
            await session.execute(
                select(User).where(User.telegram_id.in_(telegram_ids))
            )
        ).scalars().all()
        return {int(u.telegram_id): u for u in rows}

    @staticmethod
    def _stage_enabled(avail: AdminAvailability, stage: str) -> bool:
        return bool(avail.assigned_bots and stage in avail.assigned_bots)

    @classmethod
    def _recently_active(cls, user: object | None, now: datetime) -> bool:
        last_seen = getattr(user, "last_seen_at", None)
        if last_seen is None:
            return False
        return cls._utc(last_seen) >= now - timedelta(minutes=RECENT_ACTIVITY_MINUTES)

    @staticmethod
    def _is_on_break(avail: AdminAvailability, now: datetime) -> bool:
        """Check if an admin is currently on a scheduled break."""
        if not avail.scheduled_breaks:
            return False
        for b in avail.scheduled_breaks:
            try:
                start = AdminAssignmentEngine._utc(datetime.fromisoformat(b["start"]))
                end = AdminAssignmentEngine._utc(datetime.fromisoformat(b["end"]))
                if start <= now <= end:
                    return True
            except (KeyError, ValueError, TypeError):
                continue
        return False

    @staticmethod
    def _within_hours(avail: AdminAvailability, now: datetime) -> bool:
        """True when ``now`` (UTC) falls in the admin's working window."""
        wh = avail.working_hours
        if not wh:
            return True
        try:
            start = int(wh["start"]) % 24
            end = int(wh["end"]) % 24
        except (KeyError, ValueError, TypeError):
            return True
        hour = now.hour
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    @classmethod
    def _in_duty_slot(cls, avail: AdminAvailability, now: datetime) -> bool:
        """Profile-less admins are duty-ready; profiled admins must be in-slot."""
        if cls._in_quiet_hours(avail, now):
            return False
        local_now = cls._local_now(avail, now)
        slots = cls._slots_for_local_day(avail, local_now)
        if not slots:
            return True
        minute = local_now.hour * 60 + local_now.minute
        cap = None
        if avail.max_hours_per_day is not None:
            cap = max(1, min(int(avail.max_hours_per_day), 24)) * 60
        for slot in slots:
            inside_for = cls._minutes_into_slot(minute, slot)
            if inside_for is None:
                continue
            if cap is not None and inside_for >= cap:
                continue
            return True
        return False

    @classmethod
    def _in_quiet_hours(cls, avail: AdminAvailability, now: datetime) -> bool:
        local_now = cls._local_now(avail, now)
        minute = local_now.hour * 60 + local_now.minute
        return QUIET_START_MINUTE <= minute < QUIET_END_MINUTE

    @classmethod
    def _minutes_until_next_slot(
        cls, avail: AdminAvailability, now: datetime | None
    ) -> int:
        if now is None:
            return 0
        local_now = cls._local_now(avail, now)
        slots = cls._slots_for_local_day(avail, local_now)
        if not slots:
            return 0
        minute = local_now.hour * 60 + local_now.minute
        distances = []
        for slot in slots:
            clean = cls._clean_slot(slot)
            if clean is None:
                continue
            start, _end = clean
            if cls._minutes_into_slot(minute, clean) is not None:
                distances.append(0)
            else:
                distances.append((start - minute) % _DAY_MINUTES)
        return min(distances) if distances else 0

    @classmethod
    def _slots_for_local_day(cls, avail: AdminAvailability, local_now: datetime) -> list[list[int]]:
        raw = avail.slots_weekend if local_now.weekday() >= 5 else avail.slots_weekday
        slots: list[list[int]] = []
        for slot in raw or []:
            clean = cls._clean_slot(slot)
            if clean is not None:
                slots.append(clean)
        return slots

    @staticmethod
    def _clean_slot(slot: object) -> list[int] | None:
        try:
            start = int(slot[0])  # type: ignore[index]
            end = int(slot[1])  # type: ignore[index]
        except (TypeError, ValueError, IndexError):
            return None
        if start == end:
            return None
        if not (0 <= start < _DAY_MINUTES and 0 <= end < _DAY_MINUTES):
            return None
        return [start, end]

    @staticmethod
    def _minutes_into_slot(minute: int, slot: list[int]) -> int | None:
        start, end = slot
        if start <= end:
            if start <= minute < end:
                return minute - start
            return None
        if minute >= start or minute < end:
            return (minute - start) % _DAY_MINUTES
        return None

    @staticmethod
    def _local_now(avail: AdminAvailability, now: datetime) -> datetime:
        tz_name = avail.timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = UTC
        return now.astimezone(tz)

    @classmethod
    def _local_day_start_utc(cls, avail: AdminAvailability, now: datetime) -> datetime:
        local_now = cls._local_now(avail, now)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_start.astimezone(UTC)

    @staticmethod
    def _utc(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def accept_offer(
        self, request_code: str, stage: str, admin_telegram_id: int, *, _session=None
    ) -> AssignmentResult | None:
        """Turn an unexpired offer into a duty assignment."""
        now = self._utc(None)
        async with self._maybe_session(_session) as session:
            if _session is None:
                async with session.begin():
                    return await self._accept_offer_impl(
                        session, request_code, stage, admin_telegram_id, now
                    )
            return await self._accept_offer_impl(
                session, request_code, stage, admin_telegram_id, now
            )

    async def _accept_offer_impl(
        self, session, request_code: str, stage: str, admin_telegram_id: int, now: datetime
    ) -> AssignmentResult | None:
        row = (
            await session.execute(
                select(AdminAssignment).where(
                    AdminAssignment.request_code == request_code,
                    AdminAssignment.stage == stage,
                    AdminAssignment.admin_telegram_id == admin_telegram_id,
                    AdminAssignment.status == "offered",
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or (row.expires_at and self._utc(row.expires_at) <= now):
            return None
        active_count = await self._active_count(session, admin_telegram_id)
        row.status = "assigned"
        row.assignment_mode = "duty"
        row.responded_at = now
        row.decision_reason = "accepted_offer"
        row.task_count_at_assignment = active_count
        row.expires_at = None
        avail = (
            await session.execute(
                select(AdminAvailability).where(
                    AdminAvailability.admin_telegram_id == admin_telegram_id
                )
            )
        ).scalar_one_or_none()
        return AssignmentResult(
            admin_telegram_id=admin_telegram_id,
            admin_name=avail.admin_name if avail else None,
            tasks_active=active_count + 1,
            tasks_completed=avail.total_tasks_completed if avail else 0,
            status="assigned",
            assignment_mode="duty",
        )

    async def reject_offer(
        self,
        request_code: str,
        stage: str,
        admin_telegram_id: int,
        *,
        reason: str = "manual_reject",
        _session=None,
    ) -> bool:
        """Reject an offer and exclude that admin from the stage for the day."""
        now = self._utc(None)
        async with self._maybe_session(_session) as session:
            if _session is None:
                async with session.begin():
                    return await self._reject_offer_impl(
                        session, request_code, stage, admin_telegram_id, reason, now
                    )
            return await self._reject_offer_impl(
                session, request_code, stage, admin_telegram_id, reason, now
            )

    async def _reject_offer_impl(
        self,
        session,
        request_code: str,
        stage: str,
        admin_telegram_id: int,
        reason: str,
        now: datetime,
    ) -> bool:
        row = (
            await session.execute(
                select(AdminAssignment).where(
                    AdminAssignment.request_code == request_code,
                    AdminAssignment.stage == stage,
                    AdminAssignment.admin_telegram_id == admin_telegram_id,
                    AdminAssignment.status == "offered",
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.status = "rejected"
        row.responded_at = now
        row.decision_reason = (
            "quiet_reject"
            if reason == "manual_reject" and row.decision_reason == "quiet_offer"
            else reason[:64]
        )
        row.expires_at = None
        return True

    async def expire_offers(
        self,
        *,
        now: datetime | None = None,
        reassign: bool = True,
        _session=None,
    ) -> list[OfferExpiry]:
        """Expire stale offers and optionally advance the ladder.

        First silent timeout becomes ``skipped`` and the engine tries the next
        candidate. If nobody else is viable, it reoffers the same admin as the
        second pass. A second silent timeout becomes ``rejected`` for the day.
        """
        clock = self._utc(now)
        async with self._maybe_session(_session) as session:
            if _session is None:
                async with session.begin():
                    return await self._expire_offers_impl(session, clock, reassign)
            return await self._expire_offers_impl(session, clock, reassign)

    async def _expire_offers_impl(
        self, session, now: datetime, reassign: bool
    ) -> list[OfferExpiry]:
        rows = (
            await session.execute(
                select(AdminAssignment).where(
                    AdminAssignment.status == "offered",
                    AdminAssignment.expires_at.is_not(None),
                    AdminAssignment.expires_at <= now,
                ).with_for_update()
            )
        ).scalars().all()

        expired: list[OfferExpiry] = []
        for row in rows:
            second_silence = int(row.offer_attempt or 0) >= 2
            row.status = "rejected" if second_silence else "skipped"
            row.responded_at = now
            row.decision_reason = "second_silent_timeout" if second_silence else "silent_timeout"
            row.expires_at = None
            expired.append(
                OfferExpiry(
                    request_code=row.request_code,
                    stage=row.stage,
                    admin_telegram_id=row.admin_telegram_id,
                    final_status=row.status,
                    decision_reason=row.decision_reason,
                )
            )
        await session.flush()

        if not reassign:
            return expired

        for item in expired:
            result = await self._assign_impl(
                session,
                item.request_code,
                item.stage,
                None,
                now,
                second_pass=False,
                excluded_admin_ids={item.admin_telegram_id},
            )
            if result is None and item.final_status == "skipped":
                result = await self._assign_impl(
                    session,
                    item.request_code,
                    item.stage,
                    None,
                    now,
                    second_pass=True,
                    excluded_admin_ids=set(),
                )
            if result is not None:
                item.reassigned_to = result.admin_telegram_id
        return expired

    async def complete_task(self, request_code: str, stage: str, _session=None) -> None:
        """Mark a task as completed and increment the admin's counter."""
        async with self._maybe_session(_session) as session:
            assignment = (
                await session.execute(
                    select(AdminAssignment).where(
                        AdminAssignment.request_code == request_code,
                        AdminAssignment.stage == stage,
                        AdminAssignment.status.in_(ACTIVE_STATUSES),
                    )
                )
            ).scalar_one_or_none()
            if assignment is not None:
                assignment.status = "completed"
                assignment.completed_at = datetime.now(UTC)

                from sqlalchemy import update

                await session.execute(
                    update(AdminAvailability)
                    .where(AdminAvailability.admin_telegram_id == assignment.admin_telegram_id)
                    .values(total_tasks_completed=AdminAvailability.total_tasks_completed + 1)
                )

            # Keep the linked work item (WRK-N) in step with the request's
            # lifecycle, even when no open assignment row exists (e.g. a
            # force-published redo whose Gojo row was already completed). This
            # is what stops an already-processed work from counting as "open"
            # forever in the queue counts / idle nudges.
            await self._sync_work_item(session, request_code, stage)

            if _session is None:
                await session.commit()

    async def _sync_work_item(
        self, session, request_code: str, stage: str,
    ) -> None:
        """Advance/complete the ``WorkItem`` linked to a finished stage.

        * ``levi`` done  → work reopens at ``distribute`` (Senku's pool)
        * ``senku`` done → work reopens at ``publish`` (Gojo's pool)
        * ``gojo`` done  → work is ``done`` (the whole pipeline finished)

        Best-effort: a missing/optional ``work_items`` table never breaks a
        stage completion.
        """
        try:
            from kurosoden.shared.work_service import (
                STAGE_DISTRIBUTE,
                STAGE_PUBLISH,
                STATUS_CANCELLED,
                STATUS_DONE,
                STATUS_OPEN,
                WorkItem,
            )

            w = (
                await session.execute(
                    select(WorkItem).where(WorkItem.request_code == request_code)
                )
            ).scalar_one_or_none()
            if w is None:
                return
            # Never resurrect a terminal work item. Without this guard a work the
            # owner cancelled (or one already done) gets flipped back to OPEN the
            # moment its linked request completes a later stage — the "deleted
            # WRK rows keep coming back" bug on the Manage board.
            if w.status in (STATUS_CANCELLED, STATUS_DONE):
                return
            if stage == "levi":
                w.stage, w.status = STAGE_DISTRIBUTE, STATUS_OPEN
                w.assigned_admin_id = None
            elif stage == "senku":
                w.stage, w.status = STAGE_PUBLISH, STATUS_OPEN
                w.assigned_admin_id = None
            elif stage == "gojo":
                w.status = STATUS_DONE
                w.assigned_admin_id = None
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never fail a stage
            from nekofetch.core.logging import get_logger

            get_logger(__name__).debug(
                "assignment.work_item_sync_failed",
                request=request_code, stage=stage, error=str(exc)[:200],
            )

    async def get_active_tasks(
        self, admin_telegram_id: int, *, stage: str | None = None, _session=None
    ) -> list[AdminAssignment]:
        """Get active duty tasks for an admin.

        ``stage`` scopes the result to a single pipeline stage (``"senku"`` /
        ``"levi"`` / ``"gojo"``). Each staff bot MUST pass its own stage — one
        request has a separate assignment row per stage, so an unfiltered list
        makes the SAME request show up as two near-identical task buttons (the
        reported "two buttons for one task"). ``None`` returns every stage (used
        by the owner's cross-stage overview in Lelouch).
        """
        async with self._maybe_session(_session) as session:
            conds = [
                AdminAssignment.admin_telegram_id == admin_telegram_id,
                AdminAssignment.status.in_(ACTIVE_STATUSES),
            ]
            if stage is not None:
                conds.append(AdminAssignment.stage == stage)
            result = await session.execute(
                select(AdminAssignment).where(*conds)
                .order_by(AdminAssignment.created_at.asc())
            )
            return list(result.scalars().all())

    async def get_pending_offers(
        self, admin_telegram_id: int, *, stage: str | None = None,
        now: datetime | None = None, _session=None
    ) -> list[AdminAssignment]:
        """Get unexpired pending offers for an admin.

        ``stage`` scopes to a single pipeline stage — see :meth:`get_active_tasks`
        for why each bot must pass its own (avoids duplicate offer buttons).
        """
        clock = self._utc(now)
        async with self._maybe_session(_session) as session:
            conds = [
                AdminAssignment.admin_telegram_id == admin_telegram_id,
                AdminAssignment.status == "offered",
                AdminAssignment.expires_at.is_not(None),
                AdminAssignment.expires_at > clock,
            ]
            if stage is not None:
                conds.append(AdminAssignment.stage == stage)
            result = await session.execute(
                select(AdminAssignment).where(*conds)
                .order_by(AdminAssignment.expires_at.asc())
            )
            return list(result.scalars().all())

    async def get_timezone(self, admin_telegram_id: int, _session=None) -> str | None:
        """The admin's IANA timezone name, or ``None`` for global default."""
        async with self._maybe_session(_session) as session:
            row = (
                await session.execute(
                    select(AdminAvailability).where(
                        AdminAvailability.admin_telegram_id == admin_telegram_id
                    )
                )
            ).scalar_one_or_none()
            return row.timezone if row else None

    async def set_timezone(
        self,
        admin_telegram_id: int,
        tz_name: str,
        *,
        admin_name: str | None = None,
        _session=None,
    ) -> None:
        """Set the admin's IANA timezone, creating their availability row if new."""
        async with self._maybe_session(_session) as session:
            row = (
                await session.execute(
                    select(AdminAvailability).where(
                        AdminAvailability.admin_telegram_id == admin_telegram_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = AdminAvailability(
                    admin_telegram_id=admin_telegram_id,
                    admin_name=admin_name,
                    timezone=tz_name,
                )
                session.add(row)
            else:
                row.timezone = tz_name
                if admin_name and not row.admin_name:
                    row.admin_name = admin_name
            if _session is None:
                await session.commit()
