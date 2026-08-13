"""Senku's channel publisher — Phase 4 of the distribution flow.

Once the admin confirms the watch order (Phase 4), this module posts the finished
content pack straight into the distribution channel Senku's client already admins.
It is the manual-flow counterpart to the automated distribution bot: same cards,
same choreography, same URL buttons — just driven by the admin's *confirmed*
watch order rather than a fresh AniList walk.

Why not call :meth:`BotContentService.generate_posts` directly? That method

  * requires a persisted :class:`DistributionBot` row (our channel lives only in
    :class:`DistributionCache`, keyed by request code — there is no bot row), and
  * re-walks AniList to derive the ordering, which would discard the ordering the
    admin just confirmed/edited in Phase 4.

So this publisher *reuses* every card builder on :class:`BotContentService`
(``_build_info_card`` / ``_build_season_card`` / ``_build_franchise_watch_guide``
/ ``_build_season_buttons``) but feeds them a franchise whose ``tv``/``extras``/
``all`` lists are reordered to match the confirmed cache, and bridges the admin's
locally-rendered ``file://`` thumbnails to public catbox URLs so Telegram can
serve them. Posting mirrors the distribution app's ``_send_posts``: divider
stickers between sections, URL buttons from ``button_data.links``, and a pinned
info card + watch guide with the "pinned this message" service notices swept.

Best-effort throughout the *delivery* half: a single failed card is logged and
skipped so a partial channel still reaches users. The *build* half raises on a
hard failure (no packs, no franchise) so the wizard can show ``PUBLISH_FAIL``
rather than pin an empty channel.
"""

from __future__ import annotations

from pathlib import Path

from pyrogram.enums import ParseMode

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.services.bot_render import build_audio_keyboard, resolve_premium_emoji

from kurosoden.shared.distribution_cache import DistributionCache, EntryData

log = get_logger(__name__)

# TV formats, mirrored from bot_content so we split cached entries the same way.
# Multi-episode ONAs count as seasons (see franchise_flow.entry_is_season) so
# the reordered franchise split matches the mapping + guide exactly.
_TV_FORMATS = {"TV", "TV_SHORT", "TV_SPECIAL"}


def _is_season_entry(entry) -> bool:
    """Season check shared with the franchise mapping / watch guide."""
    from nekofetch.services.franchise_flow import entry_is_season

    return entry_is_season(entry)


class PublishError(RuntimeError):
    """Raised when the content pack can't be built (no packs, no franchise)."""


