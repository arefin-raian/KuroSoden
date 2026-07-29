"""Tight-walled Redis read/set/delete helpers — used by every apscheduler service.

WHY THIS MODULE EXISTS
======================

A bare ``await redis.get(key)`` against a hosted Redis provider can stall for
several seconds (Upstash's free tier: 3-5s) on a connection blip. The
apscheduler event loop ticks every few seconds for ``LogChannelService
.refresh_active`` (5s), ``ThumbnailChannelService.refresh_queue`` (60s),
``DistributionService.sweep_expired`` (60s), etc.

When one of those server-bound ticks stalls on a Redis read, the event loop
is blocked long enough that the NEXT 5-second tick cannot acquire the
per-service lock and gets logged as::

    Execution of job "LogChannelService.refresh_active (...)" skipped:
        maximum number of running instances reached (1)

That single blip then wedges every scheduled job in the same tick for as long
as the underlying Redis call takes — operator-visible "scheduler stuck"
behavior even though the Redis provider itself quickly recovers.

This module:

* Wraps every Redis op with a tight ``asyncio.wait_for`` cap (default 2.0s,
  leaving headroom below Upstash's 3s baseline).
* Treats asyncio.TimeoutError vs redis.exceptions.TimeoutError vs
  ConnectionError distinctly in the logs (``redis.timeout`` for the wait_for
  version, ``redis.blip`` for everything else) so operators can attribute
  the blip after the fact.
* Returns ``None`` (for reads) or ``False`` (for writes/deletes) on any
  transport error so caller code "absent => nothing to do" branches
  transparently handle a Redis blip.

USAGE
=====

Replace bare calls like::

    raw = await self._c.redis.get(key)
    await self._c.redis.set(key, value)
    await self._c.redis.delete(key)

with::

    raw = await safe_redis_get(self._c.redis, key, label="<service>.<op>")
    await safe_redis_set(self._c.redis, key, value, label="<service>.<op>")
    await safe_redis_delete(self._c.redis, key, label="<service>.<op>")

The ``label`` argument ends up in the warning log so operators can answer
"which service hung on the queue key during the 2026-07-10 incident?".

Both ``timeout`` defaults to ``None`` and is resolved against
``_REDIS_READ_TIMEOUT_S`` at call-time (NOT at function-definition time) so
tests can ``patch("..._REDIS_READ_TIMEOUT_S", 0.3)`` and drive a real
``asyncio.wait_for`` chain in microseconds instead of waiting the full 2s.
"""

from __future__ import annotations

import asyncio

from nekofetch.core.logging import get_logger

log = get_logger(__name__)


# Default Redis-call timeout. Upstash's free tier can stall up to 3-5s on a
# blip, and a remote Redis reached from a home connection (e.g. Windows →
# Upstash) adds real round-trip latency on top. 2s was too tight for that
# path and produced a stream of spurious ``redis.timeout`` warnings; 5s leaves
# headroom for a slow blip while still capping well under the 15s socket
# timeout so a single slow read can't starve every other apscheduler job in
# the same tick. Tests ``patch`` this constant to drive ``asyncio.wait_for``
# under a sub-second budget.
_REDIS_READ_TIMEOUT_S = 5.0


