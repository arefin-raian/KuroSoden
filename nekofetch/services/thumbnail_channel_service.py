"""Thumbnail control center — a dedicated channel for asset selection & thumbnail generation.

Similar in spirit to ``LogChannelService``, but purpose-built for thumbnail
workflows. The channel has:

1. A **pinned queue message** listing all franchises that need thumbnails.
2. A **per-franchise workflow message** (evolving) where admins select assets
   (logo → poster → background → generate) for each entry in sequence.
3. **Self-healing layout** on startup (state restored from Redis).

Workflow flow (per entry in a franchise):

    1. Entry appears in the message with ``⏳`` status.
    2. Admin clicks entry → bot posts a detail panel + action buttons.
    3. **Pick Logo** → Telegraph gallery opens → admin picks number → logo stored.
    4. **Pick Poster** → Telegraph gallery opens → admin picks number → poster stored.
    5. **Pick Background** → Telegraph gallery opens → admin picks number → bg stored.
    6. **Generate Thumbnail** → Playwright renders HTML→image → thumbnail stored.
    7. Entry marked ✅ → next entry begins.
    8. When all entries done → franchise marked complete in the queue.
"""

from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass
from pathlib import Path

import asyncio

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import safe_redis_delete, safe_redis_get, safe_redis_set
from nekofetch.localization.messages import M, t
from nekofetch.providers.metadata.telegraph_client import (
    TelegraphClient,
    ImageEntry,
    TelegraphError,
)
from nekofetch.providers.metadata.tmdb_assets import (
    fetch_logos,
    fetch_posters_ranked,
    fetch_backdrops_ranked,
)
from nekofetch.ui import thumbnail_sections as S
from nekofetch.ui.components import cb
from nekofetch.ui.screens import MESSAGE_LIMIT, _truncate_html
from nekofetch.services.thumbnail_service import ThumbnailRenderService, webp_to_jpeg_async

log = get_logger(__name__)

# ── Redis keys ──
_K_CHANNEL = "nf:thumbcc:channel_id"
_K_QUEUE = "nf:thumbcc:queue"
_K_WORKFLOW = "nf:thumbcc:workflow:{anime_doc_id}"
_K_WORKFLOW_MSG = "nf:thumbcc:workflow_msg:{anime_doc_id}"
_K_SUMMARY_MSG = "nf:thumbcc:summary_msg:{anime_doc_id}"
_K_SELECTED = "nf:thumbcc:selected:{anime_doc_id}:{entry_index}"
_K_PINNED_QUEUE = "nf:thumbcc:pinned_queue"
_K_INTRO = "nf:thumbcc:intro_id"
_K_DUTY_BOARD = "nf:thumbcc:duty_board"
_K_TRACKED = "nf:thumbcc:tracked_ids"
# Armed when an admin taps "Upload my own": the next image posted to the channel
# becomes this entry's asset. Single-worker shift channel, so one pending arm at
# a time keyed on the channel is enough; TTL clears a forgotten arm.
_K_UPLOAD_WAIT = "nf:thumbcc:upload_wait"
_UPLOAD_WAIT_TTL = 900  # 15 min — plenty to find and send an image


@dataclass
class QueueEntry:
    """One franchise in the thumbnail queue."""
    anime_doc_id: str
    anime_title: str
    total_entries: int = 0
    completed_entries: int = 0


@dataclass
class WorkflowEntry:
    """Per-entry state in the thumbnail workflow."""
    index: int
    label: str
    format: str               # "tv" | "movie" | "ova" | "ona" | "special"
    status: str = "pending"   # pending | select_logo | select_poster | select_bg | ready | generating | done
    logo_url: str | None = None
    poster_url: str | None = None
    bg_url: str | None = None
    thumbnail_url: str | None = None
    main_thumbnail_url: str | None = None  # base entry only: TMDB-synopsis variant for the main-channel post
    tmdb_id: int | None = None
    media_type: str | None = None  # "tv" | "movie"
    anilist_id: int | None = None   # ties workflow entry to a franchise installment

    def is_complete(self) -> bool:
        return self.status == "done"


