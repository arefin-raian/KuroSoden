"""Automatic distribution-entity creation — bots via @BotFather, channels via userbot.

A bot's token AND its profile photo can only be obtained/set through @BotFather, so
this drives a real BotFather conversation with the user account from the userbot pool:

    /newbot → <name> → <username> → token
    /setuserpic → @username → <photo>
    /setdescription / /setabouttext → @username → <text>

A channel requires only a Pyrogram ``create_channel`` call — no BotFather is involved.

Capacity model (configurable):
    • max 20 bots per account
    • max 10 channels per account
When the bot limit is exhausted for a userbot session, the orchestrator falls back
on channel creation.

The created token/chat_id is then handed to :class:`BotManagementService` which
validates, encrypts, stores, and (for bots) brings them online.

⚠️ The BotFather conversation is wording-sensitive and rate-limited; this is written
defensively (token regex, username-taken retries, bounded waits) but should be
exercised live once — it cannot be unit-tested against the real BotFather.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
from sqlalchemy import func, select

from nekofetch.core.container import Container
from nekofetch.core.exceptions import NekoFetchError
from nekofetch.core.logging import get_logger
from nekofetch.services.bot_naming import format_bot_name, format_bot_username

log = get_logger(__name__)

_BOTFATHER = "BotFather"
_TOKEN_RE = re.compile(r"(\d{6,}:[A-Za-z0-9_-]{30,})")


class BotFactory:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._pool = None

    def _userbot(self):
        if self._pool is None:
            from nekofetch.sources.telegram.userbot import UserbotPool

            self._pool = UserbotPool.from_env(
                self._c.env.telegram_api_id, self._c.env.telegram_api_hash,
                str(self._c.env.session_path),
            )
        return self._pool

    # ── public entry ─────────────────────────────────────────────────────────────
    async def create_for_anime(self, anime_doc_id: str) -> "BotInfo":
        """Create + configure a distribution bot for a published title, then register
        it. Returns the BotInfo from BotManagementService.

        Checks capacity first: if bots per account are exhausted, creates a channel
        instead (calls ``create_for_anime_channel``).
        """
        if not self._c.config.features.distribution_bots:
            raise NekoFetchError("distribution_bots feature is disabled")

        cfg = self._c.config.bot
        counts = await self._count_entities()

        # Exhausted bot limit → fall back to channel.
        if counts["bots"] >= cfg.max_bots_per_account:
            log.info("botfactory.bot_limit_exhausted", bots=counts["bots"],
                     max=cfg.max_bots_per_account)
            return await self.create_for_anime_channel(anime_doc_id)

        meta = await self._gather(anime_doc_id)
        name = format_bot_name(meta["english"], meta["romaji"],
                               audios=meta["audios"], languages=meta["languages"],
                               qualities=meta["qualities"])
        username = format_bot_username(meta["english"] or meta["romaji"] or "anime",
                                       anime_doc_id, is_channel=False)
        description = self._build_description(meta)
        about = self._build_about(meta)
        avatar = await self._fetch_avatar(meta["english"] or meta["romaji"] or "")

        log.info("botfactory.create", anime=anime_doc_id, name=name, username=username)
        token = await self._userbot().execute(
            lambda c: self._botfather_create(c, name, username, avatar, description, about)
        )
        if avatar:
            try:
                avatar.unlink()
            except OSError:
                pass

        from nekofetch.services.bot_management_service import BotManagementService, BotInfo

        return await BotManagementService(self._c).register(
            token, name=name, anime_doc_id=anime_doc_id,
        )

    async def create_for_anime_channel(self, anime_doc_id: str) -> "BotInfo":
        """Create a public channel for a published title via the userbot account.

        Channels support proper ``t.me/c/`` deep links (unlike bots in private
        chats), so the quality text in the watch guide can be clickable.
        Content generation and delivery are identical to bots.
        """
        if not self._c.config.features.distribution_bots:
            raise NekoFetchError("distribution_bots feature is disabled")

        cfg = self._c.config.bot
        counts = await self._count_entities()
        if counts["channels"] >= cfg.max_channels_per_account:
            raise NekoFetchError(
                f"Channel limit exhausted ({counts['channels']}/{cfg.max_channels_per_account})"
            )

        meta = await self._gather(anime_doc_id)
        name = format_bot_name(meta["english"], meta["romaji"],
                               audios=meta["audios"], languages=meta["languages"],
                               qualities=meta["qualities"])
        username = format_bot_username(meta["english"] or meta["romaji"] or "anime",
                                       anime_doc_id, is_channel=True)

        log.info("botfactory.create_channel", anime=anime_doc_id, name=name, username=username)

        from nekofetch.services.bot_management_service import BotManagementService, BotInfo

        chat_id = await self._userbot().execute(
            lambda c: self._create_channel(c, name, username)
        )

        return await BotManagementService(self._c).register_channel(
            chat_id, name=name, username=username, anime_doc_id=anime_doc_id,
        )

    async def create_channel_via_userbot(self, anime_doc_id: str) -> "BotInfo | None":
        """Create a fully-configured channel on a quota-picked userbot session.

        The "userbot" scope of the two-scope flow (feature #41): a pooled session
        creates the channel and sets its title, username **and** description itself
        (the admin only adds the profile picture + clears the service message). We
        pick a random account that still has a free channel slot — never revealing
        which — record the owning account for quota accounting, and mint the private
        invite link. Returns ``None`` when every session is at capacity so the caller
        can fall back to the "own" (admin-creates) scope.
        """
        if not self._c.config.features.distribution_bots:
            raise NekoFetchError("distribution_bots feature is disabled")

        from nekofetch.services.channel_quota_service import ChannelQuotaService

        account = await ChannelQuotaService(self._c).pick_available()
        if account is None:
            log.warning("botfactory.userbot.no_free_account", anime=anime_doc_id)
            return None

        meta = await self._gather(anime_doc_id)
        name = format_bot_name(meta["english"], meta["romaji"],
                               audios=meta["audios"], languages=meta["languages"],
                               qualities=meta["qualities"])
        username = format_bot_username(meta["english"] or meta["romaji"] or "anime",
                                       anime_doc_id, is_channel=True)
        description = self._build_description(meta)

        log.info("botfactory.create_channel_userbot", anime=anime_doc_id,
                 name=name, username=username, account=account)

        # Create + configure entirely on the chosen account so the channel it owns
        # is the same one we tally against that account's quota.
        chat_id = await self._userbot().execute_on(
            account,
            lambda c: self._create_and_configure_channel(c, name, username, description),
        )

        from nekofetch.services.bot_management_service import BotManagementService

        info = await BotManagementService(self._c).register_channel(
            chat_id, name=name, username=username, anime_doc_id=anime_doc_id,
            creation_scope="userbot", userbot_account=account,
        )
        # Add the pipeline bots (Senku + Gojo) as admins now — the userbot owns the
        # channel so it can promote them directly. Both need admin rights to post
        # the pack / publish; best-effort so a promote hiccup doesn't lose the channel.
        try:
            await self._promote_bots(account, chat_id)
        except Exception as exc:  # noqa: BLE001 — bots can be re-added manually
            log.warning("botfactory.userbot.promote_bots_failed",
                        chat_id=chat_id, error=str(exc))
        # Mint + store the private invite link now (same account owns the channel).
        try:
            from nekofetch.services.invite_link_service import InviteLinkService

            await InviteLinkService(self._c).ensure_for_bot(info.id)
        except Exception as exc:  # noqa: BLE001 — link is best-effort
            log.warning("botfactory.userbot.invite_failed",
                        anime=anime_doc_id, error=str(exc))
        return info

    def _pipeline_bot_refs(self) -> list[str | int]:
        """Resolve the Senku + Gojo bot identities to add as channel admins.

        Reads the live user id off each running pipeline client (populated after
        ``Client.start()``); falls back to the ``@username`` when the id isn't
        cached yet. Empty when the pipeline manager isn't wired (e.g. tests)."""
        refs: list[str | int] = []
        mgr = getattr(self._c, "pipeline_manager", None)
        if mgr is None:
            return refs
        for name in ("senku", "gojo"):
            client = getattr(mgr, name, None)
            me = getattr(client, "me", None) if client is not None else None
            if me is not None and getattr(me, "id", None):
                refs.append(me.id)
            elif me is not None and getattr(me, "username", None):
                refs.append(me.username)
        return refs

    async def _promote_bots(self, account: str, chat_id: int) -> None:
        """Add + promote the Senku and Gojo bots as admins on ``chat_id``.

        Runs on the channel-owning ``account`` (the only session that can promote).
        Each bot is added then given content + info rights; per-bot failures are
        logged and skipped so one bad promote never blocks the other."""
        refs = self._pipeline_bot_refs()
        if not refs:
            log.warning("botfactory.userbot.no_bot_refs", chat_id=chat_id)
            return

        async def _do(client) -> None:
            from pyrogram.types import ChatPrivileges

            privileges = ChatPrivileges(
                can_change_info=True, can_post_messages=True,
                can_edit_messages=True, can_delete_messages=True,
                can_invite_users=True, can_pin_messages=True,
                can_manage_chat=True,
            )
            for ref in refs:
                try:
                    await client.add_chat_members(chat_id, ref)
                except Exception as exc:  # noqa: BLE001 — may already be a member
                    log.debug("botfactory.userbot.add_bot_skipped",
                              ref=ref, error=str(exc))
                try:
                    await client.promote_chat_member(chat_id, ref, privileges=privileges)
                    log.info("botfactory.userbot.bot_promoted", chat_id=chat_id, ref=ref)
                except Exception as exc:  # noqa: BLE001
                    log.warning("botfactory.userbot.promote_failed",
                                ref=ref, error=str(exc))

        await self._userbot().execute_on(account, _do)

    async def promote_operator(self, chat_id: int, operator_id: int) -> bool:
        """Promote the requesting operator to admin on a userbot-created channel.

        Called after the operator joins via the invite link (you can only promote
        an existing member). Grants ``can_change_info`` so they can set the profile
        picture — the whole reason they need admin rights. Runs on the channel's
        owning userbot account (looked up from the row). Returns True on success."""
        from nekofetch.infrastructure.database.postgres.models import DistributionBot
        from nekofetch.infrastructure.database.postgres.session import session_scope

        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(DistributionBot).where(
                        DistributionBot.chat_id == chat_id,
                        DistributionBot.is_channel.is_(True),
                    )
                )
            ).scalar_one_or_none()
            account = row.userbot_account if row else None

        if not account:
            log.warning("botfactory.userbot.promote_operator_no_account", chat_id=chat_id)
            return False

        async def _do(client) -> bool:
            from pyrogram.types import ChatPrivileges

            await client.promote_chat_member(
                chat_id, operator_id,
                privileges=ChatPrivileges(
                    can_change_info=True, can_post_messages=True,
                    can_edit_messages=True, can_delete_messages=True,
                    can_invite_users=True, can_pin_messages=True,
                ),
            )
            return True

        try:
            await self._userbot().execute_on(account, _do)
            log.info("botfactory.userbot.operator_promoted",
                     chat_id=chat_id, operator=operator_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("botfactory.userbot.promote_operator_failed",
                        chat_id=chat_id, operator=operator_id, error=str(exc))
            return False

    async def _create_and_configure_channel(
        self, client, name: str, username: str, description: str,
    ) -> int:
        """Create a channel and set its title/username/description via the userbot.

        Bots can't create channels, but the userbot owns this one, so it sets
        everything the admin would otherwise paste — leaving only the profile
        picture (and clearing the service message) to the human."""
        chat = await client.create_channel(title=name, description=description or "")
        chat_id = chat.id  # type: ignore[union-attr]
        # Public username is a separate call (create_channel doesn't take it).
        try:
            await client.set_chat_username(chat_id, username)
        except Exception as exc:  # noqa: BLE001 — username may be taken; admin can fix
            log.warning("botfactory.userbot.username_failed",
                        username=username, error=str(exc))
        log.info("botfactory.userbot.channel_created", name=name, chat_id=chat_id)
        return chat_id

    # ── capacity ────────────────────────────────────────────────────────────────
    async def _count_entities(self) -> dict:
        """Count bots and channels across all userbot accounts."""
        from nekofetch.infrastructure.database.postgres.models import DistributionBot
        from nekofetch.infrastructure.database.postgres.session import session_scope

        async with session_scope(self._c.pg_sessionmaker) as session:
            bot_count = (
                await session.execute(
                    select(func.count()).where(
                        DistributionBot.is_channel.is_(False),
                        DistributionBot.enabled.is_(True),
                    )
                )
            ).scalar_one()
            channel_count = (
                await session.execute(
                    select(func.count()).where(
                        DistributionBot.is_channel.is_(True),
                        DistributionBot.enabled.is_(True),
                    )
                )
            ).scalar_one()
        return {"bots": bot_count, "channels": channel_count}

    # ── channel creation ────────────────────────────────────────────────────────
    async def _create_channel(self, client, name: str, username: str) -> int:
        """Create a public channel via Pyrogram's userbot client.

        Returns the channel's chat_id (negative int, e.g. -1001234567890).
        """
        chat = await client.create_channel(title=name, username=username)
        chat_id = chat.id  # type: ignore[union-attr]
        log.info("botfactory.channel_created", name=name, username=username,
                 chat_id=chat_id)
        return chat_id

    # ── metadata ─────────────────────────────────────────────────────────────────
    async def _gather(self, anime_doc_id: str) -> dict:
        """Resolve the bot's display-name ingredients from Postgres.

        Audios/qualities come from ``StoragePack`` (the destination-of-truth
        table for what's ACTUALLY available in the storage channel), not
        ``MediaFile`` (whose rows can become stale once the upload-and-clean
        step has finished). The bot name needs all three pieces (audio type,
        language label, qualities) so the user sees a single identifiable
        tag inside Telegram's 64-char name cap.
        """
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import (
            Request, StoragePack,
        )
        from nekofetch.infrastructure.database.postgres.session import session_scope

        english = romaji = ""
        audios: set = set()
        quals: set = set()
        async with session_scope(self._c.pg_sessionmaker) as session:
            # Strip anilist: prefix when searching the DB — the request's
            # anime_doc_id field stores the clean title, source_ref is the
            # fallback that may carry the anilist: prefix.
            lookup = anime_doc_id
            if lookup.startswith("anilist:"):
                lookup = lookup[len("anilist:"):]
            req = (await session.execute(
                select(Request).where(Request.anime_doc_id == lookup)
                .order_by(Request.id.desc())
            )).scalars().first()
            if req is None:
                req = (await session.execute(
                    select(Request).where(Request.source_ref == f"anilist:{lookup}")
                    .order_by(Request.id.desc())
                )).scalars().first()
            if req is not None:
                fr = req.franchise_data or {}
                english = fr.get("english") or req.anime_title or ""
                romaji = fr.get("romaji") or ""
            # ── Storage packs are the canonical source of "what does this title
            # actually carry": every pack has a non-null audio + resolution
            # column tied to its anime_doc_id, so we get a definitive set
            # instead of living with whatever MediaFile still has around.
            packs = (await session.execute(
                select(StoragePack).where(
                    StoragePack.anime_doc_id == anime_doc_id,
                    StoragePack.enabled.is_(True),
                )
            )).scalars().all()
            audios = {p.audio.value for p in packs if p.audio is not None}
            quals = {p.resolution for p in packs if p.resolution}
        # Real per-pack languages when probed, else the AudioType enum fallback —
        # via the shared resolver, so a genuine Eng/Jpn/Kor release names itself
        # "Korean" instead of the enum's hardcoded Hindi assumption.
        from nekofetch.services.audio_langs import pack_languages
        languages = pack_languages(packs)
        # Sort qualities by resolution.
        _QORDER = {"360p": 0, "480p": 1, "540p": 2, "720p": 3, "1080p": 4, "2160p": 5}
        quality_list = sorted(quals, key=lambda q: _QORDER.get(q, 99)) if quals else []
        # ── Romaji fallback (so the decorated channel title isn't bare) ──
        # Some requests are stored with a NULL franchise_data.romaji (e.g. a
        # torrent accepted without an AniList id resolving), which collapses
        # ``format_channel_title`` to the bare English name and then trips
        # Telegram's CHAT_NOT_MODIFIED on the wizard rename. Backfill romaji from
        # the resilient metadata chain (AniList → Kaggle → Jikan → Kitsu) exactly
        # like the main-channel post does (main_channel_service._enrich_facts_
        # fallback). Cache-first (offline) then a single live search, keyed by the
        # English title so a missing anilist_id doesn't matter.
        if not romaji and english:
            romaji = await self._resolve_romaji_fallback(anime_doc_id, english)
        return {"english": english, "romaji": romaji, "audios": audios,
                "languages": languages, "qualities": quality_list}

    async def _resolve_romaji_fallback(self, anime_doc_id: str, english: str) -> str:
        """Best-effort romaji when ``franchise_data`` didn't carry it.

        Tries the prefetched ``anilist.json["search"]`` first (no network), then a
        live *resilient* ``container.anilist.search`` (which cascades AniList →
        Kaggle → Jikan → Kitsu). Returns ``""`` on every miss — a bare title is
        still better than raising inside channel creation.
        """
        # 1) Prefetch cache — offline, keyed by the same doc-id the folder uses.
        try:
            from nekofetch.services.metadata_prefetch import load_cached
            blob = await load_cached(self._c, anime_doc_id, "anilist",
                                     anime_doc_id=anime_doc_id)
            cached = (blob or {}).get("search") or {}
            rom = (cached.get("romaji") or "").strip()
            if rom:
                return rom
        except Exception as exc:  # noqa: BLE001 — cache miss → live below
            log.debug("botfactory.romaji_cache.failed", anime=anime_doc_id,
                      error=str(exc))
        # 2) Live resilient search by title.
        try:
            anilist = getattr(self._c, "anilist", None)
            media = await anilist.search(english) if anilist else None
            rom = (getattr(media, "romaji", None) or "").strip() if media else ""
            if rom:
                return rom
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.debug("botfactory.romaji_live.failed", anime=anime_doc_id,
                      error=str(exc))
        return ""

    # ── AniXWeebs branding block ────────────────────────────────────────────────
    # The owner's exact about/description text. Telegram enforces:
    #   * /setdescription   — 512 char limit (long form)
    #   * /setabouttext     —  120 char limit (short form)
    # This block fits the long form; the short form trims to the most-essential
    # two lines so the limit is respected without dropping information.
    _BRANDING_DESCRIPTION = (
        "➥ 𝗠𝗮𝗶𝗻 𝗖𝗵𝗮𝗻𝗻𝗲𝗹: @AniXWeebs\n"
        "➥ 𝗜𝗻𝗱𝗲𝘅: @AniXWeebs_Index\n"
        "➥ 𝗢𝗻𝗴𝗼𝗶𝗻𝗴: @Ongoing_AniXWeebs\n"
        "➥ 𝗠𝗼𝘃𝗶𝗲𝘀: @AniMovieXWeebs\n"
        "➥ 𝗡𝗲𝘁𝘄𝗼𝗿𝗸: @WeebsXServer"
    )
    # Trimmed for the 120-char short-description (about) Telegram limit.
    _BRANDING_ABOUT = "➥ 𝗠𝗮𝗶𝗻 𝗖𝗵𝗮𝗻𝗻𝗲𝗹: @AniXWeebs | 𝗡𝗲𝘁𝘄𝗼𝗿𝗸: @WeebsXServer"

    @staticmethod
    def _build_description(meta: dict) -> str:
        """Build the bot's full description (Telegram /setdescription, 512 chars).

        Uses the owner's brand block by default and prepends the title so the
        bot's profile shows both WHAT it serves and WHERE the network lives.
        An operator can override via ``cfg.bot.description_text``.
        """
        from nekofetch.core.config import get_app_config
        cfg = get_app_config()
        override = (getattr(cfg.bot, "description_text", "") or "").strip()
        if override:
            return override[:512]
        title = (meta.get("english") or meta.get("romaji") or "").strip()
        # Fallback when override is unset AND there's nothing to brand against.
        if not (title or BotFactory._BRANDING_DESCRIPTION):
            return ""
        # BRANDING_DESCRIPTION is already ≤512 chars on its own. Prepending the
        # title in plain text (no newline glitch) keeps both readable.
        if not title:
            return BotFactory._BRANDING_DESCRIPTION[:512]
        return f"{title}\n\n{BotFactory._BRANDING_DESCRIPTION}"[:512]

    @staticmethod
    def _build_about(meta: dict) -> str:
        """Build the bot's short description (Telegram /setabouttext, 120 chars).

        Returns a compact two-line block trimmed to fit the Telegram limit.
        Operators may override via ``cfg.bot.about_text``.
        """
        from nekofetch.core.config import get_app_config
        cfg = get_app_config()
        override = (getattr(cfg.bot, "about_text", "") or "").strip()
        if override:
            return override[:120]
        title = (meta.get("english") or meta.get("romaji") or "").strip()
        # When no title is available, return the trimmed brand block as-is.
        # When title IS present, prepend it and truncate to 120 chars so the
        # short-description stays within Telegram's hard limit.
        if not title:
            return BotFactory._BRANDING_ABOUT[:120]
        joined = f"{title} | {BotFactory._BRANDING_ABOUT}"
        if len(joined) <= 120:
            return joined
        return BotFactory._BRANDING_ABOUT[:120]

    async def _fetch_avatar(self, title: str) -> Path | None:
        """Download a DIFFERENT TMDB poster (rank 1) for the bot's profile photo.

        We deliberately do NOT composite a background or overlay here: the user
        wants the raw poster uploaded as-is and let Telegram handle the
        square crop.
        """
        if not title:
            return None
        try:
            # w780 (not w500) keeps the poster sharp after Telegram's profile
            # cropper; ``original`` is rejected sometimes by BotFather's
            # /setuserpic on multi-MB movie posters, so w780 is the safe choice.
            url = await self._c.tmdb.poster_for(title, size="w780", rank=1)
        except Exception:  # noqa: BLE001
            url = None
        if not url:
            return None
        dest = Path(self._c.env.storage_path) / "work" / "_avatars" / f"{abs(hash(title))}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cli:
                r = await cli.get(url)
                r.raise_for_status()
                dest.write_bytes(r.content)
            return dest
        except Exception as exc:  # noqa: BLE001
            log.warning("botfactory.avatar.failed", error=str(exc))
            return None

    # ── BotFather conversation ───────────────────────────────────────────────────
    async def _botfather_create(self, client, name: str, username: str,
                                avatar: Path | None, description: str,
                                about: str = "") -> str:
        await self._say(client, "/newbot")
        await self._say(client, name)
        reply = await self._say(client, username)

        attempts = 0
        while reply and re.search(r"taken|invalid|sorry|too short|letters", reply, re.I):
            attempts += 1
            if attempts > 8:
                raise NekoFetchError(f"BotFather rejected all usernames: {reply[:120]}")
            username = self._bump(username, attempts)
            reply = await self._say(client, username)

        m = _TOKEN_RE.search(reply or "")
        if not m:
            raise NekoFetchError(f"BotFather did not return a token: {(reply or '')[:160]}")
        token = m.group(1)

        # Profile photo (must go through BotFather).
        if avatar and avatar.exists():
            try:
                await self._say(client, "/setuserpic")
                await self._say(client, f"@{username}")
                await client.send_photo(_BOTFATHER, str(avatar))
                await asyncio.sleep(2.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("botfactory.setuserpic.failed", error=str(exc))

        # Placeholder description + about (the owner edits these later via en.json).
        if description:
            for cmd, text in (("/setdescription", description),
                              ("/setabouttext", (about or description)[:120])):
                try:
                    await self._say(client, cmd)
                    await self._say(client, f"@{username}")
                    await self._say(client, text)
                except Exception as exc:  # noqa: BLE001
                    log.warning("botfactory.setinfo.failed", cmd=cmd, error=str(exc))

        log.info("botfactory.created", username=username)
        return token

    @staticmethod
    def _bump(username: str, n: int) -> str:
        stem = username[:-3] if username.endswith("bot") else username
        stem = stem.rstrip("_0123456789")[: 32 - len(f"{n}bot")]
        return f"{stem}{n}bot"

    async def _say(self, client, text: str, *, wait: float = 2.5) -> str:
        """Send a line to BotFather and return its next reply text (best-effort)."""
        await client.send_message(_BOTFATHER, text)
        await asyncio.sleep(wait)
        async for msg in client.get_chat_history(_BOTFATHER, limit=1):
            return msg.text or msg.caption or ""
        return ""
