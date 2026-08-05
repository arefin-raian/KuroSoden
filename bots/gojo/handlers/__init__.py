"""Gojo handler registration.

Reuses NekoFetch's existing publishing infrastructure:
  - MainChannelService generates and posts to the main channel.
  - IndexChannelService updates the A-Z index.
  - PublishingService orchestrates the publish flow.
  - BotOrchestratorService handles bot recreation for recovery.
"""

from __future__ import annotations

from pyrogram import Client

from nekofetch.core.container import Container


def register_all(client: Client, container: Container) -> None:
    from nekofetch.bots.middleware import install_auth_middleware
    from nekofetch.ui.components import cb
    from kurosoden.bots.gojo.handlers.tasks import register as register_tasks
    from kurosoden.bots.gojo.handlers.schedule import register as register_schedule
    from kurosoden.shared.settings_ui import register_settings
    from kurosoden.shared.timezone_ui import register_timezone_ui

    install_auth_middleware(client, container, staff_only_bot="gojo")
    register_tasks(client, container)
    register_schedule(client, container)

    # Per-admin timezone picker. This stays outside owner-only settings because
    # it changes how each admin reads scheduled-post times, not bot configuration.
    register_timezone_ui(client, container, "gojo")

    register_settings(
        client,
        container,
        "gojo",
        ["main_channel", "index_channel", "thumbnail_channel"],
        title="Gojo - Publishing Settings",
        blurb=(
            "How every public post reads - the main-channel caption, the A-Z "
            "index lines, and the thumbnail channel. Edit a template and you'll "
            "see a live preview filled with a real example before you save."
        ),
        owner_only=True,
        extra_buttons=[("My Timezone", cb("gojo", "tz", "home"))],
    )
