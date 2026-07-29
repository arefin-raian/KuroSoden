"""Postgres session utilities and schema creation helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nekofetch.infrastructure.database.postgres.base import Base


def _default_literal(col) -> str | None:
    """Render a column's Python-side scalar ``default`` as a SQL literal.

    Used to backfill a NOT NULL column being added to an already-populated
    table: the model may declare only a Python-side ``default=`` (applied by the
    ORM on insert, invisible to ``ADD COLUMN``), so we translate that scalar into
    a server ``DEFAULT`` for the one-off backfill. Returns ``None`` when the
    default is absent, callable, or a non-scalar we can't safely inline.
    """
    default = col.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    val = default.arg
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return "'" + val.replace("'", "''") + "'"
    return None


def _reconcile_columns(conn) -> None:
    """Add columns present in the ORM models but missing from existing tables.

    ``Base.metadata.create_all`` only creates whole *tables*; a column added to a
    table that already exists never lands. Because prod boots with ``python
    main.py`` and no ``alembic upgrade head``, a release that adds a column to an
    existing table leaves the live DB drifted, and the first query naming that
    column crash-loops startup (this is exactly how ``column
    index_sections.repurposed does not exist`` bit us). This inspects the live
    schema and issues additive ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` for
    each drifted column, driven by the models so future columns self-heal too.

    Strictly additive: it never drops, renames, or retypes a column, so it cannot
    lose data. Postgres-only (relies on ``ADD COLUMN IF NOT EXISTS``).
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateColumn

    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # whole table was just created by create_all above
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            coldef = str(CreateColumn(col).compile(dialect=conn.dialect)).strip()
            # A NOT NULL column with no server default fails on a populated table
            # (existing rows have no value). If the model carries a Python-side
            # scalar default, inline it as a server DEFAULT for the backfill;
            # otherwise fall back to adding it nullable so startup survives — the
            # ORM still applies its default on subsequent inserts.
            if not col.nullable and col.server_default is None:
                literal = _default_literal(col)
                if literal is not None:
                    coldef = f"{coldef} DEFAULT {literal}"
                else:
                    coldef = coldef.replace(" NOT NULL", "")
            conn.exec_driver_sql(
                f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS {coldef}'
            )

    if "admin_assignments" in existing_tables:
        conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_assignments_open_request_stage
            ON admin_assignments (request_code, stage)
            WHERE status IN ('assigned', 'in_progress', 'offered')
            """
        )


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transactional session scope: commit on success, rollback on error."""
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all(engine) -> None:
    """Create tables + required sequences for first-run/dev.

    Production also runs Alembic, but the container boots with ``python main.py``
    (no ``alembic upgrade head``), so anything NOT expressed in ``Base.metadata``
    — like the raw ``request_code_seq`` sequence that backs collision-free request
    codes — must be created here too, or every request submit crashes with
    ``relation "request_code_seq" does not exist``. Idempotent on every backend.
    """
    from sqlalchemy import text

    # Import models so they register on Base.metadata.
    from nekofetch.infrastructure.database.postgres import models  # noqa: F401
    # Kuro Sōden's admin-pool tables (admin_availability / admin_assignments)
    # live in shared/, outside the nekofetch models package, so import them here
    # too — otherwise create_all never emits them and every assignment crashes.
    try:  # optional: only present in the Kuro Sōden layout, not vanilla NekoFetch
        from kurosoden.shared import admin_assignment  # noqa: F401
    except Exception:  # noqa: BLE001
        pass

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # ``create_all`` only creates *missing tables*; it never adds a new
        # column (or index) to a table that already exists. Since prod boots with
        # ``python main.py`` and no ``alembic upgrade head``, a release that adds a
        # column to an existing table leaves the live DB drifted, and the first
        # query naming that column crash-loops startup (e.g. ``column
        # index_sections.repurposed does not exist``). Reconcile the drift here,
        # idempotently, so the next boot self-heals — same spirit as the
        # ``request_code_seq`` block below. Postgres-only (ADD COLUMN IF NOT EXISTS).
        if conn.dialect.name == "postgresql":
            await conn.run_sync(_reconcile_columns)

        # ``request_code_seq`` is a bare Postgres sequence (see request_repo
        # .next_sequence + migration 0009), not an ORM table, so create_all above
        # never emits it. SQLite has no CREATE SEQUENCE and the test suite stubs
        # next_sequence differently, so this is Postgres-only.
        if conn.dialect.name == "postgresql":
            # Seed ONLY on first creation. Running setval every boot would reset the
            # counter to MAX(code)+1 each time — so deleting the newest request then
            # rebooting would reissue its code (the exact collision the sequence
            # exists to prevent). ``to_regclass`` returns NULL when the sequence is
            # absent, which is our "freshly creating it now" signal.
            already = (
                await conn.execute(text("SELECT to_regclass('request_code_seq')"))
            ).scalar()
            await conn.execute(
                text("CREATE SEQUENCE IF NOT EXISTS request_code_seq MINVALUE 1")
            )
            if already is None:
                # Start just past the highest existing REQ-<n> so a fresh sequence on
                # an already-populated DB never reissues a live code. is_called=false
                # → the very next nextval() returns exactly the computed floor.
                start = (
                    await conn.execute(
                        text(
                            "SELECT COALESCE(MAX(CAST(substring(code from 'REQ-([0-9]+)') "
                            "AS INTEGER)), 1048) + 1 FROM requests"
                        )
                    )
                ).scalar()
                await conn.execute(
                    text("SELECT setval('request_code_seq', :n, false)").bindparams(
                        n=int(start or 1049)
                    )
                )
