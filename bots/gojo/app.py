"""Gojo — Publisher Bot (最強の術師 · The Strongest Sorcerer).

Handles:
  • Main channel post generation.
  • Franchise thumbnail generation.
  • TMDB synopsis (not AniList) for descriptions.
  • Caption template → admin review → Markdown/HTML edit.
  • Publish (immediate or scheduled).
  • Index update.
  • Channel recovery tools (banned → replace → update all buttons).

Reuses NekoFetch's PublishingService, MainChannelService, IndexChannelService.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import BotCommand, Message

from nekofetch.core.constants import BULLET
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from kurosoden.shared.ui_helpers import reply_with_screen
from nekofetch.ui.artwork import pick_artwork

GOJO_COMMANDS = [
    BotCommand("start", "View your assigned publishing tasks"),
    BotCommand("tasks", "List active publishing tasks"),
    BotCommand("publish", "Review and publish: /publish REQ-XXXX"),
    BotCommand("recover", "Recover a banned channel: /recover REQ-XXXX"),
    BotCommand("schedule", "Schedule a post for later"),
    BotCommand("updates", "Sweep the catalog for new franchise entries"),
    BotCommand("bancheck", "Probe every channel for bans"),
    BotCommand("settings", "Configure the publisher bot"),
    BotCommand("help", "How publishing works"),
]

log = get_logger(__name__)

# ── Home menu: SINGLE source of truth ────────────────────────────────────────
# Both /start and the `gojo|home` callback render from this so the two menus can
# never drift (the old bug: /start was missing Index / Change Index / Stats that
# only appeared after bouncing through Settings).
_HOME_CAPTION = (
    "<b>🔮 Gojo Satoru — Publisher</b>\n\n"
    "<i>\"Throughout heaven and earth, I alone am the honored one.\"</i>\n\n"
    "I handle the final step:\n"
    "• Generate main channel posts\n"
    "• Create franchise thumbnails\n"
    "• Review and edit captions\n"
    "• Publish or schedule\n"
    "• Update the index\n"
    "• Recover banned channels"
)


def _home_rows(container: Container, obj) -> list[list[tuple[str, str]]]:
    """The Gojo home keyboard as (label, callback) rows. ``obj`` is a Message or
    CallbackQuery so the owner-only Settings row is gated identically for both
    the /start message and the home callback."""
    from nekofetch.ui.components import cb
    from kurosoden.shared.access_gate import is_owner

    rows = [
        [("📋 Tasks", cb("gojo", "tasks")),
         ("🔮 Publish", cb("gojo", "publish"))],
        [("📅 Schedule", cb("gojo", "schedule")),
         ("🛡 Recover", cb("gojo", "recover"))],
        [("💾 Backup", cb("gojo", "backup")),
         ("📡 Change Main", cb("gojo", "change_main"))],
        [("🔎 Check Updates", cb("gojo", "check_updates")),
         ("🩺 Ban Check", cb("gojo", "check_banned"))],
        [("🗂 Index", cb("gojo", "index")),
         ("🆕 Change Index", cb("gojo", "change_index"))],
        [("📊 Stats", cb("gojo", "stats")),
         ("✏️ Edit Footer", cb("gojo", "edit_footer"))],
        [("⚙️ Settings", cb("gojo", "settings")),
         ("❓ Help", cb("gojo", "help"))],
    ]
    if not is_owner(container, obj):
        # Drop only the owner-only Settings *button*, keeping its row-mate (Help).
        rows = [[btn for btn in row if "settings" not in btn[1]] for row in rows]
        rows = [row for row in rows if row]
    return rows


async def publish_commands(client: Client) -> None:
    # Keep owner-only commands out of the global menu while seeding the owner's
    # chat scope at startup, avoiding a stale/blank owner menu.
    from kurosoden.shared.command_menu import (
        default_commands, publish_owner_commands,
    )
    await client.set_bot_commands(default_commands("gojo"))
    await publish_owner_commands(client, getattr(client, "container", None), "gojo")


def build_gojo(container: Container, token: str) -> Client:
    """Build and wire the Gojo (Publisher) bot client.

    All publish/recover/schedule handlers are registered via ``register_all``.
    """
    client = Client(
        name="kurosoden-gojo",
        api_id=container.env.telegram_api_id,
        api_hash=container.env.telegram_api_hash,
        bot_token=token,
        workdir=str(container.env.session_path),
        max_concurrent_transmissions=4,  # parallel image/card sends
    )
    client.container = container

    from kurosoden.bots.gojo.handlers import register_all
    register_all(client, container)

    # ── Catch-all menu callback ─────────────────────────────────────────────
    # Inline buttons on /start route to `gojo|<action>`. The dispatcher below
    # maps every action to a real screen — no more "Type /X in chat" toasts.
    from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                                InlineKeyboardMarkup)
    from kurosoden.shared.menu_router import tool_screen
    from nekofetch.ui.components import cb
    from nekofetch.ui.screens import Screen, send_screen

    @client.on_callback_query(filters.regex(r"^gojo\|"))
    async def _gojo_menu_fallback(client: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        parts = q.data.split("|", 2)
        action = parts[1] if len(parts) > 1 else "home"
        arg = parts[2] if len(parts) > 2 else ""
        bot = "gojo"

        # ¬¬ Home ¬¬
        if action == "home":
            from nekofetch.ui.components import keyboard as _kb

            await send_screen(
                client, q.message.chat.id,
                Screen(caption=_HOME_CAPTION, image=pick_artwork(bot),
                       keyboard=_kb(*_home_rows(container, q))),
                old_msg=q.message)
            await q.answer()
            return

        # ¬¬ Tool panels ¬¬
        # NOTE: "schedule" is intentionally NOT here — the real schedule view
        # (matching the /schedule command, with a Back button) is registered in
        # handlers/schedule.py under `gojo|schedule`, which runs before this
        # fallback and owns that tap.
        if action in ("tasks", "publish", "recover"):
            titles = {"tasks": "📋 Your Publishing Tasks",
                      "publish": "🔮 Publish Review",
                      "recover": "🛡 Channel Recovery"}
            body_map = {
                "tasks": [
                    "Everything waiting on you to publish, newest first.",
                    "",
                    f"  {BULLET} Each card shows the anime, its code, and where it is in the pipeline.",
                    f"  {BULLET} Tap a task to review and publish it.",
                    "<blockquote>Everything here already has a finished content pack.</blockquote>",
                ],
                "publish": [
                    "Review a finished title and send it to the main channel.",
                    "",
                    f"  {BULLET} Preview the caption before anything goes live.",
                    f"  {BULLET} Publish now or tweak the text first — your call.",
                    "<blockquote>Publishing commits to the main channel and updates the index.</blockquote>",
                ],
                "recover": [
                    "Rebuild a distribution channel that got banned.",
                    "",
                    f"  {BULLET} The banned channel is detected and replaced automatically.",
                    f"  {BULLET} Every button in the main and index channels is repointed.",
                    "<blockquote>You don't rebuild anything by hand — open the affected task.</blockquote>",
                ],
            }
            caption, keyboard = tool_screen(
                bot, title=titles[action],
                kicker="Everything here runs on taps — no commands to memorize.",
                lines=body_map[action],
                back="home",
            )
            await send_screen(client, q.message.chat.id,
                              Screen(caption=caption, image=pick_artwork(bot),
                                     keyboard=keyboard), old_msg=q.message)
            await q.answer()
            return

        # ¬¬ Settings ¬¬
        # The human-friendly settings surface (hub → section → live editor) is
        # registered in handlers/register_all under the `gojo|set|…` namespace and
        # the `gojo|settings` alias, before this fallback, so it owns every
        # settings tap. Nothing settings-related is handled here anymore.

        # ── Help ──
        if action == "help":
            caption = (
                "<b>🔮 Gojo — Publisher · Help</b>\n\n"
                "<b>How publishing works</b>\n"
                "1. Open <b>📋 Tasks</b> to see what's ready to publish\n"
                "2. <b>🔮 Publish</b> a task — review the caption, then post\n"
                "3. <b>📅 Schedule</b> it for later, or publish now\n"
                "4. <b>🛡 Recover</b> rebuilds a banned channel and fixes every button\n\n"
                "<i>Everything here is button-driven — no commands required.</i>"
            )
            await send_screen(
                client, q.message.chat.id,
                Screen(caption=caption, image=pick_artwork(bot),
                       keyboard=InlineKeyboardMarkup(
                           [[InlineKeyboardButton("◀ Back", callback_data=cb(bot, "home"))]])),
                old_msg=q.message)
            await q.answer()
            return

        await q.answer(f"Action “{action}” isn't available here.", show_alert=True)

    # ── /start ────────────────────────────────────────────────────────────────
    # Rich UI: sticker → loading animation → welcome screen with inline keyboard
    # and Gojo-themed artwork (images/gojo/).
    @client.on_message(filters.command("start"))
    async def _start(_: Client, message: Message) -> None:
        from nekofetch.ui.screens import Screen
        from nekofetch.ui.components import keyboard
        from nekofetch.ui.artwork import pick_artwork
        from kurosoden.shared.ui_helpers import send_rich_welcome
        from kurosoden.shared.command_menu import apply_for_user

        if message.from_user:
            await apply_for_user(client, container, "gojo",
                                 message.from_user.id, getattr(message, "nf_user", None))

        # Same source of truth as the `gojo|home` callback so the two menus can
        # never diverge (Index / Change Index / Stats used to be missing here).
        screen = Screen(
            caption=_HOME_CAPTION,
            image=pick_artwork("gojo"),
            keyboard=keyboard(*_home_rows(container, message)),
        )
        await send_rich_welcome(client, container, message, screen, bot_name="gojo")

    # ── /settings ── handled by the shared human-friendly settings engine
    # (register_settings in handlers/__init__.py) under the gojo|set|… namespace.

    return client
