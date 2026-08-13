"""Durable scheduling for deferred main-channel publishes.

APScheduler jobs live in memory and their callables aren't serializable, so a
restart would silently forget every pending scheduled publish. This service
persists each schedule as a :class:`ScheduledPost` row and a recurring
:meth:`sweep_due` job (registered in ``bots/manager.py``, same 60s cadence as the
broadcast and link-expiry sweeps) fires any past-due ``pending`` row through the
normal :class:`PublishingService` path — so a schedule survives crashes/restarts
and is never double-fired.

Times are stored in UTC (tz-aware) like everything else; each admin enters and
reads them in their own timezone (``AdminAvailability.timezone``), resolved via
:mod:`nekofetch.core.timefmt`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import ScheduledPost
from nekofetch.infrastructure.database.postgres.session import session_scope

log = get_logger(__name__)

# How close two scheduled posts may be before we warn about a clash.
_COLLISION_MINUTES = 10


@dataclass(slots=True)
class SweepResult:
    """Outcome of one sweep_due run."""
    fired: int = 0
    published: int = 0
    failed: int = 0


class ScheduleService:
    def __init__(self, container: Container) -> None:
        self._c = container

    # ── create / cancel ────────────────────────────────────────────────────────

    async def schedule(
        self, request_code: str, admin_telegram_id: int, when_utc: datetime, *,
        anime_title: str | None = None, silent: bool = False,
        caption_override: str | None = None,
    ) -> ScheduledPost:
        """Persist a pending scheduled publish. ``when_utc`` must be aware UTC."""
        if when_utc.tzinfo is None:
            when_utc = when_utc.replace(tzinfo=timezone.utc)
        row = ScheduledPost(
            request_code=request_code,
            anime_title=anime_title,
            admin_telegram_id=admin_telegram_id,
            scheduled_at=when_utc,
            silent=silent,
            caption_override=caption_override,
            status="pending",
        )
        async with session_scope(self._c.pg_sessionmaker) as session:
            # Supersede any earlier pending schedule for the same request so a
            # re-schedule doesn't fire twice.
            existing = (
                await session.execute(
                    select(ScheduledPost).where(
                        ScheduledPost.request_code == request_code,
                        ScheduledPost.status == "pending",
                    )
                )
            ).scalars().all()
            for old in existing:
                old.status = "cancelled"
            session.add(row)
        log.info("schedule.created", code=request_code, when=when_utc.isoformat(),
                 admin=admin_telegram_id)
        return row

    async def cancel(self, schedule_id: int, admin_telegram_id: int | None = None) -> bool:
        """Cancel a pending schedule. If ``admin_telegram_id`` is given, only that
        admin's own row is cancellable. Returns True if a row was cancelled."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(ScheduledPost).where(ScheduledPost.id == schedule_id)
                )
            ).scalar_one_or_none()
            if row is None or row.status != "pending":
                return False
            if admin_telegram_id is not None and row.admin_telegram_id != admin_telegram_id:
                return False
            row.status = "cancelled"
            return True

    async def cancel_for_request(self, request_code: str) -> int:
        """Cancel every pending schedule for a request (e.g. after a force
        publish, so the scheduled time can't double-post later). Returns the
        number of rows cancelled."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(ScheduledPost).where(
                        ScheduledPost.request_code == request_code,
                        ScheduledPost.status == "pending",
                    )
                )
            ).scalars().all()
            for row in rows:
                row.status = "cancelled"
            return len(rows)

    # ── queries ──────────────────────────────────────────────────────────────

    async def list_pending(self) -> list[ScheduledPost]:
        """Every admin's pending scheduled posts, soonest first (for the table)."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(ScheduledPost)
                    .where(ScheduledPost.status == "pending")
                    .order_by(ScheduledPost.scheduled_at.asc())
                )
            ).scalars().all()
            return list(rows)

    async def collision_window(
        self, when_utc: datetime, *, minutes: int = _COLLISION_MINUTES,
        exclude_code: str | None = None,
    ) -> list[ScheduledPost]:
        """Pending posts within ±``minutes`` of ``when_utc`` (possible clashes)."""
        if when_utc.tzinfo is None:
            when_utc = when_utc.replace(tzinfo=timezone.utc)
        lo = when_utc - timedelta(minutes=minutes)
        hi = when_utc + timedelta(minutes=minutes)
        async with session_scope(self._c.pg_sessionmaker) as session:
            q = select(ScheduledPost).where(
                ScheduledPost.status == "pending",
                ScheduledPost.scheduled_at >= lo,
                ScheduledPost.scheduled_at <= hi,
            )
            if exclude_code:
                q = q.where(ScheduledPost.request_code != exclude_code)
            rows = (await session.execute(q.order_by(ScheduledPost.scheduled_at.asc()))).scalars().all()
            return list(rows)

    # ── the durable sweep (scheduler job) ──────────────────────────────────────

    async def sweep_due(self, *, client=None) -> SweepResult:
        """Publish every past-due pending schedule. Registered as a 60s job.

        Each row is claimed (status flipped off ``pending``) before the publish
        so a slow publish can't be re-fired by the next sweep tick. A publish
        failure is recorded on the row and surfaced in logs, never raised, so one
        bad schedule can't wedge the sweep.
        """
        from nekofetch.services.publishing_service import PublishingService

        result = SweepResult()
        now = datetime.now(timezone.utc)
        async with session_scope(self._c.pg_sessionmaker) as session:
            due = (
                await session.execute(
                    select(ScheduledPost).where(
                        ScheduledPost.status == "pending",
                        ScheduledPost.scheduled_at <= now,
                    ).order_by(ScheduledPost.scheduled_at.asc())
                )
            ).scalars().all()
            # Claim first so a concurrent/next sweep won't double-fire.
            claimed = [(r.id, r.request_code, r.caption_override, r.silent,
                        r.anime_title, r.admin_telegram_id) for r in due]
            for r in due:
                r.status = "publishing"
                r.fired_at = now

        publisher = PublishingService(self._c)
        for sched_id, code, caption, silent, title, admin_id in claimed:
            result.fired += 1
            status, err = "published", None
            try:
                await publisher.publish(code, caption_override=caption, silent=silent)
                result.published += 1
            except Exception as exc:  # noqa: BLE001 — one bad schedule ≠ wedge sweep
                status, err = "failed", str(exc)[:500]
                result.failed += 1
                log.warning("schedule.fire_failed", code=code, error=str(exc))
            else:
                # Mark the pipeline task complete, mirroring the button path.
                try:
                    from kurosoden.shared.admin_assignment import AdminAssignmentEngine

                    await AdminAssignmentEngine(self._c.pg_sessionmaker).complete_task(code, "gojo")
                except Exception:  # noqa: BLE001 — bookkeeping only
                    pass
                await self._notify(client, admin_id, title or code, silent)

            async with session_scope(self._c.pg_sessionmaker) as session:
                row = (
                    await session.execute(
                        select(ScheduledPost).where(ScheduledPost.id == sched_id)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.status = status
                    row.error = err

        if result.fired:
            log.info("schedule.sweep.done", fired=result.fired,
                     published=result.published, failed=result.failed)
        return result

    async def _notify(self, client, admin_id: int, title: str, silent: bool) -> None:
        """Best-effort DM to the scheduling admin that their post went live."""
        client = client or getattr(self._c, "admin_client", None)
        if client is None or not admin_id:
            return
        try:
            from pyrogram.enums import ParseMode

            from kurosoden.shared import gojo_voice as V

            await client.send_message(
                admin_id, V.published(title, silent=silent), parse_mode=ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass
