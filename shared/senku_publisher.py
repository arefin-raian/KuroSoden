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
_TV_FORMATS = {"TV", "TV_SHORT", "TV_SPECIAL"}


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

        Choreography (mirrors the publish tail): delete the trailing footer
        message and the divider right before it, send ``divider → new card``
        for each new entry, then ``divider → footer`` again — and rewrite the
        tail of :class:`ChannelLayout` to match. The pinned info card and watch
        guide are left exactly where they are.

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
                    )
                )
            ).scalar_one_or_none()
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

        # 2. Build cards for the new entries (reusing publish's builders).
        new_cards = await self._build_update_cards(
            anime_doc_id, new_anilist_ids, already,
        )
        if not new_cards:
            log.info("senku.update.nothing", anime=anime_doc_id)
            return {"appended": 0, "chat_id": chat_id}

        # 3. Re-choreograph the tail: drop old footer (+ its leading divider),
        #    append each new card behind a divider, then divider + footer.
        appended = await self._append_and_refooter(
            client, chat_id, bot_id, layout, new_cards,
        )
        log.info("senku.update.done", anime=anime_doc_id, chat_id=chat_id,
                 appended=appended)
        return {"appended": appended, "chat_id": chat_id}

    async def _build_update_cards(
        self, anime_doc_id: str, new_anilist_ids: list[int] | None,
        already: set[int],
    ) -> list[dict]:
        """Build the post dicts for entries not yet present in the channel.

        Reuses the same card builders as :meth:`_build_posts`. Only entries that
        (a) have finished packs and (b) aren't already in the layout are built;
        when ``new_anilist_ids`` is given, we additionally restrict to that set.
        """
        from nekofetch.services.bot_content import BotContentService

        svc = BotContentService(self._c)
        packs = await svc._load_packs(anime_doc_id)
        if not packs:
            return []
        meta = await svc._gather_metadata(anime_doc_id)
        walked = await svc._walk_franchise(anime_doc_id, meta)

        wanted = set(new_anilist_ids or [])
        tv = list(walked.get("tv", []))
        cards: list[dict] = []

        for entry in walked.get("all", []):
            aid = getattr(entry, "anilist_id", None)
            if aid is None or aid in already:
                continue
            if wanted and aid not in wanted:
                continue

            entry_meta = svc._entry_meta(meta, entry)
            if entry.format in _TV_FORMATS:
                season = (tv.index(entry) + 1) if entry in tv else 1
                entry_packs = [p for p in packs if p.season == season]
                caption, image = svc._build_season_card(entry_meta, season, entry_packs)
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
                "caption": caption,
                "image": await self._cache_image(image),
                "button_data": buttons,
                "pinned": False,
                "anilist_id": aid,
            })
        return cards

    async def _append_and_refooter(
        self, client, chat_id: int, bot_id: int,
        layout: list[dict], new_cards: list[dict],
    ) -> int:
        """Delete *only* the trailing footer, append cards, re-post the footer.

        The divider that already sits right before the footer stays put — it's
        correctly placed, so deleting and re-sending the same sticker in the
        same spot would be pointless churn. We keep it and slot the new cards in
        after it, each followed by its own divider, then re-post the footer:

            … <kept divider> card₁ <divider> card₂ … <divider> footer

        Rewrites this channel's :class:`ChannelLayout` to reflect the new tail.
        Best-effort on Telegram calls (a failed delete/send is logged, never
        aborts), so a partial update still leaves a consistent saved layout.
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

        # Find the trailing footer. We delete only the footer message; the
        # divider before it is left in place (the new cards go after it).
        footer_idx = next(
            (i for i in range(len(layout) - 1, -1, -1)
             if layout[i]["kind"] == "footer"),
            None,
        )
        if footer_idx is not None:
            footer_post = layout[footer_idx]
            body = layout[:footer_idx]  # keeps the pre-footer divider
            fmid = footer_post.get("tg_message_id")
            if fmid:
                try:
                    await client.delete_messages(chat_id, fmid)
                except Exception as exc:  # noqa: BLE001 — stale id, already gone
                    log.warning("senku.update.delete_failed", mid=fmid, error=str(exc))
        else:
            # No footer tracked — append after everything, then add a footer.
            body = list(layout)
            footer_post = None

        new_layout = list(body)
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
            caption = self._resolve_caption(post.get("caption") or "", handle, fmt)
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
            new_layout.append({
                "kind": post.get("post_type") or "season_card",
                "tg_message_id": msg.id,
                "anilist_id": post.get("anilist_id"),
                "is_pinned": False,
            })
            appended += 1

        # A divider only needs to *lead* the first new card when the body
        # doesn't already end in one — the footer path keeps the pre-footer
        # divider, but the no-footer path ends on a card and needs a separator.
        need_leading_divider = bool(body) and body[-1].get("kind") != "divider"
        for i, post in enumerate(new_cards):
            if (i == 0 and need_leading_divider) or i > 0:
                await _emit_divider()
            await _send_card(post)

        # Divider + re-posted footer (reuse the old footer's text/image).
        await _emit_divider()
        footer_caption, footer_image = await self._footer_content(footer_post)
        try:
            caption = self._resolve_caption(footer_caption, handle, fmt)
            if footer_image:
                fmsg = await client.send_photo(
                    chat_id, footer_image, caption=caption, parse_mode=ParseMode.HTML,
                )
            else:
                fmsg = await client.send_message(
                    chat_id, caption, parse_mode=ParseMode.HTML,
                )
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
        return appended

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
        # each entry's locally-rendered thumbnail to a public URL.
        franchise = self._reorder_franchise(walked, entries)
        generated = await self._bridge_thumbnails(code, entries)

        posts: list[dict] = []
        order = 0

        # ── 1. Info card ──
        info_caption, info_default = await svc._build_info_card(meta)
        if info_caption:
            first_tv = franchise["tv"][0] if franchise["tv"] else None
            info_image = svc._pick_card_image(
                generated.get(getattr(first_tv, "anilist_id", None)),
                info_default, meta,
            )
            posts.append({
                "post_type": "info_card", "order": order,
                "caption": info_caption,
                "image": await self._cache_image(info_image),
                "button_data": None, "pinned": True,
            })
            order += 1

        # ── 2. Season cards (confirmed TV order) ──
        for i, entry in enumerate(franchise["tv"], start=1):
            season_packs = [p for p in packs if p.season == i]
            entry_meta = svc._entry_meta(meta, entry)
            gen = generated.get(entry.anilist_id)
            if gen:
                entry_meta["poster_url"] = gen
            caption, image = svc._build_season_card(entry_meta, i, season_packs)
            buttons = await svc._build_season_buttons(season_packs)
            posts.append({
                "post_type": "season_card", "order": order,
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
            gen = generated.get(entry.anilist_id)
            if gen:
                entry_meta["poster_url"] = gen
            is_movie = entry.format == "MOVIE" or (
                entry.format in ("OVA", "ONA", "SPECIAL")
                and (entry.episodes or 0) <= 1
            )
            caption, image = svc._build_season_card(entry_meta, 1, extra_packs)
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

    def _reorder_franchise(
        self, walked: dict, entries: list[EntryData],
    ) -> dict:
        """Reorder a fresh AniList walk to match the admin's confirmed entries.

        ``walked`` is ``{"tv": [...], "extras": [...], "all": [...]}`` of
        :class:`FranchiseEntry` objects (full metadata). We key those by
        ``anilist_id`` and re-emit them in the cached entry order; any AniList
        entry the admin dropped is excluded, and any cached entry AniList
        couldn't resolve is skipped (it has no card-quality metadata anyway).
        """
        by_id = {
            e.anilist_id: e
            for e in walked.get("all", [])
            if getattr(e, "anilist_id", None) is not None
        }
        ordered: list = []
        for ce in entries:
            if ce.anilist_id is not None and ce.anilist_id in by_id:
                ordered.append(by_id[ce.anilist_id])
        # If the cached entries never carried anilist_ids (bare franchise), fall
        # back to the AniList walk order so the channel still gets cards.
        if not ordered:
            ordered = list(walked.get("all", []))
        tv = [e for e in ordered if e.format in _TV_FORMATS]
        extras = [e for e in ordered if e.format not in _TV_FORMATS]
        return {"tv": tv, "extras": extras, "all": ordered}

    async def _bridge_thumbnails(
        self, code: str, entries: list[EntryData],
    ) -> dict[int, str]:
        """Map ``anilist_id → public thumbnail URL`` for entries the admin rendered.

        Phase 3 stores each rendered card as ``file://<path>`` in the entry's
        selection. Telegram can't serve a local path, so we mirror each render
        across the configured hosts (ImgBB first) here. A failed upload just omits
        that entry — the card builder falls back to the AniList poster via
        ``_pick_card_image``.
        """
        from kurosoden.shared.image_backup import backup_bytes

        out: dict[int, str] = {}
        for entry in entries:
            if entry.anilist_id is None:
                continue
            sel = await self.cache.get_selection(code, entry.index)
            url = sel.thumbnail_url if sel else None
            if not url or not url.startswith("file://"):
                continue
            path = Path(url[len("file://"):])
            try:
                data = path.read_bytes()
                result = await backup_bytes(self._c, data, mime="image/jpeg")
                public = result.primary
                if public:
                    out[entry.anilist_id] = public
            except Exception as exc:  # noqa: BLE001 — a missing render just falls back
                log.warning("senku.publish.thumb_bridge_failed",
                            code=code, entry=entry.index, error=str(exc))
        return out

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

            caption = self._resolve_caption(post.get("caption") or "", handle, fmt)
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

            layout.append({
                "kind": post.get("post_type") or "season_card",
                "tg_message_id": msg.id,
                "anilist_id": post.get("anilist_id"),
                "is_pinned": pinned,
            })

        return posted, pinned_ids, layout

    def _resolve_caption(self, caption: str, handle: str | None, fmt) -> str:
        """Resolve ``{BOT_QUAL:...}`` links + premium emoji in a channel caption."""
        import re

        if not caption:
            return caption
        if handle:
            caption = re.sub(
                r"\{BOT_QUAL:([^}]+)\}",
                rf'<a href="https://t.me/{handle}">\1</a>',
                caption,
            )
        else:
            caption = re.sub(r"\{BOT_QUAL:([^}]+)\}", r"\1", caption)
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
        swept = await self._sweep_service_notices(client, chat_id)
        if swept:
            log.info("senku.warm.notice_swept", code=code, removed=swept)

        quotes = await self._fetch_warm_texts(self._WARM_COUNT)

        # Prefer the userbot (no 20/min channel cap). Fall back to the bot client.
        ub = await self._acquire_userbot()
        sent_ids: list[int] = []
        via = "userbot" if ub is not None else "bot"
        if ub is not None:
            sent_ids = await self._warm_send(ub, chat_id, code, quotes, pace=False)
            # If the userbot couldn't post at all (not a member / no rights),
            # fall back to the bot so warm-up still happens.
            if not sent_ids:
                via = "bot"
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
