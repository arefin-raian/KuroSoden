"""Schema self-heal (``_reconcile_columns``) — the guard against the
``column index_sections.repurposed does not exist`` crash-loop.

Prod boots with ``python main.py`` and no ``alembic upgrade head``; SQLAlchemy's
``create_all`` creates missing *tables* but never adds a column to a table that
already exists. A release that adds a column therefore drifts the live DB and
the first query naming it crash-loops startup. ``_reconcile_columns`` closes
that gap with additive ``ADD COLUMN IF NOT EXISTS``, driven by the ORM models.

These use raw ``ADD COLUMN IF NOT EXISTS`` / ``information_schema`` and so are
Postgres-only; they skip on the default in-memory SQLite suite. Run with
``KAGE_TEST_DATABASE_URL=postgresql+asyncpg://…`` to exercise them.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text

from nekofetch.infrastructure.database.postgres.base import Base
from nekofetch.infrastructure.database.postgres.session import _reconcile_columns

pytestmark = pytest.mark.skipif(
    not os.environ.get("KAGE_TEST_DATABASE_URL", ""),
    reason="reconciler is Postgres-only; set KAGE_TEST_DATABASE_URL to run",
)


async def _columns(conn, table: str) -> set[str]:
    def _read(sync_conn):
        return {c["name"] for c in inspect(sync_conn).get_columns(table)}

    return await conn.run_sync(_read)


@pytest.mark.asyncio
async def test_reconciler_adds_a_dropped_column(engine):
    """Simulate the exact prod drift: table exists, later column is absent.

    Drop ``index_sections.repurposed`` to mimic a DB created before the column
    was added, then run the reconciler and confirm it comes back."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql(
            "ALTER TABLE index_sections DROP COLUMN IF EXISTS repurposed"
        )
        assert "repurposed" not in await _columns(conn, "index_sections")

        await conn.run_sync(_reconcile_columns)

        assert "repurposed" in await _columns(conn, "index_sections")


@pytest.mark.asyncio
async def test_reconciler_backfills_not_null_column_on_populated_table(engine):
    """A NOT NULL column (``repurposed``) must be addable to a table that
    already has rows — the model's Python-side ``default=False`` is inlined as a
    server DEFAULT so existing rows get a value instead of the ALTER failing."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql(
            "ALTER TABLE index_sections DROP COLUMN IF EXISTS repurposed"
        )
        # Insert a row while the column is absent — this is the "populated
        # table" that a bare NOT NULL ADD COLUMN would choke on.
        await conn.exec_driver_sql(
            "INSERT INTO index_sections (sort_order, base_letter) VALUES (0, 'A')"
        )

        await conn.run_sync(_reconcile_columns)

        # Existing row got the default, and the column is genuinely NOT NULL.
        val = (
            await conn.exec_driver_sql(
                "SELECT repurposed FROM index_sections WHERE base_letter = 'A'"
            )
        ).scalar()
        assert val is False


@pytest.mark.asyncio
async def test_reconciler_preserves_existing_data(engine):
    """Strictly additive: reconciling must never drop, retype, or clear a value
    in a column that is already present."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql(
            "INSERT INTO index_sections (sort_order, base_letter, label) "
            "VALUES (7, 'K', 'keepme')"
        )

        await conn.run_sync(_reconcile_columns)

        label = (
            await conn.exec_driver_sql(
                "SELECT label FROM index_sections WHERE base_letter = 'K'"
            )
        ).scalar()
        assert label == "keepme"


@pytest.mark.asyncio
async def test_reconciler_is_idempotent(engine):
    """Running it twice against an already-current schema is a no-op, not an
    error — it runs on every single boot."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_reconcile_columns)
        await conn.run_sync(_reconcile_columns)  # must not raise


@pytest.mark.asyncio
async def test_reconciler_heals_all_recent_drift_columns(engine):
    """The columns from features #39/#40/#41 + the ImgBB backup work — every
    one added to a pre-existing table, i.e. every one at risk of the drift."""
    drift = {
        "bots": ["invite_link", "creation_scope", "userbot_account"],
        "index_sections": ["repurposed"],
        "published_post_backups": ["image_imgbb_url"],
    }
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, cols in drift.items():
            for col in cols:
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}"
                )

        await conn.run_sync(_reconcile_columns)

        for table, cols in drift.items():
            present = await _columns(conn, table)
            for col in cols:
                assert col in present, f"{table}.{col} not restored"
