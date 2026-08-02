"""Senku task handlers — REUSES NekoFetch's BotContentService + BotFactory.

Key principle: Senku does NOT reimplement content generation. It delegates to:
  • BotContentService.generate_posts() — watch guides, info cards, seasons, footer.
  • BotFactory.create_for_anime() — auto-creates distribution bots/channels.
  • BotOrchestratorService — orchestrates the full flow.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.ui.components import cb, keyboard
from nekofetch.ui.screens import Screen, send_screen

log = get_logger(__name__)

STATE_CHANNEL_USERNAME = "senku:await_channel_username"


def register(client: Client, container: Container) -> None:
    # ── /tasks — View assigned distribution tasks ────────────────────────────
    @client.on_message(filters.command("tasks"))
    async def _tasks(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.infrastructure.repositories.request_repo import RequestRepository

        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        active = await engine.get_active_tasks(message.from_user.id, stage="senku")
        offers = await engine.get_pending_offers(message.from_user.id, stage="senku")

        from kurosoden.shared import senku_voice as V
        from nekofetch.ui.artwork import pick_artwork
        from nekofetch.ui.screens import card, send_screen

        if not active and not offers:
            await send_screen(
                client, message.chat.id,
                card(V.TASKS_EMPTY, image=pick_artwork("senku"), bot_name="senku"),
            )
            return

        rows: list[list[tuple[str, str]]] = []
        for a in offers[:5]:
            title = a.request_code
            try:
                async with session_scope(container.pg_sessionmaker) as s:
                    req = await RequestRepository(s).get_by_code(a.request_code)
                    if req:
                        title = req.anime_title
            except Exception:
                pass
            rows.append([(f"Accept - {title}"[:60],
                          cb("senku", "offer", "accept", a.request_code))])
            rows.append([(f"Reject - {title}"[:60],
                          cb("senku", "offer", "reject", a.request_code))])
        for a in active[:10]:
            title = a.request_code
            try:
                async with session_scope(container.pg_sessionmaker) as s:
                    req = await RequestRepository(s).get_by_code(a.request_code)
                    if req:
                        title = req.anime_title
            except Exception:
                pass
            icon = "🔄" if a.status == "in_progress" else "🧪"
            label = f"{icon} {title}"[:60]
            rows.append([(label, cb("senku", "wiz", "open", a.request_code))])

        caption = V.tasks_title(len(active) + len(offers))
        if offers:
            caption = f"<b>Pending offers:</b> {len(offers)}\n\n{caption}"
        await send_screen(
            client, message.chat.id,
            card(caption, image=pick_artwork("senku"),
                 bot_name="senku", buttons=rows),
        )

    @client.on_callback_query(filters.regex(r"^senku\|offer\|"))
    async def _offer_cb(_: Client, q: CallbackQuery) -> None:
        if q.from_user is None:
            await q.answer()
            return
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        parts = (q.data or "").split("|", 3)
        action = parts[2] if len(parts) > 2 else ""
        code = parts[3] if len(parts) > 3 else ""
        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        if action == "accept":
            result = await engine.accept_offer(code, "senku", q.from_user.id)
            await q.answer("Accepted." if result else "Offer expired.", show_alert=result is None)
            if result and q.message is not None:
                from nekofetch.ui.artwork import pick_artwork

                await send_screen(
                    client,
                    q.message.chat.id,
                    Screen(
                        caption=(
                            "🧪 <b>Offer accepted.</b>\n\n"
                            f"<code>{code}</code>\n"
                            "<i>Open the distribution build and continue the lab work.</i>"
                        ),
                        keyboard=keyboard([
                            ("🧪 Open Distribution", cb("senku", "wiz", "open", code)),
                        ]),
                        image=pick_artwork("senku"),
                    ),
                    old_msg=q.message,
                )
        elif action == "reject":
            ok = await engine.reject_offer(code, "senku", q.from_user.id)
            await q.answer("Rejected." if ok else "Offer expired.", show_alert=not ok)
        else:
            await q.answer()

    # ── /create — handled by the channel-creation wizard (handlers/wizard.py) ──

    # ── /generate — Generate content for a title ─────────────────────────────
    @client.on_message(filters.command("generate"))
    async def _generate_cmd(_: Client, message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "<b>🎨 Generate Distribution Content</b>\n\n"
                "Usage: <code>/generate REQ-XXXX</code>\n\n"
                "This will generate all content posts for the anime:\n"
                "• Information card\n"
                "• Stickers & dividers\n"
                "• Season separators\n"
                "• Watch guide\n"
                "• Footer\n\n"
                "<i>Uses NekoFetch's existing BotContentService.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        request_code = parts[1].strip()
        await _generate_content_for_request(client, container, message, request_code)

    # ── /help ─────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("help"))
    async def _help(_: Client, message: Message) -> None:
        from nekofetch.ui.artwork import pick_artwork

        caption = (
            "<b>🧪 Senku Ishigami — Distribution</b>\n"
            "<i>\"Ten billion percent — this channel will be perfect.\"</i>\n\n"
            "<b>✦ What I do</b>\n"
            "Once a download finishes, I turn it into a fully-built channel — "
            "info card, season separators, watch guide, and footer, all generated "
            "for you.\n\n"
            "<b>⌘ Commands</b>\n"
            "/tasks — Distribution tasks waiting on you\n"
            "/create — Channel setup wizard\n"
            "/generate — Build content for a title\n"
            "/help — This guide\n\n"
            "<i>Everything is button-driven — tap /start to begin.</i>"
        )
        await send_screen(
            client, message.chat.id,
            Screen(caption=caption, image=pick_artwork("senku"),
                   keyboard=keyboard([("◀ Back to Menu", cb("senku", "home"))])),
        )


async def _generate_content_for_request(
    client: Client, container: Container, message: Message, request_code: str,
) -> None:
    """Generate distribution content by reusing NekoFetch's BotContentService."""
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import DistributionBot
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.repositories.request_repo import RequestRepository

    async with session_scope(container.pg_sessionmaker) as session:
        req = await RequestRepository(session).get_by_code(request_code)
        if req is None:
            await message.reply(
                f"❌ <b>Request not found:</b> {request_code}",
                parse_mode=ParseMode.HTML,
            )
            return
        title = req.anime_title
        anime_doc_id = req.anime_doc_id

    if not anime_doc_id:
        await message.reply(
            f"❌ <b>No anime ID found</b> for {request_code}.\n"
            "The request must have an AniList ID to generate content.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Check if a distribution bot/channel already exists.
    async with session_scope(container.pg_sessionmaker) as session:
        existing = (
            await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()

    if existing:
        # Regenerate content for the existing bot.
        await message.reply(
            f"🔄 <b>Regenerating content</b> for <b>{title}</b>...\n"
            f"Distribution entity: @{existing.username or existing.name}",
            parse_mode=ParseMode.HTML,
        )
        try:
            from nekofetch.services.bot_content import BotContentService
            await BotContentService(container).generate_posts(existing.id, anime_doc_id)
            await message.reply(
                f"✅ <b>Content regenerated!</b>\n\n"
                f"📋 Request: <code>{request_code}</code>\n"
                f"🎬 Anime: <b>{title}</b>\n"
                f"📺 Channel: @{existing.username or existing.name}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            await message.reply(
                f"❌ <b>Generation failed:</b> {str(exc)[:300]}",
                parse_mode=ParseMode.HTML,
            )
        return

    # No existing bot — guide admin to create one.
    await message.reply(
        f"<b>📺 No distribution entity exists</b> for <b>{title}</b>.\n\n"
        "Use <b>/create</b> to start the channel creation wizard, then "
        "run <b>/generate</b> again with the channel info.",
        parse_mode=ParseMode.HTML,
    )
