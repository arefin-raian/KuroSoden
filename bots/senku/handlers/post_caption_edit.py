"""Senku post-caption editor — edit captions of published distribution posts.

Staff pick a published channel, then one of its posts (info card / season card /
movie card / watch guide / footer), send the replacement caption (HTML or
Markdown), and the editor:

  1. edits the LIVE Telegram message in the distribution channel,
  2. updates the matching ``BotContentPost`` row (so a ban-restore ships the new
     caption) and bumps ``content_revision`` (returning /start users get the
     new text),
  3. refreshes the wipe-proof channel backup.

The flow is restart-safe via the same ``channel_reply`` arm/peek/disarm pattern
the other editors use; the chat-scoped lock prevents two staff from editing the
same chat at once.
"""

from __future__ import annotations

import html

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import CallbackQuery, Message
from sqlalchemy import select

from nekofetch.bots.channel_reply import arm as arm_reply
from nekofetch.bots.channel_reply import disarm as disarm_reply
from nekofetch.bots.channel_reply import peek as peek_reply
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    ChannelLayout,
    DistributionBot,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.ui.components import cb, keyboard
from nekofetch.ui.screens import Screen, send_screen
from nekofetch.services.bot_render import build_audio_keyboard
from kurosoden.shared.access_gate import is_staff

log = get_logger(__name__)

_STATE = "senku_post_edit"
_LOCK_PREFIX = "nf:senku:post_edit_lock:"

# Telegram's hard message-text limit (captions are shared with the photo).
_TEXT_LIMIT = 4096

# Post kinds that carry user-editable captions (dividers are decorative stickers).
_EDITABLE_KINDS = {"info_card", "season_card", "movie_card", "watch_guide", "footer"}


def _kind_label(kind: str) -> str:
    return {
        "info_card": "Info card",
        "season_card": "Season card",
        "movie_card": "Movie card",
        "watch_guide": "Watch guide",
        "footer": "Footer",
    }.get(kind, kind.replace("_", " ").title())


async def _published_channels(container: Container) -> list[DistributionBot]:
    """Distribution channels that have published content (a layout or posts)."""
    async with session_scope(container.pg_sessionmaker) as session:
        rows = (
            await session.execute(
                select(DistributionBot).where(
                    DistributionBot.is_channel.is_(True),
                    DistributionBot.enabled.is_(True),
                    DistributionBot.chat_id.is_not(None),
                ).order_by(DistributionBot.name)
            )
        ).scalars().all()
        out = []
        for row in rows:
            has_layout = (
                await session.execute(
                    select(ChannelLayout.id).where(
                        ChannelLayout.channel_bot_id == row.id
                    ).limit(1)
                )
            ).first() is not None
            if has_layout:
                session.expunge(row)
                out.append(row)
        return out


async def _posts_for(container: Container, bot_id: int) -> list[dict]:
    """Editable posts of one channel: kind, message id, caption preview."""
    async with session_scope(container.pg_sessionmaker) as session:
        rows = (
            await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot_id,
                ).order_by(BotContentPost.order)
            )
        ).scalars().all()
        # Prefer the durable content rows (they carry the caption + message id).
        posts: list[dict] = []
        for r in rows:
            if r.post_type not in _EDITABLE_KINDS or not r.tg_message_id:
                continue
            posts.append({
                "bot_id": bot_id,
                "kind": r.post_type,
                "tg_message_id": int(r.tg_message_id),
                "caption": r.caption or "",
                "button_data": r.button_data,
            })
        if posts:
            return posts
        # Fallback: layout-only channel (message ids exist, content rows don't).
        layout = (
            await session.execute(
                select(ChannelLayout).where(
                    ChannelLayout.channel_bot_id == bot_id,
                ).order_by(ChannelLayout.seq)
            )
        ).scalars().all()
        return [
            {"bot_id": bot_id, "kind": item.kind,
             "tg_message_id": int(item.tg_message_id), "caption": "",
             "button_data": None}
            for item in layout
            if item.tg_message_id and item.kind in _EDITABLE_KINDS
        ]


def _channel_screen(channels: list[DistributionBot]) -> tuple[str, list[list[tuple[str, str]]]]:
    lines = ["<b>📝 Edit published post captions</b>", "",
             "Choose a channel; its posts will be listed next."]
    rows: list[list[tuple[str, str]]] = []
    if not channels:
        lines.append("<i>No published distribution channels found yet.</i>")
    for ch in channels:
        label = (ch.name or ch.anime_doc_id or f"channel {ch.chat_id}")[:48]
        rows.append([(label, cb("senku", "capedit", "posts", str(ch.id)))])
    rows.append([("⬅ Back", cb("senku", "home"))])
    return "\n".join(lines), rows


