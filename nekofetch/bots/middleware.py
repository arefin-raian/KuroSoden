"""Shared bot middleware: user resolution, rate limiting, anti-spam.

Pyrogram doesn't have ASGI-style middleware, so we register a high-priority handler
in an early group that resolves the user and enforces limits before feature handlers
(in later groups) run. The resolved user is stashed on the update for handlers to use.
"""

from __future__ import annotations

from pyrogram import Client
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.core.constants import REDIS_RATELIMIT
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.services.auth_service import AuthService
from nekofetch.ui.typography import bq

log = get_logger(__name__)


def install_auth_middleware(
    client: Client, container: Container, *, staff_only_bot: str | None = None
) -> None:
    """Resolve the user on every update; optionally gate the whole bot to staff.

    ``staff_only_bot`` (e.g. ``"levi"``) turns the client into a staff-only tool:
    a non-staff user's message or tap is intercepted here — they get the shared
    "authorized staff only" gate (with a Request-Anime button pointing at
    Lelouch) and propagation stops, so no feature handler ever runs for them.
    Only Lelouch (the request bot) is left open to users, so it passes no value.
    """
    auth = AuthService(container)
    rate_limit = container.config.security.rate_limit_per_minute

    async def _resolve(from_user) -> object | None:
        if from_user is None:
            return None
        return await auth.resolve_user(
            from_user.id, username=from_user.username, first_name=from_user.first_name
        )

    def _is_staff(user) -> bool:
        if user is None:
            return False
        try:
            from nekofetch.domain.enums import Role
            return Role(user.role) in (Role.STAFF, Role.ADMIN)
        except Exception:  # noqa: BLE001
            return False

    async def _rate_limited(user_id: int) -> bool:
        """Returns True when the user is over the rate-limit budget.

        One round-trip: an INCR + a same-pipeline EXPIRE (NX-style — only the
        first INCR of the window needs the TTL, but re-setting it every time is
        harmless and keeps it to a single RTT). On a WAN-distant Redis, folding
        the old INCR-then-conditional-EXPIRE (2 sequential RTTs) into one
        pipeline halves this check's latency on every tap.

        Fails OPEN: if Redis is unreachable (DNS error, connection drop,
        Render-internal hostname only resolvable from a different region),
        we don't rate-limit — better to accept extra messages than to
        crash every handler with a ConnectionError traceback mid-flight.
        """
        if container.redis is None:
            return False
        key = REDIS_RATELIMIT.format(user_id=user_id)
        try:
            pipe = container.redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, 60)
            count, _ = await pipe.execute()
            return int(count) > rate_limit
        except Exception as exc:
            log.warning(
                "middleware.rate_limit.redis_unreachable",
                user_id=user_id,
                error=str(exc),
            )
            return False

    # Group -1 runs before feature handlers (group 0+).
    @client.on_message(group=-1)
    async def _msg_mw(_: Client, message: Message) -> None:
        if message.from_user and await _rate_limited(message.from_user.id):
            await message.reply(
                bq(container.localizer.get("rate_limited")),
                parse_mode=ParseMode.HTML,
            )
            await message.stop_propagation()
        message.nf_user = await _resolve(message.from_user)  # type: ignore[attr-defined]
        # Gate ONLY real humans in a private chat. Channel posts, service
        # messages, and anonymous-admin posts have no ``from_user`` — gating
        # them would send the "Staff Access Only" card straight into the channel
        # the bot administers (Senku/Gojo spamming every distribution channel).
        is_private = bool(message.chat and message.chat.type == ChatType.PRIVATE)
        if (staff_only_bot and is_private and message.from_user
                and not _is_staff(message.nf_user)):
            await _show_gate_msg(message)
            await message.stop_propagation()

    @client.on_callback_query(group=-1)
    async def _cb_mw(_: Client, query: CallbackQuery) -> None:
        if query.from_user and await _rate_limited(query.from_user.id):
            await query.answer(bq(container.localizer.get("rate_limited")), show_alert=True)
            await query.stop_propagation()
        query.nf_user = await _resolve(query.from_user)  # type: ignore[attr-defined]
        if staff_only_bot and not _is_staff(query.nf_user):
            # Only render the full gate card in a private chat; never edit a
            # channel/group message into it. Elsewhere a private alert to the
            # tapping user preserves the restriction without posting publicly.
            msg = query.message
            is_private = bool(msg and msg.chat and msg.chat.type == ChatType.PRIVATE)
            if is_private:
                await _show_gate_cb(query)
            else:
                await query.answer("🔒 This bot is for authorized staff only.",
                                   show_alert=True)
            await query.stop_propagation()

    async def _show_gate_msg(message: Message) -> None:
        """Send the staff-only gate card to a non-staff user (best-effort)."""
        try:
            from kurosoden.shared.access_gate import gated_screen
            from nekofetch.ui.screens import send_screen

            screen = await gated_screen(container, staff_only_bot)
            await send_screen(client, message.chat.id, screen)
        except Exception as exc:  # noqa: BLE001 — never crash the gate itself
            log.warning("middleware.gate.failed", bot=staff_only_bot, error=str(exc))

    async def _show_gate_cb(query: CallbackQuery) -> None:
        """Edit the current callback message into the staff-only gate card."""
        try:
            from kurosoden.shared.access_gate import gated_screen
            from nekofetch.ui.screens import send_screen

            if query.message is None:
                await query.answer("🔒 This bot is for authorized staff only.", show_alert=True)
                return
            screen = await gated_screen(container, staff_only_bot)
            await send_screen(client, query.message.chat.id, screen, old_msg=query.message)
            await query.answer()
        except Exception as exc:  # noqa: BLE001
            log.warning("middleware.gate.callback_failed", bot=staff_only_bot, error=str(exc))
            await query.answer("🔒 This bot is for authorized staff only.", show_alert=True)