async def safe_redis_get(redis, key: str, *, timeout: float | None = None,
                         label: str = "") -> str | None:
    """``GET key`` with a tight timeout — NEVER wedge the caller.

    ``timeout`` defaults to ``None`` and resolves at call-time against
    ``_REDIS_READ_TIMEOUT_S`` so tests can ``patch(...)`` the constant.
    Returns ``None`` on any error so caller branches ("absent => nothing to
    do") still work. Caller MUST treat a ``None`` return as 'best-effort
    signal: try the operation again on a later tick'.
    """
    if redis is None:
        return None
    if timeout is None:
        timeout = _REDIS_READ_TIMEOUT_S
    try:
        return await asyncio.wait_for(redis.get(key), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(
            "redis.timeout", op=label, key=key, timeout_s=timeout,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - transport blips are expected
        log.warning(
            "redis.blip", op=label, key=key, error=str(exc)[:160],
        )
        return None


async def safe_redis_mget(redis, keys: list[str], *, timeout: float | None = None,
                          label: str = "") -> list[str | None]:
    """``MGET k1 k2 ...`` with a tight timeout — ONE round-trip vs N.

    Used when the caller has a small, statically-known key list and grouping
    the reads into a single Redis call matters more than any
    key-by-key isolation. Returns a list aligned with the input keys —
    positions match ``keys``, missing values come back as ``None``.

    Symmetric to :func:`safe_redis_get`:

    * Treats ``asyncio.TimeoutError`` distinctly from generic transport
      blips in the warning log (``redis.timeout`` vs ``redis.blip``) so
      operators can attribute the blip after the fact.
    * Returns an all-``None`` list on any error so the caller's "absent =>
      nothing to do" branches transparently handle a Redis blip.

    Stats scraping (A-Z + "#" = 27 keys) is the canonical caller; prior
    to this helper the caller fanned out 27 parallel ``safe_redis_get``
    awaits (``asyncio.gather``) which on a wobbly Upstash landed in
    27 stacked 2s timeouts. MGET lands in a single 2s timeout — the
    same wedge-resistance, but one timed call instead of 27.
    """
    if redis is None or not keys:
        return [None] * len(keys)
    if timeout is None:
        timeout = _REDIS_READ_TIMEOUT_S
    try:
        # redis-py's mget returns a list aligned with input keys; missing
        # keys come back as ``None``. Empty input is already short-circuited
        # above so ``redis.mget()`` is never called with no args.
        raw = await asyncio.wait_for(redis.mget(*keys), timeout=timeout)
        if raw is None:
            return [None] * len(keys)
        # Defensive: if the driver returns a shorter list than expected
        # (some shim implementations do this on partial failure), pad with
        # ``None`` so the caller's index alignment stays correct.
        result = list(raw)
        if len(result) < len(keys):
            result.extend([None] * (len(keys) - len(result)))
        return result
    except asyncio.TimeoutError:
        # List first 5 keys in the log so operators can identify which
        # group of keys tripped without blowing out the log line.
        log.warning(
            "redis.timeout", op=label, count=len(keys),
            keys=",".join(keys[:5]), timeout_s=timeout,
        )
        return [None] * len(keys)
    except Exception as exc:  # noqa: BLE001 - transport blips are expected
        log.warning(
            "redis.blip", op=label, count=len(keys),
            keys=",".join(keys[:5]), error=str(exc)[:160],
        )
        return [None] * len(keys)


async def safe_redis_set(redis, key: str, value, *, timeout: float | None = None,
                         label: str = "", ex: int | None = None) -> bool:
    """``SET key value`` with a tight timeout — symmetric to ``safe_redis_get``.

    Returns ``True`` on confirmed set, ``False`` on blip/timeout (caller
    treats it as 'try again next tick'). ``value`` is whatever the redis
    client accepts (str / bytes / JSON-encoded str).

    Pass ``ex=seconds`` to attach a server-side TTL atomically with the
    set — required for short-lived control flags (job-cancel, episode-skip)
    so an aborted worker cycle can't leave the flag stuck forever and trap
    the next ``recover_on_startup`` invocation. When ``ex`` is ``None`` the
    underlying ``redis.set`` is called without a TTL (default redis-py
    behaviour — the key persists until explicitly deleted or overwritten).
    """
    if redis is None:
        return False
    if timeout is None:
        timeout = _REDIS_READ_TIMEOUT_S
    try:
        if ex is not None:
            await asyncio.wait_for(
                redis.set(key, value, ex=ex), timeout=timeout,
            )
        else:
            await asyncio.wait_for(
                redis.set(key, value), timeout=timeout,
            )
        return True
    except asyncio.TimeoutError:
        log.warning(
            "redis.timeout", op=label, key=key, timeout_s=timeout,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - transport blips are expected
        log.warning(
            "redis.blip", op=label, key=key, error=str(exc)[:160],
        )
        return False


async def safe_redis_delete(redis, key: str, *, timeout: float | None = None,
                            label: str = "") -> bool:
    """``DEL key`` with a tight timeout — a hung DELETE wedges the scheduler
    the same way a hung GET did, so use this for ALL delete ops on the
    apscheduler path.

    Returns ``True`` on confirmed delete, ``False`` on blip/timeout.
    """
    if redis is None:
        return False
    if timeout is None:
        timeout = _REDIS_READ_TIMEOUT_S
    try:
        await asyncio.wait_for(redis.delete(key), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        log.warning(
            "redis.timeout", op=label, key=key, timeout_s=timeout,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - transport blips are expected
        log.warning(
            "redis.blip", op=label, key=key, error=str(exc)[:160],
        )
        return False
