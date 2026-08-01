"""Re-queue a request for Senku (distribution) so it's redone on next startup.

Use when a Senku publish landed badly (e.g. flood-truncated: posted=0) and you
want the bot to hand the SAME task back to an admin to redo cleanly.

    python scripts/requeue_senku.py REQ-1070
    python scripts/requeue_senku.py REQ-1070 --yes   # no confirm prompt

What it does (idempotent, scoped to ONE request code):
  • Postgres — reopen the ``senku`` admin_assignment for the code: set status
    back to ``assigned`` and clear ``completed_at`` (so /tasks lists it again).
    If there was NO senku row (older task), it prints a note — the bot's
    assignment-recovery job will re-offer it on the next handoff.
  • Redis    — clear the distribution warm-up guard (``nf:dist:<code>:warmed``)
    and the working distribution cache so warm-up + card builds run fresh.

It does NOT delete anything already posted in the channel — reopening the task
lets the admin re-run publish, which re-warms and reposts.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

# ── ``kurosoden`` namespace bootstrap (mirrors clear_database.py) ─────────────
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

async def main(code: str, assume_yes: bool) -> None:
    from nekofetch.core.config import get_env

    env = get_env()
    print(f"  Target Postgres : {env.postgres_db} @ {env.postgres_host}")
    print(f"  Target Redis    : {env.redis_url.split('@')[-1]}")
    print(f"  Request code    : {code}\n")

    if not assume_yes:
        ans = input(f"Reopen the Senku task for {code}? Type 'yes': ")
        if ans.strip().lower() != "yes":
            print("aborted")
            return

    from nekofetch.core.container import Container

    container = Container.create()
    await container.startup()

    # Register Kage's ORM tables (admin_assignments live here).
    import kurosoden.shared.models  # noqa: F401

    try:
        from sqlalchemy import select, update

        from kurosoden.shared.admin_assignment import AdminAssignment
        from nekofetch.infrastructure.database.postgres.session import session_scope

        reopened = 0
        async with session_scope(container.pg_sessionmaker) as session:
            rows = (await session.execute(
                select(AdminAssignment).where(
                    AdminAssignment.request_code == code,
                    AdminAssignment.stage == "senku",
                )
            )).scalars().all()
            if not rows:
                print(f"postgres: no 'senku' assignment for {code} — the "
                      "assignment-recovery job will re-offer it on next handoff.")
            for row in rows:
                await session.execute(
                    update(AdminAssignment)
                    .where(AdminAssignment.id == row.id)
                    .values(status="assigned", completed_at=None)
                )
                reopened += 1
            if reopened:
                print(f"postgres: reopened {reopened} senku assignment(s) for {code}")

        # ── Redis: clear warm-up guard + working distribution cache ──
        if container.redis is not None:
            keys = [f"nf:dist:{code}:warmed"]
            # Best-effort wildcard sweep of the distribution cache for this code.
            try:
                async for k in container.redis.scan_iter(match=f"*{code}*"):
                    ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                    if "dist" in ks or "senku" in ks:
                        keys.append(ks)
            except Exception as exc:  # noqa: BLE001 — scan is best-effort
                print(f"redis: scan skipped ({exc})")
            cleared = 0
            for k in set(keys):
                try:
                    cleared += int(await container.redis.delete(k) or 0)
                except Exception:  # noqa: BLE001
                    pass
            print(f"redis: cleared {cleared} key(s) for {code}")

        print(f"\nDone. Restart Senku; {code} will appear in /tasks to redo.")
    finally:
        await container.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Reopen a Senku distribution task.")
    ap.add_argument("code", help="request code, e.g. REQ-1070")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    args = ap.parse_args()
    asyncio.run(main(args.code.strip(), args.yes))
