"""Multi-bot runtime manager.

Runs the admin bot plus every enabled distribution bot as separate Pyrogram clients
on one event loop. Distribution bots are loaded from the ``bots`` table (tokens are
decrypted on demand) and can be added/removed at runtime.
"""

from __future__ import annotations

import asyncio

from pyrogram.enums import ParseMode

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.ui.typography import bq

log = get_logger(__name__)

_RESOLVE_RETRY_SECONDS = 10
_RESOLVE_MAX_RETRIES = 12  # 2 minutes of retries

# Connection watchdog — recover a dead Telegram link after the host sleeps/wakes.
_CONN_CHECK_INTERVAL = 30      # seconds between link health probes
_CONN_PROBE_TIMEOUT = 20       # seconds to wait for a probe before deeming the link dead
_CONN_RECONNECT_ATTEMPTS = 3
_CONN_RECONNECT_TIMEOUT = 60   # seconds allowed for a single restart to complete
_CONN_RECONNECT_BACKOFF = 5
# Comprehensive entity health check (monthly sweep).
# Interval read from BotConfig.entity_full_check_days at scheduler setup time.


class BotManager:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._admin = None
        self._distribution: dict[int, object] = {}
        self._worker = None
        self._worker_task: asyncio.Task | None = None
        self._scheduler = None
        self._conn_watchdog_task: asyncio.Task | None = None
        self._ban_health_task: asyncio.Task | None = None

    async def start(self) -> None:
        from nekofetch.bots.admin.app import build_admin_bot

        tracker = getattr(self._c, "startup_tracker", None)

        # Expose the manager so services can bring bots online without a restart.
        self._c.bot_manager = self  # type: ignore[attr-defined]

        self._admin = build_admin_bot(self._c)
        await self._admin.start()
        # The admin client is the privileged actor for storage/log channels (it must be an
        # administrator of both). Expose it so services can use it.
        self._c.admin_client = self._admin  # type: ignore[attr-defined]
        await self._publish_commands(self._admin, kind="admin")
        if tracker:
            tracker.service_ok("admin")
        await self._preflight_channels()
        log.info("bots.admin.started")

        if self._c.config.features.distribution_bots:
            try:
                await self._load_distribution_bots()
            except Exception as exc:  # never let a fleet-wide error block startup
                log.error("bots.distribution.load_failed", error=str(exc))

        await self._start_background_workers()

        # Start ban-detection health check.
        self._start_ban_health_check()

        # Start the connection watchdog (recovers a dead link after sleep/wake).
        self._conn_watchdog_task = asyncio.create_task(self._connection_watchdog())
        log.info("bots.conn_watchdog.started", interval_seconds=_CONN_CHECK_INTERVAL)
        tracker = getattr(self._c, "startup_tracker", None)
        if tracker:
            tracker.service_ok("conn_watchdog")

    async def _check_bots_for_bans(self) -> None:
        """Periodic health check — detect banned bots and auto-recreate them."""
        interval = self._c.config.bot.health_check_interval_minutes
        if interval <= 0:
            return

        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import DistributionBot
        from nekofetch.infrastructure.database.postgres.session import session_scope

        while True:
            await asyncio.sleep(interval * 60)
            log.debug("bots.health_check.start")

            # Check each running distribution client.
            for bot_id, client in list(self._distribution.items()):
                try:
                    me = await asyncio.wait_for(client.get_me(), timeout=_CONN_PROBE_TIMEOUT)
                    if me is not None:
                        continue  # Bot is fine.
                except asyncio.CancelledError:
                    raise
                except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                    # A dead link (e.g. the host slept), NOT a ban — reconnect instead
                    # of tearing the bot down and recreating it.
                    log.warning("bots.health_check.disconnected", bot_id=bot_id, error=str(exc))
                    await self._reconnect_client(f"dist:{bot_id}", client)
                    continue
                except Exception:  # noqa: BLE001 - a genuine auth/ban failure falls through
                    pass

                # Bot appears to be banned — mark it, stop client, and recreate.
                try:
                    await client.stop()
                except Exception:
                    pass
                self._distribution.pop(bot_id, None)
                await self._handle_banned_entity(bot_id, "bot")

    # ── comprehensive full check (monthly scheduled + manual /checkbans) ───────

    async def check_all_entities(
        self, *, alert: bool = True, on_progress=None,
    ) -> dict:
        """Comprehensive health check of ALL distribution entities (bots + channels).

        For bots:
          • Running clients → probe with ``get_me()``
          • Non-running bots → start a temporary probe client
        For channels:
          • Probe with ``get_chat()`` via the userbot pool

        Banned/gone entities are disabled and recreated. Returns a summary dict.

        ``on_progress(entity_name, status)`` is an optional callback the admin
        command uses to stream live status updates to the user.
        """
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import DistributionBot
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.services.bot_orchestrator import BotOrchestratorService

        result = {"checked": 0, "healthy": 0, "banned": 0, "recreated": 0,
                   "failed": 0, "details": []}

        async with session_scope(self._c.pg_sessionmaker) as session:
            entities = (
                await session.execute(
                    select(DistributionBot).where(DistributionBot.enabled.is_(True))
                )
            ).scalars().all()

        orch = BotOrchestratorService(self._c)

        for entity in entities:
            result["checked"] += 1
            label = f"{'channel' if entity.is_channel else 'bot'} #{entity.id} ({entity.name})"
            log.debug("bots.full_check.checking", entity=label)
            if on_progress:
                try:
                    on_progress(entity.name, "checking")
                except Exception:
                    pass

            ok = await self._probe_entity(entity)
            if ok:
                result["healthy"] += 1
                if on_progress:
                    try:
                        on_progress(entity.name, "healthy")
                    except Exception:
                        pass
                continue

            # Entity is banned/gone.
            result["banned"] += 1
            anime = entity.anime_doc_id or "unknown"
            log.warning("bots.full_check.banned", entity=label, anime=anime)
            if on_progress:
                try:
                    on_progress(entity.name, "banned")
                except Exception:
                    pass

            # Mark disabled.
            async with session_scope(self._c.pg_sessionmaker) as s:
                row = await s.get(DistributionBot, entity.id)
                if row is not None:
                    row.enabled = False

            # Stop the client if it was running.
            client = self._distribution.pop(entity.id, None)
            if client is not None:
                try:
                    await client.stop()
                except Exception:
                    pass

            # Recreate.
            if anime and anime != "unknown":
                try:
                    info = await orch.recreate_bot(anime)
                    if info:
                        result["recreated"] += 1
                        result["details"].append(f"✅ {entity.name} → {info.name} (@{info.username})")
                        if on_progress:
                            try:
                                on_progress(entity.name, "recreated")
                            except Exception:
                                pass
                    else:
                        result["failed"] += 1
                        result["details"].append(f"⚠️ {entity.name}: recreate returned None")
                        if on_progress:
                            try:
                                on_progress(entity.name, "failed")
                            except Exception:
                                pass
                except Exception as exc:
                    result["failed"] += 1
                    result["details"].append(f"❌ {entity.name}: {exc}")
                    if on_progress:
                        try:
                            on_progress(entity.name, "failed")
                        except Exception:
                            pass
            else:
                result["failed"] += 1
                result["details"].append(f"⚠️ {entity.name}: no anime_doc_id")

        log.info("bots.full_check.complete", **{k: v for k, v in result.items() if k != "details"})
        if alert and result["banned"] > 0:
            await self._alert_admin(
                f"🛡️ <b>Entity health check complete</b>\n\n"
                f"<b>Checked:</b> {result['checked']}\n"
                f"<b>Healthy:</b> {result['healthy']}\n"
                f"<b>Banned:</b> {result['banned']}\n"
                f"<b>Recreated:</b> {result['recreated']}\n"
                f"<b>Failed:</b> {result['failed']}\n\n"
                + "\n".join(result["details"][:10])
            )

        return result

    async def _probe_entity(self, entity) -> bool:
        """Check if a DistributionBot entity is still alive.

        Returns True if the entity is reachable, False if banned/gone.
        """
        if entity.is_channel:
            return await self._probe_channel(entity)
        else:
            return await self._probe_bot(entity)

    async def _probe_bot(self, entity) -> bool:
        """Check if a bot entity is still alive."""
        from pyrogram import Client

        # Try the running client first.
        client = self._distribution.get(entity.id)
        if client is not None:
            try:
                me = await asyncio.wait_for(client.get_me(), timeout=15)
                return me is not None
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception:
                return False

        # Not running — try a temporary probe client.
        token = self._c.cipher.decrypt(entity.encrypted_token)
        probe = Client(
            name=f"nf-probe-{entity.id}",
            api_id=self._c.env.telegram_api_id,
            api_hash=self._c.env.telegram_api_hash,
            bot_token=token,
            in_memory=True,
            workdir=str(self._c.env.session_path),
        )
        try:
            await asyncio.wait_for(probe.start(), timeout=20)
            me = await asyncio.wait_for(probe.get_me(), timeout=10)
            return me is not None
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            return False
        finally:
            try:
                await probe.stop()
            except Exception:
                pass

    async def _probe_channel(self, entity) -> bool:
        """Check if a channel entity is still reachable via userbot."""
        if not entity.chat_id:
            return False
        from nekofetch.sources.telegram.userbot import UserbotPool

        # Cache the pool on the container so we reuse the same Pyrogram
        # Client connections across multiple channel probes.
        pool: UserbotPool | None = getattr(self._c, "_userbot_pool", None)
        if pool is None:
            pool = UserbotPool.from_env(
                self._c.env.telegram_api_id,
                self._c.env.telegram_api_hash,
                str(self._c.env.session_path),
            )
            self._c._userbot_pool = pool  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(
                pool.execute(lambda c: c.get_chat(int(entity.chat_id))),
                timeout=20,
            )
            return True
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            return False

    async def _handle_banned_entity(self, entity_id: int, kind: str = "bot") -> None:
        """Mark an entity as disabled and auto-recreate it."""
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import DistributionBot
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.services.bot_orchestrator import BotOrchestratorService

        anime_doc_id: str | None = None
        async with session_scope(self._c.pg_sessionmaker) as session:
            row = await session.get(DistributionBot, entity_id)
            if row is not None:
                anime_doc_id = row.anime_doc_id
                row.enabled = False

        if anime_doc_id:
            log.info("bots.health_check.recreating", anime=anime_doc_id, kind=kind)
            await self._alert_admin(
                f"♻️ <b>{kind} banned!</b>\n\n"
                f"<b>id:</b> <code>{entity_id}</code>\n"
                f"<b>anime:</b> {anime_doc_id}\n\n"
                f"auto-recreate in progress…"
            )
            try:
                await BotOrchestratorService(self._c).recreate_bot(anime_doc_id)
                log.info("bots.health_check.recreated", anime=anime_doc_id, kind=kind)
                await self._alert_admin(
                    f"✅ <b>{kind} recreated</b> for {anime_doc_id}"
                )
            except Exception as exc:
                log.error("bots.health_check.recreate.failed",
                          anime=anime_doc_id, kind=kind, error=str(exc))
                await self._alert_admin(
                    f"⚠️ <b>{kind} recreate failed!</b>\n\n"
                    f"<b>anime:</b> {anime_doc_id}\n"
                    f"<b>error:</b> {str(exc)[:200]}"
                )

    def _start_ban_health_check(self) -> None:
        """Start the background health-check task."""
        interval = self._c.config.bot.health_check_interval_minutes
        if interval > 0:
            task = asyncio.create_task(self._check_bots_for_bans())
            self._ban_health_task = task
            log.info("bots.health_check.started", interval_minutes=interval)
            tracker = getattr(self._c, "startup_tracker", None)
            if tracker:
                tracker.service_ok("health_check")

    async def _monthly_full_check(self) -> None:
        """Scheduled comprehensive entity health sweep (monthly)."""
        log.info("bots.full_check.scheduled_run.start")
        try:
            result = await self.check_all_entities(alert=True)
            log.info("bots.full_check.scheduled_run.done",
                     checked=result["checked"], banned=result["banned"],
                     recreated=result["recreated"])
        except Exception as exc:
            log.error("bots.full_check.scheduled_run.failed", error=str(exc))

    async def _check_shift_afk(self) -> None:
        from nekofetch.services.shift_service import ShiftService
        shift = ShiftService(self._c)
        for channel in ("logcc", "thumbcc"):
            try:
                released = await shift.auto_release_if_afk(channel)
                if released:
                    from nekofetch.ui.duty_board import afk_release_dm
                    # DM the released worker
                    try:
                        await self._admin.send_message(
                            released.worker_id or 0,
                            afk_release_dm(channel),
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
            except Exception:
                pass

    async def _alert_admin(self, text: str) -> None:
        owner_id = self._c.config.security.owner_id
        if not owner_id or not self._admin:
            return
        try:
            await self._admin.send_message(owner_id, bq(text), parse_mode=ParseMode.HTML)
        except Exception as exc:
            log.warning("bot.alert_admin.failed", error=str(exc))

    # ── connection watchdog ─────────────────────────────────────────────────────
    async def _connection_watchdog(self) -> None:
        """Detect a dead Telegram link and force a clean reconnect.

        When the host sleeps, the underlying socket dies silently; on wake Pyrogram's
        own retry loop can stay wedged emitting ``Connection lost`` on every
        ``EditMessage``, so message updates/logging never recover without a manual
        restart. A bounded health probe (``get_me`` under a timeout) detects the dead
        link deterministically, and ``Client.restart()`` rebuilds the session — the
        cached channel peers survive because the .session file is reused.
        """
        while True:
            try:
                await asyncio.sleep(_CONN_CHECK_INTERVAL)
                clients = [("admin", self._admin)]
                clients += [(f"dist:{bid}", c) for bid, c in list(self._distribution.items())]
                for label, client in clients:
                    if client is None:
                        continue
                    try:
                        await asyncio.wait_for(client.get_me(), timeout=_CONN_PROBE_TIMEOUT)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - timeout / ConnectionError / OSError
                        log.warning("bots.conn.probe_failed", client=label, error=str(exc))
                        await self._reconnect_client(label, client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the watchdog must never die
                log.warning("bots.conn_watchdog.error", error=str(exc))

    async def _reconnect_client(self, label: str, client) -> bool:
        """Force a fresh Telegram session for a client whose link went dead.

        Returns True once reconnected. Bounded so a still-down network (mid-sleep or
        wake before the NIC is up) doesn't hang the watchdog — it retries next tick.
        """
        for attempt in range(1, _CONN_RECONNECT_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(client.restart(), timeout=_CONN_RECONNECT_TIMEOUT)
                log.info("bots.conn.reconnected", client=label, attempt=attempt)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("bots.conn.reconnect_failed",
                            client=label, attempt=attempt, error=str(exc))
                await asyncio.sleep(_CONN_RECONNECT_BACKOFF)
        log.error("bots.conn.reconnect_exhausted", client=label)
        return False

    async def _preflight_channels(self) -> None:
        """Resolve every configured Telegram channel at startup and retry in background."""
        cfg = self._c.config
        sections = [
            ("storage", cfg.storage_channel),
            ("log", cfg.log_channel),
            ("main", cfg.main_channel),
            ("index", cfg.index_channel),
            ("thumbnail", cfg.thumbnail_channel),
        ]
        for name, section in sections:
            enabled = getattr(section, "enabled", False)
            cid = getattr(section, "channel_id", 0)
            if not enabled or not cid:
                # Report the channel as explicitly disabled instead of silently
                # omitting it — the startup dashboard then shows main, storage
                # (database), index, log and thumbnail every time, so an operator
                # can see at a glance which are wired up and which aren't.
                tracker = getattr(self._c, "startup_tracker", None)
                if tracker:
                    tracker.channel_disabled(name, cid)
                continue
            if await self._try_resolve(name, cid):
                continue
            asyncio.create_task(self._retry_resolve(name, cid))

    async def _try_resolve(self, name: str, cid: int) -> bool:
        try:
            chat = await self._admin.get_chat(cid)
            log.info("bots.channel.ok", channel=name, id=cid, title=getattr(chat, "title", None))
            tracker = getattr(self._c, "startup_tracker", None)
            if tracker:
                tracker.channel_ok(name, cid, getattr(chat, "title", "") or "")
            return True
        except Exception as exc:
            log.warning(
                "bots.channel.unreachable",
                channel=name,
                id=cid,
                error=str(exc),
                hint=(
                    "Make the admin bot an administrator of this channel, then post any "
                    "message in it (or remove + re-add the bot) while NekoFetch is running "
                    "so Telegram caches the peer. Confirm the id is the full -100... value. "
                    "Deleting the Pyrogram .session on each launch wipes this cache and "
                    "brings the error back."
                ),
            )
            tracker = getattr(self._c, "startup_tracker", None)
            if tracker:
                tracker.channel_fail(name, cid)
                tracker.add_error(f"Channel {name} unreachable: {str(exc)[:100]}")
            return False

    async def _retry_resolve(self, name: str, cid: int) -> None:
        """Keep trying to resolve the channel peer in the background for ~2 minutes.

        When the bot is added to a private channel it receives a ``ChatMemberUpdated``
        update that contains only the ``chat_id`` — the ``access_hash`` needed for
        subsequent API calls is only cached after the bot *receives a message* from
        that channel (or the user mentions the bot there). This retry window gives the
        user time to trigger that event.
        """
        log.info("bots.channel.retrying", channel=name, id=cid)
        for attempt in range(1, _RESOLVE_MAX_RETRIES + 1):
            await asyncio.sleep(_RESOLVE_RETRY_SECONDS)
            try:
                chat = await self._admin.get_chat(cid)
                log.info("bots.channel.resolved", channel=name, id=cid, title=getattr(chat, "title", None))
                return
            except Exception:
                log.debug("bots.channel.retry_pending", channel=name, id=cid, attempt=attempt)
        log.warning("bots.channel.retry_exhausted", channel=name, id=cid)
        await self._alert_admin(
            f"❌ <b>channel retry exhausted!</b>\n\n"
            f"<b>channel:</b> {name}\n"
            f"<b>id:</b> <code>{cid}</code>\n\n"
            f"could not resolve channel peer after {_RESOLVE_MAX_RETRIES} attempts. "
            f"make sure the bot is an admin of the channel and that a message has been posted."
        )

    async def _publish_commands(self, client, *, kind: str) -> None:
        """Publish the Telegram command menu so users can discover commands.

        Best-effort: a transient API hiccup here must never stop a bot from running.
        """
        try:
            if kind == "admin":
                from nekofetch.bots.admin.handlers.commands import publish_admin_commands

                await publish_admin_commands(client)
            else:
                from nekofetch.bots.distribution.app import publish_distribution_commands

                await publish_distribution_commands(client)
        except Exception as exc:  # noqa: BLE001
            log.warning("bots.commands.publish_failed", kind=kind, error=str(exc))

    async def _start_background_workers(self) -> None:
        from nekofetch.infrastructure.scheduler import Scheduler
        from nekofetch.services.distribution_service import DistributionService
        from nekofetch.services.download_service import DownloadWorker

        # Download worker loop.
        if self._c.config.features.download_queue:
            self._worker = DownloadWorker(self._c)
            self._worker_task = asyncio.create_task(self._worker.run_forever())
            log.info("worker.download.started")
            tracker = getattr(self._c, "startup_tracker", None)
            if tracker:
                tracker.service_ok("download_worker")

        # Scheduled maintenance jobs.
        self._scheduler = Scheduler()
        self._c.scheduler = self._scheduler  # type: ignore[attr-defined]
        dist = DistributionService(self._c)
        if self._c.config.features.temporary_links:
            self._scheduler.every(60, dist.sweep_expired, id="link-expiry-sweep")
            log.info("bots.temp_links_sweep.scheduled", interval_seconds=60)
            tracker = getattr(self._c, "startup_tracker", None)
            if tracker:
                tracker.service_ok("temp_links")

        # Broadcast auto-deletion sweep: durable backstop for *timed* channel
        # broadcasts (APScheduler is in-memory, so a restart would forget a
        # pending delete). Runs every minute like the link sweep and catches up
        # on any past-due deletion after downtime.
        from nekofetch.services.broadcast_service import BroadcastService

        self._scheduler.every(
            60, BroadcastService(self._c).sweep_expired, id="broadcast-expiry-sweep",
        )
        log.info("bots.broadcast_sweep.scheduled", interval_seconds=60)

        # Scheduled-post sweep: durable backstop for deferred main-channel
        # publishes. APScheduler jobs are in-memory with non-serializable
        # callables, so a restart would forget every pending schedule. The
        # ScheduledPost rows are the source of truth; this fires any past-due
        # pending row via the normal PublishingService path.
        from nekofetch.services.schedule_service import ScheduleService

        self._scheduler.every(
            60, ScheduleService(self._c).sweep_due, id="scheduled-post-sweep",
        )
        log.info("bots.schedule_sweep.scheduled", interval_seconds=60)

        # Stats: refresh the pinned database stats message in the storage channel.
        if self._c.config.storage_channel.enabled:
            try:
                from nekofetch.services.stats_service import StatsService

                await StatsService(self._c).refresh()
                log.info("stats.refreshed_on_startup")
                tracker = getattr(self._c, "startup_tracker", None)
                if tracker:
                    tracker.service_ok("stats")
            except Exception as exc:  # noqa: BLE001 - stats must not block startup
                log.warning("stats.refresh_on_startup.failed", error=str(exc))
                tracker = getattr(self._c, "startup_tracker", None)
                if tracker:
                    tracker.service_fail("stats")
                    tracker.add_error(f"Stats refresh failed: {exc}")

        # Log channel: create/pin the dashboard + catalog, then refresh on an interval.
        if self._c.config.log_channel.enabled:
            from nekofetch.services.log_channel_service import LogChannelService

            log_svc = LogChannelService(self._c)
            await log_svc.ensure_pins()
            log.info("bots.log_channel.ready")
            tracker = getattr(self._c, "startup_tracker", None)
            if tracker:
                tracker.service_ok("log_channel")
            self._scheduler.every(
                self._c.config.log_channel.refresh_seconds, log_svc.refresh, id="log-pins-refresh"
            )
            # Fast lane: keep the active-tasks progress bar responsive between full refreshes.
            self._scheduler.every(
                self._c.config.log_channel.active_refresh_seconds,
                log_svc.refresh_active, id="log-active-refresh",
            )

        # Thumbnail channel: wipe + rebuild on every restart, then refresh queue on interval.
        if self._c.config.thumbnail_channel.enabled:
            from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService

            thumb_svc = ThumbnailChannelService(self._c)
            await thumb_svc.ensure_channel()
            log.info("bots.thumbnail_channel.ready")
            tracker = getattr(self._c, "startup_tracker", None)
            if tracker:
                tracker.service_ok("thumbnail_channel")
            self._scheduler.every(60, thumb_svc.refresh_queue, id="thumbcc-refresh")

        # Shift AFK timeout: auto-release idle workers every 5 minutes.
        from nekofetch.services.shift_service import ShiftService
        shift_svc = ShiftService(self._c)
        self._scheduler.every(300, self._check_shift_afk, id="shift-afk-check")
        log.info("bots.shift_afk.scheduled", interval_seconds=300)
        tracker = getattr(self._c, "startup_tracker", None)
        if tracker:
            tracker.service_ok("shift_afk")

        # Monthly comprehensive entity health check (bots + channels).
        full_check_days = self._c.config.bot.entity_full_check_days
        full_check_seconds = full_check_days * 86400
        if full_check_seconds > 0:
            self._scheduler.every(
                full_check_seconds,
                self._monthly_full_check,
                id="entity-full-check",
            )
            log.info("bots.full_check.scheduled", interval_days=full_check_days)

        # Monthly update check: scan all published anime for new franchise entries.
        from nekofetch.services.update_check_service import UpdateCheckService

        update_check_days = getattr(self._c.config, "update_check_interval_days", 30)
        update_check_seconds = update_check_days * 86400
        if update_check_seconds > 0:
            ucs = UpdateCheckService(self._c)
            self._scheduler.every(
                update_check_seconds, ucs.check_all, id="update-check",
            )
            log.info("update_check.scheduled", interval_days=update_check_days)

        self._scheduler.start()
        log.info("bots.scheduler.started")
        tracker = getattr(self._c, "startup_tracker", None)
        if tracker:
            tracker.service_ok("scheduler")

    async def _load_distribution_bots(self) -> None:
        from sqlalchemy import select

        from nekofetch.bots.distribution.app import build_distribution_bot
        from nekofetch.infrastructure.database.postgres.models import DistributionBot

        async with self._c.session() as session:
            rows = (
                await session.execute(
                    select(DistributionBot).where(DistributionBot.enabled.is_(True))
                )
            ).scalars().all()

        loaded = 0
        failed = 0
        for row in rows:
            # Skip channel rows — they don't run Pyrogram clients and have
            # a placeholder token ("channel-no-token"). Probing them here
            # would log a spurious "failed to start" error on every restart.
            if row.is_channel:
                continue
            try:
                token = self._c.cipher.decrypt(row.encrypted_token)
                client = build_distribution_bot(self._c, row, token)
                await client.start()
                await self._publish_commands(client, kind="distribution")
                self._distribution[row.id] = client
                log.info("bots.distribution.started", bot=row.name, id=row.id)
                loaded += 1
            except Exception as exc:  # one bad token must not stop the fleet
                err_str = str(exc)
                # Telegram bans/deactivates bot accounts that get reported
                # for spam; the resulting exception always carries one of
                # these literal substrings. Detecting by substring (rather
                # than importing pyrogram.errors) keeps the dependency
                # surface narrow and the failure mode easy to log.
                is_auth_failure = (
                    "USER_DEACTIVATED" in err_str
                    or "401" in err_str and "Unauthorized" in err_str
                )
                if is_auth_failure:
                    # Mark the DB row disabled immediately so the next
                    # restart's _load_distribution_bots doesn't keep
                    # retrying a banned token (the periodic health check
                    # would also catch it, but that runs every N minutes —
                    # a 30-second UI in the meantime is operator noise).
                    try:
                        async with self._c.session() as s:
                            r = await s.get(row.__class__, row.id)
                            if r is not None and r.enabled:
                                r.enabled = False
                        log.warning(
                            "bots.distribution.disabled_on_auth_failure",
                            id=row.id, name=row.name, anime=row.anime_doc_id,
                        )
                    except Exception as e2:
                        log.warning(
                            "bots.distribution.disable_db_failed",
                            id=row.id, error=str(e2),
                        )
                log.error("bots.distribution.failed", id=row.id, error=err_str)
                failed += 1
                # Specialised alert text for auth failures so the operator
                # immediately knows "this bot is banned, run /recreate_bot"
                # rather than "investigate some generic startup error".
                if is_auth_failure:
                    alert = (
                        f"🚫 <b>distribution bot DEACTIVATED by Telegram</b>\n\n"
                        f"<b>id:</b> <code>{row.id}</code>\n"
                        f"<b>name:</b> {row.name}\n"
                        f"<b>anime:</b> {row.anime_doc_id or '—'}\n"
                        f"<b>error:</b> <code>{err_str[:200]}</code>\n\n"
                        f"Telegram banned/deactivated this bot's account. "
                        f"I've disabled it in the DB so future restarts "
                        f"don't keep failing on the same token. Run "
                        f"<code>/recreate_bot {row.anime_doc_id or row.id}</code> "
                        f"to spin up a fresh one."
                    )
                else:
                    alert = (
                        f"⚠️ <b>distribution bot failed to start</b>\n\n"
                        f"<b>id:</b> <code>{row.id}</code>\n"
                        f"<b>name:</b> {row.name}\n"
                        f"<b>error:</b> <code>{err_str[:200]}</code>"
                    )
                await self._alert_admin(alert)
        if loaded > 0 or failed > 0:
            log.info("bots.distribution.loaded", loaded=loaded, failed=failed)
        tracker = getattr(self._c, "startup_tracker", None)
        if tracker:
            tracker.bots_loaded = loaded
            tracker.bots_failed = failed
            tracker.bot_names = [getattr(r, "name", "?") for r in rows if not r.is_channel]

    def get_client(self, bot_id: int):
        """Return the running Pyrogram client for a distribution bot, if any."""
        return self._distribution.get(bot_id)

    async def add_distribution_bot(self, bot_id: int) -> None:
        """Start a single newly-registered distribution bot at runtime."""
        from nekofetch.bots.distribution.app import build_distribution_bot
        from nekofetch.infrastructure.database.postgres.models import DistributionBot

        if bot_id in self._distribution:
            return
        async with self._c.session() as session:
            row = await session.get(DistributionBot, bot_id)
            if row is None or not row.enabled:
                return
            token = self._c.cipher.decrypt(row.encrypted_token)
            client = build_distribution_bot(self._c, row, token)
        await client.start()
        await self._publish_commands(client, kind="distribution")
        self._distribution[bot_id] = client
        log.info("bots.distribution.added", id=bot_id)

    async def remove_distribution_bot(self, bot_id: int) -> None:
        """Stop and drop a distribution bot's live client at runtime.

        The runtime mirror of ``add_distribution_bot`` — called when a bot is
        disabled so the process stops serving content immediately instead of the
        DB saying "off" while the client stays fully live until the next restart.
        """
        client = self._distribution.pop(bot_id, None)
        if client is None:
            return
        try:
            await client.stop()
        except Exception:  # noqa: BLE001
            pass
        log.info("bots.distribution.removed", id=bot_id)

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown()
        # Stop the connection watchdog first so it can't restart a client mid-shutdown.
        if self._conn_watchdog_task is not None and not self._conn_watchdog_task.done():
            self._conn_watchdog_task.cancel()
        if self._worker is not None:
            await self._worker.stop()
        if self._worker_task is not None:
            self._worker_task.cancel()
        # Cancel the ban health check task.
        if self._ban_health_task is not None and not self._ban_health_task.done():
            self._ban_health_task.cancel()
        for client in self._distribution.values():
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._admin is not None:
            await self._admin.stop()
        log.info("bots.stopped")
