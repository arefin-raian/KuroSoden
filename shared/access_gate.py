"""Staff-only gate for the three pipeline bots (Levi / Senku / Gojo).

Only Lelouch (the request bot) is for end users. Levi, Senku and Gojo are staff
tools — a normal user has no business inside them. This module provides:

  * :func:`is_staff` — the role check (STAFF or ADMIN via the resolved ``nf_user``).
  * :func:`gated_screen` — a beautifully structured "authorized staff only" card
    on the bot's own artwork, powered-by @WeebsXServer, with two buttons: our
    network channel and the Lelouch request bot (so a lost user is pointed at the
    one place they *can* act).
  * :func:`lelouch_link` — resolve the live Lelouch bot username once (via the
    running Lelouch client on the pipeline manager) and cache it on the container,
    so the "Request Anime" button deep-links correctly without hardcoding.

Wire it in a bot's ``/start`` and menu dispatcher: if ``not is_staff`` show the
gate and stop. The gate never leaks what the bot does — it just redirects.
"""

from __future__ import annotations

from typing import Any

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import Role
from nekofetch.ui.artwork import pick_artwork
from nekofetch.ui.screens import Screen

log = get_logger(__name__)

NETWORK_HANDLE = "WeebsXServer"
NETWORK_URL = f"https://t.me/{NETWORK_HANDLE}"

_BOT_TITLE = {
    "levi": "Levi Ackerman",
    "senku": "Senku Ishigami",
    "gojo": "Gojo Satoru",
}


def is_staff(obj: Any) -> bool:
    """True if the update's resolved user is staff or admin."""
    user = getattr(obj, "nf_user", None)
    if user is None:
        return False
    try:
        return Role(user.role) in (Role.STAFF, Role.ADMIN)
    except Exception:  # noqa: BLE001
        return False


def is_owner(container: Container, obj: Any) -> bool:
    """True if the update's resolved user is the configured owner."""
    user = getattr(obj, "nf_user", None)
    if user is None:
        return False
    try:
        from nekofetch.services.auth_service import AuthService
        return AuthService(container).is_owner(user)
    except Exception:  # noqa: BLE001
        return False


async def levi_link(container: Container) -> str | None:
    """Deep link to the Levi task bot, or ``None`` when it is unavailable."""
    cached = getattr(container, "_levi_username", None)
    if cached:
        return f"https://t.me/{cached}"
    mgr = getattr(container, "pipeline_manager", None)
    client = getattr(mgr, "levi", None) if mgr else None
    if client is None:
        return None
    try:
        me = await client.get_me()
        username = getattr(me, "username", None)
        if username:
            container._levi_username = username  # type: ignore[attr-defined]
            return f"https://t.me/{username}"
    except Exception as exc:  # noqa: BLE001 — best-effort; button just omitted
        log.warning("access_gate.levi_link_failed", error=str(exc))
    return None


async def lelouch_link(container: Container) -> str | None:
    """Deep link to the Lelouch request bot (``https://t.me/<username>``).

    Resolves the running Lelouch client's username once and caches it on the
    container. Returns ``None`` only if Lelouch isn't up or has no username yet —
    the gate then simply omits that button rather than showing a dead one.
    """
    cached = getattr(container, "_lelouch_username", None)
    if cached:
        return f"https://t.me/{cached}"
    mgr = getattr(container, "pipeline_manager", None)
    client = getattr(mgr, "lelouch", None) if mgr else None
    if client is None:
        return None
    try:
        me = await client.get_me()
        username = getattr(me, "username", None)
        if username:
            container._lelouch_username = username  # type: ignore[attr-defined]
            return f"https://t.me/{username}"
    except Exception as exc:  # noqa: BLE001 — best-effort; button just omitted
        log.warning("access_gate.lelouch_link_failed", error=str(exc))
    return None


async def gated_screen(container: Container, bot: str) -> Screen:
    """The "authorized staff only" card shown to a non-staff user on a pipeline bot."""
    title = _BOT_TITLE.get(bot, bot.title())
    caption = (
        f"🔒 <b>{title} — Staff Access Only</b>\n\n"
        "This bot is part of the crew's production line — it's operated by "
        "authorized staff, not open to the public.\n\n"
        "<blockquote>Looking for anime? You're one tap away — open our request "
        "bot below and I'll take it from there.</blockquote>\n\n"
        f"<i>Powered by</i> <b>@{NETWORK_HANDLE}</b>"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🌐 Our Server", url=NETWORK_URL)],
    ]
    link = await lelouch_link(container)
    if link:
        rows.append([InlineKeyboardButton("🎬 Request Anime", url=link)])
    return Screen(
        caption=caption,
        image=pick_artwork(bot),
        keyboard=InlineKeyboardMarkup(rows),
    )