def _posts_screen(title: str, posts: list[dict]) -> tuple[str, list[list[tuple[str, str]]]]:
    lines = [
        f"<b>📝 Posts — {html.escape(title[:60])}</b>", "",
        "Pick the post whose caption you want to change.",
    ]
    rows: list[list[tuple[str, str]]] = []
    for p in posts:
        preview = html.escape((p["caption"] or "")[:40]) or "<i>(no caption)</i>"
        label = f"{_kind_label(p['kind'])}"
        lines.append(f"• <b>{label}</b> — {preview}")
        rows.append([
            ("📝 Caption", cb("senku", "capedit", "edit",
                                  str(p.get("bot_id", "")), str(p["tg_message_id"]))),
            ("🔘 Buttons", cb("senku", "capedit", "buttons",
                                  str(p.get("bot_id", "")), str(p["tg_message_id"]))),
        ])
    rows.append([("⬅ Channels", cb("senku", "capedit", "channels"))])
    return "\n".join(lines), rows


async def show_channel_list(
    client: Client, container: Container, target: Message,
) -> None:
    """Open the caption editor from /editcaption or the home menu button."""
    channels = await _published_channels(container)
    caption, rows = _channel_screen(channels)
    await send_screen(
        client, target.chat.id,
        Screen(caption=caption, keyboard=keyboard(*rows)),
        old_msg=target,
    )


def _parse_button_lines(raw: str) -> dict | None:
    """Parse the operator's compact button format into durable button_data.

    One button per line: ``Label | https://example``.  An empty message or
    ``none`` intentionally removes all buttons.  We store the structured
    payload, rather than a Telegram markup object, so restore/re-render paths
    can use the same shared keyboard builder.
    """
    value = (raw or "").strip()
    if not value or value.casefold() in {"none", "clear", "remove"}:
        return {"type": "custom", "buttons": []}
    buttons: list[dict[str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            raise ValueError("Use one `Button label | https://link` per line.")
        label, url = (part.strip() for part in line.split("|", 1))
        if not label or not url.startswith(("http://", "https://", "tg://")):
            raise ValueError("Each button needs a label and an http(s) URL.")
        buttons.append({"text": label[:64], "url": url})
    if not buttons:
        return {"type": "custom", "buttons": []}
    return {"type": "custom", "buttons": buttons}


async def _edit_caption(
    client: Client, container: Container, message: Message,
    *, bot_id: int, tg_message_id: int, new_caption: str,
) -> tuple[bool, str]:
    """Apply a caption edit without claiming success when Telegram rejects it."""
    if len(new_caption) > _TEXT_LIMIT:
        return False, f"Caption is too long ({len(new_caption)} > {_TEXT_LIMIT} chars)."

    async with session_scope(container.pg_sessionmaker) as session:
        bot = await session.get(DistributionBot, bot_id)
        if bot is None or not bot.chat_id:
            return False, "channel no longer exists"
        chat_id = int(bot.chat_id)
        anime_doc_id = bot.anime_doc_id
        has_content = (await session.execute(
            select(BotContentPost.id).where(
                BotContentPost.bot_id == bot_id,
                BotContentPost.tg_message_id == tg_message_id,
            ).limit(1)
        )).first() is not None

    # Telegram is the live source of truth. Persist the DB copy only after this
    # succeeds; otherwise a failed live edit would make restores silently ship a
    # caption that subscribers never saw.
    try:
        live = await client.get_messages(chat_id, tg_message_id)
        if any(getattr(live, kind, None)
               for kind in ("photo", "video", "animation", "document")):
            await client.edit_message_caption(
                chat_id, tg_message_id, new_caption, parse_mode=ParseMode.HTML,
            )
        else:
            await client.edit_message_text(
                chat_id, tg_message_id, new_caption, parse_mode=ParseMode.HTML,
            )
    except Exception as exc:  # noqa: BLE001 — keep durable data unchanged
        log.warning("senku.caption.live_edit_failed", bot=bot_id,
                    mid=tg_message_id, error=str(exc))
        return False, "Telegram rejected the live edit; the database was left unchanged."

    try:
        async with session_scope(container.pg_sessionmaker) as session:
            rows = (await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot_id,
                    BotContentPost.tg_message_id == tg_message_id,
                )
            )).scalars().all()
            for row in rows:
                row.caption = new_caption
            if rows:
                bot = await session.get(DistributionBot, bot_id)
                if bot is not None:
                    bot.content_revision = (bot.content_revision or 0) + 1
    except Exception as exc:  # noqa: BLE001 — live edit succeeded, DB retry is needed
        log.error("senku.caption.database_update_failed", bot=bot_id,
                  mid=tg_message_id, error=str(exc))
        return False, "Telegram was updated, but the database update failed; retry the save."

    try:
        from nekofetch.services.backup_service import BackupService

        if anime_doc_id:
            await BackupService(container).record_distribution_channel(anime_doc_id)
    except Exception as exc:  # noqa: BLE001 — live + DB edits remain authoritative
        log.warning("senku.caption.backup_failed", bot=bot_id, error=str(exc))

    log.info("senku.caption.edited", bot=bot_id, mid=tg_message_id,
             durable=has_content)
    return True, "Caption updated in the channel and the database."


