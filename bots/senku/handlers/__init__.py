"""Senku handler registration.

Reuses NekoFetch's existing distribution infrastructure:
  • BotContentService — generates watch guides, info cards, season cards, footers.
  • BotFactory — creates distribution bots/channels.
  • BotOrchestratorService — orchestrates the full distribution flow.
"""

from __future__ import annotations

from pyrogram import Client
from nekofetch.core.container import Container


def register_all(client: Client, container: Container) -> None:
    from nekofetch.bots.middleware import install_auth_middleware
    from nekofetch.bots.admin.handlers import thumbnail_edit
    from kurosoden.bots.senku.handlers.tasks import register as register_tasks
    from kurosoden.bots.senku.handlers.wizard import register as register_wizard
    from kurosoden.shared.settings_ui import register_settings

    install_auth_middleware(client, container, staff_only_bot="senku")
    # Reuse the durable editor on Senku as the owner-facing /edit_thumbnail
    # command. Its callbacks/state are namespace-isolated from the wizard and its
    # existing owner gate remains in force.
    thumbnail_edit.register(client, container)
    register_wizard(client, container)
    register_tasks(client, container)

    # Human-friendly settings — Senku owns how posts look (cards, watch guide,
    # resolution buttons, footer) and the bot/footer branding. Registered before
    # the app.py `senku|` fallback so every `senku|set|…` tap lands here.
    register_settings(
        client, container, "senku",
        ["post_format", "bot"],
        title="Senku — Distribution Settings",
        blurb=(
            "Everything about how your channel posts <b>look</b> — the info, "
            "season and movie cards, the watch guide, the quality buttons, and "
            "the footer. Change any of it and see a live preview before you save."
        ),
        owner_only=True,
    )
