"""Runtime "are we accepting requests?" gate.

A single global switch admins flip from Lelouch's admin panel. Backed by Redis
so every bot process sees the same value instantly; when Redis is absent (local
dev), it falls back to "open" — the safe default that keeps the request flow
working rather than silently rejecting everyone.
"""

from __future__ import annotations

from typing import Any

_KEY = "kurosoden:requests_open"


async def requests_open(container: Any) -> bool:
    """True when regular users may submit new requests.

    Defaults to open when Redis is unavailable or the key was never set.
    """
    redis = getattr(container, "redis", None)
    if redis is None:
        return True
    try:
        val = await redis.get(_KEY)
    except Exception:
        return True
    if val is None:
        return True  # never configured → open
    # redis may hand back bytes or str depending on decode settings.
    if isinstance(val, bytes):
        val = val.decode("utf-8", "ignore")
    return str(val) != "0"


async def set_requests_open(container: Any, is_open: bool) -> bool:
    """Persist the gate. Returns the value that was stored (unchanged when Redis
    is missing, so callers can surface "couldn't persist" if it matters)."""
    redis = getattr(container, "redis", None)
    if redis is None:
        return is_open
    try:
        await redis.set(_KEY, "1" if is_open else "0")
    except Exception:
        pass
    return is_open


# ── Campaign mode ────────────────────────────────────────────────────────────
# A coarse tempo switch surfaced on the admin panel and honoured by the idle
# reminder job. "paused" additionally implies the request gate is closed.

_MODE_KEY = "kurosoden:mode"
VALID_MODES = ("normal", "catch-up", "paused")


async def get_mode(container: Any) -> str:
    """Current campaign mode. Defaults to ``normal`` when unset/Redis absent."""
    redis = getattr(container, "redis", None)
    if redis is None:
        return "normal"
    try:
        val = await redis.get(_MODE_KEY)
    except Exception:
        return "normal"
    if val is None:
        return "normal"
    if isinstance(val, bytes):
        val = val.decode("utf-8", "ignore")
    return str(val) if str(val) in VALID_MODES else "normal"


async def set_mode(container: Any, mode: str) -> str:
    """Persist the campaign mode; unknown values coerce to ``normal``."""
    mode = mode if mode in VALID_MODES else "normal"
    redis = getattr(container, "redis", None)
    if redis is not None:
        try:
            await redis.set(_MODE_KEY, mode)
        except Exception:
            pass
    return mode
