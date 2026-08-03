from __future__ import annotations

import asyncio
import re as _re
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.exceptions import NekoFetchError
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    BotDelivery,
    DistributionBot,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.localization.messages import M
from nekofetch.services.bot_render import build_audio_keyboard
from nekofetch.services.distribution_service import DistributionService
from nekofetch.ui.components import cb, keyboard, parse_cb
from nekofetch.ui.progress import loading_animation, staged_loading
from nekofetch.ui.typography import bq, bqx

DISTRIBUTION_COMMANDS = [
    BotCommand("start", "Browse the library / open a title"),
    BotCommand("help", "How to download & get access"),
]

# Redis key for per-user last-activity tracking (grace period extension)
_K_USER_LAST_ACTIVITY = "nf:dist:lastact:{bot_id}:{user_id}"

log = get_logger(__name__)


async def publish_distribution_commands(client: Client) -> None:
    await client.set_bot_commands(DISTRIBUTION_COMMANDS)


def build_distribution_bot(
    container: Container, record: DistributionBot, token: str
) -> Client:
    client = Client(
        name=f"nf-dist-{record.id}",
        api_id=container.env.telegram_api_id,
        api_hash=container.env.telegram_api_hash,
        bot_token=token,
        workdir=str(container.env.session_path),
    )
    client.container = container
    client.bot_record = record

    dist = DistributionService(container)
    fsm = FSM(container.redis, bot=f"dist:{record.id}")
    cfg = container.config.distribution
    ui_cfg = container.config.ui

    # ── helpers ────────────────────────────────────────────────────────────────

    async def _load_posts() -> list[BotContentPost]:
        """Load this bot's content posts in order."""
        from sqlalchemy import select

        async with session_scope(container.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(BotContentPost)
                    .where(BotContentPost.bot_id == record.id)
                    .order_by(BotContentPost.order)
                )
            ).scalars().all()
            return list(rows)

    def _build_buttons(post: BotContentPost) -> InlineKeyboardMarkup | None:
        """Build inline keyboard with **URL buttons** pointing to Fstore deep links.

        Download links are pre-generated at ``BotContentService.generate_posts``
        time and stored in ``button_data.links``. The visual layout — two
        quality buttons per row, configurable resolution labels, Japanese-first
        language sections for sub-only titles — comes from the shared
        :mod:`nekofetch.services.bot_render` builder so this path and Senku's
        manual publisher stay byte-identical.
        """
        return build_audio_keyboard(post.button_data, container.config.post_format)

    async def _send_posts(chat_id: int) -> tuple[list[int], int | None]:
        """Send all content posts for this bot, with divider stickers between sections.

        Prefers ``image_cached_url`` (catbox.moe URL set at generate time)
        over ``image_url`` so Telegram doesn't re-fetch from TMDB/AniList
        CDNs on every delivery. Falls back to ``image_url`` when catbox was
        unavailable at generate time, and to plain text when neither URL
        is set.

        Returns ``(sent_message_ids, pinned_message_id)``.
        ``pinned_message_id`` is the chat message id of the watch-guide post
        (or ``None`` if the bot doesn't have one).
        """
        posts = await _load_posts()
        sent_ids: list[int] = []
        pinned_id: int | None = None
        divider_id = container.config.bot.divider_sticker_id

        # Resolve bot username for watch-guide quality links.
        # Deep-linking to specific messages doesn't work in private chats,
        # so we link the whole quality string to the bot itself instead.
        bot_uname = record.username
        if not bot_uname:
            try:
                me = await client.get_me()
                bot_uname = me.username
            except Exception:
                bot_uname = None

        for i, post in enumerate(posts):
            # Divider sticker between major sections (not before the first post).
            if i > 0 and divider_id:
                try:
                    div = await client.send_sticker(chat_id, divider_id)
                    sent_ids.append(div.id)
                except Exception:
                    pass

            markup = _build_buttons(post)
            # Replace {BOT_QUAL…} placeholders with t.me/{username} links. In a
            # private bot chat message deep-linking is unavailable, so an anchored
            # {BOT_QUAL#<id>:…} is treated like the plain form — the ``#<id>`` group
            # is optional in the pattern and simply dropped. Falls back to plain
            # text when the bot has no username.
            caption_text = post.caption or ""
            if caption_text:
                if bot_uname:
                    caption_text = _re.sub(
                        r'\{BOT_QUAL(?:#\d+)?:([^}]+)\}',
                        rf'<a href="https://t.me/{bot_uname}">\1</a>',
                        caption_text,
                    )
                else:
                    caption_text = _re.sub(r'\{BOT_QUAL(?:#\d+)?:([^}]+)\}', r'\1', caption_text)
            try:
                # Prefer the catbox-cached URL (set at generate time) so the
                # bot doesn't hammer TMDB/AniList CDNs on every /start; fall
                # back to the original URL only if catbox was unavailable when
                # content was last regenerated; fall further back to plain text
                # if neither URL is set (no usable image source).
                image_source = post.image_cached_url or post.image_url
                if image_source:
                    msg = await client.send_photo(
                        chat_id, image_source,
                        caption=caption_text,
                        reply_markup=markup,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    msg = await client.send_message(
                        chat_id, caption_text,
                        reply_markup=markup,
                        parse_mode=ParseMode.HTML,
                    )
                sent_ids.append(msg.id)

                if post.is_pinned:
                    pinned_id = msg.id
                    try:
                        await client.pin_chat_message(chat_id, msg.id, disable_notification=True)
                    except Exception:
                        pass
            except Exception as exc:
                from nekofetch.core.logging import get_logger
                get_logger(__name__).warning(
                    "dist.send_post.failed", post_type=post.post_type, error=str(exc)
                )
                continue

        return sent_ids, pinned_id

    # ── per-user delivery tracking (revision-aware redelivery) ────────────────

    async def _get_content_revision() -> int:
        """Fresh 'what's the latest pack' counter for this bot."""
        from sqlalchemy import select

        async with session_scope(container.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(DistributionBot.content_revision).where(
                        DistributionBot.id == record.id
                    )
                )
            ).scalar_one_or_none()
            return int(row or 0)

    async def _load_my_delivery(user_id: int) -> BotDelivery | None:
        """Return this user's prior delivery row for our bot, if any."""
        from sqlalchemy import select

        async with session_scope(container.pg_sessionmaker) as session:
            return (
                await session.execute(
                    select(BotDelivery).where(
                        BotDelivery.bot_id == record.id,
                        BotDelivery.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()

    async def _delete_prior_delivery(d: BotDelivery) -> None:
        """Best-effort cleanup of a returning user's stale posts before re-delivery."""
        # Unpin first — Telegram refuses to delete a pinned message otherwise.
        if d.pinned_message_id:
            try:
                await client.unpin_chat_message(d.chat_id, d.pinned_message_id)
            except Exception:
                pass
        ids = [mid for mid in (d.message_ids or []) if mid]
        if not ids:
            return
        try:
            # Telegram caps delete_messages at 100 ids per call; chunk if needed.
            for k in range(0, len(ids), 100):
                await client.delete_messages(d.chat_id, ids[k:k + 100])
        except Exception:
            # Already gone (TG's 48h cap), sender hostage, etc. — best effort.
            pass

    async def _save_my_delivery(
        user_id: int, chat_id: int, message_ids: list[int],
        revision: int, pinned_message_id: int | None,
    ) -> None:
        """Upsert this user's delivery row so the next /start can find it."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(BotDelivery).values(
            bot_id=record.id, user_id=user_id, chat_id=chat_id,
            message_ids=list(message_ids),
            pinned_message_id=pinned_message_id,
            delivered_revision=revision,
        ).on_conflict_do_update(
            index_elements=[BotDelivery.bot_id, BotDelivery.user_id],
            set_={
                "chat_id": chat_id,
                "message_ids": list(message_ids),
                "pinned_message_id": pinned_message_id,
                "delivered_revision": revision,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        async with session_scope(container.pg_sessionmaker) as session:
            await session.execute(stmt)

    async def _track_activity(user_id: int) -> None:
        """Update the user's last activity timestamp (for per-user-with-grace retention)."""
        if container.redis:
            import time
            key = _K_USER_LAST_ACTIVITY.format(bot_id=record.id, user_id=user_id)
            await container.redis.set(key, str(int(time.time())))

    async def _schedule_cleanup(chat_id: int, user_id: int, sent_ids: list[int]) -> None:
        """Schedule auto-delete with per-user grace extension.

        The cleanup checks the user's last-activity timestamp before deleting.
        If they've interacted recently (within half the retention period), the
        cleanup is rescheduled for later.
        """
        scheduler = getattr(container, "scheduler", None)
        if scheduler is None or not sent_ids:
            return
        retention_days = container.config.bot.delivery_retention_days
        if retention_days <= 0:
            return
        retention_secs = retention_days * 86400
        half_retention = retention_secs // 2

        grace_key = _K_USER_LAST_ACTIVITY.format(bot_id=record.id, user_id=user_id)

        async def _delayed_cleanup() -> None:
            import time

            if not container.redis:
                return
            # Check if user has been active recently — extend grace if so.
            raw = await container.redis.get(grace_key)
            if raw:
                try:
                    last_act = int(raw)
                    now = int(time.time())
                    elapsed = now - last_act
                    # If they interacted within the last half-retention period,
                    # reschedule cleanup instead of deleting.
                    if elapsed < half_retention:
                        extend = half_retention + (half_retention - elapsed)
                        new_when = datetime.now(timezone.utc) + timedelta(seconds=extend)
                        scheduler.at(
                            new_when,
                            _delayed_cleanup,
                            id=f"dist-cleanup-{record.id}-{chat_id}-{sent_ids[0]}",
                        )
                        return
                except (ValueError, TypeError):
                    pass

            # Delete the delivered posts.
            try:
                await client.delete_messages(chat_id, sent_ids)
            except Exception:
                pass

        when = datetime.now(timezone.utc) + timedelta(seconds=retention_secs)
        scheduler.at(
            when,
            _delayed_cleanup,
            id=f"dist-cleanup-{record.id}-{chat_id}-{sent_ids[0]}",
        )

    # ── /start ──────────────────────────────────────────────────────────────────

    @client.on_message(filters.command("start"))
    async def _start(_: Client, message: Message) -> None:
        start_sticker = await client.send_sticker(
            chat_id=message.chat.id, sticker=ui_cfg.start_sticker_id
        )

        msg = await message.reply(
            "<b>connecting!</b>", parse_mode=ParseMode.HTML
        )
        await staged_loading(
            msg,
            ["connecting", "checking access", "preparing"],
            delay_per_stage=ui_cfg.loading_dot_delay * 3,
        )

        await asyncio.sleep(ui_cfg.sticker_delete_delay)
        await start_sticker.delete()
        await msg.delete()

        if not await _passes_force_sub(message):
            return

        parts = (message.text or "").split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""

        if payload.startswith("token_"):
            await _redeem(message, payload[len("token_"):])
        if not await _ensure_access(message):
            return

        # Track activity for retention grace period.
        await _track_activity(message.from_user.id)

        # Revision-aware redelivery: if a returning user has stale posts
        # (their last delivery was at an older content_revision than what's
        # live now — typically because this title was re-published and the bot
        # regenerated its watch guide / info card / season cards since),
        # delete the old messages from their chat and send the new pack.
        prior = await _load_my_delivery(message.from_user.id)
        current_revision = await _get_content_revision()
        if prior is not None and (prior.delivered_revision or 0) < current_revision:
            await _delete_prior_delivery(prior)
            log.info(
                "dist.redelivery.refresh",
                user_id=message.from_user.id,
                prior_revision=prior.delivered_revision,
                current_revision=current_revision,
            )

        # Deliver stored content posts.
        sent_ids, pinned_id = await _send_posts(message.chat.id)
        if sent_ids:
            await _save_my_delivery(
                message.from_user.id, message.chat.id, sent_ids,
                current_revision, pinned_id,
            )
            await _schedule_cleanup(message.chat.id, message.from_user.id, sent_ids)

    @client.on_message(filters.command("help"))
    async def _help(_: Client, message: Message) -> None:
        await message.reply(
            f"{bq('<b>how it works</b>')}\n\n"
            f"{bqx('<b>/start</b> — browse the library or open a title\n'
                   '<b>pick</b> a season > resolution > language\n'
                   '<b>tap</b> get season package to receive your files')}",
            parse_mode=ParseMode.HTML,
        )

    async def _bot_username(self_message: Message) -> str | None:
        if record.username:
            return record.username
        try:
            me = await client.get_me()
            return me.username
        except Exception:
            return None

    # ── access ──────────────────────────────────────────────────────────────────

    async def _ensure_access(message: Message) -> bool:
        from nekofetch.services.access_service import AccessService

        status = await AccessService(container).ensure_and_check(
            message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        if status.has_access:
            return True
        await message.reply(
            f"{bq('<b>access required</b>')}\n\n"
            f"{bq('your access has expired. tap below to get a new access token.')}",
            reply_markup=keyboard([("get access", cb("acc", "get"))]),
            parse_mode=ParseMode.HTML,
        )
        return False

    async def _redeem(message: Message, token: str) -> None:
        from nekofetch.services.access_service import AccessService

        try:
            until = await AccessService(container).redeem(token, message.from_user.id)
            from nekofetch.core.timefmt import to_display
            await message.reply(
                bq(f"access granted until <code>{to_display(until)}</code>."),
                parse_mode=ParseMode.HTML,
            )
        except NekoFetchError as exc:
            await message.reply(
                bq(container.localizer.get(exc.message_key)),
                parse_mode=ParseMode.HTML,
            )

    @client.on_callback_query(filters.regex(r"^acc\|get"))
    async def _get_access(_: Client, q: CallbackQuery) -> None:
        from nekofetch.services.access_service import AccessService

        await q.answer()
        username = await _bot_username(q.message)
        if not username:
            await q.message.reply(
                bq("couldn't build an access link right now. try again later."),
                parse_mode=ParseMode.HTML,
            )
            return
        url = await AccessService(container).generate_token(q.from_user.id, bot_username=username)
        days = container.config.access.token_days
        await q.message.reply(
            f"{bq(f'<b>get {days} days access</b>')}\n\n"
f"{bq(f'complete this link, then you\'ll return to the bot '
     f'with access unlocked:\n{url}')}",
            parse_mode=ParseMode.HTML,
        )

    # ── force sub ───────────────────────────────────────────────────────────────

    async def _passes_force_sub(message: Message) -> bool:
        from nekofetch.bots.force_sub import channels_to_join, join_keyboard

        pending = await channels_to_join(
            client, container, message.from_user.id, dist=True
        )
        if not pending:
            return True
        join_msg = "please join the channel(s) below, then tap i ve joined."
        await message.reply(
            f"{bq('<b>join required</b>')}\n\n"
            f"{bq(join_msg)}",
            reply_markup=join_keyboard(pending, retry_callback="fsub|retry"),
            parse_mode=ParseMode.HTML,
        )
        return False

    @client.on_callback_query(filters.regex(r"^fsub\|retry"))
    async def _fsub_retry(_: Client, q: CallbackQuery) -> None:
        from nekofetch.bots.force_sub import channels_to_join

        pending = await channels_to_join(client, container, q.from_user.id, dist=True)
        if pending:
            await q.answer(container.localizer.get(M.DIST_NOT_SUBSCRIBED), show_alert=True)
            return
        await q.answer(container.localizer.get(M.DIST_SUBSCRIBED_THANKS))
        await q.message.delete()
        # Re-send posts after force-sub is resolved — same revision check as /start.
        prior = await _load_my_delivery(q.from_user.id)
        current_revision = await _get_content_revision()
        if prior is not None and (prior.delivered_revision or 0) < current_revision:
            await _delete_prior_delivery(prior)
        sent_ids, pinned_id = await _send_posts(q.message.chat.id)
        if sent_ids:
            await _save_my_delivery(
                q.from_user.id, q.message.chat.id, sent_ids,
                current_revision, pinned_id,
            )
            await _schedule_cleanup(q.message.chat.id, q.from_user.id, sent_ids)

    # ── file delivery — all quality buttons are now URL buttons with pre-generated
    #    Fstore links stored in button_data.links. No dynamic handler needed.
    #    The ``d|nolink`` handler for language-header callbacks is kept separately.

    @client.on_callback_query(filters.regex(r"^d\|nolink"))
    async def _nolink(_: Client, q: CallbackQuery) -> None:
        # A language header isn't a link — tapping it previews the instruction to
        # pick a quality from the row beneath it.
        from nekofetch.localization.messages import M as _M, t as _t
        await q.answer(_t(_M.BOT_CHOOSE_QUALITY), show_alert=True)

    return client
