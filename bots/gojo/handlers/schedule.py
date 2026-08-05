"""Schedule management — list and control pending scheduled posts.

The ``/schedule`` command shows the admin's pending scheduled posts with buttons:
  • Edit — modify the caption before it publishes
  • Send Now — publish immediately (normal)
  • Send Silently — publish immediately (no notification)
  • Reschedule — pick a new time
  • Cancel — delete the scheduled post

Row layout: [Edit, Send Now], [Send Silently, Reschedule], [Cancel].
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from kurosoden.shared import gojo_voice as V
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.timefmt import format_scheduled_time
from nekofetch.ui.components import cb, keyboard
from nekofetch.ui.screens import Screen, send_screen
from nekofetch.ui.artwork import pick_artwork

log = get_logger(__name__)


def _schedule_card(scheduled: list, tz_name: str | None) -> Screen:
    """Build the schedule management screen with one card per pending post."""
    if not scheduled:
        return Screen(
            caption="📅 <b>No scheduled posts</b>\n\nYou don't have any posts scheduled right now.",
            image=pick_artwork("gojo"),
        )

    from nekofetch.core.timefmt import to_tz

    lines = ["📅 <b>Scheduled Posts</b>\n"]
    for s in scheduled:
        time_str = to_tz(s.scheduled_at, tz_name, with_label=False)
        title = s.anime_title or s.request_code
        silent_tag = " 🔕" if s.silent else ""
        lines.append(f"⦿ <b>{title}</b>{silent_tag}")
        lines.append(f"   {time_str}")
        lines.append(f"   Code: <code>{s.request_code}</code>\n")

    caption = "\n".join(lines)
    caption += "\n<i>Tap a post below to manage it.</i>"

    # Build buttons — one per scheduled post
    btn_rows = []
    for s in scheduled:
        label = (s.anime_title or s.request_code)[:30]
        btn_rows.append([(label, cb("gojo", "sched_manage", str(s.id)))])

    return Screen(caption=caption, image=pick_artwork("gojo"), buttons=keyboard(*btn_rows))


def _manage_card(sched_post, tz_name: str | None) -> Screen:
    """Build the individual post management screen with action buttons."""
    from nekofetch.core.timefmt import to_tz

    time_str = to_tz(sched_post.scheduled_at, tz_name, with_label=False)
    title = sched_post.anime_title or sched_post.request_code
    silent_tag = " (silent)" if sched_post.silent else ""

    caption = (
        f"📅 <b>{title}</b>\n\n"
        f"<b>Scheduled:</b> {time_str}{silent_tag}\n"
        f"<b>Code:</b> <code>{sched_post.request_code}</code>\n\n"
        f"What would you like to do?"
    )

    buttons = keyboard(
        [("✏️ Edit Caption", cb("gojo", "sched_edit", str(sched_post.id))),
         ("▶️ Send Now", cb("gojo", "sched_now", str(sched_post.id)))],
        [("🔕 Send Silently", cb("gojo", "sched_silent", str(sched_post.id))),
         ("🕐 Reschedule", cb("gojo", "sched_time", str(sched_post.id)))],
        [("❌ Cancel", cb("gojo", "sched_cancel", str(sched_post.id)))],
        [("« Back", cb("gojo", "sched_list"))],
    )

    return Screen(caption=caption, image=pick_artwork("gojo"), buttons=buttons)


async def _admin_tz(container: Container, admin_id: int) -> str | None:
    """Resolve the admin's IANA timezone from AdminAvailability."""
    try:
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine
        return await AdminAssignmentEngine(container.pg_sessionmaker).get_timezone(admin_id)
    except Exception:
        return None


