"""Tests for kurosoden/shared/admin_assignment.py — Admin assignment engine.

Covers:
  • AssignmentResult dataclass defaults
  • AdminAssignment / AdminAvailability ORM models
  • AdminAssignmentEngine._is_on_break() edge cases
  • AdminAssignmentEngine.assign() with DB
  • AdminAssignmentEngine.complete_task()
  • AdminAssignmentEngine.get_active_tasks()
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# AssignmentResult dataclass
# ═══════════════════════════════════════════════════════════════════════════════

class TestAssignmentResult:
    """Pure data class — no DB needed."""

    def test_constructor_all_fields(self):
        from kurosoden.shared.admin_assignment import AssignmentResult
        r = AssignmentResult(
            admin_telegram_id=123,
            admin_name="Alice",
            tasks_active=3,
            tasks_completed=42,
        )
        assert r.admin_telegram_id == 123
        assert r.admin_name == "Alice"
        assert r.tasks_active == 3
        assert r.tasks_completed == 42

    def test_none_admin_name(self):
        from kurosoden.shared.admin_assignment import AssignmentResult
        r = AssignmentResult(admin_telegram_id=1, admin_name=None, tasks_active=0, tasks_completed=0)
        assert r.admin_name is None

    def test_zero_tasks(self):
        from kurosoden.shared.admin_assignment import AssignmentResult
        r = AssignmentResult(admin_telegram_id=1, admin_name="New Admin", tasks_active=0, tasks_completed=0)
        assert r.tasks_active == 0
        assert r.tasks_completed == 0

    def test_high_tasks(self):
        from kurosoden.shared.admin_assignment import AssignmentResult
        r = AssignmentResult(admin_telegram_id=1, admin_name="Veteran", tasks_active=50, tasks_completed=9999)
        assert r.tasks_completed == 9999


# ═══════════════════════════════════════════════════════════════════════════════
# ORM Model field validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminAssignmentModel:
    """ORM model field defaults and constraints."""

    def test_tablename(self):
        from kurosoden.shared.admin_assignment import AdminAssignment
        assert AdminAssignment.__tablename__ == "admin_assignments"

    async def test_default_status(self, session):
        from kurosoden.shared.admin_assignment import AdminAssignment
        a = AdminAssignment(admin_telegram_id=1, request_code="REQ-1", stage="levi")
        session.add(a)
        await session.flush()
        assert a.status == "assigned"

    async def test_default_task_count(self, session):
        from kurosoden.shared.admin_assignment import AdminAssignment
        a = AdminAssignment(admin_telegram_id=1, request_code="REQ-1", stage="levi")
        session.add(a)
        await session.flush()
        assert a.task_count_at_assignment == 0

    def test_completed_at_none_by_default(self):
        from kurosoden.shared.admin_assignment import AdminAssignment
        a = AdminAssignment(admin_telegram_id=1, request_code="REQ-1", stage="levi")
        assert a.completed_at is None

    async def test_stage_persistence(self, session):
        from kurosoden.shared.admin_assignment import AdminAssignment
        a = AdminAssignment(admin_telegram_id=999, request_code="REQ-STAGE", stage="senku", status="assigned")
        session.add(a)
        await session.flush()
        assert a.id is not None
        assert a.stage == "senku"


class TestAdminAvailabilityModel:
    """ORM model for admin availability."""

    def test_tablename(self):
        from kurosoden.shared.admin_assignment import AdminAvailability
        assert AdminAvailability.__tablename__ == "admin_availability"

    async def test_default_is_available(self, session):
        from kurosoden.shared.admin_assignment import AdminAvailability
        a = AdminAvailability(admin_telegram_id=1)
        session.add(a)
        await session.flush()
        assert a.is_available is True

    async def test_default_total_tasks(self, session):
        from kurosoden.shared.admin_assignment import AdminAvailability
        a = AdminAvailability(admin_telegram_id=1)
        session.add(a)
        await session.flush()
        assert a.total_tasks_completed == 0

    async def test_assigned_bots_default(self, session):
        from kurosoden.shared.admin_assignment import AdminAvailability
        a = AdminAvailability(
            admin_telegram_id=200,
            admin_name="Bob",
            assigned_bots=["lelouch", "levi"],
        )
        session.add(a)
        await session.flush()
        assert a.assigned_bots == ["lelouch", "levi"]

    async def test_scheduled_breaks_json(self, session):
        from kurosoden.shared.admin_assignment import AdminAvailability
        breaks = [
            {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-02T00:00:00+00:00", "reason": "vacation"},
        ]
        a = AdminAvailability(admin_telegram_id=300, admin_name="Charlie", scheduled_breaks=breaks)
        session.add(a)
        await session.flush()
        assert len(a.scheduled_breaks) == 1
        assert a.scheduled_breaks[0]["reason"] == "vacation"

    async def test_unique_telegram_id_constraint(self, session):
        from kurosoden.shared.admin_assignment import AdminAvailability
        a1 = AdminAvailability(admin_telegram_id=400, admin_name="Dave")
        session.add(a1)
        await session.flush()

        a2 = AdminAvailability(admin_telegram_id=400, admin_name="Dave Duplicate")
        session.add(a2)
        with pytest.raises(Exception):
            await session.flush()

    async def test_null_admin_name_is_ok(self, session):
        from kurosoden.shared.admin_assignment import AdminAvailability
        a = AdminAvailability(admin_telegram_id=500, admin_name=None)
        session.add(a)
        await session.flush()
        assert a.admin_name is None


# ═══════════════════════════════════════════════════════════════════════════════
# AdminAssignmentEngine._is_on_break — static method, no DB needed
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsOnBreak:
    """Comprehensive edge cases for scheduled break detection."""

    @pytest.fixture
    def engine(self, sessionmaker):
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine
        return AdminAssignmentEngine(sessionmaker)

    @pytest.fixture
    def _avail(self, session):
        from kurosoden.shared.admin_assignment import AdminAvailability
        return lambda breaks=None: AdminAvailability(
            admin_telegram_id=1, admin_name="Test", scheduled_breaks=breaks
        )

    def test_no_breaks(self, engine, _avail):
        a = _avail(None)
        assert engine._is_on_break(a, datetime.now(timezone.utc)) is False

    def test_empty_breaks_list(self, engine, _avail):
        a = _avail([])
        assert engine._is_on_break(a, datetime.now(timezone.utc)) is False

    def test_active_break(self, engine, _avail):
        now = datetime.now(timezone.utc)
        a = _avail([
            {"start": (now - timedelta(hours=1)).isoformat(),
             "end": (now + timedelta(hours=1)).isoformat()},
        ])
        assert engine._is_on_break(a, now) is True

    def test_expired_break(self, engine, _avail):
        now = datetime.now(timezone.utc)
        a = _avail([
            {"start": (now - timedelta(days=2)).isoformat(),
             "end": (now - timedelta(days=1)).isoformat()},
        ])
        assert engine._is_on_break(a, now) is False

    def test_future_break(self, engine, _avail):
        now = datetime.now(timezone.utc)
        a = _avail([
            {"start": (now + timedelta(days=1)).isoformat(),
             "end": (now + timedelta(days=2)).isoformat()},
        ])
        assert engine._is_on_break(a, now) is False

    def test_boundary_start_exact(self, engine, _avail):
        now = datetime.now(timezone.utc)
        a = _avail([
            {"start": now.isoformat(), "end": (now + timedelta(hours=2)).isoformat()},
        ])
        assert engine._is_on_break(a, now) is True

    def test_boundary_end_exact(self, engine, _avail):
        end = datetime.now(timezone.utc)
        a = _avail([
            {"start": (end - timedelta(hours=2)).isoformat(), "end": end.isoformat()},
        ])
        assert engine._is_on_break(a, end) is True

    def test_multiple_breaks_one_active(self, engine, _avail):
        now = datetime.now(timezone.utc)
        a = _avail([
            {"start": (now - timedelta(days=10)).isoformat(), "end": (now - timedelta(days=8)).isoformat()},
            {"start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()},
            {"start": (now + timedelta(days=5)).isoformat(), "end": (now + timedelta(days=7)).isoformat()},
        ])
        assert engine._is_on_break(a, now) is True

    def test_invalid_break_missing_start(self, engine, _avail):
        a = _avail([{"end": "2026-01-01T00:00:00+00:00"}])
        assert engine._is_on_break(a, datetime.now(timezone.utc)) is False

    def test_invalid_break_missing_end(self, engine, _avail):
        a = _avail([{"start": "2026-01-01T00:00:00+00:00"}])
        assert engine._is_on_break(a, datetime.now(timezone.utc)) is False

    def test_invalid_break_bad_iso(self, engine, _avail):
        a = _avail([{"start": "not-a-date", "end": "also-not-a-date"}])
        assert engine._is_on_break(a, datetime.now(timezone.utc)) is False

    def test_mixed_valid_invalid_breaks(self, engine, _avail):
        now = datetime.now(timezone.utc)
        a = _avail([
            {"start": "invalid", "end": "also-invalid"},
            {"start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()},
        ])
        # The second break is active, so should return True.
        assert engine._is_on_break(a, now) is True

    def test_break_with_timezone_offset(self, engine, _avail):
        now = datetime.now(timezone.utc)
        a = _avail([
            {"start": (now - timedelta(hours=2)).isoformat(),
             "end": (now + timedelta(hours=2)).isoformat()},
        ])
        assert engine._is_on_break(a, now) is True


# ═══════════════════════════════════════════════════════════════════════════════
# AdminAssignmentEngine — DB integration tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminAssignmentEngineDB:
    """Tests that need the SQLite in-memory database."""

    @pytest.fixture
    def engine(self, sessionmaker):
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine
        return AdminAssignmentEngine(sessionmaker)

    # ── assign() ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_assign_successful(self, engine, session, admin_availability):
        result = await engine.assign("REQ-NEW", "levi")
        assert result is not None
        assert result.admin_telegram_id == 100
        assert result.tasks_active >= 1

    @pytest.mark.asyncio
    async def test_assign_no_available_admins(self, engine, session):
        """Should return None when no admins are available for the stage."""
        result = await engine.assign("REQ-X", "gojo")
        assert result is None

    @pytest.mark.asyncio
    async def test_assign_with_preferred_admin(self, engine, session, admin_availability):
        from kurosoden.tests.helpers import _create_admin_availability
        # Add a second admin.
        await _create_admin_availability(session, admin_telegram_id=200, admin_name="Admin2")
        result = await engine.assign("REQ-PREF", "levi", preferred_admin=200)
        assert result is not None
        assert result.admin_telegram_id == 200

    @pytest.mark.asyncio
    async def test_preferred_admin_ignored_if_unavailable(self, engine, session, admin_availability):
        result = await engine.assign("REQ-Y", "levi", preferred_admin=999)  # Doesn't exist
        # Should fall back to available admin.
        assert result is not None
        assert result.admin_telegram_id == 100

    @pytest.mark.asyncio
    async def test_assign_prefers_fewer_active_tasks(self, engine, session, admin_availability):
        from kurosoden.tests.helpers import _create_admin_availability
        # Admin 200: 0 active tasks, 50 completed.
        await _create_admin_availability(session, admin_telegram_id=200, admin_name="LessBusy", total_tasks_completed=50)
        # Admin 100 (default): 0 active, 0 completed.
        result = await engine.assign("REQ-BAL", "levi")
        # Both have 0 active. Admin 100 has fewer completed → should be chosen.
        assert result.admin_telegram_id == 100

    @pytest.mark.asyncio
    async def test_assign_skips_unavailable_admin(self, engine, session, admin_availability):
        from kurosoden.tests.helpers import _create_admin_availability
        await _create_admin_availability(session, admin_telegram_id=200, admin_name="Unavailable", is_available=False)
        result = await engine.assign("REQ-SKIP", "levi")
        # Admin 100 is the only available one.
        assert result.admin_telegram_id == 100

    @pytest.mark.asyncio
    async def test_assign_skips_admin_on_break(self, engine, session, admin_availability):
        from kurosoden.tests.helpers import _create_admin_availability
        now = datetime.now(timezone.utc)
        breaks = [
            {"start": (now - timedelta(hours=1)).isoformat(),
             "end": (now + timedelta(hours=1)).isoformat()},
        ]
        # Admin 200 is on break now.
        await _create_admin_availability(session, admin_telegram_id=200, admin_name="OnBreak", scheduled_breaks=breaks)
        result = await engine.assign("REQ-BREAK", "levi")
        # Only Admin 100 should be available.
        assert result is not None
        assert result.admin_telegram_id == 100

    @pytest.mark.asyncio
    async def test_assign_filter_by_stage(self, engine, session, admin_availability):
        from kurosoden.tests.helpers import _create_admin_availability
        # Admin 200: only on senku + gojo.
        await _create_admin_availability(session, admin_telegram_id=200, admin_name="SenkuAdmin", assigned_bots=["senku", "gojo"])
        result = await engine.assign("REQ-STAGE", "levi")
        # Admin 100 is on all stages including levi.
        assert result.admin_telegram_id == 100

    @pytest.mark.asyncio
    async def test_assign_creates_db_row(self, engine, session, admin_availability):
        from sqlalchemy import select
        from kurosoden.shared.admin_assignment import AdminAssignment

        await engine.assign("REQ-DB", "levi")
        result = await session.execute(
            select(AdminAssignment).where(AdminAssignment.request_code == "REQ-DB")
        )
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.stage == "levi"
        assert row.status == "assigned"
        assert row.admin_telegram_id == 100

    @pytest.mark.asyncio
    async def test_create_assignment_survives_duplicate_open_row(
        self, engine, session, admin_availability,
    ):
        """A lost check-then-insert race must NOT crash — it returns the winner.

        Reproduces the recovery-job UniqueViolationError on
        uq_admin_assignments_open_request_stage (request_code, stage): the 60s
        recovery job and a live handler both read "no open row" in separate
        transactions, both INSERT, and the second flush violates the partial
        unique index. We exercise the insert boundary directly — commit an OPEN
        row first (the race winner), then call _create_assignment for the SAME
        key. Its flush hits the constraint; the savepoint handler must roll back
        and return the winner's assignment instead of propagating IntegrityError.

        Targets _create_assignment directly (not assign()) so it doesn't depend
        on the candidacy/slot logic that is order-sensitive across the suite.
        """
        from datetime import datetime, timezone
        from sqlalchemy import select
        from kurosoden.shared.admin_assignment import AdminAssignment
        from kurosoden.tests.helpers import _create_admin_assignment

        # The race winner: an already-committed open row for the key.
        await _create_admin_assignment(
            session, admin_telegram_id=100, request_code="REQ-RACE",
            stage="levi", status="assigned",
        )

        # The loser: _create_assignment always attempts the INSERT (the top-level
        # guard lives in _assign_impl), so this deterministically hits the index.
        result = await engine._create_assignment(
            session, 100, "REQ-RACE", "levi", admin_availability,
            status="assigned", assignment_mode="duty",
            now=datetime.now(timezone.utc),
        )

        # Recovered, not crashed — returns the winner's open assignment.
        assert result is not None
        assert result.admin_telegram_id == 100
        assert result.status == "assigned"

        # Still exactly ONE open row for the key — no duplicate persisted.
        rows = (await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.request_code == "REQ-RACE",
                AdminAssignment.stage == "levi",
            )
        )).scalars().all()
        assert len(rows) == 1

    # ── complete_task() ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_complete_task_success(self, engine, session, admin_assignment, admin_availability):
        await engine.complete_task("REQ-0001", "levi")
        # The engine commits its own session; refresh to see the change.
        await session.refresh(admin_assignment)
        assert admin_assignment.status == "completed"
        assert admin_assignment.completed_at is not None

    @pytest.mark.asyncio
    async def test_complete_task_increments_counter(self, engine, session, admin_assignment, admin_availability):
        prev = admin_availability.total_tasks_completed
        await engine.complete_task("REQ-0001", "levi")
        await session.refresh(admin_availability)
        assert admin_availability.total_tasks_completed == prev + 1

    @pytest.mark.asyncio
    async def test_complete_task_non_existent(self, engine, session):
        """Should not crash when assignment doesn't exist."""
        await engine.complete_task("REQ-NOPE", "levi")  # No error expected.

    @pytest.mark.asyncio
    async def test_complete_task_only_assigned_status(self, engine, session, admin_assignment):
        """Only 'assigned' status rows should be completed."""
        admin_assignment.status = "completed"
        await engine.complete_task("REQ-0001", "levi")
        # Should have been skipped (WHERE status='assigned').
        from sqlalchemy import select
        from kurosoden.shared.admin_assignment import AdminAssignment
        result = await session.execute(
            select(AdminAssignment).where(AdminAssignment.request_code == "REQ-0001")
        )
        row = result.scalar_one()
        # Status unchanged (was already "completed", query didn't match).
        assert row.status == "completed"

    # ── get_active_tasks() ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_active_tasks_finds_assigned(self, engine, session, admin_assignment):
        tasks = await engine.get_active_tasks(100)
        assert len(tasks) >= 1
        assert tasks[0].request_code == "REQ-0001"

    @pytest.mark.asyncio
    async def test_get_active_tasks_empty(self, engine, session):
        tasks = await engine.get_active_tasks(999)
        assert tasks == []

    @pytest.mark.asyncio
    async def test_get_active_tasks_excludes_completed(self, engine, session, admin_assignment):
        admin_assignment.status = "completed"
        await session.commit()  # Persist so the engine's own session can see it.
        tasks = await engine.get_active_tasks(100)
        assert all(t.request_code != "REQ-0001" for t in tasks)

    @pytest.mark.asyncio
    async def test_get_active_tasks_includes_in_progress(self, engine, session, admin_assignment):
        admin_assignment.status = "in_progress"
        tasks = await engine.get_active_tasks(100)
        assert any(t.request_code == "REQ-0001" for t in tasks)

    @pytest.mark.asyncio
    async def test_get_active_tasks_ordered_by_created_at(self, engine, session):
        from kurosoden.tests.helpers import _create_admin_assignment, _create_admin_availability
        await _create_admin_availability(session, admin_telegram_id=700)
        await _create_admin_assignment(session, admin_telegram_id=700, request_code="REQ-A", status="assigned")
        await _create_admin_assignment(session, admin_telegram_id=700, request_code="REQ-B", status="assigned")
        await _create_admin_assignment(session, admin_telegram_id=700, request_code="REQ-C", status="assigned")
        tasks = await engine.get_active_tasks(700)
        codes = [t.request_code for t in tasks]
        assert codes == ["REQ-A", "REQ-B", "REQ-C"]


