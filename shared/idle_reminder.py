"""Idle-reminder job — nudge on-shift, idle admins when work is waiting.

Runs on the pipeline scheduler. Each tick it:
  1. Checks the campaign mode — a *paused* campaign nudges no one.
  2. Counts the work actually waiting (pending requests + open work items).
  3. Finds admins who are available, on-shift, off-break, and holding **zero**
     active tasks (:meth:`ManagementService.idle_admins` — the working-hours and
     break rules live there, so an off-clock admin is never roused).
  4. DMs each such admin one Levi-voiced nudge, rate-limited so the same
     person isn't pinged more than once per :data:`_NUDGE_COOLDOWN` seconds
     (tracked in Redis, best-effort). Levi owns this reminder because the work
     waiting is download detail — his board, his voice.

Suppressed entirely while an admin is actively working (they hold a task) — the
point is to wake the idle, not to hound the busy.
"""

from __future__ import annotations

from typing import Any

from nekofetch.core.logging import get_logger

from kurosoden.shared import levi_voice as V
from kurosoden.shared.management_service import ManagementService
from kurosoden.shared.request_gate import get_mode

log = get_logger(__name__)

# Don't ping the same admin more often than this (seconds).
_NUDGE_COOLDOWN = 3600
_COOLDOWN_KEY = "kurosoden:idle_nudge:{admin_id}"


async def _pending_work(container: Any) -> int:
    """Requests awaiting a source + open work items — the "is there work?" signal.

    Only *download-stage* work items count: a work whose request already moved
    past Levi is in Senku/Gojo's pool and is not download detail, so it must not
    rouse a downloader. Links are reconciled first so a work whose request is
    in flight stops phantom-counting here (the "works left but the board is
    empty" complaint)."""
    total = 0
    try:
        from nekofetch.services.request_service import RequestService
        total += len(await RequestService(container).list_pending())
    except Exception:  # noqa: BLE001
        pass
    try:
        from kurosoden.shared.work_service import STAGE_DOWNLOAD, WorkService
        svc = WorkService(container.pg_sessionmaker)
        try:
            await svc.reconcile_links()
        except Exception:  # noqa: BLE001 — counting must survive bookkeeping hiccups
            pass
        total += await svc.count_open(stage=STAGE_DOWNLOAD)
    except Exception:  # noqa: BLE001
        pass
    return total


async def _recently_nudged(container: Any, admin_id: int) -> bool:
    """True if this admin was pinged within the cooldown. Fails open (False)."""
    redis = getattr(container, "redis", None)
    if redis is None:
        return False
    try:
        return bool(await redis.get(_COOLDOWN_KEY.format(admin_id=admin_id)))
    except Exception:  # noqa: BLE001
        return False


async def _mark_nudged(container: Any, admin_id: int) -> None:
    redis = getattr(container, "redis", None)
    if redis is None:
        return
    try:
        await redis.set(_COOLDOWN_KEY.format(admin_id=admin_id), "1",
                        ex=_NUDGE_COOLDOWN)
    except Exception:  # noqa: BLE001
        pass


def make_idle_nudge_job(container: Any):
    """Build the coroutine the scheduler calls on each tick."""

    async def _tick() -> None:
        try:
            mode = await get_mode(container)
            if mode == "paused":
                return  # a halted campaign rouses no one
            pending = await _pending_work(container)
            if pending <= 0:
                return  # nothing waiting — let everyone be

            idle = await ManagementService(container.pg_sessionmaker).idle_admins()
            if not idle:
                return

            mgr = getattr(container, "pipeline_manager", None)
            client = getattr(mgr, "levi", None) if mgr else None
            if client is None:
                return  # download bot not running — nothing to send through

            sent = 0
            for admin in idle:
                if await _recently_nudged(container, admin.telegram_id):
                    continue
                try:
                    await client.send_message(
                        admin.telegram_id,
                        V.idle_nudge(admin.name or "", pending),
                    )
                    await _mark_nudged(container, admin.telegram_id)
                    sent += 1
                except Exception as exc:  # noqa: BLE001 — one bad DM never stops the rest
                    log.warning("levi.idle_nudge.dm_failed",
                                admin=admin.telegram_id, error=str(exc)[:200])
            if sent:
                log.info("levi.idle_nudge.sent", nudged=sent, pending=pending)
        except Exception as exc:  # noqa: BLE001 — a scheduler job must never crash the loop
            log.warning("levi.idle_nudge.tick_failed", error=str(exc)[:200])

    return _tick