class SenkuPublisher:
    """Post a confirmed distribution channel's content pack into its Telegram chat."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self.cache = DistributionCache(container)

    # ── public entry ───────────────────────────────────────────────────────────

    async def publish(self, client, code: str) -> dict:
        """Build and post the full content pack for ``code`` into its channel.

        Returns a summary dict ``{title, chat_id, posted, pinned}``. Raises
        :class:`PublishError` when there's nothing publishable (no channel,
        no franchise, no packs) so the caller shows a failure card.
        """
        channel = await self.cache.get_channel(code)
        if not channel or not channel.get("chat_id"):
            raise PublishError(f"no verified channel for {code}")
        chat_id = int(channel["chat_id"])
        handle = channel.get("handle")

        posts, title, anime_doc_id = await self._build_posts(code)
        if not posts:
            raise PublishError(f"no content to publish for {code}")

        # Push the channel into Telegram Global Search BEFORE the real posts:
        # send ~100 throwaway messages then delete them all, so the real info
        # card/watch guide land in a channel that already qualifies for search.
        # Idempotent per channel (guarded by a Redis flag) and fully best-effort.
        await self._warm_global_search(client, chat_id, code)

        posted, pinned, layout = await self._send_posts(client, chat_id, posts)

        # Register a durable channel anchor + persist the message layout so a
        # later franchise update can find this channel and its footer/divider
        # message ids. The manual (wizard) flow has no DistributionBot row yet;
        # the auto pipeline already made one — register_channel is idempotent.
        try:
            await self._persist_channel(
                anime_doc_id, chat_id, title, handle, layout,
            )
        except Exception as exc:  # noqa: BLE001 — a publish still succeeds
            log.warning("senku.publish.persist_failed",
                        code=code, anime=anime_doc_id, error=str(exc))

        # Snapshot the posted pack into a wipe-proof backup so a later ban can
        # restore it verbatim (no re-render). Best-effort — never fail a publish.
        try:
            from nekofetch.services.backup_service import BackupService

            await BackupService(self._c).record_distribution_channel(anime_doc_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("senku.publish.backup_failed",
                        code=code, anime=anime_doc_id, error=str(exc))

        log.info("senku.publish.done", code=code, chat_id=chat_id,
                 posted=posted, pinned=len(pinned))
        return {"title": title, "chat_id": chat_id, "posted": posted,
                "pinned": pinned}

    async def update_distribution_channel(
        self, client, anime_doc_id: str, new_anilist_ids: list[int] | None = None,
    ) -> dict:
        """Incrementally update a published channel with newly-finished entries.

        Phase 6 franchise-update path: a new season/extra finished the pipeline,
        so we append *only its* card(s) to the channel it belongs to. We do
        **not** re-render or repost the whole channel, and we never touch the
        main channel.

        Choreography (season auto-update): the watch guide is a franchise-wide
        summary, so a new season must *rebuild* it — not leave a stale one mid
        channel. We strip the watch-guide post and everything after it (its
        surrounding dividers + the footer), then re-emit the tail fresh:

            … <last existing card>
            → divider → new season card [→ divider → next new card …]
            → divider → watch guide (regenerated, re-pinned)
            → divider → footer

        The regenerated guide's quality links deep-link to each season's card
        message (existing cards' ids come from the saved layout, new ones from
        this run), and :class:`ChannelLayout` is rewritten to match. The pinned
        info card up top is left exactly where it is; the "pinned" service notice
        from re-pinning the guide is swept.

        ``new_anilist_ids`` restricts the update to those entries (the update
        checker knows which entry just finished). When ``None`` we reconcile:
        every entry that now has packs but isn't already in the layout.

        Returns ``{"appended": n, "chat_id": id}``. A no-op (no channel row, no
        new cards) returns ``appended=0`` — never raises for the "nothing to do"
        case, so a normal pipeline run without updates stays quiet.
        """
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import (
            ChannelLayout,
            DistributionBot,
        )
        from nekofetch.infrastructure.database.postgres.session import session_scope

        # 1. Resolve the durable channel anchor + its saved layout.
        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (
                await session.execute(
                    select(DistributionBot).where(
                        DistributionBot.anime_doc_id == anime_doc_id,
                        DistributionBot.is_channel.is_(True),
                    ).order_by(DistributionBot.id.desc())
                )
            ).scalars().first()
            if bot is None or not bot.chat_id:
                log.info("senku.update.no_channel", anime=anime_doc_id)
                return {"appended": 0, "chat_id": None}
            bot_id = bot.id
            chat_id = int(bot.chat_id)
            layout_rows = (
                await session.execute(
                    select(ChannelLayout)
                    .where(ChannelLayout.channel_bot_id == bot_id)
                    .order_by(ChannelLayout.seq)
                )
            ).scalars().all()
            layout = [
                {"kind": r.kind, "tg_message_id": r.tg_message_id,
                 "anilist_id": r.anilist_id, "is_pinned": r.is_pinned}
                for r in layout_rows
            ]

        already = {
            item["anilist_id"] for item in layout
            if item["anilist_id"] is not None
        }

        # 2. Build cards for the new entries AND regenerate the franchise-wide
        #    watch guide (it now covers the new season).
        new_cards, guide_caption = await self._build_update_cards(
            anime_doc_id, new_anilist_ids, already,
        )
        if not new_cards:
            log.info("senku.update.nothing", anime=anime_doc_id)
            return {"appended": 0, "chat_id": chat_id}

        # 3. Re-choreograph the tail: strip the old watch guide + everything
        #    after it, re-emit new card(s) → guide (re-pinned) → footer.
        appended = await self._append_and_refooter(
            client, chat_id, bot_id, layout, new_cards, guide_caption,
        )
        log.info("senku.update.done", anime=anime_doc_id, chat_id=chat_id,
                 appended=appended)

        # 4. Refresh the wipe-proof backup so a later ban restores the NEW season
        #    too. _append_and_refooter rewrote BotContentPost; snapshot it now.
        #    Best-effort — a capture hiccup must never fail the update.
        if appended:
            try:
                from nekofetch.services.backup_service import BackupService

                await BackupService(self._c).record_distribution_channel(anime_doc_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("senku.update.backup_failed",
                            anime=anime_doc_id, error=str(exc))
        return {"appended": appended, "chat_id": chat_id}

    async def relink_packs_in_place(
        self, client, anime_doc_id: str,
    ) -> dict:
        """Re-point a published channel's quality buttons at freshly-redone packs.

        The owner's ``/redo`` of an ALREADY-PUBLISHED title keeps every post
        (channel, cards, watch guide, footer, main-channel post) but re-downloads
        and re-encodes the storage packs. Their Fstore message ranges therefore
        changed, so the old quality buttons now deep-link to deleted packs. This
        regenerates the button links from the NEW packs and edits them into each
        existing season/movie card *in place* — captions, images and layout are
        left untouched, so users see the same channel with working downloads.

        For each ``ChannelLayout`` season/movie card (has ``anilist_id`` +
        ``tg_message_id``) we rebuild ``button_data`` from the fresh packs the
        same way the full publish does (``_build_season_buttons`` → new Fstore
        links), edit the message's reply markup, and update the matching
        ``BotContentPost.button_data`` so a later ban-restore ships the new links
        too. Best-effort per card — one failure never sinks the rest.

        Returns ``{"relinked": n, "chat_id": id}``. A no-op (no channel, no cards
        with packs) returns ``relinked=0`` and never raises.
        """
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import (
            BotContentPost,
            ChannelLayout,
            DistributionBot,
        )
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.services.bot_content import BotContentService

        # 1. Resolve the durable channel anchor + its card layout.
        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (
                await session.execute(
                    select(DistributionBot).where(
                        DistributionBot.anime_doc_id == anime_doc_id,
                        DistributionBot.is_channel.is_(True),
                    ).order_by(DistributionBot.id.desc())
                )
            ).scalars().first()
            if bot is None or not bot.chat_id:
                log.info("senku.relink.no_channel", anime=anime_doc_id)
                return {"relinked": 0, "chat_id": None}
            bot_id = bot.id
            chat_id = int(bot.chat_id)
            cards = (
                await session.execute(
                    select(ChannelLayout)
                    .where(
                        ChannelLayout.channel_bot_id == bot_id,
                        ChannelLayout.kind.in_(("season_card", "movie_card")),
                        ChannelLayout.anilist_id.is_not(None),
                        ChannelLayout.tg_message_id.is_not(None),
                    )
                    .order_by(ChannelLayout.seq)
                )
            ).scalars().all()
            card_rows = [
                {"anilist_id": int(r.anilist_id), "tg_message_id": r.tg_message_id}
                for r in cards
            ]
        if not card_rows:
            log.info("senku.relink.no_cards", anime=anime_doc_id)
            return {"relinked": 0, "chat_id": chat_id}

        # 2. Load the FRESH packs + franchise walk, and map each entry's anilist
        #    id to its packs exactly as the full publish does (TV by season index,
        #    extras by entry_id / legacy season=None).
        svc = BotContentService(self._c)
        packs = await svc._load_packs(anime_doc_id)
        if not packs:
            log.info("senku.relink.no_packs", anime=anime_doc_id)
            return {"relinked": 0, "chat_id": chat_id}
        meta = await svc._gather_metadata(anime_doc_id)
        walked = await svc._walk_franchise(anime_doc_id, meta)
        tv = list(walked.get("tv", []))
        identities = svc._tv_entry_identities(tv)

        packs_by_aid: dict[int, list] = {}
        for entry in walked.get("all", []):
            aid = getattr(entry, "anilist_id", None)
            if aid is None:
                continue
            if entry in tv:
                season, season_part = identities.get(
                    aid, (tv.index(entry) + 1, getattr(entry, "season_part", None))
                )
                entry_packs = svc._packs_for_tv_entry(
                    packs, season, entry, season_part=season_part,
                )
            else:
                entry_packs = [
                    p for p in packs
                    if (p.entry_id is not None and p.entry_id == aid)
                    or (p.entry_id is None and p.season is None)
                ]
            if entry_packs:
                packs_by_aid[int(aid)] = entry_packs

        fmt = self._c.config.post_format
        relinked = 0

        # 3. Per card: rebuild buttons from fresh packs, edit reply markup, and
        #    sync the stored BotContentPost.button_data. Best-effort per card.
        for row in card_rows:
            aid = row["anilist_id"]
            mid = row["tg_message_id"]
            entry_packs = packs_by_aid.get(aid)
            if not entry_packs:
                log.debug("senku.relink.no_packs_for_entry", anime=anime_doc_id, aid=aid)
                continue
            try:
                buttons = await svc._build_season_buttons(entry_packs)
            except Exception as exc:  # noqa: BLE001 — one card's build must not sink all
                log.warning("senku.relink.build_failed", aid=aid, error=str(exc))
                continue
            if not buttons:
                continue
            markup = build_audio_keyboard(buttons, fmt)
            try:
                await client.edit_message_reply_markup(chat_id, mid, reply_markup=markup)
            except Exception as exc:  # noqa: BLE001 — stale/identical markup, keep going
                log.warning("senku.relink.edit_failed", aid=aid, mid=mid, error=str(exc))
                continue

            # Sync the stored button_data so a ban-restore ships the new links.
            try:
                async with session_scope(self._c.pg_sessionmaker) as session:
                    posts = (
                        await session.execute(
                            select(BotContentPost).where(
                                BotContentPost.bot_id == bot_id,
                                BotContentPost.anilist_id == aid,
                            )
                        )
                    ).scalars().all()
                    for p in posts:
                        p.button_data = buttons
            except Exception as exc:  # noqa: BLE001 — link edit already succeeded
                log.warning("senku.relink.post_sync_failed", aid=aid, error=str(exc))
            relinked += 1

        # 4. Refresh the wipe-proof backup so a later ban restores the new links.
        if relinked:
            try:
                from nekofetch.services.backup_service import BackupService

                await BackupService(self._c).record_distribution_channel(anime_doc_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("senku.relink.backup_failed",
                            anime=anime_doc_id, error=str(exc))

        log.info("senku.relink.done", anime=anime_doc_id, chat_id=chat_id,
                 relinked=relinked, cards=len(card_rows))
        return {"relinked": relinked, "chat_id": chat_id}

    async def _build_update_cards(
        self, anime_doc_id: str, new_anilist_ids: list[int] | None,
        already: set[int],
    ) -> tuple[list[dict], str | None]:
        """Build post dicts for new entries + the regenerated watch guide.

        Returns ``(new_cards, guide_caption)``. Reuses the same card builders as
        :meth:`_build_posts`. Only entries that (a) have finished packs and (b)
        aren't already in the layout are built; when ``new_anilist_ids`` is given,
        we additionally restrict to that set. The watch guide is rebuilt over the
        FULL franchise (existing + new seasons) so the season auto-update reposts a
        current guide; ``guide_caption`` is ``None`` only if the guide couldn't be
        built (then the tail keeps whatever guide was there).
        """
        from nekofetch.services.bot_content import BotContentService

        svc = BotContentService(self._c)
        packs = await svc._load_packs(anime_doc_id)
        if not packs:
            return [], None
        meta = await svc._gather_metadata(anime_doc_id)
        walked = await svc._walk_franchise(anime_doc_id, meta)

        # Bridge the admin's rendered per-entry thumbnails to public URLs, keyed by
        # anilist_id — same as the full publish path — so appended cards carry the
        # GENERATED thumbnail, not the AniList poster. The distribution cache is
        # keyed by request code, so resolve the code from anime_doc_id first.
        generated: dict[int, str] = {}
        try:
            from sqlalchemy import select as _select

            from nekofetch.infrastructure.database.postgres.models import Request
            from nekofetch.infrastructure.database.postgres.session import session_scope
            async with session_scope(self._c.pg_sessionmaker) as _s:
                code = (await _s.execute(
                    _select(Request.code).where(
                        Request.anime_doc_id == anime_doc_id
                    ).order_by(Request.id.desc())
                )).scalars().first()
            if code:
                entries = await self.cache.get_entries(code)
                if entries:
                    # Incremental updates identify entries by anilist_id, so use
                    # the id-keyed map (index-keyed map is for the full publish).
                    _by_index, generated = await self._bridge_thumbnails(code, entries)
        except Exception as exc:  # noqa: BLE001 — missing thumbs just fall back
            log.warning("senku.update.thumb_bridge_failed",
                        anime=anime_doc_id, error=str(exc))

        wanted = set(new_anilist_ids or [])
        tv = list(walked.get("tv", []))
        identities = svc._tv_entry_identities(tv)
        cards: list[dict] = []

        for entry in walked.get("all", []):
            aid = getattr(entry, "anilist_id", None)
            if aid is None or aid in already:
                continue
            if wanted and aid not in wanted:
                continue

            entry_meta = svc._entry_meta(meta, entry)
            gen = generated.get(aid)
            if gen:
                entry_meta["poster_url"] = gen
            if _is_season_entry(entry):
                season, season_part = identities.get(
                    aid, (tv.index(entry) + 1, getattr(entry, "season_part", None))
                )
                entry_packs = svc._packs_for_tv_entry(
                    packs, season, entry, season_part=season_part,
                )
                caption, image = svc._build_season_card(
                    entry_meta, season, entry_packs, season_part=season_part,
                )
                buttons = await svc._build_season_buttons(entry_packs)
                post_type = "season_card"
            else:
                entry_packs = [
                    p for p in packs
                    if (p.entry_id is not None and p.entry_id == aid)
                    or (p.entry_id is None and p.season is None)
                ]
                if not entry_packs:
                    continue
                is_movie = entry.format == "MOVIE" or (
                    entry.format in ("OVA", "ONA", "SPECIAL")
                    and (getattr(entry, "episodes", 0) or 0) <= 1
                )
                caption, image = svc._build_season_card(entry_meta, 1, entry_packs)
                buttons = await svc._build_season_buttons(entry_packs)
                post_type = "movie_card" if is_movie else "season_card"

            # Skip an entry that has no packs at all (not finished yet).
            if not entry_packs:
                continue
            cards.append({
                "post_type": post_type,
                "season": season if _is_season_entry(entry) else None,
                "season_part": season_part if _is_season_entry(entry) else None,
                "caption": caption,
                "image": await self._cache_image(image),
                "button_data": buttons,
                "pinned": False,
                "anilist_id": aid,
            })

        # Regenerate the franchise-wide watch guide so it now covers the new
        # season. Built over the FULL walk (existing + new) in release order; its
        # {BOT_QUAL#id:…} anchors are resolved to each season's card message by
        # _append_and_refooter (existing ids from the saved layout, new ones from
        # the cards we just built).
        guide_caption = svc._build_franchise_watch_guide(meta, packs, walked)
        return cards, guide_caption

    async def _append_and_refooter(
        self, client, chat_id: int, bot_id: int,
        layout: list[dict], new_cards: list[dict],
        guide_caption: str | None = None,
    ) -> int:
        """Strip the watch-guide tail, re-emit new cards → guide → footer.

        The watch guide summarises the whole franchise, so a new season must
        rebuild it rather than leave a stale one mid-channel. We delete the old
        watch-guide post and everything after it (its dividers + the footer),
        keeping the body up to the last existing card, then re-emit:

            <body> → divider → newcard₁ [→ divider → newcard₂ …]
                   → divider → watch guide (re-pinned) → divider → footer

        The regenerated ``guide_caption`` (built over the full franchise) has its
        ``{BOT_QUAL#id:…}`` anchors resolved to each season's card message — ids
        for cards already in the channel come from ``layout``, ids for the cards
        we post here are collected as we go. Falls back to the old footer-only
        choreography when no guide was rebuilt (``guide_caption`` is None) or the
        channel has no tracked guide (older layout). Best-effort on every Telegram
        call, so a partial update still leaves a consistent saved layout.
        """
        from sqlalchemy import delete
        from pyrogram.enums import ParseMode

        from nekofetch.infrastructure.database.postgres.models import ChannelLayout
        from nekofetch.infrastructure.database.postgres.session import session_scope

        fmt = self._c.config.post_format
        divider_id = fmt.divider_sticker_id or self._c.config.bot.divider_sticker_id

        try:
            chat = await client.get_chat(chat_id)
            handle = getattr(chat, "username", None)
        except Exception:  # noqa: BLE001
            handle = None

        # Seed the id→message map with the season cards ALREADY in the channel so
        # the regenerated guide can deep-link them; new cards add to it below.
        msg_by_id: dict[int, int] = {
            int(it["anilist_id"]): it["tg_message_id"]
            for it in layout
            if it.get("anilist_id") is not None and it.get("tg_message_id")
        }

        # Locate the watch guide. Everything from it onward (guide, trailing
        # dividers, footer) is stripped and re-emitted; the body keeps every card
        # + the info card up top. When there's no guide tracked (older channel),
        # fall back to stripping just the footer so we still append cleanly.
        guide_idx = next(
            (i for i in range(len(layout) - 1, -1, -1)
             if layout[i].get("kind") == "watch_guide"),
            None,
        )
        if guide_idx is not None and guide_caption:
            # Strip from the guide onward, plus a divider immediately before it
            # (it belonged to the guide section — we re-emit a fresh one). Both the
            # body cut and the deleted tail start at ``cut`` so that leading divider
            # is actually removed from Telegram, not just dropped from the layout.
            cut = guide_idx
            if cut > 0 and layout[cut - 1].get("kind") == "divider":
                cut -= 1
            body = layout[:cut]
            tail = layout[cut:]
        else:
            # No guide to rebuild — keep the classic footer-only tail rewrite.
            footer_idx = next(
                (i for i in range(len(layout) - 1, -1, -1)
                 if layout[i].get("kind") == "footer"),
                None,
            )
            if footer_idx is not None:
                body = layout[:footer_idx]
                tail = layout[footer_idx:]
            else:
                body = list(layout)
                tail = []

        # Delete the stripped tail messages (guide/dividers/footer). We re-pin the
        # new guide later, so dropping the old pinned guide here is intentional.
        for item in tail:
            mid = item.get("tg_message_id")
            if not mid:
                continue
            try:
                await client.delete_messages(chat_id, mid)
            except Exception as exc:  # noqa: BLE001 — stale id, already gone
                log.warning("senku.update.delete_failed", mid=mid, error=str(exc))

        # Preserve the old footer's content for the re-post (text/image from config).
        footer_post = next((it for it in tail if it.get("kind") == "footer"), None)

        new_layout = list(body)
        # Freshly-posted content cards (season/movie), captured so the ban-restore
        # backup (built from BotContentPost, not ChannelLayout) picks up the new
        # season. Guide + footer content are captured separately below.
        new_content: list[dict] = []
        appended = 0

        async def _emit_divider() -> None:
            if not divider_id:
                return
            div = await self._send_divider(client, chat_id, divider_id)
            if div is not None:
                new_layout.append({"kind": "divider", "tg_message_id": div,
                                   "anilist_id": None, "is_pinned": False})

        async def _send_card(post: dict) -> None:
            nonlocal appended
            caption = self._resolve_caption(
                post.get("caption") or "", handle, fmt, msg_by_id,
            )
            markup = build_audio_keyboard(post.get("button_data"), fmt)
            image = post.get("image")
            try:
                if image:
                    msg = await client.send_photo(
                        chat_id, image, caption=caption,
                        reply_markup=markup, parse_mode=ParseMode.HTML,
                    )
                else:
                    msg = await client.send_message(
                        chat_id, caption, reply_markup=markup,
                        parse_mode=ParseMode.HTML,
                    )
            except Exception as exc:  # noqa: BLE001 — a partial update still ships
                log.warning("senku.update.card_failed",
                            post_type=post.get("post_type"), error=str(exc))
                return
            aid = post.get("anilist_id")
            if aid is not None:
                msg_by_id[int(aid)] = msg.id
            new_layout.append({
                "kind": post.get("post_type") or "season_card",
                "tg_message_id": msg.id,
                "anilist_id": aid,
                "season": post.get("season"),
                "season_part": post.get("season_part"),
                "is_pinned": False,
            })
            # Snapshot the card CONTENT for the ban-restore backup. The raw
            # (unresolved) caption is stored so {BOT_QUAL#id:…} re-resolves against
            # whatever handle a restored channel later gets.
            new_content.append({
                "post_type": post.get("post_type") or "season_card",
                "season": post.get("season"),
                "season_part": post.get("season_part"),
                "caption": post.get("caption") or "",
                "image": post.get("image"),
                "button_data": post.get("button_data"),
                "anilist_id": aid,
                "is_pinned": False,
                "tg_message_id": msg.id,
            })
            appended += 1

        # A divider only needs to *lead* the first new card when the body doesn't
        # already end in one (e.g. the body ends on an existing season card).
        need_leading_divider = bool(body) and body[-1].get("kind") != "divider"
        for i, post in enumerate(new_cards):
            if (i == 0 and need_leading_divider) or i > 0:
                await _emit_divider()
            await _send_card(post)

        # Re-emit the watch guide (pinned) — now that every card it references has
        # a message id, its quality links deep-link to the right season cards.
        guide_mid: int | None = None
        if guide_caption:
            await _emit_divider()
            try:
                gcap = self._resolve_caption(guide_caption, handle, fmt, msg_by_id)
                gmsg = await client.send_message(
                    chat_id, gcap, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                await self._pin_silently(client, chat_id, gmsg.id)
                guide_mid = gmsg.id
                new_layout.append({"kind": "watch_guide", "tg_message_id": gmsg.id,
                                   "anilist_id": None, "is_pinned": True})
            except Exception as exc:  # noqa: BLE001 — guide is best-effort
                log.warning("senku.update.guide_failed", error=str(exc))

        # Divider + re-posted footer (reuse the old footer's text/image).
        await _emit_divider()
        footer_caption, footer_image = await self._footer_content(footer_post)
        footer_mid: int | None = None
        try:
            caption = self._resolve_caption(footer_caption, handle, fmt, msg_by_id)
            if footer_image:
                fmsg = await client.send_photo(
                    chat_id, footer_image, caption=caption, parse_mode=ParseMode.HTML,
                )
            else:
                fmsg = await client.send_message(
                    chat_id, caption, parse_mode=ParseMode.HTML,
                )
            footer_mid = fmsg.id
            new_layout.append({"kind": "footer", "tg_message_id": fmsg.id,
                               "anilist_id": None, "is_pinned": False})
        except Exception as exc:  # noqa: BLE001 — footer is best-effort
            log.warning("senku.update.footer_failed", error=str(exc))

        # Persist the rewritten layout.
        async with session_scope(self._c.pg_sessionmaker) as session:
            await session.execute(
                delete(ChannelLayout).where(ChannelLayout.channel_bot_id == bot_id)
            )
            for seq, item in enumerate(new_layout):
                session.add(ChannelLayout(
                    channel_bot_id=bot_id, seq=seq, kind=item["kind"],
                    tg_message_id=item.get("tg_message_id"),
                    anilist_id=item.get("anilist_id"),
                    is_pinned=bool(item.get("is_pinned")),
                ))

        # Reconcile BotContentPost so the ban-restore backup (built from these
        # rows, NOT ChannelLayout) includes the new season card + regenerated
        # guide. Without this, a post-update ban would restore the STALE pre-update
        # snapshot (missing the new season). Content-only cards are appended /
        # updated in place; the guide + footer rows are rewritten. Best-effort:
        # a reconcile hiccup must never fail an otherwise-successful update.
        try:
            await self._reconcile_content_posts(
                bot_id, new_content,
                guide_caption=guide_caption, guide_mid=guide_mid,
                footer_caption=footer_caption, footer_image=footer_image,
                footer_mid=footer_mid,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("senku.update.content_reconcile_failed",
                        bot_id=bot_id, error=str(exc))
        return appended

    async def _reconcile_content_posts(
        self, bot_id: int, new_content: list[dict], *,
        guide_caption: str | None, guide_mid: int | None,
        footer_caption: str | None, footer_image: str | None,
        footer_mid: int | None,
    ) -> None:
        """Fold an incremental update's new cards into the ``BotContentPost`` set.

        The ban-restore backup (:meth:`BackupService.record_distribution_channel`)
        reads ``BotContentPost`` in ``order``, so after a season auto-update those
        rows must reflect the appended season card(s) + the regenerated watch
        guide — otherwise a later ban restores the pre-update channel.

        Strategy: keep the existing info/season/movie cards (they're unchanged),
        append the new content cards after them, then rewrite the single watch
        guide + footer rows to the freshly-posted versions. Content revision is
        bumped so returning /start users get the new set too.
        """
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import (
            BotContentPost,
            DistributionBot,
        )
        from nekofetch.infrastructure.database.postgres.session import session_scope

        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(BotContentPost)
                    .where(BotContentPost.bot_id == bot_id)
                    .order_by(BotContentPost.order)
                )
            ).scalars().all()

            # Split existing rows: keep content/info cards as-is; the guide + footer
            # are rewritten (their content changed / message ids moved).
            kept = [r for r in rows
                    if r.post_type not in ("watch_guide", "footer")]
            existing_guide = next(
                (r for r in rows if r.post_type == "watch_guide"), None)
            existing_footer = next(
                (r for r in rows if r.post_type == "footer"), None)
            known_ids = {r.anilist_id for r in kept if r.anilist_id is not None}

            order = len(kept)
            # Append genuinely-new content cards (dedupe on anilist_id so a
            # re-run — e.g. a retried update — doesn't double-insert).
            for it in new_content:
                aid = it.get("anilist_id")
                if aid is not None and aid in known_ids:
                    continue
                session.add(BotContentPost(
                    bot_id=bot_id,
                    post_type=it.get("post_type") or "season_card",
                    season=it.get("season"),
                    season_part=it.get("season_part"),
                    order=order,
                    caption=it.get("caption") or "",
                    image_url=it.get("image"),
                    image_cached_url=it.get("image"),
                    button_data=it.get("button_data"),
                    is_pinned=bool(it.get("is_pinned")),
                    tg_message_id=it.get("tg_message_id"),
                    anilist_id=aid,
                ))
                if aid is not None:
                    known_ids.add(aid)
                order += 1

            # Rewrite the watch guide row (regenerated over the full franchise).
            if guide_caption:
                if existing_guide is not None:
                    existing_guide.caption = guide_caption
                    existing_guide.order = order
                    existing_guide.tg_message_id = guide_mid
                    existing_guide.is_pinned = True
                else:
                    session.add(BotContentPost(
                        bot_id=bot_id, post_type="watch_guide", order=order,
                        caption=guide_caption, is_pinned=True,
                        tg_message_id=guide_mid,
                    ))
                order += 1

            # Rewrite the footer row (re-posted, new message id).
            if existing_footer is not None:
                existing_footer.order = order
                existing_footer.tg_message_id = footer_mid
                if footer_caption:
                    existing_footer.caption = footer_caption
                if footer_image:
                    existing_footer.image_url = footer_image
                    existing_footer.image_cached_url = footer_image
            elif footer_caption:
                session.add(BotContentPost(
                    bot_id=bot_id, post_type="footer", order=order,
                    caption=footer_caption, image_url=footer_image,
                    image_cached_url=footer_image, tg_message_id=footer_mid,
                ))

            bot_row = await session.get(DistributionBot, bot_id)
            if bot_row is not None:
                bot_row.content_revision = (bot_row.content_revision or 0) + 1
            await session.commit()

    async def _footer_content(self, footer_post: dict | None) -> tuple[str, str | None]:
        """Resolve the footer caption + image for a re-posted footer.

        We don't keep the footer's rendered text in the layout table (only its
        message id), so rebuild it from config exactly as :meth:`_build_posts`
        does. ``footer_post`` is accepted for future use (per-channel footers).
        """
        from nekofetch.localization.messages import M, t

        footer_text = self._c.config.bot.footer_text or t(M.BOT_FOOTER)
        footer_image = self._c.config.bot.footer_image_url or None
        return footer_text, await self._cache_image(footer_image)

    # ── build ────────────────────────────────────────────────────────────────────

    async def _build_posts(self, code: str) -> tuple[list[dict], str, str]:
        """Assemble the ordered post list, reusing BotContentService builders.

        The returned posts are plain dicts (not persisted ``BotContentPost``
        rows — the channel has no bot row); each carries ``caption``, an
        ``image`` (catbox/AniList URL or ``None``), ``button_data``, and the
        ``pinned``/``post_type`` flags the sender needs.

        Also returns the resolved ``anime_doc_id`` so the caller can anchor the
        channel + its message layout for later incremental updates.
        """
        from nekofetch.services.bot_content import BotContentService

        svc = BotContentService(self._c)

        franchise_cache = await self.cache.get_franchise(code) or await self.cache.ensure(code)
        if not franchise_cache:
            raise PublishError(f"no franchise for {code}")
        anime_doc_id = franchise_cache.get("anime_doc_id") or code
        title = (franchise_cache.get("english") or franchise_cache.get("title")
                 or franchise_cache.get("anime_title") or code)

        entries = await self.cache.get_entries(code)

        # Data the builders need — loaded exactly as generate_posts does.
        packs = await svc._load_packs(anime_doc_id)
        # Pass the resolved title so a REQ-#### doc id never reaches AcuteBot.
        meta = await svc._gather_metadata(anime_doc_id, title_hint=title)
        walked = await svc._walk_franchise(anime_doc_id, meta)

        # Reorder the AniList walk to the admin's *confirmed* order, and bridge
        # each entry's locally-rendered thumbnail to a public URL. The bridge
        # returns BOTH index-keyed and anilist_id-keyed maps (an index is always
        # present; an id may be None for a mapping-built entry like Takopi's ONA),
        # so every card — info, TV and extras — can use the generated render.
        franchise = self._reorder_franchise(walked, entries)
        generated_by_index, generated_by_id = await self._bridge_thumbnails(code, entries)

        def _gen(entry) -> str | None:
            """The rendered thumbnail for a walked franchise entry, if any."""
            url = generated_by_id.get(getattr(entry, "anilist_id", None))
            if url:
                return url
            return generated_by_index.get(
                franchise.get("origin_index", {}).get(id(entry))
            )

        posts: list[dict] = []
        order = 0

        # ── 1. Info card ──
        info_caption, info_default = await svc._build_info_card(meta)
        if info_caption:
            # The info card NEVER uses a generated entry thumbnail — those are for
            # the per-entry season/movie cards only. Its image is AcuteBot's card
            # (primary), else the metadata banner/poster (AniList/MAL/TMDB) that
            # _build_info_card already resolved — preferring an English backdrop.
            info_image = svc._pick_card_image(None, info_default, meta)
            posts.append({
                "post_type": "info_card", "order": order,
                "caption": info_caption,
                "image": await self._cache_image(info_image),
                "button_data": None, "pinned": True,
            })
            order += 1

        # ── 2. Season cards (confirmed TV order) ──
        identities = svc._tv_entry_identities(franchise["tv"])
        for i, entry in enumerate(franchise["tv"], start=1):
            season, season_part = identities.get(
                entry.anilist_id, (i, getattr(entry, "season_part", None))
            )
            season_packs = svc._packs_for_tv_entry(
                packs, season, entry, season_part=season_part,
            )
            entry_meta = svc._entry_meta(meta, entry)
            gen = _gen(entry)
            if gen:
                entry_meta["poster_url"] = gen
            caption, image = svc._build_season_card(
                entry_meta, season, season_packs, season_part=season_part,
            )
            buttons = await svc._build_season_buttons(season_packs)
            posts.append({
                "post_type": "season_card", "order": order,
                "season": season, "season_part": season_part,
                "caption": caption,
                "image": await self._cache_image(image),
                "button_data": buttons, "pinned": False,
                "anilist_id": entry.anilist_id,
            })
            order += 1

        # ── 3. Extra cards (OVA / ONA / Movie / Special) ──
        for entry in franchise["extras"]:
            extra_packs = [
                p for p in packs
                if (p.entry_id is not None and p.entry_id == entry.anilist_id)
                or (p.entry_id is None and p.season is None)
            ]
            entry_meta = svc._entry_meta(meta, entry)
            gen = _gen(entry)
            if gen:
                entry_meta["poster_url"] = gen
            is_movie = entry.format == "MOVIE" or (
                entry.format in ("OVA", "ONA", "SPECIAL")
                and (entry.episodes or 0) <= 1
            )
            caption, image = svc._build_season_card(entry_meta, 1, extra_packs or [])
            buttons = await svc._build_season_buttons(extra_packs) if extra_packs else None
            posts.append({
                "post_type": "movie_card" if is_movie else "season_card",
                "order": order, "caption": caption,
                "image": await self._cache_image(image),
                "button_data": buttons, "pinned": False,
                "anilist_id": entry.anilist_id,
            })
            order += 1

        # ── 4. Watch guide (pinned) — reuses the franchise builder, so the
        # release-order listing matches the reordered franchise exactly. ──
        guide = svc._build_franchise_watch_guide(meta, packs, franchise)
        if guide:
            posts.append({
                "post_type": "watch_guide", "order": order,
                "caption": guide, "image": None,
                "button_data": None, "pinned": True,
            })
            order += 1

        # ── 5. Footer ──
        from nekofetch.localization.messages import M, t

        footer_text = self._c.config.bot.footer_text or t(M.BOT_FOOTER)
        footer_image = self._c.config.bot.footer_image_url or None
        posts.append({
            "post_type": "footer", "order": order,
            "caption": footer_text,
            "image": await self._cache_image(footer_image),
            "button_data": None, "pinned": False,
        })

        return posts, title, anime_doc_id

    async def _persist_channel(
        self, anime_doc_id: str, chat_id: int, title: str,
        handle: str | None, layout: list[dict],
    ) -> None:
        """Register the channel's DistributionBot anchor + save its layout.

        Idempotent on the anchor: the auto pipeline already registered a bot
        row; the manual wizard flow hasn't, so we create one keyed by
        ``anime_doc_id``. Either way we then replace this channel's
        :class:`ChannelLayout` rows with the freshly-posted message list.
        """
        from sqlalchemy import delete, select

        from nekofetch.infrastructure.database.postgres.models import (
            BotContentPost,
            ChannelLayout,
            DistributionBot,
        )
        from nekofetch.infrastructure.database.postgres.session import session_scope

        # Resolve (or create) the durable channel anchor.
        bot_id: int | None = None
        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(DistributionBot).where(
                        DistributionBot.chat_id == chat_id,
                        DistributionBot.is_channel.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                bot_id = row.id
                if row.anime_doc_id is None:
                    row.anime_doc_id = anime_doc_id

        if bot_id is None:
            from nekofetch.services.bot_management_service import BotManagementService

            handle_clean = (handle or "").lstrip("@") or None
            name = title or (handle_clean or f"channel-{chat_id}")
            try:
                info = await BotManagementService(self._c).register_channel(
                    chat_id, name=name, username=handle_clean,
                    anime_doc_id=anime_doc_id,
                )
                bot_id = info.id
            except Exception as exc:  # noqa: BLE001 — anchor is best-effort
                log.warning("senku.publish.register_channel_failed",
                            anime=anime_doc_id, chat_id=chat_id, error=str(exc))
                return

        # Replace the layout snapshot for this channel.
        async with session_scope(self._c.pg_sessionmaker) as session:
            await session.execute(
                delete(ChannelLayout).where(ChannelLayout.channel_bot_id == bot_id)
            )
            for seq, item in enumerate(layout):
                session.add(ChannelLayout(
                    channel_bot_id=bot_id,
                    seq=seq,
                    kind=item["kind"],
                    tg_message_id=item.get("tg_message_id"),
                    anilist_id=item.get("anilist_id"),
                    is_pinned=bool(item.get("is_pinned")),
                ))

        # Snapshot the card CONTENT into BotContentPost rows so the ban-restore
        # backup (BackupService.record_distribution_channel) has something to
        # capture. The manual wizard path only ever wrote ChannelLayout (message
        # ids), so a restore found an empty channel. Only content cards are stored
        # (dividers/footers carry no reusable caption+image+buttons). Replaces any
        # prior rows so a re-publish refreshes the snapshot.
        content = [
            it for it in layout
            if it.get("caption") and it.get("kind") not in ("divider",)
        ]
        async with session_scope(self._c.pg_sessionmaker) as session:
            await session.execute(
                delete(BotContentPost).where(BotContentPost.bot_id == bot_id)
            )
            for order, it in enumerate(content):
                session.add(BotContentPost(
                    bot_id=bot_id,
                    post_type=it.get("post_type") or it.get("kind") or "season_card",
                    season=it.get("season"),
                    season_part=it.get("season_part"),
                    order=order,
                    caption=it.get("caption") or "",
                    image_url=it.get("image"),
                    image_cached_url=it.get("image"),
                    button_data=it.get("button_data"),
                    is_pinned=bool(it.get("is_pinned")),
                    tg_message_id=it.get("tg_message_id"),
                    # Carry the entry id so a ban-restore can remap this card's
                    # {BOT_QUAL#id:…} deep-links to its fresh message id.
                    anilist_id=it.get("anilist_id"),
                ))

    def _reorder_franchise(
        self, walked: dict, entries: list[EntryData],
    ) -> dict:
        """Reorder a fresh AniList walk to match the admin's confirmed entries.

        ``walked`` is ``{"tv": [...], "extras": [...], "all": [...]}`` of
        :class:`FranchiseEntry` objects (full metadata). We key those by
        ``anilist_id`` and re-emit them in the cached entry order; any AniList
        entry the admin dropped is excluded, and any cached entry AniList
        couldn't resolve is skipped (it has no card-quality metadata anyway).

        Also returns ``origin_index``: ``id(franchise_entry) → cache EntryData
        index``. The thumbnail bridge is keyed by cache ``index`` (renders exist
        per confirmed entry, and a mapping-built entry may have no ``anilist_id``),
        so the card builders use this to find the right render even when ids are
        absent — including the ``anilist_id``-less fallback below.
        """
        by_id = {
            e.anilist_id: e
            for e in walked.get("all", [])
            if getattr(e, "anilist_id", None) is not None
        }
        origin_index: dict[int, int] = {}
        ordered: list = []
        for ce in entries:
            if ce.anilist_id is not None and ce.anilist_id in by_id:
                fe = by_id[ce.anilist_id]
                ordered.append(fe)
                origin_index[id(fe)] = ce.index
        # If the cached entries never carried anilist_ids (bare franchise), fall
        # back to the AniList walk order so the channel still gets cards. Map each
        # walked entry positionally to the cached entry at the same slot so the
        # per-entry render still resolves by index.
        if not ordered:
            ordered = list(walked.get("all", []))
            for k, fe in enumerate(ordered):
                if k < len(entries):
                    origin_index[id(fe)] = entries[k].index
        tv = [e for e in ordered if _is_season_entry(e)]
        extras = [e for e in ordered if not _is_season_entry(e)]
        return {"tv": tv, "extras": extras, "all": ordered,
                "origin_index": origin_index}

    async def _bridge_thumbnails(
        self, code: str, entries: list[EntryData],
    ) -> tuple[dict[int, str], dict[int, str]]:
        """Mirror each rendered entry thumbnail to a public URL.

        Phase 3 stores each rendered card in the entry's selection. Modern
        renders are mirrored across the image hosts AT RENDER TIME and carry a
        public ``http(s)`` URL, which passes straight through here; legacy
        ``file://<path>`` renders (from before that change, or a render whose
        host upload failed) are mirrored across the configured hosts (ImgBB
        first) on the spot.

        Returns ``(by_index, by_anilist_id)``:
          * ``by_index``  — ``entry.index → url`` for EVERY rendered entry. Index
            is always present, so this works for mapping-built entries whose
            ``anilist_id`` is ``None`` (e.g. a single-ONA franchise like Takopi —
            the earlier ``anilist_id``-only map came back EMPTY, so the render was
            never uploaded and the cards fell back to the AniList poster).
          * ``by_anilist_id`` — ``anilist_id → url`` for entries that HAVE an id,
            for callers that can match on it directly.

        A failed upload just omits that entry — the card builder falls back to the
        AniList poster via ``_pick_card_image``. The upload preserves the render's
        real image type (webp/png/jpg) so a ``.webp`` render is not silently
        relabelled ``.jpg``.
        """
        from kurosoden.shared.image_backup import backup_bytes

        _MIME_BY_SUFFIX = {
            ".webp": "image/webp", ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        }

        by_index: dict[int, str] = {}
        by_anilist: dict[int, str] = {}
        for entry in entries:
            sel = await self.cache.get_selection(code, entry.index)
            url = sel.thumbnail_url if sel else None
            if not url:
                continue
            # Already public — uploaded when the entry rendered. No re-upload.
            if url.startswith(("http://", "https://")):
                by_index[entry.index] = url
                if entry.anilist_id is not None:
                    by_anilist[entry.anilist_id] = url
                continue
            if not url.startswith("file://"):
                continue
            path = Path(url[len("file://"):])
            try:
                data = path.read_bytes()
                mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/jpeg")
                result = await backup_bytes(self._c, data, mime=mime)
                public = result.primary
                if public:
                    by_index[entry.index] = public
                    if entry.anilist_id is not None:
                        by_anilist[entry.anilist_id] = public
            except Exception as exc:  # noqa: BLE001 — a missing render just falls back
                log.warning("senku.publish.thumb_bridge_failed",
                            code=code, entry=entry.index, error=str(exc))
        return by_index, by_anilist

    async def _cache_image(self, image) -> str | None:
        """Mirror a card image across the configured hosts and return the best URL.

        ``image`` may be a URL string, a ``Path``, or ``None``. A ``file://`` /
        local path is read to bytes; a remote URL is downloaded once — then both
        go through the multi-host uploader (``image_backup``), which honours
        ``bot.image_host_order`` (ImgBB first by default, catbox/telegraph as
        fallbacks). :attr:`BackupImage.primary` picks the best surviving mirror.

        Previously this was catbox-ONLY, which is why the log only ever showed
        catbox.moe even though ImgBB is configured and more reliable. On any
        failure the original string is returned so a broken cache never drops the
        image. ``None`` passes through.
        """
        if not image:
            return None
        image_str = str(image)
        try:
            from kurosoden.shared.image_backup import backup_bytes, backup_image

            # Local file → read bytes and mirror across hosts.
            if image_str.startswith("file://") or Path(image_str).exists():
                raw = Path(image_str[len("file://"):] if image_str.startswith("file://")
                           else image_str).read_bytes()
                result = await backup_bytes(self._c, raw, mime="image/jpeg")
                return result.primary or image_str
            # Remote URL → mirror across hosts (gated by the cache feature flag,
            # preserving the prior behaviour of not re-hosting remote URLs when
            # caching is disabled).
            if self._c.config.features.catbox_image_cache:
                result = await backup_image(self._c, image_str)
                return result.primary or image_str
        except Exception as exc:  # noqa: BLE001 — caching is best-effort
            log.debug("senku.publish.image_cache_failed", url=image_str, error=str(exc))
        return image_str

    # ── send ───────────────────────────────────────────────────────────────────

    async def _send_posts(
        self, client, chat_id: int, posts: list[dict],
    ) -> tuple[int, list[int], list[dict]]:
        """Post every card into the channel, mirroring the distribution app.

        Divider sticker between sections, URL buttons from ``button_data``,
        ``{BOT_QUAL:...}`` placeholders resolved to the channel handle, and
        the info card + watch guide pinned (service notices swept).

        Returns ``(posted_count, pinned_message_ids, layout)`` where ``layout``
        is the ordered list of every message actually sent — including the
        divider stickers — as ``{"kind", "tg_message_id", "anilist_id",
        "is_pinned"}`` dicts. A later incremental update reads this to find the
        footer + trailing divider it must delete.
        """
        import re

        fmt = self._c.config.post_format
        divider_id = fmt.divider_sticker_id or self._c.config.bot.divider_sticker_id
        posted = 0
        pinned_ids: list[int] = []
        layout: list[dict] = []
        # anilist_id → message id, filled as season/extra cards post. The watch
        # guide is emitted LAST, so by the time we resolve its {BOT_QUAL#id:…}
        # placeholders every entry it references already has a message id here —
        # each quality then deep-links to that entry's own card.
        msg_by_id: dict[int, int] = {}

        # Resolve a public handle for {BOT_QUAL} links. In a channel these point
        # at the channel itself (deep-linking to messages fails in private chat).
        try:
            chat = await client.get_chat(chat_id)
            handle = getattr(chat, "username", None)
        except Exception:  # noqa: BLE001
            handle = None

        for i, post in enumerate(posts):
            if i > 0 and divider_id:
                div = await self._send_divider(client, chat_id, divider_id)
                if div is not None:
                    layout.append({"kind": "divider", "tg_message_id": div,
                                   "anilist_id": None, "is_pinned": False})

            caption = self._resolve_caption(
                post.get("caption") or "", handle, fmt, msg_by_id,
            )
            markup = build_audio_keyboard(post.get("button_data"), fmt)
            image = post.get("image")

            async def _do_send():
                if image:
                    return await client.send_photo(
                        chat_id, image, caption=caption,
                        reply_markup=markup, parse_mode=ParseMode.HTML,
                    )
                # Text posts (notably the watch guide) carry quality hyperlinks —
                # suppress the link preview so a t.me thumbnail card never appears.
                return await client.send_message(
                    chat_id, caption,
                    reply_markup=markup, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

            try:
                # Honour FloodWait: the publish cards are few, but they share the
                # bot's per-channel rate budget with the warm-up burst, so a
                # transient FLOOD_WAIT here should WAIT + retry (bounded) rather
                # than drop the card (which left the channel with posted=0).
                msg = await self._send_with_flood_retry(_do_send)
                posted += 1
            except Exception as exc:  # noqa: BLE001 — a partial channel still ships
                log.warning("senku.publish.post_failed",
                            post_type=post.get("post_type"), error=str(exc))
                continue

            pinned = bool(post.get("pinned"))
            if pinned:
                await self._pin_silently(client, chat_id, msg.id)
                pinned_ids.append(msg.id)

            aid = post.get("anilist_id")
            if aid is not None:
                msg_by_id[int(aid)] = msg.id

            layout.append({
                "kind": post.get("post_type") or "season_card",
                "tg_message_id": msg.id,
                "anilist_id": post.get("anilist_id"),
                "season": post.get("season"),
                "season_part": post.get("season_part"),
                "is_pinned": pinned,
                # Carry the card CONTENT so _persist_channel can snapshot it into
                # BotContentPost rows — the manual (wizard) publish path never had
                # those, so the ban-restore backup found an empty channel. The raw
                # (unresolved) caption is stored so {BOT_QUAL:…} re-resolves against
                # whatever handle the restored channel gets.
                "caption": post.get("caption") or "",
                "image": post.get("image"),
                "button_data": post.get("button_data"),
                "post_type": post.get("post_type") or "season_card",
            })

        return posted, pinned_ids, layout

    def _resolve_caption(
        self, caption: str, handle: str | None, fmt,
        msg_by_id: dict[int, int] | None = None,
    ) -> str:
        """Resolve ``{BOT_QUAL…}`` links + premium emoji in a channel caption.

        Two placeholder forms are honoured:
          * ``{BOT_QUAL#<anilist_id>:LABEL}`` — anchored to a specific entry. If
            ``msg_by_id`` maps that id to a posted message, the label links to
            ``t.me/<handle>/<msg_id>`` (jumps to that season's card); otherwise it
            degrades to the channel-root link.
          * ``{BOT_QUAL:LABEL}`` — unanchored; links to the channel root.
        Without a ``handle`` every placeholder collapses to its bare label.
        """
        import re

        if not caption:
            return caption
        msg_by_id = msg_by_id or {}

        def _sub(m: re.Match) -> str:
            aid_raw, label = m.group(1), m.group(2)
            if not handle:
                return label
            mid = None
            if aid_raw:
                try:
                    mid = msg_by_id.get(int(aid_raw))
                except (TypeError, ValueError):
                    mid = None
            if mid:
                href = f"https://t.me/{handle}/{mid}"
            else:
                href = f"https://t.me/{handle}"
            return f'<a href="{href}">{label}</a>'

        # ``#<id>`` group is optional so the legacy unanchored form still matches.
        caption = re.sub(r"\{BOT_QUAL(?:#(\d+))?:([^}]+)\}", _sub, caption)
        return resolve_premium_emoji(caption, fmt)

    @staticmethod
    async def _send_with_flood_retry(fn, *, max_waits: int = 3):
        """Run ``fn`` (an async send), honouring Telegram FloodWait up to
        ``max_waits`` times. A FloodWait means "wait N seconds then it'll work",
        so we sleep and retry rather than dropping the message. Non-flood errors
        and waits longer than ~90s propagate to the caller."""
        import asyncio

        from pyrogram.errors import FloodWait

        for _ in range(max_waits + 1):
            try:
                return await fn()
            except FloodWait as fw:
                wait = int(getattr(fw, "value", 0) or 0)
                if wait > 90:
                    raise
                await asyncio.sleep(wait + 1)
        return await fn()

    async def _send_divider(self, client, chat_id: int, divider_id: str) -> int | None:
        """Post a divider sticker; return its message id (``None`` on failure)."""
        try:
            msg = await self._send_with_flood_retry(
                lambda: client.send_sticker(chat_id, divider_id)
            )
            return msg.id
        except Exception:  # noqa: BLE001 — divider is decorative
            return None

    _WARM_COUNT = 100
    _WARM_BATCH_DELETE = 100

    async def _acquire_userbot(self):
        """Return a started userbot Client, or ``None`` if none is configured.

        Reuses the pool cached on the container (same pattern as bot_content /
        bot_factory) so we don't leak Pyrogram connections."""
        try:
            from nekofetch.sources.telegram.userbot import UserbotPool

            pool = getattr(self._c, "_userbot_pool", None)
            if pool is None:
                pool = UserbotPool.from_env(
                    self._c.env.telegram_api_id,
                    self._c.env.telegram_api_hash,
                    str(self._c.env.session_path),
                )
                self._c._userbot_pool = pool  # type: ignore[attr-defined]
            return await pool.acquire()
        except Exception as exc:  # noqa: BLE001 — no userbot → caller falls back to bot
            log.info("senku.warm.no_userbot", error=str(exc))
            return None

    @staticmethod
    def _member_status(member) -> str:
        return getattr(getattr(member, "status", None), "value",
                       str(getattr(member, "status", "")))

    async def _ensure_userbot_can_post(self, admin_client, ub, chat_id: int) -> bool:
        """Make sure the userbot can post the warm-up burst in ``chat_id``.

        The userbot needs to be an admin (or at least a posting member) to fire
        100 messages without the bot's 20/min channel cap. If it ISN'T already an
        admin, we add + promote it via the bot admin client (which is a channel
        admin), then tell the caller to demote it afterwards. If it's already an
        admin we leave it alone (and the caller won't touch it).

        Returns ``True`` when WE promoted the userbot (caller must undo it), and
        ``False`` when it was already an admin or promotion failed (leave as-is).
        """
        try:
            me = await ub.get_me()
            uid = me.id
        except Exception as exc:  # noqa: BLE001 — can't identify userbot → skip
            log.debug("senku.warm.userbot_me_failed", error=str(exc))
            return False

        # Already an admin/creator? Then we didn't add it — never demote it.
        try:
            member = await admin_client.get_chat_member(chat_id, uid)
            if self._member_status(member) in ("administrator", "creator"):
                return False
        except Exception:  # noqa: BLE001 — not a member yet → try to add + promote
            pass

        from pyrogram.types import ChatPrivileges

        try:
            try:
                await admin_client.add_chat_members(chat_id, uid)
            except Exception as exc:  # noqa: BLE001 — may already be a member
                log.debug("senku.warm.userbot_add_skipped", error=str(exc))
            await admin_client.promote_chat_member(
                chat_id, uid,
                privileges=ChatPrivileges(
                    can_post_messages=True, can_delete_messages=True,
                    can_invite_users=True,
                ),
            )
            log.info("senku.warm.userbot_promoted", chat_id=chat_id, user_id=uid)
            return True
        except Exception as exc:  # noqa: BLE001 — bot lacks promote rights → bot path
            log.info("senku.warm.userbot_promote_failed",
                     chat_id=chat_id, error=str(exc))
            return False

    async def _demote_userbot(self, admin_client, ub, chat_id: int) -> None:
        """Undo :meth:`_ensure_userbot_can_post` — strip all rights then remove the
        userbot from the channel, so we leave it exactly as we found it."""
        from pyrogram.types import ChatPrivileges

        try:
            me = await ub.get_me()
            uid = me.id
        except Exception:  # noqa: BLE001
            return
        try:
            # Strip admin rights (all-False privileges), then drop membership.
            await admin_client.promote_chat_member(
                chat_id, uid, privileges=ChatPrivileges(),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("senku.warm.userbot_demote_failed", error=str(exc))
        try:
            await admin_client.ban_chat_member(chat_id, uid)
            await admin_client.unban_chat_member(chat_id, uid)  # kick, don't blacklist
            log.info("senku.warm.userbot_removed", chat_id=chat_id, user_id=uid)
        except Exception as exc:  # noqa: BLE001 — leaving it in is harmless
            log.debug("senku.warm.userbot_remove_failed", error=str(exc))

    @staticmethod
    async def _sweep_service_notices(client, chat_id: int) -> int:
        """Delete Telegram's auto-posted 'channel name/photo/description changed'
        service messages. Best-effort; returns how many were removed. Scans a
        small recent window since these notices are always the latest messages."""
        removed = 0
        try:
            async for m in client.get_chat_history(chat_id, limit=15):
                if (getattr(m, "new_chat_title", None) is not None
                        or getattr(m, "new_chat_photo", None) is not None
                        or getattr(m, "delete_chat_photo", None)
                        or getattr(m, "service", None) is not None):
                    try:
                        await client.delete_messages(chat_id, m.id)
                        removed += 1
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001 — sweep is best-effort
            pass
        return removed

    async def _warm_global_search(self, client, chat_id: int, code: str) -> None:
        """Send ~100 throwaway messages then delete them, to enter Global Search.

        Telegram only surfaces a channel in global search once it has a minimum
        posting history. We satisfy that by posting 100 short quotes (from a free
        public API, with a baked-in fallback list when it's unreachable) and then
        deleting every one, leaving the channel clean for the real content.

        BOTS hit a hard ~20 msg/min channel limit (that's why this used to die at
        'warm sent=20' with FLOOD_WAIT). USER accounts don't have that per-channel
        cap, so we send via the USERBOT when one is configured and can reach the
        channel — the userbot is an admin on the distribution channels anyway.
        Only if there's no usable userbot do we fall back to the bot client, and
        there we PACE the sends and honour FloodWait so it degrades gracefully
        instead of erroring out.

        Guarded by a Redis flag so a re-publish never re-warms. Fully best-effort.
        """
        flag = f"nf:dist:{code}:warmed"
        try:
            if self._c.redis and await self._c.redis.get(flag):
                return
        except Exception:  # noqa: BLE001
            pass

        # Before the warm-up burst, clear any leftover 'channel name changed to …'
        # service notice Telegram posted when the wizard renamed the channel — the
        # user wants it gone immediately after the rename, not buried under quotes.
        # Sweep with the USERBOT when available: a bot client can't reliably page
        # channel history, so a bot-only sweep silently finds nothing. Acquire it
        # once here and reuse it for the warm-up burst below.
        ub = await self._acquire_userbot()
        swept = await self._sweep_service_notices(ub or client, chat_id)
        if swept:
            log.info("senku.warm.notice_swept", code=code, removed=swept)

        quotes = await self._fetch_warm_texts(self._WARM_COUNT)

        # Prefer the userbot (no 20/min channel cap). Fall back to the bot client.
        # If the userbot isn't already an admin of the channel, promote it (via the
        # bot admin client) so it can fire all 100 — then demote + remove it after,
        # but ONLY if WE were the ones who promoted it (leave a pre-existing admin
        # userbot untouched).
        sent_ids: list[int] = []
        via = "userbot" if ub is not None else "bot"
        we_promoted = False
        if ub is not None:
            we_promoted = await self._ensure_userbot_can_post(client, ub, chat_id)
            sent_ids = await self._warm_send(ub, chat_id, code, quotes, pace=False)
            # If the userbot couldn't post at all (not a member / no rights),
            # fall back to the bot so warm-up still happens.
            if not sent_ids:
                via = "bot"
                if we_promoted:
                    await self._demote_userbot(client, ub, chat_id)
                    we_promoted = False
                ub = None
        if ub is None:
            sent_ids = await self._warm_send(
                client, chat_id, code, quotes, pace=True,
            )
            deleter = client
        else:
            deleter = ub

        # Delete everything we sent (chunked — delete_messages takes a list).
        for start in range(0, len(sent_ids), self._WARM_BATCH_DELETE):
            chunk = sent_ids[start:start + self._WARM_BATCH_DELETE]
            try:
                await deleter.delete_messages(chat_id, chunk)
            except Exception as exc:  # noqa: BLE001
                log.warning("senku.warm.delete_blip", code=code, error=str(exc))

        # Undo the temporary promotion (strip rights + kick) now that the burst is
        # done — only when we added the userbot ourselves.
        if we_promoted and ub is not None:
            await self._demote_userbot(client, ub, chat_id)

        try:
            if self._c.redis:
                await self._c.redis.set(flag, "1", ex=30 * 24 * 3600)
        except Exception:  # noqa: BLE001
            pass
        log.info("senku.warm.done", code=code, sent=len(sent_ids), via=via)

    async def _warm_send(self, sender, chat_id: int, code: str,
                         quotes: list[str], *, pace: bool) -> list[int]:
        """Send the warm-up quotes via ``sender`` (userbot or bot). Returns the
        message ids sent. When ``pace`` (bot path), sleep ~1s between sends and
        honour FloodWait so the bot's channel rate-limit degrades gracefully
        instead of erroring the whole warm-up out at message 20."""
        import asyncio

        from pyrogram.errors import FloodWait

        sent_ids: list[int] = []
        for text in quotes:
            try:
                m = await sender.send_message(
                    chat_id, text, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                sent_ids.append(m.id)
                if pace:
                    await asyncio.sleep(1.1)  # stay under the bot's per-chat cap
            except FloodWait as fw:
                # Honour Telegram's requested wait, then continue (bot path).
                wait = int(getattr(fw, "value", 0) or 0)
                if wait and wait <= 60:
                    await asyncio.sleep(wait + 1)
                    continue
                log.warning("senku.warm.flood", code=code,
                            sent=len(sent_ids), wait=wait)
                break
            except Exception as exc:  # noqa: BLE001 — stop, delete what we have
                log.warning("senku.warm.send_stopped", code=code,
                            sent=len(sent_ids), error=str(exc))
                break
        return sent_ids

    async def _fetch_warm_texts(self, count: int) -> list[str]:
        """``count`` short throwaway strings for the global-search warm-up.

        Pulls from ZenQuotes' free batch endpoint (no key), falling back to a
        generated list when it's unreachable so warm-up never depends on a third
        party. Each string is made unique (index-suffixed) so Telegram never
        rejects a duplicate send."""
        texts: list[str] = []
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as cli:
                # ZenQuotes returns up to 50 per call on the free "quotes" route.
                for _ in range((count // 50) + 1):
                    r = await cli.get("https://zenquotes.io/api/quotes")
                    if r.status_code != 200:
                        break
                    for item in r.json():
                        q = (item.get("q") or "").strip()
                        a = (item.get("a") or "").strip()
                        if q:
                            texts.append(f"“{q}” — {a}" if a else q)
                    if len(texts) >= count:
                        break
        except Exception as exc:  # noqa: BLE001 — fall back to generated text
            log.debug("senku.warm.quotes_api_failed", error=str(exc))

        # Ensure exactly ``count`` unique strings (index-suffixed to dodge
        # Telegram's duplicate-message rejection).
        out: list[str] = []
        for i in range(count):
            base = texts[i] if i < len(texts) else "Preparing the archive…"
            out.append(f"{base} ·{i + 1}")
        return out

    @staticmethod
    async def _pin_silently(client, chat_id: int, message_id: int) -> None:
        """Pin a message and sweep the "pinned this message" service notice.

        Mirrors :meth:`LogChannelService._pin_silently` — pin without a
        notification, then delete the auto-posted service notice so the channel
        stays clean. Every step is best-effort.
        """
        try:
            await client.pin_chat_message(chat_id, message_id, disable_notification=True)
        except Exception:  # noqa: BLE001
            return
        for candidate in range(message_id + 1, message_id + 4):
            try:
                msg = await client.get_messages(chat_id, candidate)
                if msg and getattr(msg, "pinned_message", None) is not None:
                    await client.delete_messages(chat_id, candidate)
            except Exception:  # noqa: BLE001 — sweep is best-effort
                pass