class TestSlotAwareAssignment:
    @pytest.fixture
    def engine(self, sessionmaker):
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        return AdminAssignmentEngine(sessionmaker)

    @pytest.mark.asyncio
    async def test_in_slot_admin_gets_duty_assignment(self, engine, session):
        from sqlalchemy import select
        from kurosoden.shared.admin_assignment import AdminAssignment
        from kurosoden.tests.helpers import _create_admin_availability, _create_user

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        await _create_admin_availability(
            session,
            admin_telegram_id=810,
            admin_name="InSlot",
            timezone="UTC",
            slots_weekday=[[11 * 60, 13 * 60]],
            slots_weekend=[],
            total_tasks_completed=4,
        )
        await _create_admin_availability(
            session,
            admin_telegram_id=811,
            admin_name="OutSlot",
            timezone="UTC",
            slots_weekday=[[18 * 60, 20 * 60]],
            slots_weekend=[],
            total_tasks_completed=0,
        )
        await _create_user(session, telegram_id=811, role="staff",
                           username="outslot", last_seen_at=now - timedelta(minutes=5))

        result = await engine.assign("REQ-SLOT-DUTY", "levi", now=now)

        assert result is not None
        assert result.admin_telegram_id == 810
        assert result.status == "assigned"
        assert result.assignment_mode == "duty"
        row = (await session.execute(
            select(AdminAssignment).where(AdminAssignment.request_code == "REQ-SLOT-DUTY")
        )).scalar_one()
        assert row.assignment_mode == "duty"

    @pytest.mark.asyncio
    async def test_out_of_slot_recently_active_admin_gets_offer(self, engine, session):
        from kurosoden.tests.helpers import _create_admin_availability, _create_user

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        await _create_admin_availability(
            session,
            admin_telegram_id=820,
            admin_name="ActiveOutSlot",
            timezone="UTC",
            slots_weekday=[[18 * 60, 20 * 60]],
            slots_weekend=[],
        )
        await _create_user(session, telegram_id=820, role="staff",
                           username="active", last_seen_at=now - timedelta(minutes=3))

        result = await engine.assign("REQ-OFFER", "levi", now=now)

        assert result is not None
        assert result.admin_telegram_id == 820
        assert result.status == "offered"
        assert result.assignment_mode == "offer"
        assert result.expires_at == now + timedelta(hours=1)
        assert await engine.get_active_tasks(820) == []
        offers = await engine.get_pending_offers(820, now=now)
        assert len(offers) == 1
        assert offers[0].request_code == "REQ-OFFER"

    @pytest.mark.asyncio
    async def test_inactive_out_of_slot_admin_gets_closest_slot_fallback(self, engine, session):
        from kurosoden.tests.helpers import _create_admin_availability

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        await _create_admin_availability(
            session,
            admin_telegram_id=830,
            admin_name="Closest",
            timezone="UTC",
            slots_weekday=[[13 * 60, 14 * 60]],
            slots_weekend=[],
        )
        await _create_admin_availability(
            session,
            admin_telegram_id=831,
            admin_name="Later",
            timezone="UTC",
            slots_weekday=[[20 * 60, 21 * 60]],
            slots_weekend=[],
        )

        result = await engine.assign("REQ-FALLBACK", "levi", now=now)

        assert result is not None
        assert result.admin_telegram_id == 830
        assert result.status == "assigned"
        assert result.assignment_mode == "fallback"

    @pytest.mark.asyncio
    async def test_accept_offer_promotes_to_duty_assignment(self, engine, session):
        from kurosoden.shared.admin_assignment import AdminAssignment
        from kurosoden.tests.helpers import _create_admin_availability

        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await _create_admin_availability(session, admin_telegram_id=840, admin_name="Accepting")
        row = AdminAssignment(
            admin_telegram_id=840,
            request_code="REQ-ACCEPT",
            stage="senku",
            status="offered",
            assignment_mode="offer",
            offer_attempt=1,
            expires_at=expires,
            offered_at=expires - timedelta(hours=1),
        )
        session.add(row)
        await session.commit()

        result = await engine.accept_offer("REQ-ACCEPT", "senku", 840)
        await session.refresh(row)

        assert result is not None
        assert result.status == "assigned"
        assert row.status == "assigned"
        assert row.assignment_mode == "duty"
        assert row.decision_reason == "accepted_offer"

    @pytest.mark.asyncio
    async def test_reject_offer_excludes_admin(self, engine, session):
        from kurosoden.shared.admin_assignment import AdminAssignment
        from kurosoden.tests.helpers import _create_admin_availability

        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await _create_admin_availability(session, admin_telegram_id=850, admin_name="Rejecting")
        row = AdminAssignment(
            admin_telegram_id=850,
            request_code="REQ-REJECT",
            stage="gojo",
            status="offered",
            assignment_mode="offer",
            offer_attempt=1,
            expires_at=expires,
        )
        session.add(row)
        await session.commit()

        assert await engine.reject_offer("REQ-REJECT", "gojo", 850) is True
        await session.refresh(row)
        assert row.status == "rejected"
        assert row.decision_reason == "manual_reject"

    @pytest.mark.asyncio
    async def test_first_silent_timeout_reoffers_on_second_pass(self, engine, session):
        from sqlalchemy import select
        from kurosoden.shared.admin_assignment import AdminAssignment
        from kurosoden.tests.helpers import _create_admin_availability, _create_user

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        await _create_admin_availability(
            session,
            admin_telegram_id=860,
            admin_name="Silent",
            timezone="UTC",
            slots_weekday=[[18 * 60, 20 * 60]],
            slots_weekend=[],
        )
        await _create_user(session, telegram_id=860, role="staff",
                           username="silent", last_seen_at=now)
        session.add(AdminAssignment(
            admin_telegram_id=860,
            request_code="REQ-SILENT",
            stage="levi",
            status="offered",
            assignment_mode="offer",
            offer_attempt=1,
            offered_at=now - timedelta(hours=2),
            expires_at=now - timedelta(minutes=1),
        ))
        await session.commit()

        expired = await engine.expire_offers(now=now, reassign=True)

        assert len(expired) == 1
        assert expired[0].final_status == "skipped"
        rows = (await session.execute(
            select(AdminAssignment)
            .where(AdminAssignment.request_code == "REQ-SILENT")
            .order_by(AdminAssignment.id.asc())
        )).scalars().all()
        assert [r.status for r in rows] == ["skipped", "offered"]
        assert rows[1].offer_attempt == 2

    @pytest.mark.asyncio
    async def test_second_silent_timeout_rejects_for_day(self, engine, session):
        from kurosoden.shared.admin_assignment import AdminAssignment
        from kurosoden.tests.helpers import _create_admin_availability

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        await _create_admin_availability(session, admin_telegram_id=870, admin_name="SecondSilent")
        row = AdminAssignment(
            admin_telegram_id=870,
            request_code="REQ-SECOND-SILENT",
            stage="levi",
            status="offered",
            assignment_mode="offer",
            offer_attempt=2,
            offered_at=now - timedelta(hours=2),
            expires_at=now - timedelta(minutes=1),
        )
        session.add(row)
        await session.commit()

        expired = await engine.expire_offers(now=now, reassign=False)
        await session.refresh(row)

        assert expired[0].final_status == "rejected"
        assert row.status == "rejected"
        assert row.decision_reason == "second_silent_timeout"

    @pytest.mark.asyncio
    async def test_quiet_hours_turn_in_slot_admin_into_offer_when_active(self, engine, session):
        from sqlalchemy import select
        from kurosoden.shared.admin_assignment import AdminAssignment
        from kurosoden.tests.helpers import _create_admin_availability, _create_user

        now = datetime(2026, 7, 21, 4, 30, tzinfo=timezone.utc)
        await _create_admin_availability(
            session,
            admin_telegram_id=880,
            admin_name="NightActive",
            timezone="UTC",
            slots_weekday=[[4 * 60, 6 * 60]],
            slots_weekend=[],
        )
        await _create_user(session, telegram_id=880, role="staff",
                           username="night", last_seen_at=now - timedelta(minutes=2))

        result = await engine.assign("REQ-QUIET-OFFER", "levi", now=now)

        assert result is not None
        assert result.admin_telegram_id == 880
        assert result.status == "offered"
        row = (await session.execute(
            select(AdminAssignment).where(AdminAssignment.request_code == "REQ-QUIET-OFFER")
        )).scalar_one()
        assert row.decision_reason == "quiet_offer"

    @pytest.mark.asyncio
    async def test_quiet_hours_do_not_assign_inactive_admin(self, engine, session):
        from kurosoden.tests.helpers import _create_admin_availability

        now = datetime(2026, 7, 21, 4, 30, tzinfo=timezone.utc)
        await _create_admin_availability(
            session,
            admin_telegram_id=881,
            admin_name="NightInactive",
            timezone="UTC",
            slots_weekday=[[4 * 60, 6 * 60]],
            slots_weekend=[],
        )

        result = await engine.assign("REQ-QUIET-FALLBACK", "levi", now=now)

        assert result is None

    @pytest.mark.asyncio
    async def test_quiet_reject_blocks_until_8am_then_slot_can_assign(self, engine, session):
        from kurosoden.shared.admin_assignment import AdminAssignment
        from kurosoden.tests.helpers import _create_admin_availability

        quiet = datetime(2026, 7, 21, 4, 30, tzinfo=timezone.utc)
        after_start = datetime(2026, 7, 21, 8, 30, tzinfo=timezone.utc)
        await _create_admin_availability(
            session,
            admin_telegram_id=882,
            admin_name="Morning",
            timezone="UTC",
            slots_weekday=[[8 * 60, 10 * 60]],
            slots_weekend=[],
        )
        row = AdminAssignment(
            admin_telegram_id=882,
            request_code="REQ-QUIET-REJECT",
            stage="levi",
            status="offered",
            assignment_mode="offer",
            offer_attempt=1,
            offered_at=quiet,
            expires_at=quiet + timedelta(hours=1),
            decision_reason="quiet_offer",
        )
        session.add(row)
        await session.commit()

        assert await engine.reject_offer("REQ-QUIET-REJECT", "levi", 882) is True
        await session.refresh(row)
        assert row.status == "rejected"
        assert row.decision_reason == "quiet_reject"

        blocked = await engine.assign("REQ-QUIET-NEW", "levi", now=quiet)
        assert blocked is None

        released = await engine.assign("REQ-MORNING-NEW", "levi", now=after_start)
        assert released is not None
        assert released.admin_telegram_id == 882
        assert released.status == "assigned"
        assert released.assignment_mode == "duty"


