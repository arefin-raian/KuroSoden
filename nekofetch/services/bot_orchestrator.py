"""Bot/Channel orchestration — coordinates the storage→entity→content→main channel flow.

After content is uploaded to the storage channel, this service:
  1. Creates a distribution bot (or channel if bot limit is exhausted) via BotFactory
  2. Generates content posts (watch guide, season cards, etc.)
  3. Binds the entity to the title and applies branding
  4. Posts to the main channel with a Download button pointing to the entity

Also handles bot re-creation when a bot is banned.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost, DistributionBot, StoragePack,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.services.bot_management_service import BotInfo

log = get_logger(__name__)


class BotOrchestratorService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def ensure_bot_for_anime(self, anime_doc_id: str, *, publish: bool = True) -> BotInfo | None:
        """Create a distribution bot for an anime if one doesn't exist.

        Set ``publish=False`` to skip the main + index channel post (used by
        the preview script when iterating on the content rendering without
        flooding the public channels).

        Returns the BotInfo if a bot was created or already exists.
        Returns None if distribution_bots feature is disabled.
        """
        if not self._c.config.features.distribution_bots:
            return None

        # Check if a bot already exists for this title.
        existing = await self._find_existing_bot(anime_doc_id)
        if existing is not None:
            log.info("bot.orchestrator.exists", anime=anime_doc_id, bot=existing.id)
            return existing

        # Create brand new bot via BotFactory.
        from nekofetch.core.exceptions import NekoFetchError
        from nekofetch.services.bot_factory import BotFactory

        log.info("bot.orchestrator.creating", anime=anime_doc_id)

        try:
            bot_info = await BotFactory(self._c).create_for_anime(anime_doc_id)
        except NekoFetchError as exc:
            log.error("bot.orchestrator.create.failed", anime=anime_doc_id, error=str(exc))
            return None

        # Generate content posts for this bot.
        await self._generate_content(bot_info.id, anime_doc_id)

        # Bind and refresh main channel (skipped in preview/test mode).
        if publish:
            await self._bind_and_publish(bot_info.id, anime_doc_id)

        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(self._c).event(
            "bot", "created", id=bot_info.id, name=bot_info.name,
            anime=anime_doc_id,
        )

        log.info("bot.orchestrator.created", anime=anime_doc_id, bot=bot_info.id)
        return bot_info

    async def recreate_bot(self, anime_doc_id: str) -> BotInfo | None:
        """Recreate a distribution entity for an anime (after a ban or failure).

        Handles both bots and channels — detects the entity type from the DB row
        and recreates accordingly. Removes the old record + content posts, then
        creates a fresh entity via BotFactory.
        """
        if not self._c.config.features.distribution_bots:
            return None

        # Snapshot the current pack into the wipe-proof backup *before* we delete
        # the live BotContentPost rows below — normally publish-time capture has
        # already stored it, but a channel published before backups existed (or
        # since re-generated) would otherwise lose its verbatim content. The row
        # upserts, so a fresh capture just refreshes an existing one. Best-effort.
        try:
            from nekofetch.services.backup_service import BackupService

            await BackupService(self._c).record_distribution_channel(anime_doc_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("bot.orchestrator.prewipe_backup_failed",
                        anime=anime_doc_id, error=str(exc))

        # Remove the old entity record and its content posts.
        was_channel = False
        async with session_scope(self._c.pg_sessionmaker) as session:
            old = (
                await session.execute(
                    select(DistributionBot)
                    .where(DistributionBot.anime_doc_id == anime_doc_id)
                    .order_by(DistributionBot.id.desc())
                )
            ).scalars().first()
            if old is not None:
                was_channel = old.is_channel
                await session.execute(
                    delete(BotContentPost).where(BotContentPost.bot_id == old.id)
                )
                await session.delete(old)
                await session.flush()

        from nekofetch.services.bot_factory import BotFactory
        from nekofetch.services.bot_management_service import BotInfo

        from nekofetch.core.exceptions import NekoFetchError

        factory = BotFactory(self._c)
        try:
            if was_channel:
                log.info("bot.orchestrator.recreating_channel", anime=anime_doc_id)
                info = await factory.create_for_anime_channel(anime_doc_id)
            else:
                log.info("bot.orchestrator.recreating_bot", anime=anime_doc_id)
                info = await factory.create_for_anime(anime_doc_id)
        except NekoFetchError as exc:
            # Mirror ensure_bot_for_anime: a recreate is driven by the ban-health
            # watchdog / scheduler, so a domain error (feature disabled, entity
            # limit hit) must be logged and swallowed, not propagated into and
            # killing the background loop.
            log.warning("bot.orchestrator.recreate_failed", anime=anime_doc_id, error=str(exc))
            return None

        if info is not None:
            # A recreated *channel* is re-posted verbatim from its wipe-proof
            # backup (no re-render, no re-fetch) when a snapshot exists. Bots
            # deliver on /start, so they only need their content regenerated.
            restored = False
            if was_channel and info.chat_id:
                restored = await self._restore_channel(anime_doc_id, info.chat_id)
            await self._generate_content(info.id, anime_doc_id)
            if not restored:
                await self._bind_and_publish(info.id, anime_doc_id)
            else:
                # Content is already live on the fresh channel; just re-bind the
                # title (which also refreshes the main-channel post) instead of
                # re-publishing the channel from scratch.
                await self._bind_title(info.id, anime_doc_id)
                # The backup preserves the old buttons so the restore can happen
                # without metadata calls. Once fresh pack rows/content exist,
                # re-point each restored card at the new storage messages and
                # persist those links for the next backup.
                try:
                    from kurosoden.shared.senku_publisher import SenkuPublisher

                    await SenkuPublisher(self._c).relink_packs_in_place(
                        self._c.admin_client, anime_doc_id,
                    )
                except Exception as exc:  # noqa: BLE001 — cosmetic relink is best-effort
                    log.warning("bot.orchestrator.restore_relink_failed",
                                anime=anime_doc_id, error=str(exc))
            # Rebinding refreshes the main post's Download button. Follow it with
            # a reply so subscribers can find the replacement channel immediately.
            if was_channel and info is not None:
                try:
                    from nekofetch.services.main_channel_service import MainChannelService
                    main = MainChannelService(self._c)
                    channel_link = await main.distribution_link(anime_doc_id)
                    if channel_link:
                        async with session_scope(self._c.pg_sessionmaker) as session:
                            from nekofetch.infrastructure.database.postgres.models import StoragePack
                            pack = (
                                await session.execute(
                                    select(StoragePack)
                                    .where(StoragePack.anime_doc_id == anime_doc_id)
                                    .limit(1)
                                )
                            ).scalars().first()
                        await main.reply_recovery(
                            anime_doc_id,
                            pack.anime_title if pack else anime_doc_id,
                            channel_link,
                        )
                except Exception as exc:  # noqa: BLE001 — recovery reply is best-effort
                    log.warning("bot.orchestrator.recovery_reply_failed",
                                anime=anime_doc_id, error=str(exc))
            # A recreated channel gets a fresh private invite link (the row is new,
            # so gather_facts mints one on the main-channel refresh above). Refresh
            # this title's index letter too so its hyperlink points at the new link.
            await self._refresh_index_for(anime_doc_id)

        return info

    async def recover_human_channel(
        self,
        anime_doc_id: str,
        new_chat_id: int,
        *,
        username: str | None = None,
        name: str | None = None,
        client=None,
    ) -> BotInfo | None:
        """Adopt a human-created replacement channel and restore it in place.

        Unlike :meth:`recreate_bot`, this method never deletes the old row before
        the replacement has been verified. That matters for the human wizard: a
        mistyped channel id must not destroy the only live database reference.
        Once the replacement is registered, the old entity is disabled, the
        wipe-proof distribution backup is restored, and the existing relink/main
        reply/index refresh path is applied.
        """
        if not self._c.config.features.distribution_bots:
            return None

        from nekofetch.services.backup_service import BackupService
        from nekofetch.services.bot_management_service import BotManagementService

        # Capture before changing either entity. This is idempotent and preserves
        # the exact cards/buttons needed by the restore.
        await BackupService(self._c).record_distribution_channel(anime_doc_id)

        client = client or getattr(self._c, "admin_client", None)
        chat = None
        if client is not None:
            try:
                chat = await client.get_chat(new_chat_id)
            except Exception as exc:  # noqa: BLE001 — verification belongs to caller
                log.warning("bot.orchestrator.human_recovery_chat_lookup_failed",
                            anime=anime_doc_id, chat=new_chat_id, error=str(exc))
                return None
        resolved_name = name or getattr(chat, "title", None) or anime_doc_id
        resolved_username = username or getattr(chat, "username", None)
        if resolved_username:
            resolved_username = str(resolved_username).lstrip("@")

        try:
            info = await BotManagementService(self._c).register_channel(
                new_chat_id,
                name=resolved_name,
                username=resolved_username,
                anime_doc_id=anime_doc_id,
                creation_scope="human_recovery",
            )
        except Exception as exc:  # noqa: BLE001 — duplicate/invalid channel
            log.warning("bot.orchestrator.human_recovery_register_failed",
                        anime=anime_doc_id, chat=new_chat_id, error=str(exc))
            return None

        # Keep the old entity active while the replacement is restored and its
        # controlled invite is minted. Only switch the active row after every
        # subscriber-facing prerequisite has succeeded.
        old_ids: list[int] = []
        async with session_scope(self._c.pg_sessionmaker) as session:
            old_rows = (await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.id != info.id,
                    DistributionBot.enabled.is_(True),
                )
            )).scalars().all()
            old_ids = [int(old.id) for old in old_rows]

        # A previous attempt may have restored some cards before a later invite,
        # relink, or index step failed. Remove only messages tracked for this exact
        # replacement before reposting; this makes retry idempotent without wiping
        # unrelated human content in the channel.
        try:
            await BackupService(self._c).clear_distribution_channel(
                anime_doc_id, new_chat_id, client=client,
            )
        except Exception as exc:  # noqa: BLE001 — restore still reports failure safely
            log.warning("bot.orchestrator.recovery_cleanup_failed",
                        anime=anime_doc_id, chat=new_chat_id, error=str(exc))
            return await self._rollback_human_recovery(
                info.id, old_ids, anime_doc_id, client=client,
            )

        restored = await self._restore_channel(
            anime_doc_id, new_chat_id, client=client,
        )
        if not restored:
            log.warning("bot.orchestrator.human_recovery_restore_empty",
                        anime=anime_doc_id, chat=new_chat_id)
            return await self._rollback_human_recovery(
                info.id, old_ids, anime_doc_id, client=client,
            )

        # A human-created channel has no BotFactory setup step, so mint its fresh
        # controlled invite explicitly before refreshing the public surfaces.
        try:
            from nekofetch.services.invite_link_service import InviteLinkService

            invite = await InviteLinkService(self._c).ensure_for_bot(info.id)
        except Exception as exc:  # noqa: BLE001 — a failed invite is unsafe
            log.warning("bot.orchestrator.human_recovery_invite_failed",
                        anime=anime_doc_id, error=str(exc))
            invite = None
        if not invite:
            log.warning("bot.orchestrator.human_recovery_invite_empty",
                        anime=anime_doc_id, chat=new_chat_id)
            return await self._rollback_human_recovery(
                info.id, old_ids, anime_doc_id, client=client,
            )

        # Reuse the same post-restore behavior as automated recovery. Keep the
        # old row active until all subscriber-facing surfaces have refreshed.
        # buttons, backups, the main-channel reply, and the index synchronized.
        try:
            from kurosoden.shared.senku_publisher import SenkuPublisher

            await SenkuPublisher(self._c).relink_packs_in_place(
                client, anime_doc_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("bot.orchestrator.human_recovery_relink_failed",
                        anime=anime_doc_id, error=str(exc))
            return await self._rollback_human_recovery(
                info.id, old_ids, anime_doc_id, client=client,
            )
        if not await self._bind_title(info.id, anime_doc_id, client=client):
            return await self._rollback_human_recovery(
                info.id, old_ids, anime_doc_id, client=client,
            )

        try:
            from nekofetch.services.main_channel_service import MainChannelService

            main = MainChannelService(self._c)
            channel_link = await main.distribution_link(anime_doc_id)
            if channel_link:
                async with session_scope(self._c.pg_sessionmaker) as session:
                    from nekofetch.infrastructure.database.postgres.models import StoragePack
                    pack = (await session.execute(
                        select(StoragePack).where(
                            StoragePack.anime_doc_id == anime_doc_id
                        ).limit(1)
                    )).scalars().first()
                replied = await main.reply_recovery(
                    anime_doc_id,
                    pack.anime_title if pack else resolved_name,
                    channel_link,
                    client=client,
                )
                if not replied:
                    return await self._rollback_human_recovery(
                        info.id, old_ids, anime_doc_id, client=client,
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("bot.orchestrator.human_recovery_reply_failed",
                        anime=anime_doc_id, error=str(exc))
            return await self._rollback_human_recovery(
                info.id, old_ids, anime_doc_id, client=client,
            )
        if not await self._refresh_index_for(anime_doc_id, client=client):
            return await self._rollback_human_recovery(
                info.id, old_ids, anime_doc_id, client=client,
            )

        # Only now switch active lookups. The replacement is fully restored,
        # linked, published, and has a controlled invite.
        async with session_scope(self._c.pg_sessionmaker) as session:
            for old_id in old_ids:
                old = await session.get(DistributionBot, old_id)
                if old is not None:
                    old.enabled = False
        return info

    async def _rollback_human_recovery(
        self, replacement_id: int, old_ids: list[int], anime_doc_id: str,
        *, client=None,
    ) -> None:
        """Disable an incomplete replacement and restore subscriber-facing links."""
        invite_needs_revoke = False
        async with session_scope(self._c.pg_sessionmaker) as session:
            replacement = await session.get(DistributionBot, replacement_id)
            if replacement is not None:
                invite_needs_revoke = bool(replacement.invite_link)
                replacement.enabled = False
            for old_id in old_ids:
                old = await session.get(DistributionBot, old_id)
                if old is not None:
                    old.enabled = True

        # The replacement may already have refreshed the main/index surfaces before
        # a later step failed. Re-publish after the old row is enabled again so the
        # Download button and index link point back to the known-good channel.
        try:
            from nekofetch.services.index_channel_service import IndexChannelService
            from nekofetch.services.main_channel_service import MainChannelService

            await MainChannelService(self._c).publish(anime_doc_id, client=client)
            pack = (
                await self._first_storage_pack(anime_doc_id)
            )
            if pack is not None:
                index = IndexChannelService(self._c, client=client)
                await index.refresh_letter(index.letter_of(pack.anime_title))
        except Exception as exc:  # noqa: BLE001 — rollback remains DB-safe
            log.warning("bot.orchestrator.rollback_surface_refresh_failed",
                        anime=anime_doc_id, error=str(exc))

        if invite_needs_revoke:
            try:
                from nekofetch.services.invite_link_service import InviteLinkService

                await InviteLinkService(self._c).revoke_for_bot(
                    replacement_id, client=client,
                )
            except Exception as exc:  # noqa: BLE001 — stale invite is logged for cleanup
                log.warning("bot.orchestrator.rollback_invite_revoke_failed",
                            anime=anime_doc_id, error=str(exc))
        log.warning("bot.orchestrator.human_recovery_rolled_back",
                    anime=anime_doc_id, replacement=replacement_id)
        return None

    async def _first_storage_pack(self, anime_doc_id: str):
        async with session_scope(self._c.pg_sessionmaker) as session:
            return (
                await session.execute(
                    select(StoragePack)
                    .where(StoragePack.anime_doc_id == anime_doc_id)
                    .limit(1)
                )
            ).scalars().first()

    async def _refresh_index_for(self, anime_doc_id: str, *, client=None) -> bool:
        """Rebuild the index letter section for this title (best-effort).

        The index caption hyperlinks each title to its channel's private invite
        link; after a recreate mints a new link, the letter must be rebuilt so the
        hyperlink follows. Resolved from the title's storage packs."""
        try:
            from nekofetch.infrastructure.database.postgres.models import StoragePack
            from nekofetch.services.index_channel_service import IndexChannelService

            async with session_scope(self._c.pg_sessionmaker) as session:
                pack = (
                    await session.execute(
                        select(StoragePack)
                        .where(StoragePack.anime_doc_id == anime_doc_id)
                        .limit(1)
                    )
                ).scalars().first()
            if pack is None:
                return False
            svc = IndexChannelService(self._c, client=client)
            return await svc.refresh_letter(svc.letter_of(pack.anime_title)) is not None
        except Exception as exc:  # noqa: BLE001 — index refresh is best-effort
            log.warning("bot.orchestrator.index_refresh.failed",
                        anime=anime_doc_id, error=str(exc))
            return False

    async def _restore_channel(
        self, anime_doc_id: str, new_chat_id: int, *, client=None,
    ) -> bool:
        """Re-post a banned channel verbatim from backup. True if anything posted."""
        try:
            from nekofetch.services.backup_service import BackupService

            stats = await BackupService(self._c).restore_distribution_channel(
                anime_doc_id, new_chat_id, client=client,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to regeneration
            log.warning("bot.orchestrator.restore_failed",
                        anime=anime_doc_id, error=str(exc))
            return False
        if stats.total > 0 and stats.restored == stats.total and stats.failed == 0:
            log.info("bot.orchestrator.channel_restored", anime=anime_doc_id,
                     restored=stats.restored, failed=stats.failed)
            return True
        log.warning("bot.orchestrator.channel_restore_incomplete", anime=anime_doc_id,
                    total=stats.total, restored=stats.restored, failed=stats.failed)
        return False

    async def _bind_title(
        self, bot_id: int, anime_doc_id: str, *, client=None,
    ) -> bool:
        """Bind the entity and report whether the main surface refreshed."""
        from nekofetch.services.bot_management_service import BotManagementService

        try:
            return await BotManagementService(self._c).bind_title(
                bot_id, anime_doc_id, client=client,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("bot.orchestrator.bind.failed", bot_id=bot_id, error=str(exc))
            return False

    async def _find_existing_bot(self, anime_doc_id: str) -> BotInfo | None:
        """Find an existing enabled bot/channel bound to this anime."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (
                await session.execute(
                    select(DistributionBot).where(
                        DistributionBot.anime_doc_id == anime_doc_id,
                        DistributionBot.enabled.is_(True),
                    )
                )
            ).scalars().first()
            if bot is None:
                return None
            return BotInfo(
                id=bot.id, name=bot.name, username=bot.username,
                enabled=bot.enabled, is_channel=bot.is_channel,
                chat_id=bot.chat_id,
            )

    async def _generate_content(self, bot_id: int, anime_doc_id: str) -> None:
        """Generate and store content posts for a bot."""
        from nekofetch.services.bot_content import BotContentService

        try:
            await BotContentService(self._c).generate_posts(bot_id, anime_doc_id)
        except Exception as exc:
            log.warning("bot.orchestrator.content.failed", bot_id=bot_id, error=str(exc))

    async def _bind_and_publish(self, bot_id: int, anime_doc_id: str) -> None:
        """Bind the bot to the title, apply branding, and refresh main channel."""
        from nekofetch.services.bot_management_service import BotManagementService
        from nekofetch.services.main_channel_service import MainChannelService

        try:
            await BotManagementService(self._c).bind_title(bot_id, anime_doc_id)
        except Exception as exc:
            log.warning("bot.orchestrator.bind.failed", bot_id=bot_id, error=str(exc))

        try:
            await MainChannelService(self._c).publish(anime_doc_id)
        except Exception as exc:
            log.warning("bot.orchestrator.mainchannel.failed", anime=anime_doc_id, error=str(exc))
