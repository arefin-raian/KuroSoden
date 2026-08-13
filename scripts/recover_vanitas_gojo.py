"""Create a Gojo task for an already-indexed Vanitas request.

This utility is deliberately read-only unless ``--apply`` is supplied. It does
not start any bots, touch Redis, or post to Telegram. It only creates the durable
``admin_assignments`` row that makes the request appear in Gojo's ``/tasks``
view; the operator can then use the normal review/publish flow, including the
stale-main-message recovery in ``MainChannelService``.

Examples (run from the KuroSoden repository root)::

    # Inspect the exact request and planned assignment; writes nothing.
    python scripts/recover_vanitas_gojo.py

    # Apply after reviewing the dry-run output. The prompt is an extra guard.
    python scripts/recover_vanitas_gojo.py --apply

    # Non-interactive deployment/maintenance invocation after reviewing it.
    python scripts/recover_vanitas_gojo.py --apply --yes --admin-id 123456789

Use ``--code REQ-XXXX`` when the title has more than one matching request.
The default title is exact (case-insensitive); a partial fallback is shown only
when no exact title exists, and an ambiguous match is never selected silently.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

# ── Standalone ``kurosoden`` namespace bootstrap (same as other scripts) ──────
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
os.chdir(str(_HERE))

_kage = types.ModuleType("kurosoden")
_kage.__path__ = [str(_HERE)]
sys.modules["kurosoden"] = _kage
for _sub in ("shared", "bots", "nekofetch", "tests"):
    if (_HERE / _sub / "__init__.py").is_file():
        _shim = types.ModuleType(f"kurosoden.{_sub}")
        _shim.__path__ = [str(_HERE / _sub)]
        sys.modules[f"kurosoden.{_sub}"] = _shim
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_TITLE = "The Case Study of Vanitas"
OPEN_ASSIGNMENT_STATUSES = ("assigned", "in_progress", "offered")


@dataclass(frozen=True)
class RecoveryResult:
    request_code: str
    anime_title: str
    admin_telegram_id: int
    created: bool
    assignment_id: int | None = None
    reason: str = ""


async def ensure_gojo_assignment(
    session,
    request_code: str,
    admin_telegram_id: int,
    *,
    apply: bool,
) -> RecoveryResult:
    """Plan or create one active Gojo assignment without duplicating it.

    The open-row lookup is repeated under the same transaction by the caller;
    PostgreSQL's partial unique index on ``(request_code, stage)`` remains the
    final concurrency guard. A completed/rejected historical row is intentionally
    left intact and a new active row is added.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from kurosoden.shared.admin_assignment import AdminAssignment
    from nekofetch.infrastructure.database.postgres.models import Request

    request = (
        await session.execute(
            select(Request).where(Request.code == request_code).with_for_update()
        )
    ).scalar_one_or_none()
    if request is None:
        raise LookupError(f"request {request_code!r} was not found")

    existing = (
        await session.execute(
            select(AdminAssignment)
            .where(
                AdminAssignment.request_code == request_code,
                AdminAssignment.stage == "gojo",
                AdminAssignment.status.in_(OPEN_ASSIGNMENT_STATUSES),
            )
            .order_by(AdminAssignment.created_at.desc())
        )
    ).scalars().first()
    if existing is not None:
        return RecoveryResult(
            request_code=request.code,
            anime_title=request.anime_title,
            admin_telegram_id=int(existing.admin_telegram_id),
            created=False,
            assignment_id=existing.id,
            reason="an open Gojo assignment already exists",
        )

    if not apply:
        return RecoveryResult(
            request_code=request.code,
            anime_title=request.anime_title,
            admin_telegram_id=admin_telegram_id,
            created=False,
            reason="dry run; no database row written",
        )

    assignment = AdminAssignment(
        admin_telegram_id=admin_telegram_id,
        request_code=request.code,
        stage="gojo",
        status="assigned",
        assignment_mode="fallback",
        decision_reason="manual_vanitas_recovery",
    )
    try:
        # Keep a concurrent invocation from poisoning the outer transaction when
        # the partial unique index wins the race. PostgreSQL aborts a transaction
        # after IntegrityError unless the insert is inside a SAVEPOINT.
        async with session.begin_nested():
            session.add(assignment)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                select(AdminAssignment)
                .where(
                    AdminAssignment.request_code == request_code,
                    AdminAssignment.stage == "gojo",
                    AdminAssignment.status.in_(OPEN_ASSIGNMENT_STATUSES),
                )
                .order_by(AdminAssignment.created_at.desc())
            )
        ).scalars().first()
        if existing is None:
            raise
        return RecoveryResult(
            request_code=request.code,
            anime_title=request.anime_title,
            admin_telegram_id=int(existing.admin_telegram_id),
            created=False,
            assignment_id=existing.id,
            reason="a concurrent invocation created the open Gojo assignment",
        )
    return RecoveryResult(
        request_code=request.code,
        anime_title=request.anime_title,
        admin_telegram_id=admin_telegram_id,
        created=True,
        assignment_id=assignment.id,
        reason="created an active Gojo assignment",
    )


