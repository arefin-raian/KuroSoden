"""Tests for kurosoden/shared/models.py + kurosoden/__init__.py — Package metadata and model re-exports.

Covers:
  • kurosoden.__version__
  • kurosoden.shared.models re-exports
  • ORM table names and column counts
  • Schema creation with all models
"""

from __future__ import annotations

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Package metadata
# ═══════════════════════════════════════════════════════════════════════════════

class TestKagePackage:
    """kurosoden/__init__.py metadata."""

    def test_version_is_string(self):
        import kurosoden
        assert isinstance(kurosoden.__version__, str)

    def test_version_format(self):
        import kurosoden
        # Should be semver-like: X.Y.Z
        parts = kurosoden.__version__.split(".")
        assert len(parts) == 3
        for p in parts:
            assert p.isdigit()

    def test_package_docstring_exists(self):
        import kurosoden
        assert kurosoden.__doc__ is not None
        assert "Kage" in kurosoden.__doc__


# ═══════════════════════════════════════════════════════════════════════════════
# Models re-exports
# ═══════════════════════════════════════════════════════════════════════════════

class TestModelsReExports:
    """kurosoden.shared.models should re-export AdminAssignment and AdminAvailability."""

    def test_admin_assignment_reexported(self):
        from kurosoden.shared.models import AdminAssignment
        from kurosoden.shared.admin_assignment import AdminAssignment as _Orig
        assert AdminAssignment is _Orig

    def test_admin_availability_reexported(self):
        from kurosoden.shared.models import AdminAvailability
        from kurosoden.shared.admin_assignment import AdminAvailability as _Orig
        assert AdminAvailability is _Orig

    def test_all_exports_both(self):
        from kurosoden.shared.models import __all__
        assert "AdminAssignment" in __all__
        assert "AdminAvailability" in __all__

    def test_models_import_registers_on_base(self):
        """Importing models should make tables available on Base.metadata."""
        import kurosoden.shared.models  # noqa: F401
        from nekofetch.infrastructure.database.postgres.base import Base

        table_names = list(Base.metadata.tables.keys())
        assert "admin_assignments" in table_names
        assert "admin_availability" in table_names


# ═══════════════════════════════════════════════════════════════════════════════
# Schema verification — all tables
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaTables:
    """Verify ALL expected tables are created."""

    def test_all_neko_fetch_tables_exist(self):
        """The vendored NekoFetch models must all have their tables."""
        import kurosoden.shared.models  # noqa: F401
        import nekofetch.infrastructure.database.postgres.models  # noqa: F401
        from nekofetch.infrastructure.database.postgres.base import Base

        table_names = list(Base.metadata.tables.keys())
        expected = [
            "users", "requests", "download_queue", "files",
            "bots", "access_links", "storage_packs", "channel_posts",
            "index_sections", "access_tokens", "analytics_events",
            "bot_content_posts", "bot_deliveries", "audit_logs",
        ]
        for name in expected:
            assert name in table_names, f"Table '{name}' missing from schema"

    def test_kage_tables_exist(self):
        """Kage's own models should be in the schema."""
        import kurosoden.shared.models  # noqa: F401
        from nekofetch.infrastructure.database.postgres.base import Base

        table_names = list(Base.metadata.tables.keys())
        assert "admin_assignments" in table_names
        assert "admin_availability" in table_names

    def test_total_table_count(self):
        """Sanity check: should have 22 tables (19 NekoFetch + 3 Kage:
        admin_assignments, admin_availability, work_items).

        NekoFetch side includes ``channel_layout`` — the per-channel message
        map that lets a franchise update append cards in place — ``channel_broadcasts``,
        the durable record of operator broadcasts posted to every distribution
        channel (so a timed auto-delete survives restarts) — ``scheduled_posts``,
        the durable record of deferred main-channel publishes (so a schedule
        survives a restart and is never double-fired) — and
        ``channel_content_backups``, the wipe-proof snapshot of a distribution /
        index channel's content pack (so a banned channel is re-posted verbatim
        rather than re-rendered)."""
        import kurosoden.shared.models  # noqa: F401
        import nekofetch.infrastructure.database.postgres.models  # noqa: F401
        from nekofetch.infrastructure.database.postgres.base import Base

        table_names = list(Base.metadata.tables.keys())
        assert len(table_names) == 22


