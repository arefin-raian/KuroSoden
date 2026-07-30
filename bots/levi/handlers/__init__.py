"""Levi handler registration.

Reuses NekoFetch's existing download infrastructure:
  • DownloadWorker — background loop picks up QUEUED jobs automatically.
  • QueueService — enqueues jobs for the worker.
  • ProcessingPipeline — runs after downloads complete.
  • SourceRegistry — lists available sources for admin selection.

Levi's role is manual source selection + download queuing — the heavy lifting
is all done by NekoFetch's existing services.
"""

from __future__ import annotations

from pyrogram import Client

from nekofetch.core.container import Container


def register_all(client: Client, container: Container) -> None:
    """Wire all Levi handlers — reuses NekoFetch's download infrastructure.

    The real source-pick → website-report → torrent-picker → franchise-map →
    queue flow lives in NekoFetch's admin ``review`` handler. That admin bot is
    never started in the Kuro Sōden pipeline, so the flow would otherwise be dead
    code — we mount it directly onto Levi (the downloader stage that owns source
    selection), then put Levi-native request cards in front of it. Its callbacks
    use ``staff|`` / ``franchise|`` / ``anizone|`` prefixes and its message
    handlers use explicit ``group=`` slots, so nothing collides with Levi's own
    ``levi|`` menu or default-group photo handler.
    """

    # ── Auth middleware ────────────────────────────────────────────────────
    from nekofetch.bots.middleware import install_auth_middleware

    install_auth_middleware(client, container, staff_only_bot="levi")

    # ── The full download/source machinery (mounted from admin.review) ─────
    from nekofetch.bots.admin.handlers import review

    # The shared torrent/source flow's "Back" button defaults to the admin
    # review detail card (staff|rdetail) — but on Levi the admin arrived via
    # Levi's own route picker (Telegram / Website / Torrent), NOT the review
    # card. Point Back back at Levi's route picker so it doesn't dump the admin
    # onto the review card with its stray Reject button.
    def _levi_source_back(code: str) -> str:
        return f"levi|sources|{code}"

    container.source_back_cb = _levi_source_back  # type: ignore[attr-defined]

    review.register(client, container)

    # ── Levi's settings panel (levi|set|…) — the shared human-friendly engine ──
    # Registered before the app.py `levi|` fallback so every settings tap is
    # handled here. Levi owns the download/processing side of the config.
    from kurosoden.shared.settings_ui import register_settings

    register_settings(
        client, container, "levi",
        ["downloads", "acquisition", "processing",
         "rename", "metadata", "thumbnail", "watermark", "branding"],
        title="Levi — Downloader Settings",
        blurb=(
            "How the download detail runs — how many files at once, which "
            "qualities to grab, how files are named and branded. On/off "
            "switches flip in place; text fields open an editor with an example."
        ),
        owner_only=True,
    )

    # ── Levi task handlers (task list → routes into the review flow) ───────
    from kurosoden.bots.levi.handlers.tasks import register as register_tasks

    register_tasks(client, container)

    # ── Live download progress card + its skip/cancel/abandon controls ─────
    # Kuro Sōden has no log channel, so this is the only live download UI the
    # admin sees. The card is spawned at enqueue time (see review.register,
    # which is handed the spawn hook below).
    from kurosoden.bots.levi.handlers.progress_monitor import register as register_progress
    from kurosoden.bots.levi.handlers.progress_monitor import start_monitor

    register_progress(client, container)

    # Expose the spawn hook on the container so the shared review enqueue path
    # can raise a live card without importing a Levi module directly.
    async def _spawn_card(job_id: int, chat_id: int) -> None:
        await start_monitor(client, container, job_id, chat_id)

    container.levi_progress_card = _spawn_card  # type: ignore[attr-defined]
