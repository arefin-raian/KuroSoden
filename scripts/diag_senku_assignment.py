"""Diagnose + repair stuck Senku (distribution) assignments.

Symptom this addresses: a download finishes, Levi completes, but the Senku task
never arrives — the log shows ``handoff › deferred or unassigned stage=senku``.
By elimination the only stage-specific exclusion is the skip/reject block: the
sole eligible admin carries a stale ``skipped``/``rejected`` senku assignment
(often an expired offer, e.g. an ORV row that also lingers in /tasks), which
blocks EVERY new senku assignment for the local day. The handoff + recovery now
retry with second_pass to bypass a *skipped* block, but this script (a) shows the
exact live state so the cause is visible, and (b) unsticks what's already stuck.

    # DIAGNOSE (read-only): owner availability + recent senku rows + stuck READY reqs
    python scripts/diag_senku_assignment.py

    # REPAIR: force-assign a READY request's senku task to the owner (bypasses the
    # skip/reject block via the preferred-admin path) so it shows in Senku /tasks
    python scripts/diag_senku_assignment.py --assign REQ-1089
    python scripts/diag_senku_assignment.py --assign REQ-1089 --admin 6161189904

    # REPAIR: complete a stale senku row for an ALREADY-published title (e.g. ORV)
    # so it stops showing in /tasks and stops blocking new senku work
    python scripts/diag_senku_assignment.py --complete REQ-ORV-CODE

Read-only by default; --assign / --complete are the only writes. Restart of Senku
is NOT required — the row lands in the DB and /tasks reads it live.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

# Anime titles / decision reasons can be non-ASCII (Japanese, etc.); force a
# UTF-8 stdout with replacement so printing them never dies on a cp1252 console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — older Python / non-reconfigurable stream
    pass

# ── ``kurosoden`` namespace bootstrap (mirrors requeue_senku.py) ─────────────
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


async def _dump(container) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from kurosoden.shared.admin_assignment import AdminAssignment, AdminAvailability
    from nekofetch.infrastructure.database.postgres.models import Request
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.domain.enums import RequestStatus

    now = datetime.now(UTC)
    since = now - timedelta(days=2)
    async with session_scope(container.pg_sessionmaker) as session:
        # 1. Every admin's availability + whether senku is enabled for them.
        avails = (await session.execute(select(AdminAvailability))).scalars().all()
        print("== Admins (availability) ==")
        for a in avails:
            bots = a.assigned_bots or []
            print(f"  id={a.admin_telegram_id} name={a.admin_name!r} "
                  f"available={a.is_available} senku_enabled={'senku' in bots} "
                  f"assigned_bots={bots}")
            print(f"      working_hours={a.working_hours} "
                  f"breaks={a.scheduled_breaks or []}")

        # 2. Recent senku assignment rows — the skip/reject rows here are what
        #    block new senku work (status in skipped/rejected within 2 days).
        rows = (await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.stage == "senku",
                AdminAssignment.created_at >= since,
            ).order_by(AdminAssignment.created_at.desc())
        )).scalars().all()
        print(f"\n== Recent senku assignments (last 2 days: {len(rows)}) ==")
        blockers = []
        for r in rows:
            flag = ""
            if r.status in ("skipped", "rejected"):
                flag = "  <-- BLOCKS new senku assigns (until local-day rollover)"
                blockers.append(r)
            print(f"  {r.request_code:14} admin={r.admin_telegram_id} "
                  f"status={r.status} mode={r.assignment_mode} "
                  f"reason={r.decision_reason} at={r.created_at}{flag}")

        # 3. READY requests with NO open/completed senku row = stuck (should have
        #    a senku task but don't).
        ready = (await session.execute(
            select(Request).where(Request.status == RequestStatus.READY)
        )).scalars().all()
        print(f"\n== READY requests missing a senku assignment ==")
        stuck = []
        for req in ready:
            has = (await session.execute(
                select(AdminAssignment).where(
                    AdminAssignment.request_code == req.code,
                    AdminAssignment.stage == "senku",
                    AdminAssignment.status.in_(
                        ("assigned", "in_progress", "offered", "completed")),
                )
            )).scalars().first()
            if has is None:
                stuck.append(req)
                print(f"  {req.code:14} {req.anime_title!r} status={req.status.value}"
                      f"  <-- STUCK (no senku task)")
        if not stuck:
            print("  (none — every READY request has a senku task)")

    print("\n── Diagnosis ──")
    if blockers:
        print(f"  {len(blockers)} skipped/rejected senku row(s) are BLOCKING new "
              "assignments. The code fix (second_pass retry) now bypasses a "
              "'skipped' block automatically; for an already-stuck task run "
              "--assign REQ-XXXX to force it to the owner now. If a row is "
              "'rejected' (not skipped), --assign is the way to override it.")
    if stuck:
        codes = " ".join(r.code for r in stuck)
        print(f"  Force-assign the stuck task(s):  "
              f"python scripts/diag_senku_assignment.py --assign {codes}")


async def _resolve_owner(container, explicit: int | None) -> int | None:
    if explicit:
        return explicit
    try:
        from nekofetch.services.auth_service import AuthService

        ids = sorted(AuthService(container).owner_ids())
        return ids[0] if ids else None
    except Exception as exc:  # noqa: BLE001
        print(f"  could not resolve owner id ({exc}); pass --admin <id>")
        return None


async def _assign(container, code: str, admin: int | None) -> None:
    from kurosoden.shared.admin_assignment import AdminAssignmentEngine

    owner = await _resolve_owner(container, admin)
    if owner is None:
        return
    engine = AdminAssignmentEngine(container.pg_sessionmaker)
    # preferred_admin uses the forced path, which does NOT consult the skip/reject
    # block — it force-assigns as long as the admin is available + senku-enabled +
    # not on break / within hours. That's exactly the override we want here.
    result = await engine.assign(code, "senku", preferred_admin=owner)
    if result is None:
        # Fall back to second_pass (bypasses a skipped-block) if the forced path
        # was refused (e.g. outside working hours).
        result = await engine.assign(code, "senku", second_pass=True)
    if result is None:
        print(f"  {code}: could NOT assign — the owner may be unavailable, "
              "senku-disabled, on break, or outside working hours. Check the dump.")
        return
    print(f"  {code}: assigned to admin={result.admin_telegram_id} "
          f"status={result.status}. Open Senku /tasks — it's there now.")


async def _complete(container, code: str) -> None:
    from kurosoden.shared.admin_assignment import AdminAssignmentEngine

    engine = AdminAssignmentEngine(container.pg_sessionmaker)
    await engine.complete_task(code, "senku")
    print(f"  {code}: senku assignment marked completed (clears it from /tasks + "
          "unblocks new senku work).")


async def main(assign_codes: list[str], complete_codes: list[str],
               admin: int | None) -> None:
    from nekofetch.core.config import get_env

    env = get_env()
    print(f"  Target Postgres : {env.postgres_db} @ {env.postgres_host}\n")

    from nekofetch.core.container import Container

    container = Container.create()
    await container.startup()
    import kurosoden.shared.models  # noqa: F401 — register ORM tables

    try:
        if not assign_codes and not complete_codes:
            await _dump(container)
            return
        for code in complete_codes:
            await _complete(container, code.strip().upper())
        for code in assign_codes:
            await _assign(container, code.strip().upper(), admin)
    finally:
        await container.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Diagnose + repair stuck Senku assignments.")
    ap.add_argument("--assign", nargs="*", default=[], metavar="REQ",
                    help="force-assign these READY requests' senku task to the owner")
    ap.add_argument("--complete", nargs="*", default=[], metavar="REQ",
                    help="complete a stale senku row for an already-published title")
    ap.add_argument("--admin", type=int, default=None,
                    help="target admin telegram id (defaults to the owner id)")
    args = ap.parse_args()
    asyncio.run(main(args.assign, args.complete, args.admin))