async def _edit_buttons(
    client: Client, container: Container,
    *, bot_id: int, tg_message_id: int, button_data: dict,
) -> tuple[bool, str]:
    """Replace one post's buttons without claiming success on live-edit failure."""
    async with session_scope(container.pg_sessionmaker) as session:
        bot = await session.get(DistributionBot, bot_id)
        if bot is None or not bot.chat_id:
            return False, "channel no longer exists"
        chat_id = int(bot.chat_id)
        anime_doc_id = bot.anime_doc_id
        row = (await session.execute(
            select(BotContentPost).where(
                BotContentPost.bot_id == bot_id,
                BotContentPost.tg_message_id == tg_message_id,
            )
        )).scalars().first()
        if row is None:
            return False, "post content is no longer available"

    try:
        markup = build_audio_keyboard(button_data, container.config.post_format)
        await client.edit_message_reply_markup(
            chat_id, tg_message_id, reply_markup=markup,
        )
    except Exception as exc:  # noqa: BLE001 — keep durable data unchanged
        log.warning("senku.buttons.live_edit_failed", bot=bot_id,
                    mid=tg_message_id, error=str(exc))
        return False, "Telegram rejected the live button edit; the database was left unchanged."

    try:
        async with session_scope(container.pg_sessionmaker) as session:
            row = (await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot_id,
                    BotContentPost.tg_message_id == tg_message_id,
                )
            )).scalars().first()
            if row is None:
                return False, "post content disappeared before it could be saved"
            row.button_data = button_data
            bot = await session.get(DistributionBot, bot_id)
            if bot is not None:
                bot.content_revision = (bot.content_revision or 0) + 1
    except Exception as exc:  # noqa: BLE001 — live edit succeeded, DB retry is needed
        log.error("senku.buttons.database_update_failed", bot=bot_id,
                  mid=tg_message_id, error=str(exc))
        return False, "Telegram was updated, but the database update failed; retry the save."

    try:
        from nekofetch.services.backup_service import BackupService
        if anime_doc_id:
            await BackupService(container).record_distribution_channel(anime_doc_id)
    except Exception as exc:  # noqa: BLE001 — live + DB edits remain authoritative
        log.warning("senku.buttons.backup_failed", bot=bot_id, error=str(exc))
    return True, "Buttons updated in the channel and the database."