def register(client: Client, container: Container) -> None:
    """Register /schedule command and its management buttons."""
    from nekofetch.services.schedule_service import ScheduleService
    from nekofetch.services.publishing_service import PublishingService
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.database.postgres.models import ScheduledPost
    from sqlalchemy import select
    from nekofetch.core.timefmt import to_tz

    @client.on_message(filters.command("schedule") & filters.private)
    async def _schedule_cmd(_: Client, message: Message) -> None:
        if not message.from_user:
            return

        admin_id = message.from_user.id
        tz_name = await _admin_tz(container, admin_id)

        # Fetch admin's pending schedules
        async with session_scope(container.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(ScheduledPost)
                    .where(
                        ScheduledPost.admin_telegram_id == admin_id,
                        ScheduledPost.status == "pending",
                    )
                    .order_by(ScheduledPost.scheduled_at.asc())
                )
            ).scalars().all()

        screen = _schedule_card(list(rows), tz_name)
        await send_screen(client, message.chat.id, screen)

    @client.on_callback_query(filters.regex(r"^gojo\|sched_list$"))
    async def _sched_list(_: Client, q: CallbackQuery) -> None:
        if not q.from_user or not q.message:
            await q.answer()
            return

        admin_id = q.from_user.id
        tz_name = await _admin_tz(container, admin_id)

        async with session_scope(container.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(ScheduledPost)
                    .where(
                        ScheduledPost.admin_telegram_id == admin_id,
                        ScheduledPost.status == "pending",
                    )
                    .order_by(ScheduledPost.scheduled_at.asc())
                )
            ).scalars().all()

        screen = _schedule_card(list(rows), tz_name)
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^gojo\|sched_manage\|(\d+)$"))
    async def _sched_manage(_: Client, q: CallbackQuery) -> None:
        if not q.from_user or not q.message:
            await q.answer()
            return

        sched_id = int(q.data.split("|")[2])
        admin_id = q.from_user.id
        tz_name = await _admin_tz(container, admin_id)

        async with session_scope(container.pg_sessionmaker) as session:
            row = await session.get(ScheduledPost, sched_id)
            if not row or row.admin_telegram_id != admin_id or row.status != "pending":
                await q.answer("This schedule is no longer available.", show_alert=True)
                return

        screen = _manage_card(row, tz_name)
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^gojo\|sched_now\|(\d+)$"))
    async def _sched_now(_: Client, q: CallbackQuery) -> None:
        if not q.from_user or not q.message:
            await q.answer()
            return

        sched_id = int(q.data.split("|")[2])
        admin_id = q.from_user.id

        async with session_scope(container.pg_sessionmaker) as session:
            row = await session.get(ScheduledPost, sched_id)
            if not row or row.admin_telegram_id != admin_id or row.status != "pending":
                await q.answer("This schedule is no longer available.", show_alert=True)
                return
            code = row.request_code
            caption = row.caption_override

        # Cancel the schedule
        await ScheduleService(container).cancel(sched_id, admin_id)

        # Publish immediately
        await q.answer("Publishing now...", show_alert=True)
        try:
            await PublishingService(container).publish(code, caption_override=caption, silent=False)
            await q.message.edit_text(
                f"✅ <b>Published</b>\n\n<code>{code}</code> has been published to the main channel.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.error("schedule.send_now.failed", code=code, error=str(exc))
            await q.message.edit_text(
                f"❌ <b>Publish failed</b>\n\n{str(exc)[:200]}",
                parse_mode=ParseMode.HTML,
            )

    @client.on_callback_query(filters.regex(r"^gojo\|sched_silent\|(\d+)$"))
    async def _sched_silent(_: Client, q: CallbackQuery) -> None:
        if not q.from_user or not q.message:
            await q.answer()
            return

        sched_id = int(q.data.split("|")[2])
        admin_id = q.from_user.id

        async with session_scope(container.pg_sessionmaker) as session:
            row = await session.get(ScheduledPost, sched_id)
            if not row or row.admin_telegram_id != admin_id or row.status != "pending":
                await q.answer("This schedule is no longer available.", show_alert=True)
                return
            code = row.request_code
            caption = row.caption_override

        # Cancel the schedule
        await ScheduleService(container).cancel(sched_id, admin_id)

        # Publish silently
        await q.answer("Publishing silently...", show_alert=True)
        try:
            await PublishingService(container).publish(code, caption_override=caption, silent=True)
            await q.message.edit_text(
                f"✅ <b>Published (silent)</b>\n\n<code>{code}</code> has been published silently.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.error("schedule.send_silent.failed", code=code, error=str(exc))
            await q.message.edit_text(
                f"❌ <b>Publish failed</b>\n\n{str(exc)[:200]}",
                parse_mode=ParseMode.HTML,
            )

    @client.on_callback_query(filters.regex(r"^gojo\|sched_cancel\|(\d+)$"))
    async def _sched_cancel(_: Client, q: CallbackQuery) -> None:
        if not q.from_user or not q.message:
            await q.answer()
            return

        sched_id = int(q.data.split("|")[2])
        admin_id = q.from_user.id

        cancelled = await ScheduleService(container).cancel(sched_id, admin_id)
        if cancelled:
            await q.answer("Scheduled post cancelled.", show_alert=True)
            await q.message.edit_text(
                "✅ <b>Cancelled</b>\n\nThe scheduled post has been removed.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await q.answer("This schedule is no longer available.", show_alert=True)
