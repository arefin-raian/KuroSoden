"""Shared bot UI helpers — sticker → staged-loading → welcome-screen pattern.

Used by all four Kuro Sōden pipeline bots so their /start feels consistent
and NekoFetch-rich. Centralises the start-flow so keyboard layouts,
delays, artwork char-pools, and failure modes stay uniform — when a bot
crashes on its sticker or artwork, it doesn't surface a scary traceback
to the user.

Single entry point: :func:`send_rich_welcome`.

Pattern (mirrors NekoFetch's admin bot at
``nekofetch.bots.admin.handlers.start``):

    1. ``send_sticker`` with ``ui_cfg.start_sticker_id`` (best-effort)
    2. reply ``🎬 loading…`` placeholder
    3. ``staged_loading`` animates it through Connecting / Loading / Verifying
    4. ``sticker_delete_delay`` pause (≈1.5s)
    5. delete sticker + loading message
    6. ``send_screen(client, chat_id, screen)`` with the bot's pre-built
       Screen (caption + per-character artwork via ``pick_artwork(bot_name)``
       + inline keyboard).

Everything is wrapped in try/except so a single broken asset (sticker
file_id, image fetch, network blip) cannot abort the user's first
interaction.
"""

from __future__ import annotations

import asyncio

from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, Message

from nekofetch.core.container import Container
from nekofetch.ui.artwork import pick_artwork
from nekofetch.ui.screens import Screen, send_screen


async def send_rich_welcome(
    client: Client,
    container: Container,
    message: Message,
    screen: Screen,
    *,
    bot_name: str | None = None,
) -> None:
    """Theatrical /start: sticker → loading animation → welcome screen.

    Args:
        client: Pyrogram client (the bot).
        container: Kage container — pulls ``UIConfig`` for sticker id,
            sticker-delete delay, and loading-animation timing.
        message: The inbound ``/start`` message.
        screen: Pre-built :class:`nekofetch.ui.screens.Screen` holding the
            caption, bot-specific image (``pick_artwork(bot_name)``), and
            inline keyboard. The helper does NOT modify it; caller owns it.
        bot_name: Persona name (lelouch/levi/senku/gojo). Selects that bot's
            own /start sticker via ``UIConfig.sticker_for``; falls back to the
            shared ``start_sticker_id`` when the bot has none configured.
    """
    from pyrogram.enums import ParseMode

    ui_cfg = container.config.ui
    chat_id = message.chat.id

    # ── 1. Sticker (best-effort; some deployments lack a sticker id) ──
    sticker = None
    sticker_id = ui_cfg.sticker_for(bot_name)
    if sticker_id:
        try:
            sticker = await client.send_sticker(chat_id, sticker_id)
        except Exception:
            sticker = None

    # ── 2. Loading placeholder — pure symbol animation, no words ──────
    # A quick "typing rhythm" that reads as anticipation and clears the
    # instant verification is done. No "connecting/loading/verifying" text —
    # it never took that long anyway, so the words just looked slow.
    msg = await message.reply("·", parse_mode=ParseMode.HTML)

    # ── 3. Symbol beat animation (fast; ~0.9s total) ───────────────────
    _FRAMES = ("· ·", "· · ·", "!", "! !", "?", "✦")
    try:
        for frame in _FRAMES:
            await asyncio.sleep(0.15)
            try:
                await msg.edit_text(frame)
            except Exception:
                # Ignore "message not modified" / transient edit hiccups.
                pass
    except Exception:
        await asyncio.sleep(0.3)

    # ── 4. Cleanup intermediate messages (sequential so no race) ──────
    if sticker is not None:
        try:
            await sticker.delete()
        except Exception:
            pass
    try:
        await msg.delete()
    except Exception:
        pass

    # ── 5. The actual welcome screen ────────────────────────────────────
    await send_screen(client, chat_id, screen)


async def reply_with_screen(
    client: Client,
    chat_id: int,
    caption: str,
    *,
    bot_name: str,
    keyboard: InlineKeyboardMarkup | None = None,
    old_msg: Message | None = None,
) -> Message:
    """Reply (or replace a previous message) with a character-image Screen.

    Centralises the ``every reply carries the bot's recurring artwork``
    pattern. Calls ``pick_artwork(bot_name)`` (no back-to-back repeats
    handled per-bot in ``nekofetch.ui.artwork._pools``) and falls back
    to the shared default pool when the character directory is empty.

    Use this instead of ``message.reply_text(...)`` for any reply that
    should look like a NekoFetch card -- ``/help``, ``/myrequests``,
    fallback alerts, brief admin panels, etc. ``old_msg`` lets the
    helper replace an existing message in place so the user sees the
    new screen on the same bubble as the navigation button.
    """
    screen = Screen(
        caption=caption,
        image=pick_artwork(bot_name),
        keyboard=keyboard,
    )
    return await send_screen(client, chat_id, screen, old_msg=old_msg)
