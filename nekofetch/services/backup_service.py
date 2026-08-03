"""Main-channel backup & restore — survive a channel ban byte-for-byte.

The main channel is the store's public face. If it's banned, every post has to
reappear on a fresh channel exactly as it was — same caption styling, same photo,
same Index/Download buttons, same divider stickers — with **no re-rendering and no
re-fetching** (TMDB/AniList may have changed, thumbnails may be gone). That's only
possible if we captured everything ahead of time.

:class:`BackupService` does two jobs:

* **capture** (:meth:`backup_all` / :meth:`backup_one`) — snapshot each live
  ``ChannelPost`` into a :class:`PublishedPostBackup`: the finished caption HTML,
  the photo (mirrored to catbox + telegraph via :mod:`image_backup` so it outlives
  the original CDN), the structured button layout, and the divider sticker id.
* **restore** (:meth:`restore_to_channel`) — rebuild every backed-up post on a new
  channel from those rows alone, with pacing (all at once / N per day / every X
  minutes), then repoint ``ChannelPost`` + config at the new channel.

Capture is best-effort per post: one unreachable image or message never aborts the
sweep. Restore reports how many posts it rebuilt so the operator sees progress.

Beyond the main channel, two more scopes are captured into
:class:`ChannelContentBackup` (see :meth:`record_distribution_channel` /
:meth:`record_index` / :meth:`restore_distribution_channel`): a per-title
**distribution** channel's ordered card list and the **index** channel's letter
sections. These matter because ``BotOrchestratorService.recreate_bot`` *deletes*
the live ``BotContentPost`` rows before regenerating — so a banned channel is
re-posted verbatim from the wipe-proof snapshot instead of re-rendered.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    ChannelContentBackup,
    ChannelPost,
    DistributionBot,
    PublishedPostBackup,
)
from nekofetch.infrastructure.database.postgres.session import session_scope

log = get_logger(__name__)


@dataclass(slots=True)
class BackupStats:
    posts: int = 0          # ChannelPost rows considered
    backed_up: int = 0      # snapshots written/updated
    images_mirrored: int = 0  # images that landed on at least one durable host


@dataclass(slots=True)
class RestoreStats:
    total: int = 0          # backups to restore
    restored: int = 0       # messages successfully re-posted
    failed: int = 0


INDEX_KEY = "index"  # channel_key for the single index-channel backup row


def _markup_to_rows(markup: InlineKeyboardMarkup | None) -> list | None:
    """Serialize an inline keyboard to plain JSON rows for storage.

    Main-channel buttons are all URL buttons (Index/Download), so we keep
    ``text`` + ``url`` (and ``callback_data`` if a future button uses it).
    """
    if markup is None:
        return None
    rows: list[list[dict]] = []
    for row in markup.inline_keyboard:
        out: list[dict] = []
        for btn in row:
            entry: dict = {"text": btn.text}
            if btn.url:
                entry["url"] = btn.url
            if btn.callback_data:
                entry["callback_data"] = btn.callback_data
            out.append(entry)
        if out:
            rows.append(out)
    return rows or None


def _rows_to_markup(rows: list | None) -> InlineKeyboardMarkup | None:
    """Rebuild an inline keyboard from stored JSON rows (inverse of above)."""
    if not rows:
        return None
    kb: list[list[InlineKeyboardButton]] = []
    for row in rows:
        out: list[InlineKeyboardButton] = []
        for entry in row:
            out.append(InlineKeyboardButton(
                entry.get("text", " "),
                url=entry.get("url"),
                callback_data=entry.get("callback_data"),
            ))
        if out:
            kb.append(out)
    return InlineKeyboardMarkup(kb) if kb else None


class BackupService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self.cfg = container.config.main_channel

    # ── Capture ──────────────────────────────────────────────────────────────

    async def backup_all(self) -> BackupStats:
        """Snapshot every tracked main-channel post. Best-effort per post."""
        stats = BackupStats()
        async with session_scope(self._c.pg_sessionmaker) as session:
            posts = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.main_message_id.isnot(None))
                )
            ).scalars().all()
        stats.posts = len(posts)
        for post in posts:
            ok = await self.backup_one(post.anime_doc_id)
            if ok:
                stats.backed_up += 1
                if ok.image_catbox_url or ok.image_telegraph_url or ok.image_imgbb_url:
                    stats.images_mirrored += 1
        log.info("backup.all.done", posts=stats.posts, backed_up=stats.backed_up)
        return stats

    async def backup_one(self, anime_doc_id: str) -> PublishedPostBackup | None:
        """Snapshot a single post into a ``PublishedPostBackup`` (upsert).

        Rebuilds the exact caption + buttons from the same service the publisher
        uses, mirrors the photo to durable hosts, and records the divider sticker.
        Returns the saved row, or ``None`` if there's nothing live to back up.
        """
        from nekofetch.services.main_channel_service import MainChannelService

        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
                )
            ).scalar_one_or_none()
            if post is None or not post.main_message_id:
                return None

        svc = MainChannelService(self._c)
        facts = await svc.gather_facts(anime_doc_id)
        caption = svc._caption(facts)
        markup = await svc._buttons(facts)
        source_url = facts.backdrop_url or facts.poster_url or ""

        # Mirror the photo onto independent hosts so it survives the ban.
        catbox_url = telegraph_url = imgbb_url = None
        if source_url:
            from kurosoden.shared.image_backup import backup_image

            mirrored = await backup_image(self._c, source_url)
            catbox_url = mirrored.catbox_url
            telegraph_url = mirrored.telegraph_url
            imgbb_url = mirrored.imgbb_url

        divider = getattr(self.cfg, "divider_sticker_id", None)

        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(PublishedPostBackup).where(
                        PublishedPostBackup.anime_doc_id == anime_doc_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = PublishedPostBackup(anime_doc_id=anime_doc_id)
                session.add(row)
            row.title = facts.title
            row.caption = caption
            row.image_source_url = source_url or None
            row.image_catbox_url = catbox_url
            row.image_telegraph_url = telegraph_url
            row.image_imgbb_url = imgbb_url
            row.button_data = _markup_to_rows(markup)
            row.divider_sticker_id = divider
            row.source_channel_id = post.main_channel_id
            row.source_message_id = post.main_message_id
            await session.commit()
            await session.refresh(row)
            log.info("backup.one.done", anime=anime_doc_id,
                     mirrored=bool(catbox_url or telegraph_url or imgbb_url))
            return row

    # ── Restore ──────────────────────────────────────────────────────────────

    async def restore_to_channel(
        self, new_channel_id: int, *,
        per_batch: int = 0, delay_seconds: float = 0.0,
        update_config: bool = True,
    ) -> RestoreStats:
        """Rebuild every backed-up post on ``new_channel_id`` from the DB alone.

        Pacing: ``per_batch`` posts, then sleep ``delay_seconds`` before the next
        batch (``per_batch=0`` → no pacing, post as fast as the API allows). When
        ``update_config`` is set, the main-channel id is repointed and each
        ``ChannelPost`` is updated to the new message id so future edits land on
        the restored post.

        No re-rendering: images come from the mirrored URLs, captions and buttons
        from the stored snapshot. A per-post failure is counted and skipped.
        """
        client = getattr(self._c, "admin_client", None)
        if client is None:
            return RestoreStats()

        async with session_scope(self._c.pg_sessionmaker) as session:
            backups = (
                await session.execute(
                    select(PublishedPostBackup).order_by(PublishedPostBackup.id)
                )
            ).scalars().all()

        stats = RestoreStats(total=len(backups))
        divider = getattr(self.cfg, "divider_sticker_id", None)
        posted_in_batch = 0

        for b in backups:
            # Divider sticker first (channel layout detail), best-effort.
            sticker = b.divider_sticker_id or divider
            if sticker:
                try:
                    await client.send_sticker(new_channel_id, sticker)
                except Exception as exc:  # noqa: BLE001
                    log.debug("restore.divider.failed", error=str(exc))

            photo = (b.image_catbox_url or b.image_telegraph_url
                     or b.image_imgbb_url or b.image_source_url)
            markup = _rows_to_markup(b.button_data)
            try:
                if photo:
                    sent = await client.send_photo(
                        new_channel_id, photo, caption=b.caption or "",
                        reply_markup=markup, parse_mode=ParseMode.HTML,
                    )
                else:
                    sent = await client.send_message(
                        new_channel_id, b.caption or "",
                        reply_markup=markup, parse_mode=ParseMode.HTML,
                    )
                stats.restored += 1
            except Exception as exc:  # noqa: BLE001
                stats.failed += 1
                log.warning("restore.post.failed", anime=b.anime_doc_id, error=str(exc))
                continue

            if update_config:
                await self._repoint(b.anime_doc_id, new_channel_id, sent.id)

            posted_in_batch += 1
            if per_batch and posted_in_batch >= per_batch:
                posted_in_batch = 0
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)

        if update_config and stats.restored:
            try:
                from nekofetch.services.settings_service import SettingsService

                await SettingsService(self._c).set_value(
                    "main_channel", "channel_id", new_channel_id
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("restore.config_write_failed", error=str(exc))

        log.info("restore.done", total=stats.total, restored=stats.restored,
                 failed=stats.failed, channel=new_channel_id)
        return stats

    async def _repoint(self, anime_doc_id: str, channel_id: int, message_id: int) -> None:
        """Point the ChannelPost row at the restored message on the new channel."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
                )
            ).scalar_one_or_none()
            if post is None:
                post = ChannelPost(anime_doc_id=anime_doc_id)
                session.add(post)
            post.main_channel_id = channel_id
            post.main_message_id = message_id
            await session.commit()

    # ── Distribution / index scope ─────────────────────────────────────────────

    def _divider(self) -> str | None:
        """The channel divider sticker id (bot config, main-channel fallback)."""
        bot = getattr(self._c.config, "bot", None)
        return (getattr(bot, "divider_sticker_id", None)
                or getattr(self.cfg, "divider_sticker_id", None))

    async def _mirror_url(self, source_url: str) -> str | None:
        """Mirror a remote image URL to a durable host; fall back to the source.

        Used for images we only have as URLs (the index poster / reserved-slot
        graphic). Best-effort — returns the original URL if mirroring fails so
        the restore still has *something* to post."""
        if not source_url:
            return None
        try:
            from kurosoden.shared.image_backup import backup_image

            mirrored = await backup_image(self._c, source_url)
            return mirrored.primary or source_url
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("backup.index.mirror_url_failed", url=source_url, error=str(exc))
            return source_url

    async def record_distribution_channel(
        self, anime_doc_id: str,
    ) -> ChannelContentBackup | None:
        """Snapshot a distribution channel's content pack into a wipe-proof row.

        Reads the live ``BotContentPost`` rows (in send order) for the channel
        bound to ``anime_doc_id``, mirrors each card's image onto a durable host,
        and stores the finished caption + structured buttons + pin flag as an
        ordered card list. Upserts on ``(distribution, anime_doc_id)`` so a later
        re-capture (e.g. after an incremental update) replaces the snapshot.

        Called at publish time and before a recreate wipes the live posts, so a
        banned channel can be re-posted verbatim by :meth:`restore_distribution_channel`.
        Returns the saved row, or ``None`` if there's no channel/content to back up.
        """
        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (
                await session.execute(
                    select(DistributionBot)
                    .where(DistributionBot.anime_doc_id == anime_doc_id)
                    .order_by(DistributionBot.id.desc())
                )
            ).scalars().first()
            if bot is None:
                return None
            posts = (
                await session.execute(
                    select(BotContentPost)
                    .where(BotContentPost.bot_id == bot.id)
                    .order_by(BotContentPost.order)
                )
            ).scalars().all()
            bot_chat_id = bot.chat_id
            bot_name = bot.name

        if not posts:
            return None

        divider = self._divider()
        cards: list[dict] = []
        footer_mid: int | None = None
        for i, p in enumerate(posts):
            # Mirror the card image onto a durable host so the restore survives
            # the original CDN. Prefer the already-cached (catbox) URL.
            source = p.image_cached_url or p.image_url or ""
            durable = source
            if source:
                try:
                    from kurosoden.shared.image_backup import backup_image

                    mirrored = await backup_image(self._c, source)
                    durable = mirrored.primary or source
                except Exception as exc:  # noqa: BLE001 — best-effort per card
                    log.warning("backup.dist.image_failed",
                                anime=anime_doc_id, error=str(exc))
            cards.append({
                "kind": p.post_type,
                "caption": p.caption or "",
                "image_url": durable or None,
                "button_data": p.button_data,
                "is_pinned": bool(p.is_pinned),
                # Entry id (season/movie cards) so restore can remap the watch
                # guide's {BOT_QUAL#id:…} deep-links to the fresh message ids.
                "anilist_id": p.anilist_id,
                # A divider sticker precedes every card except the first, matching
                # the distribution app's ``_send_posts`` choreography.
                "divider_before": bool(i > 0 and divider),
            })
            if p.post_type == "footer" and p.tg_message_id:
                footer_mid = p.tg_message_id

        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(ChannelContentBackup).where(
                        ChannelContentBackup.scope == "distribution",
                        ChannelContentBackup.channel_key == anime_doc_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ChannelContentBackup(
                    scope="distribution", channel_key=anime_doc_id,
                )
                session.add(row)
            row.title = bot_name
            row.source_chat_id = bot_chat_id
            row.cards = cards
            row.footer_message_id = footer_mid
            await session.commit()
            await session.refresh(row)
            log.info("backup.dist.done", anime=anime_doc_id, cards=len(cards))
            return row

    async def record_index(self) -> ChannelContentBackup | None:
        """Snapshot the whole index channel (verbatim) for a fresh-channel rebuild.

        Captures **every** slot in ``sort_order`` — labelled letter sections
        *and* trailing reserved slots — plus the pinned poster, so
        :meth:`restore_index` can repost the channel end-to-end and remap every
        ``IndexSection.message_id`` to the new post. Letter/reserved/poster
        graphics are mirrored to a durable host. Upserts in place on the fixed
        ``index`` key. Returns the saved row, or ``None`` when the index channel
        is inactive or has no sections.
        """
        from nekofetch.services.index_channel_service import (
            IndexChannelService,
            _IMG_DIR,
            _RESERVED_CAP,
            _RESERVED_IMG,
            _POSTER_CAP,
            _letter_caption,
        )
        from nekofetch.infrastructure.database.postgres.models import IndexSection

        svc = IndexChannelService(self._c)
        if not svc._active():
            return None

        async with session_scope(self._c.pg_sessionmaker) as session:
            all_sections = (
                await session.execute(
                    select(IndexSection).order_by(IndexSection.sort_order)
                )
            ).scalars().all()
        if not all_sections:
            return None

        async def _mirror_file(path) -> str | None:
            if not path.exists():
                return None
            try:
                from kurosoden.shared.image_backup import backup_bytes

                mirrored = await backup_bytes(
                    self._c, path.read_bytes(), mime="image/jpeg",
                )
                return mirrored.primary
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.warning("backup.index.image_failed", path=str(path), error=str(exc))
                return None

        # Cache mirrored letter graphics so a letter with multiple chunks (A, A(2))
        # only uploads its image once.
        letter_img: dict[str, str | None] = {}
        reserved_seq = 0
        reserved_total = sum(1 for s in all_sections if s.label is None)
        cards: list[dict] = []
        for sec in all_sections:
            if sec.label is None:
                # Reserved slot — verbatim caption + shared reserved graphic.
                reserved_seq += 1
                if "__reserved__" not in letter_img:
                    letter_img["__reserved__"] = await self._mirror_url(_RESERVED_IMG)
                cards.append({
                    "kind": "index_reserved",
                    "label": None,
                    "sort_order": sec.sort_order,
                    "base_letter": None,
                    "caption": f"{_RESERVED_CAP}\n\n<i>Slot {reserved_seq}/{reserved_total}</i>",
                    "image_url": letter_img["__reserved__"],
                    "is_pinned": False,
                    "divider_before": True,
                })
                continue
            base = sec.base_letter or (sec.label or "")[:1]
            titles = await svc._titles_for_letter(base) if base else []
            caption = _letter_caption(sec.label or base, titles)
            if base not in letter_img:
                letter_img[base] = await _mirror_file(_IMG_DIR / f"{base}.jpg")
            cards.append({
                "kind": "index_section",
                "label": sec.label,
                "sort_order": sec.sort_order,
                "base_letter": base,
                "caption": caption,
                "image_url": letter_img[base],
                "is_pinned": False,
                "divider_before": True,
            })

        # The pinned poster (letter-grid navigation) — mirror its graphic too.
        poster_img = await self._mirror_url(_RESERVED_IMG)  # poster reuses a hosted image
        poster = {
            "kind": "index_poster",
            "caption": _POSTER_CAP,
            "image_url": poster_img,
            "is_pinned": True,
            "divider_before": False,
        }

        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(ChannelContentBackup).where(
                        ChannelContentBackup.scope == "index",
                        ChannelContentBackup.channel_key == INDEX_KEY,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ChannelContentBackup(scope="index", channel_key=INDEX_KEY)
                session.add(row)
            row.title = "Index"
            row.source_chat_id = getattr(svc.cfg, "channel_id", None) or None
            # Poster first (it's pinned at the top), then every slot in order.
            row.cards = [poster] + cards
            await session.commit()
            await session.refresh(row)
            log.info("backup.index.done", slots=len(cards))
            return row

    @staticmethod
    async def _resolve_handle(client, chat_id: int) -> str | None:
        """Best-effort ``@username`` of ``chat_id`` for watch-guide deep-links."""
        try:
            chat = await client.get_chat(chat_id)
            return getattr(chat, "username", None)
        except Exception:  # noqa: BLE001 — no handle → plain text quals
            return None

    @staticmethod
    def _resolve_quals(
        caption: str, handle: str | None,
        msg_by_id: dict[int, int] | None = None,
    ) -> str:
        """Expand ``{BOT_QUAL…}`` placeholders (mirrors the distribution app).

        Honours both the anchored ``{BOT_QUAL#<id>:LABEL}`` and legacy
        ``{BOT_QUAL:LABEL}`` forms. With ``msg_by_id`` an anchored id links to
        ``t.me/<handle>/<msg_id>``; otherwise an anchored/plain placeholder links
        to the channel root. Without a handle it collapses to the bare label.
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
            href = (f"https://t.me/{handle}/{mid}" if mid
                    else f"https://t.me/{handle}")
            return f'<a href="{href}">{label}</a>'

        return re.sub(r"\{BOT_QUAL(?:#(\d+))?:([^}]+)\}", _sub, caption)

    async def restore_distribution_channel(
        self, anime_doc_id: str, new_chat_id: int,
    ) -> RestoreStats:
        """Re-post a backed-up distribution channel verbatim onto ``new_chat_id``.

        No regeneration and no re-render: captions, buttons, dividers, pins, and
        mirrored images come straight from the :class:`ChannelContentBackup` row.
        Wired into ban recovery — after a replacement channel is created, this
        reposts the saved pack instead of rebuilding it from metadata.

        Returns a :class:`RestoreStats`; a per-card failure is counted and skipped.
        """
        client = getattr(self._c, "admin_client", None)
        if client is None:
            return RestoreStats()

        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(ChannelContentBackup).where(
                        ChannelContentBackup.scope == "distribution",
                        ChannelContentBackup.channel_key == anime_doc_id,
                    )
                )
            ).scalar_one_or_none()

        cards = list(row.cards) if row and row.cards else []
        stats = RestoreStats(total=len(cards))
        if not cards:
            return stats

        # A distribution card's ``button_data`` is the audio-keyboard payload
        # ({type, links, qualities}), not plain URL rows — rebuild it with the
        # same builder the live distribution app uses so the Download buttons
        # come back verbatim from the stored links (no regeneration).
        from nekofetch.services.bot_render import build_audio_keyboard

        fmt = self._c.config.post_format
        # Resolve the new channel's @handle so watch-guide quality deep-links
        # ({BOT_QUAL…}) point at the fresh channel; fall back to plain text.
        handle = await self._resolve_handle(client, new_chat_id)

        divider = self._divider()
        # entry anilist_id → freshly-posted message id, filled as season/movie
        # cards restore. Cards are stored in send order (season cards precede the
        # watch guide), so by the time the guide's caption resolves every entry it
        # references already has a new id → its {BOT_QUAL#id:…} quality links jump
        # to the restored season card, not the channel root.
        msg_by_id: dict[int, int] = {}
        for card in cards:
            try:
                if card.get("divider_before") and divider:
                    try:
                        await client.send_sticker(new_chat_id, divider)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("restore.dist.divider_failed", error=str(exc))

                caption = self._resolve_quals(
                    card.get("caption") or "", handle, msg_by_id,
                )
                markup = build_audio_keyboard(card.get("button_data"), fmt)
                image = card.get("image_url")
                if image:
                    sent = await client.send_photo(
                        new_chat_id, image, caption=caption,
                        reply_markup=markup, parse_mode=ParseMode.HTML,
                    )
                else:
                    sent = await client.send_message(
                        new_chat_id, caption,
                        reply_markup=markup, parse_mode=ParseMode.HTML,
                    )
                aid = card.get("anilist_id")
                if aid is not None:
                    msg_by_id[int(aid)] = sent.id
                if card.get("is_pinned"):
                    try:
                        await client.pin_chat_message(
                            new_chat_id, sent.id, disable_notification=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug("restore.dist.pin_failed", error=str(exc))
                stats.restored += 1
            except Exception as exc:  # noqa: BLE001 — skip one bad card
                stats.failed += 1
                log.warning("restore.dist.card_failed",
                            anime=anime_doc_id, error=str(exc))

        log.info("restore.dist.done", anime=anime_doc_id, total=stats.total,
                 restored=stats.restored, failed=stats.failed, chat=new_chat_id)
        return stats

    async def restore_index(
        self, new_channel_id: int, *, new_username: str | None = None,
    ) -> RestoreStats:
        """Rebuild the whole index channel onto ``new_channel_id`` from backup.

        Reposts the pinned poster and every slot (labelled sections + reserved)
        in ``sort_order`` from the :class:`ChannelContentBackup` row, remaps each
        ``IndexSection.message_id`` to the freshly-posted message, and repoints
        config (``index_channel.channel_id`` / ``username`` / ``poster_message_id``)
        so every t.me link, poster button and "Go to Top" target follows to the
        new channel. Mirrors :meth:`restore_distribution_channel` for the index
        scope — used when the index channel itself is banned.

        ``new_username`` (the fresh channel's public @handle, no ``@``) is needed
        for the poster/letter deep-links; when omitted we resolve it from the chat.
        Returns a :class:`RestoreStats`; a per-slot failure is counted and skipped.
        """
        client = getattr(self._c, "admin_client", None)
        if client is None:
            return RestoreStats()

        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(ChannelContentBackup).where(
                        ChannelContentBackup.scope == "index",
                        ChannelContentBackup.channel_key == INDEX_KEY,
                    )
                )
            ).scalar_one_or_none()

        cards = list(row.cards) if row and row.cards else []
        stats = RestoreStats(total=len(cards))
        if not cards:
            return stats

        username = (new_username or await self._resolve_handle(client, new_channel_id)
                    or "").lstrip("@")

        divider = self._divider()
        # Repost in order, recording each slot's new message id keyed by its
        # original sort_order so we can remap IndexSection afterwards.
        new_ids: dict[int, int] = {}
        poster_new_id: int | None = None
        for card in cards:
            try:
                if card.get("divider_before") and divider:
                    try:
                        await client.send_sticker(new_channel_id, divider)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("restore.index.divider_failed", error=str(exc))

                caption = card.get("caption") or ""
                image = card.get("image_url")
                if image:
                    sent = await client.send_photo(
                        new_channel_id, image, caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    sent = await client.send_message(
                        new_channel_id, caption, parse_mode=ParseMode.HTML,
                    )

                if card.get("kind") == "index_poster":
                    poster_new_id = sent.id
                    if card.get("is_pinned"):
                        try:
                            await client.pin_chat_message(
                                new_channel_id, sent.id, disable_notification=True,
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.debug("restore.index.pin_failed", error=str(exc))
                else:
                    so = card.get("sort_order")
                    if so is not None:
                        new_ids[int(so)] = sent.id
                stats.restored += 1
            except Exception as exc:  # noqa: BLE001 — skip one bad slot
                stats.failed += 1
                log.warning("restore.index.slot_failed", error=str(exc))

        # Remap every IndexSection to its new message id (by sort_order).
        if new_ids:
            from nekofetch.infrastructure.database.postgres.models import IndexSection

            async with session_scope(self._c.pg_sessionmaker) as session:
                rows = (
                    await session.execute(select(IndexSection))
                ).scalars().all()
                for sec in rows:
                    mid = new_ids.get(sec.sort_order)
                    if mid is not None:
                        sec.message_id = mid

        # Repoint config so every future link/poster targets the new channel.
        cfg = self.cfg_index
        cfg.channel_id = new_channel_id
        if username:
            cfg.username = username
        if poster_new_id is not None:
            cfg.poster_message_id = poster_new_id
        await self._persist_index_config(new_channel_id, username, poster_new_id)

        # Rebuild the poster's letter-button grid so it points at the new ids.
        try:
            from nekofetch.services.index_channel_service import IndexChannelService

            await IndexChannelService(self._c)._rebuild_poster()
        except Exception as exc:  # noqa: BLE001 — best-effort cosmetic pass
            log.warning("restore.index.poster_rebuild_failed", error=str(exc))

        log.info("restore.index.done", total=stats.total, restored=stats.restored,
                 failed=stats.failed, chat=new_channel_id, poster=poster_new_id)
        return stats

    @property
    def cfg_index(self):
        return self._c.config.index_channel

    async def _persist_index_config(
        self, channel_id: int, username: str | None, poster_id: int | None,
    ) -> None:
        """Persist the repointed index-channel identity through SettingsService
        so it survives a restart (mirrors how the settings panel writes)."""
        try:
            from nekofetch.services.settings_service import SettingsService

            svc = SettingsService(self._c)
            await svc.set_value("index_channel", "channel_id", channel_id)
            if username:
                await svc.set_value("index_channel", "username", username)
            if poster_id is not None:
                await svc.set_value("index_channel", "poster_message_id", poster_id)
        except Exception as exc:  # noqa: BLE001 — live config already updated
            log.warning("restore.index.config_persist_failed", error=str(exc))