@pytest.mark.asyncio
async def test_recovery_assigns_quiet_deferred_queued_request_after_8am(
    sessionmaker, session,
):
    from types import SimpleNamespace

    from sqlalchemy import select
    from kurosoden.shared.admin_assignment import AdminAssignment
    from kurosoden.shared.assignment_recovery import recover_assignment_queue
    from kurosoden.tests.helpers import _create_admin_availability, _create_request

    await _create_admin_availability(
        session,
        admin_telegram_id=883,
        admin_name="MorningLevi",
        assigned_bots=["levi"],
        timezone="UTC",
        slots_weekday=[[8 * 60, 16 * 60]],
    )
    await _create_request(session, code="REQ-QUIET-WAKE", status="queued")

    container = SimpleNamespace(pg_sessionmaker=sessionmaker, pipeline_manager=None)
    report = await recover_assignment_queue(
        container,
        now=datetime(2026, 7, 21, 8, 5, tzinfo=timezone.utc),
        notify=False,
    )

    assert report.recovered_assignments == 1
    row = (
        await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.request_code == "REQ-QUIET-WAKE",
                AdminAssignment.stage == "levi",
            )
        )
    ).scalar_one()
    assert row.admin_telegram_id == 883
    assert row.status == "assigned"


@pytest.mark.asyncio
async def test_recovery_assigns_ready_request_to_next_missing_stage(
    sessionmaker, session,
):
    from types import SimpleNamespace

    from sqlalchemy import select
    from kurosoden.shared.admin_assignment import AdminAssignment
    from kurosoden.shared.assignment_recovery import recover_assignment_queue
    from kurosoden.tests.helpers import (
        _create_admin_assignment,
        _create_admin_availability,
        _create_request,
    )

    await _create_admin_availability(
        session,
        admin_telegram_id=884,
        admin_name="SenkuAdmin",
        assigned_bots=["senku"],
        timezone="UTC",
        slots_weekday=[[8 * 60, 16 * 60]],
    )
    await _create_request(session, code="REQ-SENKU-WAKE", status="ready")
    await _create_admin_assignment(
        session,
        admin_telegram_id=800,
        request_code="REQ-SENKU-WAKE",
        stage="levi",
        status="completed",
    )

    container = SimpleNamespace(pg_sessionmaker=sessionmaker, pipeline_manager=None)
    report = await recover_assignment_queue(
        container,
        now=datetime(2026, 7, 21, 8, 5, tzinfo=timezone.utc),
        notify=False,
    )

    assert report.recovered_assignments == 1
    row = (
        await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.request_code == "REQ-SENKU-WAKE",
                AdminAssignment.stage == "senku",
            )
        )
    ).scalar_one()
    assert row.admin_telegram_id == 884
    assert row.status == "assigned"
