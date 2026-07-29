"""Shift Handlers — takeover/relief/reply handlers for the admin duty rotation system.

Handles:
- Takeover requests (Admin B wants Admin A's shift)
- Relief requests (Admin A wants someone else to take over)
- Handoff notes (Admin A leaves context before handing off)
- Duty Board refresh callbacks
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import Permission
from nekofetch.localization.messages import M
from nekofetch.services.auth_service import AuthService
from nekofetch.services.shift_service import ShiftService
from nekofetch.ui.components import cb, keyboard
from nekofetch.ui.duty_board import (
    afk_release_dm,
    blocked_alert,
    handoff_dm,
    handoff_notes_prompt,
    relief_claimed_dm,
    relief_request_dm,
    takeover_denied_dm,
    takeover_request_dm,
)

log = get_logger(__name__)

# FSM states for handoff notes
STATE_SHIFT_NOTES_TAKEOVER = "shift:notes:takeover"
STATE_SHIFT_NOTES_RELIEF = "shift:notes:relief"


async def _resolve_name(client: Client, user_id: int) -> str:
    """Resolve a Telegram user's display name."""
    try:
        tg = await client.get_users(user_id)
        full = " ".join(p for p in (tg.first_name, tg.last_name) if p)
        return full or tg.username or str(user_id)
    except Exception:
        return str(user_id)


async def _refresh_duty_boards(container: Container) -> None:
    """Refresh the persistent duty board messages in both operational channels."""
    try:
        if container.config.log_channel.enabled:
            from nekofetch.services.log_channel_service import LogChannelService
            await LogChannelService(container).update_duty_board()
    except Exception:
        pass
    try:
        if container.config.thumbnail_channel.enabled:
            from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService
            await ThumbnailChannelService(container).update_duty_board()
    except Exception:
        pass