def register(client: Client, container: Container) -> None:
    """Register the /editpost command, its caption/button editor, and legacy alias."""

    async def _show_channels(target: Message) -> None:
        await show_channel_list(client, container, target)

    @client.on_message(filters.command(["editcaption", "editpost"]) & filters.private)
    async def _command(_: Client, message: Message) -> None:
        if not is_staff(message):
            return
        await _show_channels(message)

    @client.on_callback_query(filters.regex(r"^senku\|capedit\|"), group=2)
    async def _callback(_: Client, q: CallbackQuery) -> None:
        if q.message is None or not is_staff(q):
            await q.answer("Staff access required.", show_alert=True)
            return
        parts = (q.data or "").split("|")
        action = parts[2] if len(parts) > 2 else ""
        if action == "channels":
            await q.answer()
            await _show_channels(q.message)
            return
        if action == "posts":
            await q.answer()
            try:
                bot_id = int(parts[3])
            except (IndexError, ValueError):
                return
            async with session_scope(container.pg_sessionmaker) as session:
                bot = await session.get(DistributionBot, bot_id)
                title = (bot.name if bot else None) or str(bot_id)
            posts = await _posts_for(container, bot_id)
            if not posts:
                await q.answer("This channel has no editable posts.", show_alert=True)
                return
            caption, rows = _posts_screen(title, posts)
            await send_screen(
                client, q.message.chat.id,
                Screen(caption=caption, keyboard=keyboard(*rows)),
                old_msg=q.message,
            )
            return
        if action in {"edit", "buttons"}:
            if q.message.chat.type != ChatType.PRIVATE:
                await q.answer("Open Senku in a private chat to edit captions.",
                               show_alert=True)
                return
            try:
                bot_id = int(parts[3])
                tg_message_id = int(parts[4])
            except (IndexError, ValueError):
                await q.answer("Invalid post.", show_alert=True)
                return
            # The callback carries the channel id because Telegram message ids
            # are only unique within a chat, not across distribution channels.
            async with session_scope(container.pg_sessionmaker) as session:
                bot = await session.get(DistributionBot, bot_id)
                if bot is None:
                    await q.answer("Couldn't find that channel.", show_alert=True)
                    return
                owned = (
                    await session.execute(
                        select(ChannelLayout.id).where(
                            ChannelLayout.channel_bot_id == bot_id,
                            ChannelLayout.tg_message_id == tg_message_id,
                        ).limit(1)
                    )
                ).first()
                if owned is None:
                    owned = (
                        await session.execute(
                            select(BotContentPost.id).where(
                                BotContentPost.bot_id == bot_id,
                                BotContentPost.tg_message_id == tg_message_id,
                            ).limit(1)
                        )
                    ).first()
                if owned is None:
                    await q.answer("That post is no longer tracked.", show_alert=True)
                    return
            lock_key = f"{_LOCK_PREFIX}{q.message.chat.id}"
            if container.redis is None:
                await q.answer("Caption editing is temporarily unavailable.",
                               show_alert=True)
                return
            acquired = await container.redis.set(
                lock_key, str(tg_message_id), nx=True, ex=900,
            )
            if not acquired:
                await q.answer("Finish the current caption edit first.", show_alert=True)
                return
            mode = "buttons" if action == "buttons" else "caption"
            await arm_reply(
                container.redis, q.message.chat.id, _STATE,
                bot_id=bot_id, tg_message_id=tg_message_id, mode=mode,
            )
            await q.answer(
                "Send button lines." if mode == "buttons" else "Send the replacement caption.",
                show_alert=True,
            )
            try:
                prompt = (
                    "<b>Replace the buttons.</b>\n\n"
                    "Send one per line as <code>Label | https://link</code>.\n"
                    "Send <code>none</code> to remove them."
                    if mode == "buttons" else
                    "<b>Send the new caption.</b>\n\n"
                    "HTML or Markdown is allowed — it replaces the live post's "
                    "caption and is saved to the database."
                )
                await q.message.reply_text(prompt, parse_mode=ParseMode.HTML)
            except Exception as exc:  # noqa: BLE001 — prompt is best-effort
                log.debug("senku.caption.prompt_failed", error=str(exc))
            return
        await q.answer("Unknown caption-editor action.", show_alert=True)

    @client.on_message(
        filters.text & filters.private & ~filters.command(["start", "editcaption", "editpost"]),
        group=14,
    )
    async def _consume(_: Client, message: Message) -> None:
        redis = container.redis
        if redis is None or not message.from_user or not is_staff(message):
            return
        state, data = await peek_reply(redis, message.chat.id)
        if state != _STATE:
            return
        text = (message.text or "").strip()
        if not text:
            return
        lock_key = f"{_LOCK_PREFIX}{message.chat.id}"
        try:
            tg_message_id = int(data.get("tg_message_id"))
            locked = await redis.get(lock_key)
            if isinstance(locked, bytes):
                locked = locked.decode()
            if str(tg_message_id) != str(locked):
                return
            bot_id = int(data.get("bot_id"))
            mode = str(data.get("mode") or "caption")
        except (TypeError, ValueError):
            await disarm_reply(redis, message.chat.id)
            await redis.delete(lock_key)
            return
        await disarm_reply(redis, message.chat.id)
        await redis.delete(lock_key)

        if mode == "buttons":
            try:
                button_data = _parse_button_lines(text)
            except ValueError as exc:
                await message.reply_text(f"⚠️ {html.escape(str(exc))}")
                return
            ok, result = await _edit_buttons(
                client, container, bot_id=bot_id,
                tg_message_id=tg_message_id, button_data=button_data,
            )
        else:
            from kurosoden.shared.settings_ui import parse_user_markup
            new_caption = parse_user_markup(message)
            ok, result = await _edit_caption(
                client, container, message,
                bot_id=bot_id, tg_message_id=tg_message_id, new_caption=new_caption,
            )
        try:
            await message.reply_text(
                ("✅ " if ok else "⚠️ ") + result, parse_mode=ParseMode.HTML,
            )
            await message.delete()
        except Exception:  # noqa: BLE001 — cosmetic cleanup
            pass
        log.info("senku.caption.consumed", bot=bot_id, mid=tg_message_id,
                 admin=message.from_user.id, ok=ok)
