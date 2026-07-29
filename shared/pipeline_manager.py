"""PipelineManager — starts and supervises all four pipeline bots.

Modeled after NekoFetch's BotManager but designed for the multi-bot pipeline
where each bot watches the database for work in its stage of the request lifecycle.

Architecture:
    PipelineManager
       ├── Lelouch  (Request Bot)    —  REQUEST_BOT_TOKEN
       ├── Levi     (Downloader Bot) —  DOWNLOADER_BOT_TOKEN
       ├── Senku    (Distribution)   —  DISTRIBUTION_BOT_TOKEN
       └── Gojo     (Publisher)      —  PUBLISHER_BOT_TOKEN

Bots communicate through shared DB state (requests.status transitions), not
through direct inter-bot messaging. Each bot polls for rows in its relevant
status and picks them up when an admin is assigned.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)

# Connection watchdog intervals (mirrors BotManager's approach).
_CONN_CHECK_INTERVAL = 30
_CONN_PROBE_TIMEOUT = 20
_CONN_RECONNECT_ATTEMPTS = 3
_CONN_RECONNECT_TIMEOUT = 60
_CONN_RECONNECT_BACKOFF = 5


class PipelineManager:
    """Manages the lifecycle of all four pipeline bots on one event loop."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._clients: dict[str, Any] = {}  # name → Pyrogram Client
        self._conn_watchdog_task: asyncio.Task | None = None
        self._scheduler = None
        self._worker = None                       # DownloadWorker instance
        self._worker_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start all pipeline bots. Order: Lelouch → Levi → Senku → Gojo."""
        from nekofetch.infrastructure.scheduler import Scheduler

        self._c.pipeline_manager = self  # type: ignore[attr-defined]

        # Wire the download → distribution handoff. NekoFetch's download worker
        # fires this hook when a job finishes; we hand the request to Senku.
        from kurosoden.shared.handoff import handoff_download_to_distribution

        async def _on_download_complete(code: str, title: str) -> None:
            await handoff_download_to_distribution(self._c, code, title)

        self._c.on_download_complete = _on_download_complete  # type: ignore[attr-defined]

        # ── 1. Lelouch — Request Bot ──────────────────────────────────────────
        await self._start_bot("lelouch", "REQUEST_BOT_TOKEN")

        try:
            from kurosoden.shared.owner_seed import seed_configured_principals

            await seed_configured_principals(self._c, client=self.lelouch)
        except Exception as exc:  # noqa: BLE001
            log.warning("principal_seed.refresh_failed", error=str(exc))

        # ── 2. Levi — Downloader Bot ──────────────────────────────────────────
        await self._start_bot("levi", "DOWNLOADER_BOT_TOKEN")

        # ── 3. Senku — Distribution Bot ───────────────────────────────────────
        await self._start_bot("senku", "DISTRIBUTION_BOT_TOKEN")

        # ── 4. Gojo — Publisher Bot ───────────────────────────────────────────
        await self._start_bot("gojo", "PUBLISHER_BOT_TOKEN")

        # ── Download worker loop ──────────────────────────────────────────────
        # NekoFetch's BotManager (unused here) is what normally launches this
        # loop, so without it queued jobs would sit in QUEUED forever. Levi owns
        # the download stage, so the worker is meaningful only once Levi is up.
        if self._c.config.features.download_queue and self.levi is not None:
            from nekofetch.services.download_service import DownloadWorker

            self._worker = DownloadWorker(self._c)
            self._worker_task = asyncio.create_task(self._worker.run_forever())
            log.info("kuro-soden.download_worker.started")
        else:
            log.warning(
                "kuro-soden.download_worker.skipped",
                download_queue=self._c.config.features.download_queue,
                levi_up=self.levi is not None,
            )

        # ── Scheduler for background tasks ────────────────────────────────────
        self._scheduler = Scheduler()
        self._c.scheduler = self._scheduler  # type: ignore[attr-defined]

        # Idle-reminder: every 10 min, nudge on-shift idle admins when work is
        # waiting. The job itself honours mode/availability/hours/breaks and a
        # per-admin cooldown, so it's safe to tick often.
        from kurosoden.shared.assignment_recovery import make_assignment_recovery_job
        from kurosoden.shared.idle_reminder import make_idle_nudge_job

        self._scheduler.every(
            60, make_assignment_recovery_job(self._c), id="assignment-recovery",
        )
        self._scheduler.every(600, make_idle_nudge_job(self._c), id="idle-nudge")

        # Monthly maintenance (Gojo): a detect-only update sweep that DMs admins a
        # reviewable list (nothing auto-created) and a ban check that auto-recovers
        # down distribution channels. Both fire only when the Gojo bot is up and
        # honour a config interval (0 disables). Registered here because the
        # scheduler + bot clients both live on the manager.
        if self.gojo is not None:
            from kurosoden.bots.gojo.handlers.tasks import (
                make_monthly_bancheck_job,
                make_monthly_update_notify_job,
            )

            bcfg = self._c.config.bot
            upd_days = getattr(bcfg, "update_check_interval_days", 30)
            ban_days = getattr(bcfg, "ban_check_interval_days", 30)
            # Two gates each: the enable flag (operator switch) AND a positive
            # interval. Either off → that scheduled job is skipped; the manual
            # /updates and /bancheck commands stay available regardless.
            if getattr(bcfg, "monthly_update_check_enabled", True) and upd_days > 0:
                self._scheduler.every(
                    upd_days * 86400, make_monthly_update_notify_job(self._c),
                    id="gojo-update-notify",
                )
            if getattr(bcfg, "monthly_ban_check_enabled", True) and ban_days > 0:
                self._scheduler.every(
                    ban_days * 86400, make_monthly_bancheck_job(self._c),
                    id="gojo-ban-check",
                )

        self._scheduler.start()

        # ── Connection watchdog ───────────────────────────────────────────────
        self._conn_watchdog_task = asyncio.create_task(self._connection_watchdog())

        # Global admin_client fallback — Gojo is the primary publisher/index actor.
        self._c.admin_client = self.gojo or next(iter(self._clients.values()), None)  # type: ignore[attr-defined]

        await self._preflight_channels()

        log.info("kuro-soden.pipeline.started", bots=list(self._clients.keys()))

    async def _preflight_channels(self) -> None:
        """Resolve each configured channel with its owning bot and log the result."""
        cfg = self._c.config
        tracker = getattr(self._c, "startup_tracker", None)

        # channel-name → (config_section, owning_client)
        # KuroSoden is a pure bot pipeline — no log/thumbnail "management" channels
        # (those are NekoFetch concepts). Only the content channels matter:
        # database/storage (Levi uploads files), main + index (Gojo publishes).
        channel_map = [
            ("storage", cfg.storage_channel, self.levi or self._c.admin_client),
            ("main",    cfg.main_channel,    self.gojo or self._c.admin_client),
            ("index",   cfg.index_channel,   self.gojo or self._c.admin_client),
        ]

        for name, ch_cfg, client in channel_map:
            enabled = getattr(ch_cfg, "enabled", False)
            cid = getattr(ch_cfg, "channel_id", 0)
            if not enabled or not cid:
                log.info("kuro-soden.channel.disabled", channel=name, id=cid)
                if tracker:
                    tracker.channel_disabled(name, cid or 0)
                continue
            if client is None:
                log.warning("kuro-soden.channel.unreachable", channel=name, id=cid,
                            reason="no_client")
                if tracker:
                    tracker.channel_fail(name, cid)
                continue
            try:
                chat = await client.get_chat(cid)
                title = getattr(chat, "title", str(cid))
                log.info("kuro-soden.channel.ok", channel=name, id=cid, title=title)
                if tracker:
                    tracker.channel_ok(name, cid, title)
            except Exception as exc:  # noqa: BLE001
                log.warning("kuro-soden.channel.unreachable", channel=name, id=cid,
                            error=str(exc))
                if tracker:
                    tracker.channel_fail(name, cid)

    async def _start_bot(self, name: str, env_var: str) -> None:
        """Start a single pipeline bot by name."""
        import os

        token = self._token_from_config(env_var) or os.getenv(env_var, "").strip()
        if not token:
            log.warning("kuro-soden.bot.token_missing", bot=name, env_var=env_var,
                        hint=f"Set {env_var} in .env to enable the {name} bot")
            return

        try:
            if name == "lelouch":
                from kurosoden.bots.lelouch.app import build_lelouch, publish_commands
                client = build_lelouch(self._c, token)
            elif name == "levi":
                from kurosoden.bots.levi.app import build_levi, publish_commands
                client = build_levi(self._c, token)
            elif name == "senku":
                from kurosoden.bots.senku.app import build_senku, publish_commands
                client = build_senku(self._c, token)
            elif name == "gojo":
                from kurosoden.bots.gojo.app import build_gojo, publish_commands
                client = build_gojo(self._c, token)
            else:
                log.error("kuro-soden.bot.unknown", name=name)
                return

            await client.start()
            self._clients[name] = client
            # Populate the Telegram burger-menu command list (best-effort — a
            # failed set_bot_commands must never stop the bot from running).
            try:
                await publish_commands(client)
            except Exception as exc:  # noqa: BLE001
                log.warning("kuro-soden.bot.commands_failed", bot=name, error=str(exc))
            log.info("kuro-soden.bot.started", bot=name)
        except Exception as exc:
            log.error("kuro-soden.bot.start_failed", bot=name, error=str(exc))

    def _token_from_config(self, env_var: str) -> str:
        """Read pipeline bot tokens from EnvSettings, where .env is actually loaded."""
        env = getattr(self._c, "env", None)
        if env is None:
            return ""
        attr = {
            "REQUEST_BOT_TOKEN": "request_bot_token",
            "DOWNLOADER_BOT_TOKEN": "downloader_bot_token",
            "DISTRIBUTION_BOT_TOKEN": "distribution_bot_token",
            "PUBLISHER_BOT_TOKEN": "publisher_bot_token",
        }.get(env_var)
        return str(getattr(env, attr, "") or "").strip() if attr else ""

    @property
    def lelouch(self):
        """Return the Lelouch (Request Bot) Pyrogram client."""
        return self._clients.get("lelouch")

    @property
    def levi(self):
        """Return the Levi (Downloader Bot) Pyrogram client."""
        return self._clients.get("levi")

    @property
    def senku(self):
        """Return the Senku (Distribution Bot) Pyrogram client."""
        return self._clients.get("senku")

    @property
    def gojo(self):
        """Return the Gojo (Publisher Bot) Pyrogram client."""
        return self._clients.get("gojo")

    # ── download worker lifecycle (used by DatabaseClearService) ──────────────

    async def stop_download_worker(self) -> None:
        """Stop the download worker and await its in-flight tasks.

        Called by :class:`DatabaseClearService` **before** truncating tables so
        that no background download task is mid-write when the rows vanish.
        Without this, an in-flight download finishes after the clear and tries
        to INSERT a :class:`MediaFile` row — producing a foreign-key or
        orphaned-row error (or worse, a partial row against an empty DB).
        """
        if self._worker is not None:
            await self._worker.stop()
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._worker = None
        self._worker_task = None
        log.info("kuro-soden.download_worker.stopped")

    async def restart_download_worker(self) -> None:
        """Re-create and re-start the download worker after a DB clear.

        ``recover_on_startup`` re-queues any orphaned jobs — but after a clear
        there are none, so the loop simply idles until new requests arrive.
        """
        if self._worker_task is not None and not self._worker_task.done():
            return  # already running
        if not self._c.config.features.download_queue or self.levi is None:
            return
        from nekofetch.services.download_service import DownloadWorker

        self._worker = DownloadWorker(self._c)
        self._worker_task = asyncio.create_task(self._worker.run_forever())
        log.info("kuro-soden.download_worker.restarted")

    # ── connection watchdog ──────────────────────────────────────────────────

    async def _connection_watchdog(self) -> None:
        """Detect dead Telegram links and force clean reconnects."""
        while True:
            try:
                await asyncio.sleep(_CONN_CHECK_INTERVAL)
                for name, client in list(self._clients.items()):
                    if client is None:
                        continue
                    try:
                        await asyncio.wait_for(client.get_me(), timeout=_CONN_PROBE_TIMEOUT)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning("kuro-soden.conn.probe_failed", bot=name, error=str(exc))
                        await self._reconnect_client(name, client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("kuro-soden.conn_watchdog.error", error=str(exc))

    async def _reconnect_client(self, name: str, client) -> bool:
        """Force a fresh Telegram session for a dead link."""
        for attempt in range(1, _CONN_RECONNECT_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(client.restart(), timeout=_CONN_RECONNECT_TIMEOUT)
                log.info("kuro-soden.conn.reconnected", bot=name, attempt=attempt)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "kuro-soden.conn.reconnect_failed",
                    bot=name,
                    attempt=attempt,
                    error=str(exc),
                )
                await asyncio.sleep(_CONN_RECONNECT_BACKOFF)
        log.error("kuro-soden.conn.reconnect_exhausted", bot=name)
        return False

    async def stop(self) -> None:
        """Gracefully stop all bots."""
        await self.stop_download_worker()
        if self._scheduler is not None:
            self._scheduler.shutdown()
        if self._conn_watchdog_task is not None and not self._conn_watchdog_task.done():
            self._conn_watchdog_task.cancel()
        for name, client in self._clients.items():
            try:
                await client.stop()
                log.info("kuro-soden.bot.stopped", bot=name)
            except Exception:
                pass
        log.info("kuro-soden.pipeline.stopped")