class ThumbnailChannelService:
    """Manages the thumbnail control center channel and asset selection workflow."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self.cfg = container.config.thumbnail_channel
        self._render_service: ThumbnailRenderService | None = None
        self._telegraph: TelegraphClient | None = None

    # ── lazy helpers ───────────────────────────────────────────────────────

    @property
    def _client(self):
        return getattr(self._c, "admin_client", None)

    def _active(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.channel_id != 0 and self._client is not None)

    def _telegraph_client(self) -> TelegraphClient | None:
        if not self.cfg.telegraph_access_token:
            return None
        if self._telegraph is None:
            self._telegraph = TelegraphClient(self.cfg.telegraph_access_token)
        return self._telegraph

    def _render(self) -> ThumbnailRenderService | None:
        if self._render_service is None:
            try:
                self._render_service = ThumbnailRenderService()
            except Exception as exc:
                log.warning("thumbcc.render.init_failed", error=str(exc))
                return None
        return self._render_service

    # ── low-level message helpers ───────────────────────────────────────────

    async def _send(self, text: str, **kw):
        for attempt in range(3):
            try:
                return await self._client.send_message(
                    self.cfg.channel_id, text, parse_mode=ParseMode.HTML, **kw
                )
            except FloodWait as fw:
                log.warning("thumbcc.send.flood_wait", wait=fw.value, attempt=attempt)
                await asyncio.sleep(fw.value + 1)
        return await self._client.send_message(
            self.cfg.channel_id, text, parse_mode=ParseMode.HTML, **kw
        )

    async def _edit(self, mid: int, text: str, reply_markup=None) -> None:
        for attempt in range(3):
            try:
                await self._client.edit_message_text(
                    self.cfg.channel_id, mid, _truncate_html(text, MESSAGE_LIMIT),
                    parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                )
                return
            except FloodWait as fw:
                log.warning("thumbcc.edit.flood_wait", wait=fw.value, attempt=attempt)
                await asyncio.sleep(fw.value + 1)

    async def _exists(self, mid: int) -> bool:
        try:
            msg = await self._client.get_messages(self.cfg.channel_id, mid)
        except Exception:
            return False
        return bool(msg) and not getattr(msg, "empty", False)

    async def _post_divider(self) -> int | None:
        if not self.cfg.divider_sticker_id:
            return None
        for attempt in range(3):
            try:
                msg = await self._client.send_sticker(
                    self.cfg.channel_id, self.cfg.divider_sticker_id
                )
                return msg.id
            except FloodWait as fw:
                log.warning("thumbcc.divider.flood_wait", wait=fw.value, attempt=attempt)
                await asyncio.sleep(fw.value + 1)
            except Exception as exc:
                log.debug("thumbcc.divider.failed", error=str(exc))
                return None
        return None

    def _ts(self) -> str:
        from nekofetch.core.timefmt import now_label
        return now_label()

    # ── Redis persistence ──────────────────────────────────────────────────

    async def _get_queue(self) -> list[QueueEntry]:
        # Safe ``GET`` — a hung read would wedge ``refresh_queue`` (the
        # traceback that motivated the migration).
        raw = await safe_redis_get(self._c.redis, _K_QUEUE,
                                    label="thumbcc.queue.get")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [QueueEntry(**d) for d in data]
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("thumbcc.queue.parse_failed", error=str(exc))
            return []

    async def _save_queue(self, entries: list[QueueEntry]) -> None:
        if not self._c.redis:
            return
        data = [{"anime_doc_id": e.anime_doc_id, "anime_title": e.anime_title,
                 "total_entries": e.total_entries, "completed_entries": e.completed_entries}
                for e in entries]
        await safe_redis_set(self._c.redis, _K_QUEUE, json.dumps(data),
                              label="thumbcc.queue.set")

    async def _get_workflow(self, anime_doc_id: str) -> list[WorkflowEntry] | None:
        # Safe ``GET`` so a single Upstash blip can't wedge the channel.
        raw = await safe_redis_get(self._c.redis,
                                    _K_WORKFLOW.format(anime_doc_id=anime_doc_id),
                                    label="thumbcc.workflow.get")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return [WorkflowEntry(**d) for d in data]
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("thumbcc.workflow.parse_failed", anime=anime_doc_id, error=str(exc))
            return None

    async def _save_workflow(self, anime_doc_id: str, entries: list[WorkflowEntry]) -> None:
        if not self._c.redis:
            return
        data = [{"index": e.index, "label": e.label, "format": e.format,
                 "status": e.status, "logo_url": e.logo_url, "poster_url": e.poster_url,
                 "bg_url": e.bg_url, "thumbnail_url": e.thumbnail_url,
                 "main_thumbnail_url": e.main_thumbnail_url,
                 "tmdb_id": e.tmdb_id, "media_type": e.media_type,
                 "anilist_id": e.anilist_id}
                for e in entries]
        await safe_redis_set(self._c.redis,
                              _K_WORKFLOW.format(anime_doc_id=anime_doc_id),
                              json.dumps(data),
                              label="thumbcc.workflow.set")

    async def mark_entry_rendered(
        self, anime_doc_id: str, anilist_id: int | None, image_path: str,
    ) -> bool:
        """Persist a rendered image in the Redis workflow through a public API.

        Admin editors must not reach through this service's private Redis helpers.
        The workflow is operator state, so a failed sync never invalidates the
        durable ``ThumbnailSource`` edit.
        """
        workflow = await self._get_workflow(anime_doc_id)
        if not workflow:
            return False
        target_id = None if anilist_id in (None, -1) else int(anilist_id)
        entry = next((item for item in workflow if item.anilist_id == target_id), None)
        if entry is None:
            return False
        entry.thumbnail_url = f"thumb://{image_path}"
        entry.status = "done"
        await self._save_workflow(anime_doc_id, workflow)
        return True

    async def refresh_published_thumbnail(
        self, anime_doc_id: str, anilist_id: int, image_path: str,
        *, caption_override: str | None = None,
    ) -> bool:
        """Replace one live distribution-card image and its restore snapshot.

        ``caption_override`` (HTML) replaces the card's caption in the SAME edit —
        used by the multi-season repair to correct a season card whose rating was
        baked from franchise-level data. The live inline keyboard is still read
        and re-passed, so the quality buttons survive."""
        from pathlib import Path
        from nekofetch.infrastructure.database.postgres.models import (
            BotContentPost, ChannelContentBackup, ChannelLayout, DistributionBot,
        )
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from sqlalchemy.orm.attributes import flag_modified
        from kurosoden.shared.image_backup import backup_bytes
        from pyrogram.types import InputMediaPhoto

        if not Path(image_path).exists():
            return False
        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.enabled.is_(True),
                ).order_by(DistributionBot.id.desc())
            )).scalars().first()
            if bot is None or not bot.chat_id:
                return False
            layout = (await session.execute(
                select(ChannelLayout).where(
                    ChannelLayout.channel_bot_id == bot.id,
                    ChannelLayout.anilist_id == anilist_id,
                    ChannelLayout.kind.in_(("season_card", "movie_card")),
                    ChannelLayout.tg_message_id.is_not(None),
                ).order_by(ChannelLayout.seq)
            )).scalars().first()
            post = (await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot.id,
                    BotContentPost.anilist_id == anilist_id,
                    BotContentPost.post_type.in_(("season_card", "movie_card")),
                ).order_by(BotContentPost.order)
            )).scalars().first()
            if layout is None and (post is None or not post.tg_message_id):
                return False
            chat_id = int(bot.chat_id)
            message_id = int(layout.tg_message_id if layout else post.tg_message_id)
            fallback_caption = post.caption if post else ""

        # Senku owns distribution-channel edits when its client is running;
        # the admin client remains a safe fallback for single-client deployments.
        client = getattr(getattr(self._c, "pipeline_manager", None), "senku", None)
        client = client or self._client
        if client is None:
            return False
        caption = fallback_caption
        markup = None
        try:
            if hasattr(client, "get_messages"):
                live = await client.get_messages(chat_id, message_id)
                # HTML rendering so styling survives; keep the live keyboard so the
                # image swap doesn't strip the season card's quality buttons.
                src = getattr(live, "caption", None)
                if src is None:
                    src = getattr(live, "text", None)
                caption = getattr(src, "html", None) or (str(src) if src else None) or caption
                markup = getattr(live, "reply_markup", None)
            # A caller (the multi-season repair) can supply a corrected caption;
            # it wins over the live text, while the live keyboard is preserved.
            if caption_override:
                caption = caption_override
            if not caption:
                log.warning("thumbcc.published_refresh.no_caption",
                            anime=anime_doc_id, anilist_id=anilist_id)
                return False
            # The webp card is the sticker format to Telegram's media endpoint,
            # so the live edit sends a JPEG conversion of the same render.
            photo = (await webp_to_jpeg_async(image_path)) or Path(image_path)
            await client.edit_message_media(
                chat_id, message_id,
                InputMediaPhoto(str(photo), caption=caption,
                                parse_mode=ParseMode.HTML),
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001 - one live surface must not abort the edit
            log.warning("thumbcc.published_refresh.failed", anime=anime_doc_id,
                        anilist_id=anilist_id, error=str(exc))
            return False

        path = Path(image_path)
        mime = {
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(path.suffix.lower(), "image/jpeg")
        mirrored = await backup_bytes(self._c, path.read_bytes(), mime=mime)
        durable = mirrored.primary
        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.enabled.is_(True),
                ).order_by(DistributionBot.id.desc())
            )).scalars().first()
            if bot is not None:
                post = (await session.execute(
                    select(BotContentPost).where(
                        BotContentPost.bot_id == bot.id,
                        BotContentPost.anilist_id == anilist_id,
                        BotContentPost.post_type.in_(("season_card", "movie_card")),
                    ).order_by(BotContentPost.order)
                )).scalars().first()
                if post is not None:
                    post.image_url = str(image_path)
                    post.image_cached_url = durable
                    post.tg_message_id = message_id
                    if caption_override:
                        post.caption = caption_override
                row = (await session.execute(
                    select(ChannelContentBackup).where(
                        ChannelContentBackup.scope == "distribution",
                        ChannelContentBackup.channel_key == anime_doc_id,
                    )
                )).scalar_one_or_none()
                if row is not None and row.cards:
                    cards = list(row.cards)
                    changed = False
                    for card in cards:
                        if (card.get("anilist_id") == anilist_id and
                                card.get("kind") in ("season_card", "movie_card")):
                            card["image_url"] = durable
                            if caption_override:
                                card["caption"] = caption_override
                            changed = True
                    if changed:
                        row.cards = cards
                        flag_modified(row, "cards")
        return True

    async def _get_selected(self, anime_doc_id: str, entry_index: int) -> dict | None:
        raw = await safe_redis_get(self._c.redis,
                                    _K_SELECTED.format(anime_doc_id=anime_doc_id,
                                                        entry_index=entry_index),
                                    label="thumbcc.selected.get")
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        return None

    async def _save_selected(self, anime_doc_id: str, entry_index: int, data: dict) -> None:
        if not self._c.redis:
            return
        await safe_redis_set(
            self._c.redis,
            _K_SELECTED.format(anime_doc_id=anime_doc_id, entry_index=entry_index),
            json.dumps(data),
            label="thumbcc.selected.set",
        )

    # ── queue management ───────────────────────────────────────────────────

    async def _pinned_queue_id(self) -> int | None:
        """Return the id of the pinned queue message, or ``None`` if the
        Redis value is missing / unparseable. The safe parse avoids
        crashing the rebuild when the key was overwritten by an unrelated
        caller (e.g. a stale `'[]'` from a previous code path)."""
        # ``safe_redis_get`` returns ``None`` for both "absent" and "blip" so
        # the caller treats them identically — both yield a re-pin.
        raw = await safe_redis_get(self._c.redis, _K_PINNED_QUEUE,
                                    label="thumbcc.pinned_queue.get")
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.warning("thumbcc.pinned_queue.parse_failed", raw=raw)
            return None

    async def _set_pinned_queue_id(self, mid: int) -> None:
        if self._c.redis:
            await safe_redis_set(self._c.redis, _K_PINNED_QUEUE, str(mid),
                                  label="thumbcc.pinned_queue.set")

    async def _intro_id(self) -> int | None:
        raw = await safe_redis_get(self._c.redis, _K_INTRO,
                                    label="thumbcc.intro.get")
        return int(raw) if raw else None

    async def _tracked_ids(self) -> list[int]:
        # Safe ``GET`` — used by every ensure_channel rebuild.
        raw = await safe_redis_get(self._c.redis, _K_TRACKED,
                                    label="thumbcc.tracked.get")
        return json.loads(raw) if raw else []

    async def _add_tracked(self, *mids: int) -> None:
        if not self._c.redis:
            return
        ids = list(set(await self._tracked_ids() + list(mids)))
        await safe_redis_set(self._c.redis, _K_TRACKED, json.dumps(ids),
                              label="thumbcc.tracked.set")

    async def refresh_published_caption(
        self, anime_doc_id: str, anilist_id: int, caption: str,
    ) -> bool:
        """Edit one live distribution-card caption + its restore snapshot.

        The redo metadata refresh (Task O) rebuilds an entry card's caption when
        its language/episode-count line changed. This is the caption-only sibling
        of :meth:`refresh_published_thumbnail`: media stays untouched, the live
        message's caption is edited in place, and the matching ``BotContentPost``
        + ``ChannelContentBackup`` card copy are updated so a restore ships the
        corrected text.
        """
        from nekofetch.infrastructure.database.postgres.models import (
            BotContentPost,
            ChannelContentBackup,
            ChannelLayout,
            DistributionBot,
        )
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from sqlalchemy.orm.attributes import flag_modified

        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.enabled.is_(True),
                ).order_by(DistributionBot.id.desc())
            )).scalars().first()
            if bot is None or not bot.chat_id:
                return False
            layout = (await session.execute(
                select(ChannelLayout).where(
                    ChannelLayout.channel_bot_id == bot.id,
                    ChannelLayout.anilist_id == anilist_id,
                    ChannelLayout.kind.in_(("season_card", "movie_card")),
                    ChannelLayout.tg_message_id.is_not(None),
                ).order_by(ChannelLayout.seq)
            )).scalars().first()
            post = (await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot.id,
                    BotContentPost.anilist_id == anilist_id,
                )
            )).scalars().first()
            if layout is None and post is None:
                return False
            chat_id = bot.chat_id
            message_id = (
                layout.tg_message_id if layout is not None else None
            ) or (post.tg_message_id if post is not None else None)
            if message_id is None:
                return False
            bot_id = bot.id

        mgr = getattr(self._c, "pipeline_manager", None)
        client = getattr(mgr, "senku", None) if mgr is not None else None
        if client is None:
            client = self._client
        if client is None:
            return False
        try:
            # editMessageCaption drops the inline keyboard unless re-supplied;
            # read the live season/movie card's markup and hand it back so the
            # quality buttons survive a caption refresh.
            markup = None
            if hasattr(client, "get_messages"):
                try:
                    live = await client.get_messages(chat_id, message_id)
                    markup = getattr(live, "reply_markup", None)
                except Exception:  # noqa: BLE001 — best-effort; None keeps old behaviour
                    markup = None
            await client.edit_message_caption(
                chat_id, message_id, caption=caption, parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001 - redo survives a failed caption edit
            log.warning("thumbcc.caption_refresh.failed",
                        anime=anime_doc_id, entry=anilist_id, error=str(exc))
            return False

        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot_id,
                    BotContentPost.anilist_id == anilist_id,
                )
            )).scalars().first()
            if post is not None:
                post.caption = caption
            backup = (await session.execute(
                select(ChannelContentBackup).where(
                    ChannelContentBackup.scope == "distribution",
                    ChannelContentBackup.channel_key == anime_doc_id,
                )
            )).scalars().first()
            if backup is not None and backup.cards:
                changed = False
                for card in backup.cards:
                    if (card.get("kind") in ("season_card", "movie_card")
                            and card.get("anilist_id") == anilist_id):
                        card["caption"] = caption
                        changed = True
                if changed:
                    backup.cards = list(backup.cards)
                    flag_modified(backup, "cards")
        return True

    async def refresh_queue(self) -> None:
        """Rebuild the pinned queue message in the channel."""
        if not self._active():
            return
        entries = await self._get_queue()
        text = S.queue_section(
            [{"anime_title": e.anime_title, "anime_doc_id": e.anime_doc_id,
              "total_entries": e.total_entries, "completed_entries": e.completed_entries}
             for e in entries],
            self._ts(),
        )
        # Build inline keyboard — one button per franchise
        keyboard_rows = []
        for e in entries:
            keyboard_rows.append([
                InlineKeyboardButton(
                    f"{'✅' if e.completed_entries >= e.total_entries else '🔄'} {e.anime_title}",
                    callback_data=cb("thumb", "open", e.anime_doc_id),
                )
            ])

        mid = await self._pinned_queue_id()
        markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

        if mid and await self._exists(mid):
            await self._edit(mid, text, reply_markup=markup)
        else:
            msg = await self._send(text, reply_markup=markup)
            await self._set_pinned_queue_id(msg.id)
            await self._pin_silently(msg.id)

    async def add_to_queue(self, anime_doc_id: str, anime_title: str,
                           entries: list[dict]) -> None:
        """Add a franchise to the thumbnail queue with its entries.

        Posts TWO messages for the new franchise, in order:

          1. **Summary card** — per-entry synopsis (from AniList via the caller)
             so admins have CONTEXT before they pick logos/posters/backgrounds.
             This is what the user noticed was missing: "Before you go to create
             the board, I think it's important that you first get the summary".
          2. **Workflow card** — the asset-picker / generate buttons per entry,
             now with a **Skip Custom Thumbnails** button so admins can force
             the pipeline ahead when they're short on time (falls back to
             AniList posters downstream).

        Both messages are tracked in Redis so a restart wipes them as a unit
        during ``ensure_channel``.

        **Idempotency:** if the workflow for ``anime_doc_id`` already exists AND
        every entry is already ``done``, we skip the UI reposts so a pipeline
        re-run (e.g. recovery after a transient publish() error) doesn't spam
        the thumbnail channel with duplicate workflow cards.
        """
        if not self._active():
            return

        # Build workflow entries — carry forward per-entry synopsis AND the
        # anilist_id so :class:`ThumbnailOrchestratorService` and downstream
        # :class:`BotContentService` can map `<season_num>` ↔ `<anilist_id>`
        # ↔ `<generated thumbnail_url>` cleanly.
        workflow: list[WorkflowEntry] = []
        for i, e in enumerate(entries, start=1):
            workflow.append(WorkflowEntry(
                index=i,
                label=e.get("label", f"Entry {i}"),
                format=e.get("format", "tv"),
                tmdb_id=e.get("tmdb_id"),
                media_type=e.get("media_type", "tv"),
                anilist_id=e.get("anilist_id"),
            ))

        await self._save_workflow(anime_doc_id, workflow)

        # ── Idempotency check: skip the channel posts if every entry is done.
        existing_workflow = await self._get_workflow(anime_doc_id)
        if existing_workflow and existing_workflow and all(
            w.is_complete() for w in existing_workflow
        ):
            log.info(
                "thumbcc.queued.skip_already_done",
                anime=anime_doc_id, entries=len(workflow),
            )
            await self._save_workflow(anime_doc_id, workflow)
            return

        # Add to queue
        queue = await self._get_queue()
        # Don't dupe
        if any(q.anime_doc_id == anime_doc_id for q in queue):
            return
        queue.append(QueueEntry(
            anime_doc_id=anime_doc_id,
            anime_title=anime_title,
            total_entries=len(workflow),
        ))
        await self._save_queue(queue)
        await self.refresh_queue()

        # Post the per-entry SUMMARY card FIRST (above the workflow card).
        # Even when every synopsis is empty (Telegram-source or no AniList
        # metadata), we still post the summary header so the layout stays
        # uniform — admins see the labels/episodes regardless.
        await self._post_summary_message(anime_doc_id, anime_title, entries)
        # Send the workflow message for this franchise
        await self._post_or_update_workflow(anime_doc_id, workflow)

        log.info("thumbcc.queued", anime=anime_doc_id, title=anime_title,
                 entries=len(workflow))

    async def ensure_channel(self) -> None:
        """Bring the thumbnail channel into a known-good state on every restart.

        On every restart, wipe all tracked messages and rebuild the intro
        and pinned queue from Redis state. Workflow messages are rebuilt
        from their stored state automatically."""
        if not self._active():
            return
        try:
            await self._wipe_and_rebuild()
        except Exception as exc:
            log.warning("thumbcc.ensure.failed", error=str(exc))

    async def _wipe_and_rebuild(self) -> None:
        """Delete every tracked message and rebuild intro + queue + workflows.

        When ``cfg.wipe_all_on_rebuild`` is True (default), also wipes
        messages posted by other users — admins manually drafting notes,
        anonymous-admin messages via ``sender_chat``, and untracked card
        echoes — so the channel lands on a true clean slate. Honours
        ``wipe_max_history`` for safety. Set the flag False to restore
        the legacy "delete only what's tracked in Redis" behaviour.
        """
        # Collect and delete all known messages, then clear tracking.
        known_ids = await self._tracked_ids()
        for mid in known_ids:
            try:
                await self._client.delete_messages(self.cfg.channel_id, mid)
            except Exception:
                pass

        # Full-channel wipe: clears anything newer than the intro even when
        # posted by an admin or another user. Pinned queue message is
        # preserved so the layout still has something pinned to land on.
        # ``getattr`` with defaults keeps the sweep opt-in for deployments
        # whose ``ThumbnailChannelConfig`` snapshot predates the flag.
        if getattr(self.cfg, "wipe_all_on_rebuild", True):
            from nekofetch.core.channel_safety import safe_full_channel_wipe
            intro_raw = await safe_redis_get(self._c.redis, _K_INTRO,
                                             label="thumbcc.intro.get")
            intro_id = int(intro_raw) if intro_raw else None
            pinned_ids: list[int] = []
            pinned_qid = await self._pinned_queue_id()
            if pinned_qid:
                pinned_ids.append(pinned_qid)
            # Acquire a userbot so ``safe_full_channel_wipe`` can iterate
            # history (bots cannot call messages.getHistory). Lazily build
            # the pool on the container so startup isn't gated by an
            # external login. Failure falls back to the (bot) client path
            # inside the helper with a clear ``channel_wipe.skipped_bot_client``
            # warning.
            userbot_client = await self._acquire_userbot()
            await safe_full_channel_wipe(
                self._client, self.cfg.channel_id,
                intro_id=intro_id,
                max_history=getattr(self.cfg, "wipe_max_history", 200),
                preserve_pinned_ids=pinned_ids,
                userbot_client=userbot_client,
            )

        # ── rebuild ─────────────────────────────────────────────────────────────
        # Post intro + dividers + duty board + pinned queue. This block
        # used to live inside this method body; a prior edit accidentally
        # merged it into ``_acquire_userbot`` as unreachable dead code
        # (after the ``return None`` line), so the channel would land on
        # an empty state on every restart. Moving it back here restores
        # true self-healing-on-rebuild behaviour.
        if self._c.redis:
            await safe_redis_delete(self._c.redis, _K_TRACKED,
                                    label="thumbcc.tracked.del")
            await safe_redis_delete(self._c.redis, _K_DUTY_BOARD,
                                    label="thumbcc.duty_board.del")
        intro_text = (
            "<b>The Canvas</b>\n\n"
            "<i>This channel manages custom thumbnail generation for published anime. "
            "Franchises needing banners appear in the pinned queue above.</i>"
        )
        if self.cfg.cover_image:
            try:
                intro_msg = await self._client.send_photo(
                    self.cfg.channel_id, self.cfg.cover_image,
                    caption=intro_text, parse_mode=ParseMode.HTML,
                )
            except Exception as exc:
                log.debug("thumbcc.cover.failed", error=str(exc))
                intro_msg = await self._send(intro_text)
        else:
            intro_msg = await self._send(intro_text)
        await self._add_tracked(intro_msg.id)
        if self._c.redis:
            await safe_redis_set(self._c.redis, _K_INTRO, str(intro_msg.id),
                                  label="thumbcc.intro.set")

        # Divider
        div_id = await self._post_divider()
        if div_id:
            await self._add_tracked(div_id)

        # Duty board — shows who is on duty for this channel.
        await self._post_duty_board()

        # Divider between duty board and pinned queue.
        div2_id = await self._post_divider()
        if div2_id:
            await self._add_tracked(div2_id)

        # Pinned queue message.
        text = S.queue_section([], self._ts())
        msg = await self._send(text)
        await self._set_pinned_queue_id(msg.id)
        await self._add_tracked(msg.id)
        await self._pin_silently(msg.id)
        log.info("thumbcc.rebuilt", channel_id=self.cfg.channel_id)

    async def _acquire_userbot(self):
        """Acquire a working user account from the lazily-built userbot pool.

        Returns ``None`` if no userbot is configured or pool acquisition
        fails — callers fall back to the (bot) admin_client and the
        safety helper emits the distinct ``channel_wipe.skipped_bot_client``
        warning so the operator can either provision a userbot or set
        ``wipe_all_on_rebuild = false`` in config.
        """
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
        except Exception as exc:
            log.debug("thumbcc.userbot.acquire.failed", error=str(exc))
            return None

    async def _pin_silently(self, message_id: int) -> None:
        """Pin a message and delete the service notice Telegram auto-posts."""
        for attempt in range(3):
            try:
                await self._client.pin_chat_message(
                    self.cfg.channel_id, message_id, disable_notification=True,
                )
                break
            except FloodWait as fw:
                log.warning("thumbcc.pin.flood_wait", wait=fw.value, attempt=attempt)
                await asyncio.sleep(fw.value + 1)
            except Exception:
                return
        for candidate in range(message_id + 1, message_id + 4):
            try:
                msg = await self._client.get_messages(self.cfg.channel_id, candidate)
                if msg and getattr(msg, "pinned_message", None) is not None:
                    await self._client.delete_messages(self.cfg.channel_id, candidate)
            except Exception:
                pass

    async def _post_duty_board(self) -> None:
        """Post or update the persistent duty board message in the channel."""
        if not self._c.redis:
            return
        from nekofetch.services.shift_service import ShiftService
        from nekofetch.ui.duty_board import duty_board

        shift = ShiftService(self._c)
        state = await shift.get_state("thumbcc")
        # Resolve missing worker name (can_act auto-assigns with empty name)
        if state.worker_id and not state.worker_name:
            try:
                tg = await self._client.get_users(state.worker_id)
                state.worker_name = " ".join(p for p in (tg.first_name, tg.last_name) if p) or tg.username or ""
            except Exception:
                pass
        text = duty_board(state)

        # Build inline buttons
        kb_rows: list[list[InlineKeyboardButton]] = []
        if state.worker_id is None:
            kb_rows.append([InlineKeyboardButton(
                "🛡️ Take Shift", callback_data=cb("shift", "take", "thumbcc"),
            )])
        if state.worker_id is not None:
            kb_rows.append([
                InlineKeyboardButton(
                    "🟡 Need Relief", callback_data=cb("shift", "relief", "thumbcc"),
                ),
                InlineKeyboardButton(
                    "🔵 Request Takeover", callback_data=cb("shift", "takeover", "thumbcc"),
                ),
            ])

        markup = InlineKeyboardMarkup(kb_rows) if kb_rows else None

        mid_raw = await safe_redis_get(self._c.redis, _K_DUTY_BOARD,
                                        label="thumbcc.duty_board.get")
        existing_id = int(mid_raw) if mid_raw else None

        if existing_id and await self._exists(existing_id):
            await self._edit(existing_id, text, reply_markup=markup)
        else:
            msg = await self._send(text, reply_markup=markup)
            await safe_redis_set(self._c.redis, _K_DUTY_BOARD, str(msg.id),
                                  label="thumbcc.duty_board.set")
            await self._add_tracked(msg.id)

    async def update_duty_board(self) -> None:
        """Refresh the duty board message (called when shift state changes)."""
        if not self._active():
            return
        await self._post_duty_board()


    async def _post_summary_message(self, anime_doc_id: str,
                                    title: str,
                                    entries: list[dict]) -> int | None:
        """Post a per-entry summary card ABOVE the workflow card.

        Builds one HTML block per entry showing label, format, episode count
        and (when present) the per-entry synopsis. The per-entry synopsis
        comes from ``bot_content.py`` which enriches each ``entries`` dict
        with the AniList ``walk_franchise_full`` synopsis. Without this card,
        admins click into the workflow card with zero context for the title
        they're picking assets for \u2014 which is what the user reported:
        "you did not send a request to the thumbnail generation channel".

        Returns the posted message id, or ``None`` on failure (the workflow
        is still posted so admins can still pick assets).
        """
        if not self._active():
            return None
        # Cap each synopsis so the message stays scannable; the workflow card
        # has a "view details" affordance for full reading later.
        _SYNOPSIS_CAP = 400
        lines: list[str] = [
            f"<b>{_html.escape(title)}</b>",
            "",
            "<i>Franchise summary \u2014 review each entry before picking assets.</i>",
            "",
        ]
        any_synopsis = False
        for i, e in enumerate(entries, start=1):
            label = _html.escape(e.get("label", f"Entry {i}"))
            fmt = _html.escape(e.get("format", "") or "")
            ep = e.get("episodes")
            head = f"<b>{i}. {label}</b>"
            tags: list[str] = []
            if fmt:
                tags.append(f"({fmt})")
            if ep:
                tags.append(f"{ep} ep")
            if tags:
                head += " \u2014 " + " \u00b7 ".join(tags)
            lines.append(head)
            summary = (e.get("summary") or "").strip()
            if summary:
                any_synopsis = True
                cut = (summary if len(summary) <= _SYNOPSIS_CAP
                       else summary[: _SYNOPSIS_CAP - 1].rstrip() + "\u2026")
                lines.append("")
                lines.append(_html.escape(cut).replace("\n", " "))
            lines.append("")
        if not any_synopsis:
            lines.append(
                "<i>No per-entry synopses available \u2014 admins can still pick assets directly below.</i>"
            )
            lines.append("")
        text = "\n".join(lines).rstrip()
        try:
            msg = await self._send(text)
            await self._add_tracked(msg.id)
            if self._c.redis:
                await safe_redis_set(
                    self._c.redis,
                    _K_SUMMARY_MSG.format(anime_doc_id=anime_doc_id),
                    str(msg.id),
                    label="thumbcc.summary.set",
                )
            return msg.id
        except Exception as exc:  # noqa: BLE001
            log.warning("thumbcc.summary.post_failed",
                        anime=anime_doc_id, error=str(exc))
            return None

    async def _get_workflow_message_id(self, anime_doc_id: str) -> int | None:
        raw = await safe_redis_get(self._c.redis,
                                    _K_WORKFLOW_MSG.format(anime_doc_id=anime_doc_id),
                                    label="thumbcc.workflow_msg.get")
        return int(raw) if raw else None

    async def _set_workflow_message_id(self, anime_doc_id: str, mid: int) -> None:
        if self._c.redis:
            await safe_redis_set(self._c.redis,
                                  _K_WORKFLOW_MSG.format(anime_doc_id=anime_doc_id),
                                  str(mid),
                                  label="thumbcc.workflow_msg.set")

    async def _post_or_update_workflow(self, anime_doc_id: str,
                                        workflow: list[WorkflowEntry]) -> None:
        """Post or edit the workflow message for a franchise."""
        # Find anime title from queue
        queue = await self._get_queue()
        title = next((q.anime_title for q in queue if q.anime_doc_id == anime_doc_id), "—")

        # Build entry data for UI
        entry_data = []
        current_index = next(
            (e.index for e in workflow if not e.is_complete()),
            None,
        )

        for e in workflow:
            entry_data.append({
                "index": e.index,
                "label": e.label,
                "format": e.format,
                "status": "done" if e.is_complete() else
                          ("generating" if e.status == "generating" else
                           "ready" if (e.logo_url and e.poster_url and e.bg_url)
                           and e.status != "done" else
                           e.status),
                "logo_url": e.logo_url,
                "poster_url": e.poster_url,
                "bg_url": e.bg_url,
                "thumbnail_url": e.thumbnail_url,
            })

        text = S.franchise_workflow(title, entry_data, ts=self._ts())

        # Build action buttons for the current active entry
        keyboard_rows = []
        for e in workflow:
            if e.is_complete() or e.status == "generating":
                continue
            is_active = current_index == e.index if current_index else (e.index == 1 and not any(w.is_complete() for w in workflow))
            if is_active:
                # Show action buttons for this entry
                row = [
                    InlineKeyboardButton(
                        f"🎨 Logo #{e.index}",
                        callback_data=cb("thumb", "pick_logo", anime_doc_id, str(e.index))
                    ),
                    InlineKeyboardButton(
                        f"📰 Poster #{e.index}",
                        callback_data=cb("thumb", "pick_poster", anime_doc_id, str(e.index))
                    ),
                ]
                keyboard_rows.append(row)
                row2 = [
                    InlineKeyboardButton(
                        f"🌄 BG #{e.index}",
                        callback_data=cb("thumb", "pick_bg", anime_doc_id, str(e.index))
                    ),
                ]
                if e.logo_url and e.poster_url and e.bg_url:
                    row2.append(InlineKeyboardButton(
                        f"🖼️ Generate #{e.index}",
                        callback_data=cb("thumb", "generate", anime_doc_id, str(e.index))
                    ))
                keyboard_rows.append(row2)

        # Refresh button + Skip Custom Thumbnails (always present — the orchestrator
        # waits on this so admin can unblock the pipeline without generating).
        keyboard_rows.append([
            InlineKeyboardButton(t(M.THUMB_REFRESH_BTN),
                                  callback_data=cb("thumb", "refresh", anime_doc_id))
        ])
        keyboard_rows.append([
            InlineKeyboardButton(
                "⏭ Skip Custom Thumbnails",
                callback_data=cb("thumb", "skip", anime_doc_id),
            ),
        ])

        markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
        mid = await self._get_workflow_message_id(anime_doc_id)

        if mid and await self._exists(mid):
            await self._edit(mid, text, reply_markup=markup)
        else:
            divider_id = await self._post_divider()
            if divider_id:
                await self._add_tracked(divider_id)
            msg = await self._send(text, reply_markup=markup)
            await self._set_workflow_message_id(anime_doc_id, msg.id)
            await self._add_tracked(msg.id)

    # ── asset selection ────────────────────────────────────────────────────

    async def get_tmdb_id(self, anime_doc_id: str) -> tuple[int | None, str | None]:
        """Resolve a franchise to its TMDB id. Returns (tmdb_id, media_type).

        Prefers the prefetched ``tmdb.json`` (the id + media_type resolved at
        acceptance) before a live TMDB search."""
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            blob = await load_cached(self._c, anime_doc_id, "tmdb",
                                     anime_doc_id=anime_doc_id)
            res = (blob or {}).get("result") or {}
            if res.get("id"):
                return res["id"], res.get("media_type") or "tv"
        except Exception as exc:  # noqa: BLE001 — cache miss → live
            log.debug("thumbcc.tmdb_id.cache_failed", error=str(exc))
        try:
            from nekofetch.core.parsing import clean_anilist_id
            query = clean_anilist_id(anime_doc_id)
            result = await self._c.tmdb.search(query)
            if result is not None:
                return result.id, result.media_type
        except Exception as exc:
            log.warning("thumbcc.tmdb_search.failed", anime=anime_doc_id, error=str(exc))
        return None, None

    async def create_telegraph_gallery(
        self,
        asset_type: str,
        anime_title: str,
        tmdb_id: int,
        media_type: str,
        assets: list[dict] | None = None,
    ) -> str | None:
        """Create a Telegraph gallery for an asset type. Returns the gallery URL.

        If ``assets`` is provided (pre-fetched from TMDB), it's used directly
        instead of making a second TMDB API call.
        """
        telegraph = self._telegraph_client()
        if not telegraph:
            log.warning("thumbcc.no_telegraph")
            return None

        if assets is None:
            try:
                if asset_type == "logo":
                    assets = await fetch_logos(self._c.tmdb, tmdb_id, media_type)
                elif asset_type == "poster":
                    assets = await fetch_posters_ranked(self._c.tmdb, tmdb_id, media_type)
                elif asset_type == "backdrop":
                    assets = await fetch_backdrops_ranked(self._c.tmdb, tmdb_id, media_type)
                else:
                    return None
            except Exception as exc:
                log.warning("thumbcc.fetch_assets.failed", type=asset_type, error=str(exc))
                return None

        if not assets:
            log.info("thumbcc.no_assets", type=asset_type, anime=anime_title)
            return None

        # Build Telegraph image entries with numbered captions
        type_label = {"logo": "Logo", "poster": "Poster", "backdrop": "Background"}.get(asset_type)
        images = []
        for i, asset in enumerate(assets, start=1):
            caption_parts = [f"{i}"]
            if asset.get("language") == "en":
                caption_parts.append("English")
            elif not asset.get("language"):
                caption_parts.append("Neutral")
            if asset_type == "logo":
                caption_parts.append(f"({asset.get('width', 0)}x{asset.get('height', 0)})")
            caption = " — ".join(caption_parts)
            images.append(ImageEntry(url=asset["url"], caption=caption))

        try:
            page = await telegraph.create_gallery(
                title=f"{anime_title} — {type_label}s",
                images=images,
                author_name="NekoFetch",
            )
            return page.url
        except TelegraphError as exc:
            log.warning("thumbcc.telegraph.failed", type=asset_type, error=str(exc))
            return None

    async def _fetch_assets_for_type(
        self, asset_type: str, tmdb_id: int, media_type: str,
        anime_doc_id: str | None = None,
    ) -> list[dict]:
        """Ranked TMDB assets for a type — prefetch cache first, live on a miss.

        The same three fetchers (logos/posters/backdrops) already ran at
        acceptance and are stored in ``tmdb.json``; reading them here avoids a
        duplicate TMDB round-trip every time the wizard opens a gallery."""
        if anime_doc_id:
            try:
                from nekofetch.services.metadata_prefetch import load_cached_tmdb_assets

                cached = await load_cached_tmdb_assets(
                    self._c, anime_doc_id, asset_type, anime_doc_id=anime_doc_id,
                    tmdb_id=tmdb_id)
                if cached is not None:
                    log.info("thumbcc.assets.cache_hit", type=asset_type,
                             anime=anime_doc_id, count=len(cached))
                    return cached
            except Exception as exc:  # noqa: BLE001 — cache miss → live below
                log.debug("thumbcc.assets.cache_failed", error=str(exc))
        try:
            if asset_type == "logo":
                return await fetch_logos(self._c.tmdb, tmdb_id, media_type)
            elif asset_type == "poster":
                return await fetch_posters_ranked(self._c.tmdb, tmdb_id, media_type)
            elif asset_type == "backdrop":
                return await fetch_backdrops_ranked(self._c.tmdb, tmdb_id, media_type)
        except Exception as exc:
            log.warning("thumbcc.fetch_assets.failed", type=asset_type, error=str(exc))
        return []

    async def handle_pick_asset(
        self,
        query,
        anime_doc_id: str,
        entry_index: str,
        asset_type: str,
    ) -> None:
        """Handle an admin clicking a Pick button for an asset type.

        Creates a Telegraph gallery and shows numbered inline buttons
        (1, 2, 3, ...) so the admin can pick an asset with one tap.
        """
        workflow = await self._get_workflow(anime_doc_id)
        if not workflow:
            await query.answer("Workflow expired — please refresh.", show_alert=True)
            return

        entry = next((e for e in workflow if e.index == int(entry_index)), None)
        if not entry:
            await query.answer("Entry not found.", show_alert=True)
            return

        # Resolve TMDB id if we don't have one
        tmdb_id = entry.tmdb_id
        media_type = entry.media_type
        if not tmdb_id:
            tmdb_id, media_type = await self.get_tmdb_id(anime_doc_id)
            if tmdb_id:
                entry.tmdb_id = tmdb_id
                entry.media_type = media_type
                await self._save_workflow(anime_doc_id, workflow)
            else:
                await query.answer("Could not find the title on TMDB.", show_alert=True)
                return

        queue = await self._get_queue()
        title = next((q.anime_title for q in queue if q.anime_doc_id == anime_doc_id), "—")

        queue_for_title = await self._get_queue()
        title_early = next((q.anime_title for q in queue_for_title
                            if q.anime_doc_id == anime_doc_id), "—")

        # Fetch assets to know how many we have
        assets = await self._fetch_assets_for_type(
            asset_type, tmdb_id, media_type, anime_doc_id=anime_doc_id)
        if not assets:
            # TMDB had nothing for this type — still let the admin upload their own.
            type_label = {"logo": "🎨 Logo", "poster": "📰 Poster",
                          "backdrop": "🌄 Background"}.get(asset_type, asset_type)
            entry.status = f"select_{asset_type}"
            await self._save_workflow(anime_doc_id, workflow)
            await query.message.reply(
                f"<b>{title_early}</b> — <i>{entry.label}</i>\n\n"
                f"No {type_label}s found on TMDB. Tap ⬆️ to send your own image:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "⬆️ Upload my own",
                    callback_data=cb("thumb", "upload", anime_doc_id,
                                     entry_index, asset_type),
                )]]),
                parse_mode=ParseMode.HTML,
            )
            await query.answer()
            return

        # Create Telegraph gallery (pass pre-fetched assets to avoid a second TMDB API call)
        gallery_url = await self.create_telegraph_gallery(
            asset_type, title, tmdb_id, media_type, assets=assets,
        )

        type_label = {"logo": "🎨 Logo", "poster": "📰 Poster", "backdrop": "🌄 Background"}.get(asset_type, asset_type)

        # Update entry status
        entry.status = f"select_{asset_type}"
        await self._save_workflow(anime_doc_id, workflow)

        # ── Build keyboard: Telegraph gallery button + numbered selection buttons ──
        keyboard_rows: list[list[InlineKeyboardButton]] = []

        # Telegraph gallery link (always present when gallery was created)
        if gallery_url:
            keyboard_rows.append([
                InlineKeyboardButton(f"📋 Open in Telegraph", url=gallery_url),
            ])

        # Numbered asset buttons (up to 3 per row)
        num_rows: list[InlineKeyboardButton] = []
        for i in range(1, len(assets) + 1):
            num_rows.append(InlineKeyboardButton(
                str(i),
                callback_data=cb("thumb", "select_num", anime_doc_id, entry_index, asset_type, str(i)),
            ))
            if len(num_rows) == 3:
                keyboard_rows.append(num_rows)
                num_rows = []
        if num_rows:
            keyboard_rows.append(num_rows)

        # Manual override: skip TMDB entirely and send your own image.
        keyboard_rows.append([InlineKeyboardButton(
            "⬆️ Upload my own",
            callback_data=cb("thumb", "upload", anime_doc_id, entry_index, asset_type),
        )])

        await query.message.reply(
            f"<b>{title}</b> — <i>{entry.label}</i>\n\n"
            f"{type_label}s  ·  {len(assets)} available\n"
            f"Browse the gallery, then tap the number of the one you want — "
            f"or tap ⬆️ to send your own image:",
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            parse_mode=ParseMode.HTML,
        )
        await query.answer()

    async def handle_select_num(
        self,
        query,
        anime_doc_id: str,
        entry_index: int,
        asset_type: str,
        number: int,
    ) -> None:
        """Handle an admin tapping a numbered asset button."""
        workflow = await self._get_workflow(anime_doc_id)
        if not workflow:
            await query.answer("Workflow expired.", show_alert=True)
            return

        entry = next((e for e in workflow if e.index == int(entry_index)), None)
        if not entry:
            await query.answer("Entry not found.", show_alert=True)
            return

        assets = await self._fetch_assets_for_type(
            asset_type, entry.tmdb_id, entry.media_type, anime_doc_id=anime_doc_id)
        if not assets:
            await query.answer("Could not load assets. Please try picking again.", show_alert=True)
            return

        if number < 1 or number > len(assets):
            await query.answer(f"Number must be between 1 and {len(assets)}.", show_alert=True)
            return

        selected_url = assets[number - 1]["url"]

        # Store the selection
        if asset_type == "logo":
            entry.logo_url = selected_url
        elif asset_type == "poster":
            entry.poster_url = selected_url
        elif asset_type == "backdrop":
            entry.bg_url = selected_url

        # Update status
        entry.status = "ready" if (entry.logo_url and entry.poster_url and entry.bg_url) else "pending"
        await self._save_workflow(anime_doc_id, workflow)

        # Update the workflow message
        await self._post_or_update_workflow(anime_doc_id, workflow)

        type_label = {"logo": "Logo", "poster": "Poster", "backdrop": "Background"}.get(asset_type)
        await query.message.edit_text(
            f"✅ <b>{type_label} #{number}</b> selected for <b>{entry.label}</b>.",
            parse_mode=ParseMode.HTML,
        )
        await query.answer(f"{type_label} #{number} selected!")

    # ── manual upload ───────────────────────────────────────────────────────

    async def handle_upload_arm(
        self, query, anime_doc_id: str, entry_index: str, asset_type: str,
    ) -> None:
        """Arm the channel so the next posted image becomes this asset.

        The workflow is single-worker (shift-gated), so one pending arm keyed on
        the channel is enough. :meth:`handle_uploaded_image` consumes it.
        """
        arm = {"anime_doc_id": anime_doc_id, "entry_index": str(entry_index),
               "asset_type": asset_type}
        await safe_redis_set(self._c.redis, _K_UPLOAD_WAIT, json.dumps(arm),
                             ex=_UPLOAD_WAIT_TTL, label="thumbcc.upload.arm")
        type_label = {"logo": "🎨 Logo", "poster": "📰 Poster",
                      "backdrop": "🌄 Background"}.get(asset_type, asset_type)
        await query.message.reply(
            f"⬆️ <b>Send your own {type_label}</b> now — post the image (photo or "
            f"image file) here in the next 15 minutes and it becomes this entry's "
            f"asset. Tapping another button cancels the upload.",
            parse_mode=ParseMode.HTML,
        )
        await query.answer("Waiting for your image…")

    async def handle_uploaded_image(self, message) -> bool:
        """If an upload is armed, store this posted image as the target asset.

        Returns True if the image was consumed (armed + stored), False if no
        upload was pending (the caller ignores the message). Never raises out.
        """
        raw = await safe_redis_get(self._c.redis, _K_UPLOAD_WAIT,
                                   label="thumbcc.upload.wait.get")
        if not raw:
            return False
        try:
            arm = json.loads(raw)
        except (ValueError, TypeError):
            await safe_redis_delete(self._c.redis, _K_UPLOAD_WAIT,
                                    label="thumbcc.upload.wait.bad")
            return False

        anime_doc_id = arm.get("anime_doc_id", "")
        entry_index = arm.get("entry_index", "1")
        asset_type = arm.get("asset_type", "logo")

        # A document must actually be an image — reject PDFs, archives, etc.
        doc = getattr(message, "document", None)
        if doc and not (getattr(doc, "mime_type", "") or "").startswith("image/"):
            await message.reply("⚠️ That's not an image. Send a photo or an image file.")
            return True  # consumed the arm's turn; admin can re-tap upload

        try:
            buf = await self._client.download_media(message, in_memory=True)
            file_bytes = buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            log.warning("thumbcc.upload.download_failed",
                        anime=anime_doc_id, error=str(exc))
            await message.reply("⚠️ Could not download that image. Try again.")
            return True

        try:
            stored = await self.store_upload(anime_doc_id, entry_index,
                                             asset_type, file_bytes)
        except Exception as exc:  # noqa: BLE001 — every host rejected it
            log.warning("thumbcc.upload.store_failed",
                        anime=anime_doc_id, error=str(exc))
            await message.reply("⚠️ Every image host rejected that upload. Try again.")
            return True

        await safe_redis_delete(self._c.redis, _K_UPLOAD_WAIT,
                                label="thumbcc.upload.wait.done")
        if not stored:
            await message.reply("⚠️ Workflow expired — refresh and pick again.")
            return True

        type_label = {"logo": "Logo", "poster": "Poster",
                      "backdrop": "Background"}.get(asset_type, asset_type)
        await message.reply(f"✅ Your {type_label} was uploaded and selected.")
        return True

    async def store_upload(
        self, anime_doc_id: str, entry_index: str, asset_type: str,
        file_bytes: bytes,
    ) -> bool:
        """Mirror uploaded bytes and store the URL as the entry's asset.

        Routes through :func:`image_backup.backup_bytes` (catbox → telegraph →
        ImgBB) so an admin upload gets the same durable mirror as a numbered
        pick, then updates the workflow message. Returns False if the workflow
        vanished; raises if every host rejected the bytes.
        """
        from kurosoden.shared.image_backup import backup_bytes

        backup = await backup_bytes(self._c, file_bytes, mime="image/jpeg")
        url = backup.primary
        if not url:
            raise RuntimeError("every image host rejected the upload")

        workflow = await self._get_workflow(anime_doc_id)
        if not workflow:
            return False
        entry = next((e for e in workflow if e.index == int(entry_index)), None)
        if not entry:
            return False

        if asset_type == "logo":
            entry.logo_url = url
        elif asset_type == "poster":
            entry.poster_url = url
        elif asset_type == "backdrop":
            entry.bg_url = url

        entry.status = ("ready" if (entry.logo_url and entry.poster_url
                                    and entry.bg_url) else "pending")
        await self._save_workflow(anime_doc_id, workflow)
        await self._post_or_update_workflow(anime_doc_id, workflow)
        return True

    # ── thumbnail generation ───────────────────────────────────────────────

    async def handle_generate(self, query, anime_doc_id: str, entry_index: int) -> None:
        """Generate a thumbnail for the given entry using Playwright."""
        workflow = await self._get_workflow(anime_doc_id)
        if not workflow:
            await query.answer("Workflow expired.", show_alert=True)
            return

        entry = next((e for e in workflow if e.index == int(entry_index)), None)
        if not entry:
            await query.answer("Entry not found.", show_alert=True)
            return

        if not (entry.logo_url and entry.poster_url and entry.bg_url):
            await query.answer("Not all assets selected yet.", show_alert=True)
            return

        renderer = self._render()
        if not renderer:
            await query.answer("Thumbnail renderer unavailable.", show_alert=True)
            return

        await query.answer("Generating thumbnail...")

        # Mark as generating
        entry.status = "generating"
        await self._save_workflow(anime_doc_id, workflow)
        await self._post_or_update_workflow(anime_doc_id, workflow)

        # Find anime title & metadata
        queue = await self._get_queue()
        title = next((q.anime_title for q in queue if q.anime_doc_id == anime_doc_id), "—")

        # ── Enrich from TMDB (display facts) + AniList (romaji/native/score/
        # studio). The user-picked logo/poster/bg always override the providers'
        # art; everything else (meta line, rating, studio, flag, genres) is
        # sourced automatically so the card is fully populated. Both lookups are
        # best-effort — a provider miss degrades one field, never the render.
        # Shared with Senku's distribution wizard via ``gather_thumbnail_fields``. ──
        from nekofetch.services.thumbnail_service import gather_thumbnail_fields
        # Distribution entry cards use the per-title AniList synopsis (the base
        # entry also gets a TMDB-synopsis variant below, for the main post).
        fields = await gather_thumbnail_fields(
            self._c, title, anime_doc_id, prefer_anilist_synopsis=True)
        source_fields = {
            **fields,
            "title": title,
            "logo_url": entry.logo_url,
            "poster_url": entry.poster_url,
            "bg_url": entry.bg_url,
            "entry_label": entry.label,
            "anilist_id": entry.anilist_id,
        }

        # Build the thumbnail
        thumbnail_path = None
        try:
            thumbnail_path = await renderer.render_thumbnail(
                title=title,
                logo_url=entry.logo_url,
                poster_url=entry.poster_url,
                bg_url=entry.bg_url,
                **fields,
            )
            from nekofetch.services.thumbnail_service import persist_thumbnail_source
            await persist_thumbnail_source(
                self._c, anime_doc_id, entry.anilist_id, source_fields,
                image_path=thumbnail_path,
            )
        except Exception as exc:
            log.warning("thumbcc.render.failed", anime=anime_doc_id, error=str(exc))
            await query.message.reply(f"⚠️ Thumbnail generation failed: {exc}")
            entry.status = "ready"
            await self._save_workflow(anime_doc_id, workflow)
            await self._post_or_update_workflow(anime_doc_id, workflow)
            return

        if not thumbnail_path:
            await query.message.reply("⚠️ Thumbnail generation returned no image.")
            entry.status = "ready"
            await self._save_workflow(anime_doc_id, workflow)
            await self._post_or_update_workflow(anime_doc_id, workflow)
            return

        # Upload the thumbnail to the channel for reference. Keep the message
        # coordinates in the durable source row so /edit_thumbnail can replace
        # this exact Telegram message instead of creating an orphaned card.
        source_fields["thumbnail_chat_id"] = self.cfg.channel_id
        # Telegram's photo endpoint is unreliable with the rendered .webp (the
        # sticker format), so the channel reference card is sent as a JPEG — the
        # stored artifact stays the original WebP.
        preview = (await webp_to_jpeg_async(thumbnail_path)) or Path(thumbnail_path)
        try:
            photo_msg = await self._client.send_photo(
                self.cfg.channel_id, str(preview),
                caption=f"<b>{title}</b> — <i>{entry.label}</i>\n\n"
                        f"<b>Logo:</b> {entry.logo_url[:60]}...\n"
                        f"<b>Poster:</b> {entry.poster_url[:60]}...\n"
                        f"<b>Background:</b> {entry.bg_url[:60]}...",
                parse_mode=ParseMode.HTML,
            )
            entry.thumbnail_url = f"thumb://{thumbnail_path}"
            source_fields["thumbnail_message_id"] = photo_msg.id
        except Exception as exc:
            log.warning("thumbcc.upload.failed", error=str(exc))
            entry.thumbnail_url = f"file://{thumbnail_path}"

        # The first persistence happens before upload so a render survives a
        # Telegram hiccup. Persist once more with the message coordinates when
        # the upload succeeds; retries converge on the same durable row.
        try:
            from nekofetch.services.thumbnail_service import persist_thumbnail_source
            await persist_thumbnail_source(
                self._c, anime_doc_id, entry.anilist_id, source_fields,
                image_path=thumbnail_path,
            )
        except Exception as exc:  # noqa: BLE001 - DB is authoritative but best effort here
            log.warning("thumbcc.source.message_ref_failed", error=str(exc))

        # ── Base entry: also render a MAIN-CHANNEL variant with the TMDB
        # synopsis (the main post summarizes the whole franchise; distribution
        # cards describe each season via AniList). "Base" = the lowest-index TV
        # entry with a real anilist_id — exactly what get_first_season_thumbnail
        # picks. Stored as a real http(s) URL (NOT thumb://, which nothing
        # resolves) so the main post's send_photo can consume it. Best-effort:
        # any failure just leaves the main post on its existing fallback.
        try:
            tv_indexes = [w.index for w in workflow if w.anilist_id not in (None, -1)]
            if tv_indexes and int(entry_index) == min(tv_indexes):
                main_fields = await gather_thumbnail_fields(
                    self._c, title, anime_doc_id, prefer_anilist_synopsis=False)
                main_path = await renderer.render_thumbnail(
                    title=title, logo_url=entry.logo_url,
                    poster_url=entry.poster_url, bg_url=entry.bg_url,
                    variant_key="main", **main_fields,
                )
                if main_path:
                    from kurosoden.shared.image_backup import backup_bytes
                    mirrored = await backup_bytes(
                        self._c, Path(main_path).read_bytes(), mime="image/webp")
                    entry.main_thumbnail_url = mirrored.primary or f"file://{main_path}"
                    log.info("thumbcc.main_variant.rendered", anime=anime_doc_id,
                             url=entry.main_thumbnail_url)
        except Exception as exc:  # noqa: BLE001 — main post keeps its fallback
            log.warning("thumbcc.main_variant.failed", anime=anime_doc_id,
                        error=str(exc))

        # Mark as done
        entry.status = "done"
        await self._save_workflow(anime_doc_id, workflow)

        # Update queue progress
        queue = await self._get_queue()
        for q in queue:
            if q.anime_doc_id == anime_doc_id:
                q.completed_entries = sum(1 for w in workflow if w.is_complete())
                break
        await self._save_queue(queue)

        # Update messages
        await self._post_or_update_workflow(anime_doc_id, workflow)
        await self.refresh_queue()

        # Notify the orchestrator so wait_for_thumbnails can short-circuit on
        # the next poll cycle rather than waiting out its full timeout.
        try:
            from nekofetch.services.thumbnail_orchestrator_service import (
                ThumbnailOrchestratorService,
            )
            await ThumbnailOrchestratorService(self._c).emit_completion(
                anime_doc_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("thumbcc.completion_emit.failed", error=str(exc))

        log.info("thumbcc.thumbnail_generated", anime=anime_doc_id,
                 entry=entry_index, path=str(thumbnail_path))

    # ── callback handler ───────────────────────────────────────────────────

    async def handle_callback(self, query) -> bool:
        """Route a callback query to the appropriate handler.

        Returns True if the callback was handled by this service.
        """
        from nekofetch.ui.components import parse_cb

        action, args = parse_cb(query.data)
        if action != "thumb":
            return False

        sub_action = args[0] if args else ""
        # Any button other than arming an upload cancels a pending upload wait,
        # matching the "tapping another button cancels" promise in the prompt.
        if sub_action != "upload":
            await safe_redis_delete(self._c.redis, _K_UPLOAD_WAIT,
                                    label="thumbcc.upload.wait.cancel")
        if sub_action == "open":
            anime_doc_id = args[1] if len(args) > 1 else ""
            workflow = await self._get_workflow(anime_doc_id)
            if workflow:
                await self._post_or_update_workflow(anime_doc_id, workflow)
            await query.answer()
            return True

        if sub_action == "refresh":
            anime_doc_id = args[1] if len(args) > 1 else ""
            workflow = await self._get_workflow(anime_doc_id)
            if workflow:
                await self._post_or_update_workflow(anime_doc_id, workflow)
            await query.answer("Refreshed!")
            return True

        if sub_action in ("pick_logo", "pick_poster", "pick_bg"):
            asset_map = {"pick_logo": "logo", "pick_poster": "poster", "pick_bg": "backdrop"}
            asset_type = asset_map[sub_action]
            anime_doc_id = args[1] if len(args) > 1 else ""
            entry_index = args[2] if len(args) > 2 else "1"
            await self.handle_pick_asset(query, anime_doc_id, entry_index, asset_type)
            return True

        if sub_action == "select_num":
            anime_doc_id = args[1] if len(args) > 1 else ""
            entry_index = args[2] if len(args) > 2 else "1"
            asset_type = args[3] if len(args) > 3 else "logo"
            number = int(args[4]) if len(args) > 4 else 0
            await self.handle_select_num(query, anime_doc_id, entry_index, asset_type, number)
            return True

        if sub_action == "upload":
            anime_doc_id = args[1] if len(args) > 1 else ""
            entry_index = args[2] if len(args) > 2 else "1"
            asset_type = args[3] if len(args) > 3 else "logo"
            await self.handle_upload_arm(query, anime_doc_id, entry_index, asset_type)
            return True

        if sub_action == "generate":
            anime_doc_id = args[1] if len(args) > 1 else ""
            entry_index = args[2] if len(args) > 2 else "1"
            await self.handle_generate(query, anime_doc_id, entry_index)
            return True

        if sub_action == "skip":
            # Admin chose to bypass the thumbnail generation step. Stamps a
            # redis flag the orchestrator polls — :meth:`ThumbnailOrchestrator
            # .wait_for_thumbnails` returns False immediately on the next cycle
            # and downstream cards fall back to AniList posters.
            anime_doc_id = args[1] if len(args) > 1 else ""
            try:
                from nekofetch.services.thumbnail_orchestrator_service import (
                    ThumbnailOrchestratorService,
                )
                await ThumbnailOrchestratorService(self._c).mark_admin_skipped(
                    anime_doc_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("thumbcc.skip.failed", error=str(exc))
            await query.answer(
                "Skipped — pipeline will use AniList posters.",
                show_alert=True,
            )
            # Edit the workflow card so admins don't keep clicking it.
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return True

        return False
