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
    from kurosoden.bots.senku.handlers.tasks import register as register_tasks
    from kurosoden.bots.senku.handlers.wizard import register as register_wizard
    from kurosoden.shared.settings_ui import register_settings

    install_auth_middleware(client, container, staff_only_bot="senku")
    # Senku-native thumbnail editor (recurring artwork/voice, franchise list with
    # pagination, single/multi + main/distribution routing, 13 editable fields,
    # and an isolated regenerate picker). Replaces the plain admin editor on the
    # Senku surface; the admin bot keeps its own thumbnail_edit registration.
    from kurosoden.bots.senku.handlers.thumbnail_edit_senku import (
        register as register_thumbnail_edit,
    )
    from kurosoden.bots.senku.handlers.thumbnail_regen import (
        register as register_thumbnail_regen,
    )

    register_thumbnail_edit(client, container)
    register_thumbnail_regen(client, container)
    # Staff-facing post editor: paste a post link, then edit its caption or
    # buttons — live message + DB rows are both updated.
    from kurosoden.bots.senku.handlers.post_caption_edit import register as register_caption_edit

    register_caption_edit(client, container)
    register_wizard(client, container)
    register_tasks(client, container)

    # Human-friendly settings — Senku owns how posts look (cards, watch guide,
    # resolution buttons, footer) and the bot/footer branding. Registered before
    # the app.py `senku|` fallback so every `senku|set|…` tap lands here.
    register_settings(
        client, container, "senku",
        ["post_format", "bot", "thumbnail_style"],
        title="Senku — Distribution Settings",
        blurb=(
            "Everything about how your channel posts <b>look</b> — the info, "
            "season and movie cards, the watch guide, the quality buttons, the "
            "footer, and the franchise thumbnail card's shadows &amp; sizes. "
            "Change any of it and see a live preview before you save."
        ),
        owner_only=True,
    )
