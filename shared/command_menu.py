"""Per-user command menus — show people only the commands they can actually use.

Telegram's ``☰`` command menu is global by default, so a plain user opening a
staff-only bot would still see ``/settings``, ``/publish`` … that just refuse.
Worse, it leaks what the bot does. This module scopes the menu per person:

  * The GLOBAL default (set once at startup by each bot's ``publish_commands``)
    is the *lowest-privilege* view — empty for the three staff-only pipeline bots,
    and the plain-user commands for Lelouch.
  * On ``/start`` we call :func:`apply_for_user`, which sets a
    ``BotCommandScopeChat`` menu tailored to that user's role — so staff see the
    operational commands and only the owner sees owner-only ones.

Roles: ``user`` < ``staff``/``admin`` < ``owner``. The owner is resolved via
:class:`AuthService.is_owner`; staff/admin via the resolved ``nf_user`` role.
"""

from __future__ import annotations

from pyrogram import Client
from pyrogram.types import BotCommand, BotCommandScopeChat

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import Role

log = get_logger(__name__)


def _c(cmd: str, desc: str) -> BotCommand:
    return BotCommand(cmd, desc)


# ── Lelouch (the only user-facing bot) ────────────────────────────────────────
_LELOUCH_USER = [
    _c("start", "Request an anime"),
    _c("myrequests", "View your request status"),
    _c("help", "How requests work"),
]
_LELOUCH_STAFF = _LELOUCH_USER + [_c("batch", "Batch work (staff)")]
_LELOUCH_OWNER = _LELOUCH_STAFF + [
    _c("admin", "Command console (owner)"),
    _c("settings", "Configure the request bot (owner)"),
    _c("cleardatabase", "Clear operational database data (owner)"),
]

# ── Levi / Senku / Gojo — staff tools, no user commands at all ────────────────
# Operational commands are for staff; owner-only ones (settings) only for owner.
_LEVI_STAFF = [
    _c("start", "Your download tasks"),
    _c("tasks", "Open your download tasks"),
    _c("help", "How the downloader works"),
]
_LEVI_OWNER = _LEVI_STAFF + [_c("settings", "Configure the downloader (owner)")]

_SENKU_STAFF = [
    _c("start", "Your distribution tasks"),
    _c("tasks", "List active distribution tasks"),
    _c("create", "Create a distribution channel"),
    _c("generate", "Generate content: /generate REQ-XXXX"),
    _c("help", "How distribution works"),
]
_SENKU_OWNER = _SENKU_STAFF + [_c("settings", "Configure distribution (owner)")]

_GOJO_STAFF = [
    _c("start", "Your publishing tasks"),
    _c("tasks", "List active publishing tasks"),
    _c("publish", "Review and publish: /publish REQ-XXXX"),
    _c("recover", "Recover a banned channel: /recover REQ-XXXX"),
    _c("schedule", "Schedule a post for later"),
    _c("help", "How publishing works"),
]
_GOJO_OWNER = _GOJO_STAFF + [_c("settings", "Configure the publisher (owner)")]

# bot → (user, staff, owner) command tiers. ``user`` empty for the staff-only bots.
_TIERS: dict[str, tuple[list, list, list]] = {
    "lelouch": (_LELOUCH_USER, _LELOUCH_STAFF, _LELOUCH_OWNER),
    "levi": ([], _LEVI_STAFF, _LEVI_OWNER),
    "senku": ([], _SENKU_STAFF, _SENKU_OWNER),
    "gojo": ([], _GOJO_STAFF, _GOJO_OWNER),
}


def default_commands(bot: str) -> list[BotCommand]:
    """The global-default menu for a bot — shown before per-user scoping kicks in.

    For the staff-only pipeline bots (levi/senku/gojo) this is the STAFF tier,
    NOT empty. The old empty default meant the ``☰`` menu stayed blank until
    ``/start`` fired, the user's role resolved, AND Telegram refreshed the
    per-chat scope — any hiccup in that chain left staff/owner with no menu at
    all (the reported "Senku has no commands"). Strangers are already blocked by
    the auth middleware, so surfacing the command list here leaks nothing the bot
    description doesn't; ``apply_for_user`` still refines the owner up to the
    settings-inclusive tier on ``/start``. Lelouch (user-facing) keeps its user
    tier as the default."""
    tiers = _TIERS.get(bot)
    if not tiers:
        return []
    user, staff, _owner = tiers
    # Lelouch is open to users → its user tier is the right public default.
    # The staff-only bots have an empty user tier, so fall back to the staff tier
    # so their menu is never blank for the people who actually use them.
    return list(user) if user else list(staff)


def _role_tier(bot: str, *, is_staff: bool, is_owner: bool) -> list[BotCommand]:
    tiers = _TIERS.get(bot)
    if tiers is None:
        return []
    user, staff, owner = tiers
    if is_owner:
        return owner
    if is_staff:
        return staff
    return user


async def apply_for_user(
    client: Client, container: Container, bot: str, user_id: int, nf_user,
) -> None:
    """Set the per-chat command menu for ``user_id`` based on their role.

    Best-effort: a failed ``set_bot_commands`` never blocks the handler. Called
    on ``/start`` so the menu is correct the moment someone opens the bot.
    """
    is_staff = False
    is_owner = False
    if nf_user is not None:
        try:
            is_staff = Role(nf_user.role) in (Role.STAFF, Role.ADMIN)
        except Exception:  # noqa: BLE001
            is_staff = False
        try:
            from nekofetch.services.auth_service import AuthService
            is_owner = AuthService(container).is_owner(nf_user)
        except Exception:  # noqa: BLE001
            is_owner = False
    cmds = _role_tier(bot, is_staff=is_staff, is_owner=is_owner)
    try:
        await client.set_bot_commands(cmds, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("command_menu.apply_failed", bot=bot, user=user_id, error=str(exc))
