"""Senku — Distribution Bot (科学の使者 · The Science Messenger).

Handles:
  • Channel creation guidance for admins.
  • TMDB poster → profile picture prompt.
  • Auto-generate: info card, stickers, season separators, watch guide, footer.
  • Season thumbnail generation (posters, logos, layouts).
  • Per-bot settings panel.

Reuses NekoFetch's BotContentService + BotFactory for all content generation.
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

SENKU_COMMANDS = [
    BotCommand("start", "View your assigned distribution tasks"),
    BotCommand("tasks", "List active distribution tasks"),
    BotCommand("create", "Create a new distribution channel"),
    BotCommand("generate", "Generate content: /generate REQ-XXXX"),
    BotCommand("edit_thumbnail", "Edit saved thumbnails"),
    BotCommand("editpost", "Edit a published post"),
    BotCommand("settings", "Configure the distribution bot"),
    BotCommand("help", "How distribution works"),
]

log = get_logger(__name__)


async def publish_commands(client: Client) -> None:
    # Keep owner-only commands out of the global menu, but seed the configured
    # owner's chat scope at startup so /edit_thumbnail is visible immediately.
    from kurosoden.shared.command_menu import (
        default_commands, publish_owner_commands,
    )
    await client.set_bot_commands(default_commands("senku"))
    await publish_owner_commands(client, getattr(client, "container", None), "senku")


def build_senku(container: Container, token: str) -> Client:
    """Build and wire the Senku (Distribution) bot client.

    All task/generate/create handlers are registered via ``register_all``.
    """
    client = Client(
        name="kurosoden-senku",
        api_id=container.env.telegram_api_id,
        api_hash=container.env.telegram_api_hash,
        bot_token=token,
        workdir=str(container.env.session_path),
        max_concurrent_transmissions=4,  # parallel image/card sends
    )
    client.container = container

    from kurosoden.bots.senku.handlers import register_all
    register_all(client, container)

    # ── Catch-all menu callback ─────────────────────────────────────────────
    # Inline buttons on /start route to `senku|<action>`. The dispatcher below
    # maps every action to a real screen — no more "Type /X in chat" toasts.
    from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                                InlineKeyboardMarkup)
    from kurosoden.shared.menu_router import tool_screen
    from nekofetch.ui.components import cb
    from nekofetch.ui.screens import Screen, send_screen

    @client.on_callback_query(filters.regex(r"^senku\|"))
    async def _senku_menu_fallback(client: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        parts = q.data.split("|", 2)
        action = parts[1] if len(parts) > 1 else "home"
        arg = parts[2] if len(parts) > 2 else ""
        bot = "senku"

        # Delegated sub-namespaces own their own dedicated handlers in LATER
        # handler groups (post editor = group 2, thumbnail editor/regen = group
        # 3). Groups all fire, so without this guard the group-0 fallback would
        # ALSO answer these callbacks with the "not wired yet" alert before the
        # real handler runs. Yield silently and let the dedicated handler reply.
        if action in ("postedit", "thumbedit", "thumbregen"):
            return

        # ¬¬ Home ¬¬
        if action == "home":
            caption = (
                "<b>🧪 Senku Ishigami — Distribution</b>\n\n"
                "<i>\"Ten billion percent — this channel will be perfect.\"</i>\n\n"
                "I handle distribution:\n"
                "• Guide channel creation\n"
                "• Generate info cards & stickers\n"
                "• Create season separators & watch guides\n"
                "• Add footers and branding"
            )
            from kurosoden.shared.access_gate import is_owner

            rows = [
                [InlineKeyboardButton("📋 Tasks", callback_data=cb(bot, "tasks")),
                 InlineKeyboardButton("🧪 Generate", callback_data=cb(bot, "generate"))],
                [InlineKeyboardButton("📢 Create Channel", callback_data=cb(bot, "create"))],
                [InlineKeyboardButton("✏️ Edit Thumbnail", callback_data=cb(bot, "edit_thumbnail")),
                 InlineKeyboardButton("📝 Edit Post", callback_data=cb(bot, "edit_post"))],
                [InlineKeyboardButton("⚙️ Settings", callback_data=cb(bot, "settings"))],
            ]
            if not is_owner(container, q):
                # Staff keep the edit tools; only the owner-only settings row is hidden.
                rows = [
                    row for row in rows
                    if all(
                        "settings" not in (btn.callback_data or "")
                        for btn in row
                    )
                ]
            keyboard = InlineKeyboardMarkup(rows)
            await send_screen(client, q.message.chat.id,
                              Screen(caption=caption, image=pick_artwork(bot),
                                     keyboard=keyboard), old_msg=q.message)
            await q.answer()
            return

        # ¬¬ Tool panels ¬¬
        if action == "edit_thumbnail":
            from kurosoden.bots.senku.handlers.thumbnail_edit_senku import (
                _show_franchises,
            )
            from kurosoden.shared.access_gate import is_staff
            if not is_staff(q):
                await q.answer("Staff access required.", show_alert=True)
                return
            await q.answer()
            await _show_franchises(client, container, q, 0, old_msg=q.message)
            return

        if action == "edit_post":
            from kurosoden.shared.access_gate import is_staff
            if not is_staff(q):
                await q.answer("Staff access required.", show_alert=True)
                return
            from kurosoden.bots.senku.handlers.post_caption_edit import start_post_edit

            await q.answer()
            await start_post_edit(client, container, q.message)
            return

        if action in ("tasks", "create", "generate"):
            titles = {"tasks": "📋 Your Distribution Tasks",
                      "create": "📢 Create a Channel",
                      "generate": "🧪 Generate Channel Content"}
            body_map = {
                "tasks": [
                    "Everything waiting on you, newest first.",
                    "",
                    f"  {BULLET} Each card shows the anime, its code, and where it is in the pipeline.",
                    f"  {BULLET} Tap a task to open it and build its content.",
                    "<blockquote>Requests reach you automatically once downloading is done.</blockquote>",
                ],
                "create": [
                    "Spin up the distribution channel for a title.",
                    "",
                    f"  {BULLET} The channel and its profile picture are built for you.",
                    f"  {BULLET} Naming follows your branding template — override per-title if you want.",
                    "<blockquote>Open a task to create its channel — no codes to type.</blockquote>",
                ],
                "generate": [
                    "The full content pack for a channel.",
                    "",
                    f"  {BULLET} Info card, season separators, watch guide, footer, stickers.",
                    f"  {BULLET} Every piece is editable — review and approve before it locks.",
                    "<blockquote>Open a task to generate its pack.</blockquote>",
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
        # The human-friendly settings surface (hub → section → field → live
        # edit) lives in the shared engine (shared/settings_ui.py), registered
        # under `senku|set|…` and `senku|settings` in handlers/register_all
        # BEFORE this fallback, so it handles every settings tap here.

        # ── Help ──
        if action == "help":
            caption = (
                "<b>🧪 Senku — Distribution · Help</b>\n\n"
                "<b>How distribution works</b>\n"
                "1. Open <b>📋 Tasks</b> to see titles awaiting a content pack\n"
                "2. <b>📢 Create Channel</b> spins up the channel + profile picture\n"
                "3. <b>🧪 Generate</b> builds the info card, season separators,\n"
                "   watch guide, footer, and stickers — each editable\n"
                "4. Approve, and the publisher locks it in\n\n"
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

        await q.answer(f"Action “{action}” not wired yet.", show_alert=True)

    # ── /start ────────────────────────────────────────────────────────────────
    # Rich UI: sticker → loading animation → welcome screen with inline keyboard
    # and Senku-themed artwork (images/senku/).
    @client.on_message(filters.command("start"))
    async def _start(_: Client, message: Message) -> None:
        from nekofetch.ui.screens import Screen
        from nekofetch.ui.components import cb, keyboard
        from nekofetch.ui.artwork import pick_artwork
        from kurosoden.shared.ui_helpers import send_rich_welcome
        from kurosoden.shared.command_menu import apply_for_user

        if message.from_user:
            await apply_for_user(client, container, "senku",
                                 message.from_user.id, getattr(message, "nf_user", None))

        rows = [
            [("📋 Tasks", cb("senku", "tasks")),
             ("🧪 Generate", cb("senku", "generate"))],
            [("📢 Create Channel", cb("senku", "create"))],
            [("✏️ Edit Thumbnail", cb("senku", "edit_thumbnail")),
             ("📝 Edit Post", cb("senku", "edit_post"))],
            [("⚙️ Settings", cb("senku", "settings")),
             ("❓ Help", cb("senku", "help"))],
        ]
        from kurosoden.shared.access_gate import is_owner
        if not is_owner(container, message):
            # Staff keep the edit tools; only the owner-only settings row is hidden.
            rows = [
                row for row in rows
                if all(
                    "settings" not in data
                    for _label, data in row
                )
            ]
        screen = Screen(
            caption=(
                "<b>🧪 Senku Ishigami — Distribution</b>\n\n"
                "<i>\"Ten billion percent — this channel will be perfect.\"</i>\n\n"
                "I handle distribution:\n"
                "• Guide channel creation\n"
                "• Generate info cards & stickers\n"
                "• Create season separators & watch guides\n"
                "• Add footers and branding"
            ),
            image=pick_artwork("senku"),
            keyboard=keyboard(*rows),
        )
        await send_rich_welcome(client, container, message, screen, bot_name="senku")

    # /settings is handled by the shared human-friendly settings engine
    # (kurosoden.shared.settings_ui.register_settings), wired in register_all.

    return client