async def _find_requests(session, title: str):
    """Find exact title matches, then a clearly reported partial fallback."""
    from sqlalchemy import func, select

    from nekofetch.infrastructure.database.postgres.models import Request

    wanted = title.strip().casefold()
    exact = list(
        (
            await session.execute(
                select(Request)
                .where(func.lower(Request.anime_title) == wanted)
                .order_by(Request.id.desc())
            )
        ).scalars().all()
    )
    if exact:
        return exact, "exact"

    partial = list(
        (
            await session.execute(
                select(Request)
                .where(Request.anime_title.ilike(f"%{title.strip()}%"))
                .order_by(Request.id.desc())
            )
        ).scalars().all()
    )
    return partial, "partial"


def _default_admin_id(env, config) -> int:
    """Match application owner precedence without exposing a secret value."""
    config_owner = int(getattr(config.security, "owner_id", 0) or 0)
    env_owner = int(getattr(env, "owner_id", 0) or 0)
    configured = config_owner or env_owner
    if configured > 0:
        return configured
    ids = [int(value) for value in (getattr(env, "admin_ids", []) or [])]
    for value in ids:
        if value > 0:
            return value
    raise RuntimeError("no owner/admin id configured; pass --admin-id explicitly")


def _print_matches(rows, match_kind: str) -> None:
    label = "exact" if match_kind == "exact" else "partial"
    print(f"{label} title matches: {len(rows)}")
    for row in rows:
        print(
            f"  {row.code} · {row.anime_title!r} · status={row.status} "
            f"· anime_doc_id={row.anime_doc_id or '—'}"
        )


async def main(args) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from nekofetch.core.config import AppConfig, EnvSettings, get_env
    from nekofetch.infrastructure.database.postgres.models import Request

    title = (args.title or DEFAULT_TITLE).strip()
    code = args.code.strip() if args.code else None
    env = get_env() if not args.env_file else EnvSettings(_env_file=args.env_file)
    config = AppConfig.load()
    admin_id = int(args.admin_id or _default_admin_id(env, config))

    engine = create_async_engine(env.postgres_dsn, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            if code:
                request = (
                    await session.execute(select(Request).where(Request.code == code))
                ).scalar_one_or_none()
                rows, match_kind = ([request] if request else []), "code"
            else:
                rows, match_kind = await _find_requests(session, title)

            if not rows:
                target = code or title
                raise LookupError(f"no request matched {target!r}")
            if len(rows) != 1:
                _print_matches(rows, match_kind)
                raise RuntimeError("ambiguous request; rerun with --code REQ-XXXX")

            request = rows[0]
            print(f"Postgres : {env.postgres_db} @ {env.postgres_host}")
            print(f"Request  : {request.code} · {request.anime_title}")
            print(f"Status   : {request.status}")
            print(f"Anime id : {request.anime_doc_id or '—'}")
            print(f"Gojo id  : {admin_id}")
            print("Action   : create one active assigned Gojo task; publish remains manual")

            if not args.apply:
                result = await ensure_gojo_assignment(
                    session, request.code, admin_id, apply=False,
                )
                print(f"DRY RUN  : {result.reason}")
                return

            if not args.yes:
                answer = input(
                    "Type 'yes' to create this database task (no Telegram post): "
                )
                if answer.strip().casefold() != "yes":
                    print("aborted")
                    return

            result = await ensure_gojo_assignment(
                session, request.code, admin_id, apply=True,
            )
            await session.commit()
            if result.created:
                print(
                    f"APPLIED  : created AdminAssignment id={result.assignment_id}; "
                    "open Gojo /tasks and publish manually"
                )
            else:
                print(f"UNCHANGED: {result.reason}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--title", default=DEFAULT_TITLE,
        help="exact title to find (default: The Case Study of Vanitas)",
    )
    parser.add_argument(
        "--env-file", default=None,
        help="load database/owner settings from this file instead of the local .env",
    )
    parser.add_argument("--code", help="unambiguous request code, e.g. REQ-1234")
    parser.add_argument("--admin-id", type=int, help="Telegram id that should own Gojo task")
    parser.add_argument(
        "--apply", action="store_true",
        help="write the assignment; without this flag the script is read-only",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt (only meaningful with --apply)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except (LookupError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