def register(client: Client, container: Container) -> None:
    auth = AuthService(container)
    fsm = FSM(container.redis, bot="admin")
    shift = ShiftService(container)
    L = container.localizer.get

    def _can(obj, perm: Permission) -> bool:
        user = getattr(obj, "nf_user", None)
        return bool(user and auth.has_permission(user, perm))

    async def _dm(user_id: int, text: str, reply_markup=None) -> None:
        """Send a DM to a user (best-effort)."""
        try:
            await client.send_message(
                user_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
            )
        except Exception as exc:
            log.debug("shift.dm.failed", user=user_id, error=str(exc))

    # ════════════════════════════════════════════════════════════════════════
    # Shift action callbacks: take | relief | takeover_request
    # ════════════════════════════════════════════════════════════════════════

    @client.on_callback_query(filters.regex(r"^shift\|"))
    async def _shift_callback(_c: Client, q: CallbackQuery) -> None:
        if not _can(q, Permission.QUEUE_DOWNLOADS):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return

        parts = q.data.split("|")
        action = parts[1] if len(parts) > 1 else ""
        channel = parts[2] if len(parts) > 2 else "logcc"

        if action == "take":
            # Take an available shift
            name = await _resolve_name(_c, q.from_user.id)
            await shift.assign(channel, q.from_user.id, name)
            await q.answer(f"You are now on duty in {channel.upper()}.", show_alert=True)
            await _refresh_duty_boards(container)

        elif action == "relief":
            # Current worker seeks relief → notify all off-duty staff
            state = await shift.seek_relief(channel, q.from_user.id)
            if state is None:
                await q.answer("You are not on duty.", show_alert=True)
                return
            name = state.worker_name or await _resolve_name(_c, q.from_user.id)
            staff = await shift.list_staff_ids()
            accept_cb = f"shift|relief_accept|{channel}|{q.from_user.id}"
            notified = 0
            for s in staff:
                if s["telegram_id"] == q.from_user.id:
                    continue
                await _dm(
                    s["telegram_id"],
                    relief_request_dm(name, channel),
                    reply_markup=keyboard([("✅ Accept Shift", cb("shift", "relief_accept", channel))]),
                )
                notified += 1
            await q.answer(
                f"Relief requested — {notified} admin(s) notified.", show_alert=True,
            )
            await _refresh_duty_boards(container)

        elif action == "takeover":
            # Non-worker requests to take over from current worker
            state = await shift.get_state(channel)
            if state.worker_id is None:
                await q.answer("No one is on duty to take over from.", show_alert=True)
                return
            if state.worker_id == q.from_user.id:
                await q.answer("You are already on duty.", show_alert=True)
                return
            name = await _resolve_name(_c, q.from_user.id)
            result = await shift.request_takeover(channel, q.from_user.id, name)
            if result is None:
                await q.answer("Could not request takeover.", show_alert=True)
                return
            # DM the current worker
            approve_cb = f"shift|takeover_approve|{channel}|{q.from_user.id}"
            deny_cb = f"shift|takeover_deny|{channel}|{q.from_user.id}"
            await _dm(
                state.worker_id,
                takeover_request_dm(name, channel),
                reply_markup=keyboard(
                    [("✅ Approve & Hand Off", cb("shift", "takeover_approve", channel))],
                    [("❌ Deny", cb("shift", "takeover_deny", channel))],
                ),
            )
            await q.answer(f"Takeover requested from {state.worker_name}.", show_alert=True)
            await _refresh_duty_boards(container)

        elif action == "relief_accept":
            # Off-duty admin accepts a relief request
            name = await _resolve_name(_c, q.from_user.id)
            ok, new_state, old_worker_id = await shift.accept_relief(
                channel, q.from_user.id, name,
            )
            if not ok:
                await q.answer("Shift already claimed or no longer available.", show_alert=True)
                return
            # Edit the DM for all other notified admins
            try:
                await q.message.edit_text(
                    relief_claimed_dm(name, channel), parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            # DM the old worker that relief has been accepted
            if old_worker_id:
                await _dm(
                    old_worker_id,
                    f"🟢 <b>{name}</b> has taken over your shift in the channel.\n"
                    f"You are now off duty.",
                )
            await q.answer(f"You are now on duty in {channel}.", show_alert=True)
            await _refresh_duty_boards(container)

        elif action == "takeover_approve":
            # Current worker approves takeover → enter notes FSM
            requester_id = int(parts[3]) if len(parts) > 3 else 0
            await fsm.set(
                q.from_user.id, STATE_SHIFT_NOTES_TAKEOVER,
                channel=channel, requester_id=requester_id,
            )
            await q.answer()
            try:
                await q.message.edit_text(
                    handoff_notes_prompt(channel), parse_mode=ParseMode.HTML,
                    reply_markup=keyboard(
                        [("⏭️ Skip Notes", cb("shift", "takeover_confirm", channel, str(requester_id)))],
                    ),
                )
            except Exception as exc:
                log.debug("shift.notes.prompt.failed", error=str(exc))

        elif action == "takeover_confirm":
            # Confirm takeover (with or without notes)
            requester_id = int(parts[3]) if len(parts) > 3 else 0
            notes = ""
            # Try to get notes from FSM if they were entered
            state_fsm, data = await fsm.get(q.from_user.id)
            if data:
                notes = data.get("handoff_notes", "")
                requester_id = requester_id or data.get("requester_id", 0)
            ok, new_state, _ = await shift.approve_takeover(channel, q.from_user.id)
            if not ok:
                await q.answer("Takeover expired or no longer valid.", show_alert=True)
                return
            await fsm.clear(q.from_user.id)
            # DM the new worker
            summary = await shift.build_handoff_summary(channel, notes=notes)
            await _dm(requester_id, handoff_dm(summary))
            await q.answer("Shift handed off. You are now off duty.", show_alert=True)
            await _refresh_duty_boards(container)
            try:
                await q.message.edit_text(
                    "✅ Shift handed off. You are now off duty.", parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        elif action == "takeover_deny":
            # Current worker denies takeover
            requester_id = await shift.deny_takeover(channel, q.from_user.id)
            worker_name = await _resolve_name(_c, q.from_user.id)
            if requester_id:
                await _dm(requester_id, takeover_denied_dm(channel, worker_name))
            await q.answer("Takeover denied.", show_alert=True)
            await _refresh_duty_boards(container)
            try:
                await q.message.edit_text(
                    "❌ Takeover denied.", parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        elif action == "duty_board":
            # Refresh the duty board display (no FSM change, just visual)
            state = await shift.get_state(channel)
            from nekofetch.ui.duty_board import duty_board
            text = duty_board(state)
            # Build inline buttons for the duty board
            kb_rows = []
            if state.worker_id is None:
                kb_rows.append([("🛡️ Take Shift", cb("shift", "take", channel))])
            elif state.worker_id == q.from_user.id:
                kb_rows.append([("🟡 Need Relief", cb("shift", "relief", channel))])
            elif state.status.value == "takeover_pending":
                pass  # Already pending — don't spam
            else:
                kb_rows.append([("🔵 Request Takeover", cb("shift", "takeover", channel))])
            import asyncio
            try:
                await asyncio.sleep(0.2)
                await q.message.edit_text(
                    text, parse_mode=ParseMode.HTML,
                    reply_markup=keyboard(*kb_rows) if kb_rows else None,
                )
            except Exception:
                pass
            await q.answer()

    # ════════════════════════════════════════════════════════════════════════
    # Handoff notes reply handler
    # ════════════════════════════════════════════════════════════════════════

    @client.on_message(filters.text & filters.private & ~filters.command(["start"]), group=11)
    async def _handoff_notes_reply(_c: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, data = await fsm.get(message.from_user.id)
        if state not in (STATE_SHIFT_NOTES_TAKEOVER, STATE_SHIFT_NOTES_RELIEF):
            return
        user = getattr(message, "nf_user", None)
        if not (user and auth.has_permission(user, Permission.QUEUE_DOWNLOADS)):
            return

        notes = (message.text or "").strip()
        channel = data.get("channel", "logcc")
        requester_id = data.get("requester_id", 0)

        if state == STATE_SHIFT_NOTES_TAKEOVER:
            ok, new_state, _ = await shift.approve_takeover(channel, message.from_user.id)
            if not ok:
                await message.reply("Takeover expired or no longer valid.")
                return
            await _refresh_duty_boards(container)
            summary = await shift.build_handoff_summary(channel, notes=notes)
            await _dm(requester_id, handoff_dm(summary))
            await message.reply(
                "✅ Shift handed off with your notes. You are now off duty.",
                parse_mode=ParseMode.HTML,
            )
        elif state == STATE_SHIFT_NOTES_RELIEF:
            # Notes were left during relief handoff — just clear FSM
            # (the new worker was already assigned by accept_relief)
            await message.reply(
                "📝 Notes saved. You are now off duty.",
                parse_mode=ParseMode.HTML,
            )

        await fsm.clear(message.from_user.id)
