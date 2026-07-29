from __future__ import annotations

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.ui.progress import loading_animation
from nekofetch.ui.typography import bq

log = get_logger(__name__)

_NOT_MEMBER = {"LEFT", "BANNED", "RESTRICTED"}


async def channels_to_join(
    client: Client, container: Container, user_id: int, *, dist: bool = False
) -> list[tuple[str, str | None]]:
    """Return the channels this user still needs to join.

    Set ``dist=True`` to use the distribution-bot-specific force-sub settings;
    otherwise the NekoFetch admin bot settings are used.
    """
    sec = container.config.security
    if dist:
        if not sec.dist_force_subscribe or not sec.dist_force_subscribe_channels:
            return []
        channels = sec.dist_force_subscribe_channels
    else:
        if not sec.force_subscribe or not sec.force_subscribe_channels:
            return []
        channels = sec.force_subscribe_channels

    missing: list[int] = []
    for channel in channels:
        try:
            member = await client.get_chat_member(channel, user_id)
            status = getattr(member.status, "name", str(member.status)).upper()
            if status in _NOT_MEMBER:
                missing.append(channel)
        except UserNotParticipant:
            # Genuinely not a member — this is what force-sub is meant to catch.
            missing.append(channel)
        except Exception:
            # The bot itself can't query this chat (not an admin there, wrong id,
            # channel private/deleted). That's an operator misconfig, not the
            # user's doing — fail OPEN and skip it, otherwise a single bad channel
            # permanently locks EVERY user out of the bot with no way to pass.
            log.warning("force_sub.query_failed", channel=channel)
            continue

    out: list[tuple[str, str | None]] = []
    for channel in missing:
        try:
            chat = await client.get_chat(channel)
            url = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else None)
            out.append((chat.title or str(channel), url))
        except Exception:
            out.append((str(channel), None))
    return out


def join_keyboard(channels: list[tuple[str, str | None]], retry_callback: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"➜ join {title}", url=url)]
        for title, url in channels
        if url
    ]
    rows.append([InlineKeyboardButton("✓ i've joined", callback_data=retry_callback)])
    return InlineKeyboardMarkup(rows)


async def check_with_animation(
    client: Client, container: Container, message: Message
) -> list[tuple[str, str | None]]:
    msg = await message.reply(
        "<b>checking subscription!</b>", parse_mode=ParseMode.HTML
    )
    await loading_animation(msg, "checking subscription")
    pending = await channels_to_join(client, container, message.from_user.id)
    if not pending:
        await msg.edit_text(
            bq("<b>🔒 subscription status: passed</b>"),
            parse_mode=ParseMode.HTML,
        )
    return pending