# ═══════════════════════════════════════════════════════════════════════════════
# Table column verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminAssignmentColumns:
    """AdminAssignment table has all expected columns."""

    def test_column_names(self):
        from kurosoden.shared.admin_assignment import AdminAssignment
        cols = {c.name for c in AdminAssignment.__table__.columns}
        expected = {"id", "created_at", "updated_at", "admin_telegram_id",
                     "request_code", "stage", "status", "assignment_mode",
                     "offer_attempt", "offered_at", "expires_at",
                     "responded_at", "decision_reason",
                     "task_count_at_assignment", "completed_at"}
        assert cols == expected

    def test_admin_telegram_id_is_bigint(self):
        from kurosoden.shared.admin_assignment import AdminAssignment
        col = AdminAssignment.__table__.columns["admin_telegram_id"]
        assert "bigint" in str(col.type).lower()

    def test_request_code_is_varchar(self):
        from kurosoden.shared.admin_assignment import AdminAssignment
        col = AdminAssignment.__table__.columns["request_code"]
        assert "varchar" in str(col.type).lower() or "string" in str(col.type).lower()


class TestAdminAvailabilityColumns:
    """AdminAvailability table has all expected columns."""

    def test_column_names(self):
        from kurosoden.shared.admin_assignment import AdminAvailability
        cols = {c.name for c in AdminAvailability.__table__.columns}
        expected = {"id", "created_at", "updated_at", "admin_telegram_id",
                     "admin_name", "is_available", "assigned_bots",
                     "scheduled_breaks", "total_tasks_completed",
                     "weight", "working_hours", "timezone",
                     "country", "max_hours_per_day",
                     "slots_weekday", "slots_weekend"}
        assert cols == expected

    def test_is_available_is_boolean(self):
        from kurosoden.shared.admin_assignment import AdminAvailability
        col = AdminAvailability.__table__.columns["is_available"]
        assert "bool" in str(col.type).lower()

    def test_total_tasks_is_integer(self):
        from kurosoden.shared.admin_assignment import AdminAvailability
        col = AdminAvailability.__table__.columns["total_tasks_completed"]
        assert "int" in str(col.type).lower()


# ═══════════════════════════════════════════════════════════════════════════════
# DB create_all integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestDBCreateAll:
    """Tests that actually use the SQLite engine fixture."""

    @pytest.mark.asyncio
    async def test_create_all_runs_without_error(self, engine):
        """create_all should have created all tables."""
        import kurosoden.shared.models  # noqa: F401
        from nekofetch.infrastructure.database.postgres.base import Base

        table_names = list(Base.metadata.tables.keys())
        assert "admin_assignments" in table_names
        assert "admin_availability" in table_names

    @pytest.mark.asyncio
    async def test_can_insert_and_query_admin_assignment(self, session):
        from kurosoden.shared.admin_assignment import AdminAssignment
        from sqlalchemy import select

        a = AdminAssignment(
            admin_telegram_id=1, request_code="REQ-T", stage="levi", status="assigned"
        )
        session.add(a)
        await session.flush()

        result = await session.execute(select(AdminAssignment).where(AdminAssignment.request_code == "REQ-T"))
        row = result.scalar_one()
        assert row.stage == "levi"
        assert row.admin_telegram_id == 1

    @pytest.mark.asyncio
    async def test_can_insert_and_query_admin_availability(self, session):
        from kurosoden.shared.admin_assignment import AdminAvailability
        from sqlalchemy import select

        a = AdminAvailability(
            admin_telegram_id=5000, admin_name="Test", is_available=True,
            assigned_bots=["lelouch"], total_tasks_completed=5,
        )
        session.add(a)
        await session.flush()

        result = await session.execute(
            select(AdminAvailability).where(AdminAvailability.admin_telegram_id == 5000)
        )
        row = result.scalar_one()
        assert row.admin_name == "Test"
        assert row.total_tasks_completed == 5
