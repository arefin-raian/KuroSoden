from __future__ import annotations

import asyncio

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.domain.enums import Permission
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.infrastructure.repositories.user_repo import UserRepository
from nekofetch.localization.messages import M
from nekofetch.services.auth_service import AuthService
from nekofetch.ui.progress import loading_animation
from nekofetch.ui.screens import show

STATE_BROADCAST = "admin:await_broadcast"
STATE_CH_BROADCAST = "admin:await_ch_broadcast"

# Auto-delete presets offered after the channel-broadcast message is captured.
# 0 = permanent; other values are minutes.
_CH_BC_DURATIONS = [
    (M.BTN_CH_BC_PERMANENT, 0),
    (M.BTN_CH_BC_1H, 60),
    (M.BTN_CH_BC_6H, 360),
    (M.BTN_CH_BC_24H, 1440),
    (M.BTN_CH_BC_7D, 10080),
]


def register(client: Client, container: Container) -> None:
    auth = AuthService(container)
    fsm = FSM(container.redis, bot="admin")
    L = container.localizer.get

    def _allowed(obj) -> bool:
        # Broadcasting to every user is owner-only.
        user = getattr(obj, "nf_user", None)
        return bool(user and auth.is_owner(user)
                    and auth.has_permission(user, Permission.MANAGE_STAFF))

    @client.on_callback_query(filters.regex(r"^admin\|broadcast"))
    async def _start(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await fsm.set(q.from_user.id, STATE_BROADCAST)
        await q.answer()
        from nekofetch.ui.components import cb, keyboard

        kb = keyboard([(L(M.BTN_CANCEL), cb("admin", "home"))])
        await show(client, q.message, L(M.BROADCAST_PROMPT), kb)

    @client.on_message(filters.text & filters.private & ~filters.command(["start"]), group=8)
    async def _broadcast(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, _ = await fsm.get(message.from_user.id)
        if state != STATE_BROADCAST or not _allowed(message):
            return
        await fsm.clear(message.from_user.id)

        async with session_scope(container.pg_sessionmaker) as session:
            ids = await UserRepository(session).all_telegram_ids()

        status = await message.reply(L(M.BROADCAST_SENDING), parse_mode=ParseMode.HTML)
        await loading_animation(status, L(M.BROADCAST_SENDING))
        sent = failed = 0
        for uid in ids:
            try:
                await message.copy(uid)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(container).event(
            "admin", "broadcast", sent=sent, failed=failed, by=message.from_user.id
        )
        await status.edit_text(
            L(M.BROADCAST_DONE, sent=sent, failed=failed), parse_mode=ParseMode.HTML
        )

    # ── Channel broadcast: post one message to every distribution channel ──────

    @client.on_callback_query(filters.regex(r"^admin\|chbroadcast$"))
    async def _ch_start(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await fsm.set(q.from_user.id, STATE_CH_BROADCAST)
        await q.answer()
        from nekofetch.ui.components import cb, keyboard

        kb = keyboard([(L(M.BTN_CANCEL), cb("admin", "home"))])
        await show(client, q.message, L(M.CH_BROADCAST_PROMPT), kb)

    # group=13 is unused elsewhere on the admin client. A broad private filter
    # (any media, not just text) MUST NOT share a group with another handler:
    # Pyrogram runs only the first matching handler per group, so sharing group 3
    # (batch.py's title-capture) would let batch swallow text broadcasts. Gated on
    # the FSM state so it stays inert unless a channel broadcast is being composed.
    @client.on_message(
        filters.private & ~filters.command(["start"]), group=13
    )
    async def _ch_capture(_: Client, message: Message) -> None:
        """Capture the message to broadcast, then ask for a retention window.

        Any message type is accepted (text or media) — the service copies it,
        so it renders natively in each channel. We stash the source coordinates
        in the FSM bag and switch the keyboard to the duration picker.
        """
        if not message.from_user:
            return
        state, _ = await fsm.get(message.from_user.id)
        if state != STATE_CH_BROADCAST or not _allowed(message):
            return
        await fsm.update(
            message.from_user.id,
            src_chat=message.chat.id, src_msg=message.id,
        )
        from nekofetch.ui.components import cb, keyboard

        rows = [[(L(key), cb("admin", "chbc_send", minutes))]
                for key, minutes in _CH_BC_DURATIONS]
        rows.append([(L(M.BTN_CANCEL), cb("admin", "home"))])
        await message.reply(
            L(M.CH_BROADCAST_DURATION), parse_mode=ParseMode.HTML,
            reply_markup=keyboard(*rows),
        )

    @client.on_callback_query(filters.regex(r"^admin\|chbc_send\|"))
    async def _ch_send(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_CH_BROADCAST or "src_chat" not in data:
            await q.answer()
            return
        await fsm.clear(q.from_user.id)
        await q.answer()

        minutes = int(q.data.split("|")[-1])
        from nekofetch.services.broadcast_service import BroadcastService

        status = await q.message.reply(
            L(M.CH_BROADCAST_SENDING), parse_mode=ParseMode.HTML
        )
        result = await BroadcastService(container).broadcast_copy(
            from_chat_id=int(data["src_chat"]),
            message_id=int(data["src_msg"]),
            delete_after_minutes=minutes or None,
        )

        if result.targets == 0:
            await status.edit_text(
                L(M.CH_BROADCAST_NO_CHANNELS), parse_mode=ParseMode.HTML
            )
            return

        if result.delete_at is not None:
            retention = f"Auto-deletes: <b>{result.delete_at:%Y-%m-%d %H:%M} UTC</b>"
        else:
            retention = "Retention: <b>permanent</b>"

        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(container).event(
            "admin", "channel_broadcast",
            targets=result.targets, sent=result.sent, failed=result.failed,
            delete_after_minutes=minutes, by=q.from_user.id,
        )
        await status.edit_text(
            L(M.CH_BROADCAST_DONE, targets=result.targets, sent=result.sent,
              failed=result.failed, retention=retention),
            parse_mode=ParseMode.HTML,
        )
