"""Database (storage) channel service.

Content lives in a single Telegram channel as ordered packs:

    header text  ->  file 1, 2, 3 ... N  ->  end sticker

One pack per (anime, season, resolution, language). NekoFetch records each pack's message
range so a "season pack" is a slice of the channel it can copy to a user on demand.

Three responsibilities:
  • index_pack   — assisted ingestion of content you already posted to the channel
  • upload_pack  — automated ingestion: post header, upload files in order, post sticker
  • deliver      — copy a pack's messages to a user (protect / temp / auto-delete aware)

All operations use Levi (the downloader bot, ``container.pipeline_manager.levi``),
which must be an administrator of the database channel. Falls back to
``container.admin_client`` when the pipeline isn't wired.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pyrogram.enums import ParseMode
from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.exceptions import FeatureDisabled
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import AudioType
from nekofetch.infrastructure.database.postgres.models import StoragePack
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.services.branding_service import BrandingService
from nekofetch.ui import templates

log = get_logger(__name__)

_LANG_LABELS = {
    AudioType.SUBBED: "Sub",
    AudioType.DUBBED: "Dub",
    AudioType.DUAL_AUDIO: "Dual",
    AudioType.MULTI: "Multi",
}


@dataclass(slots=True)
class PackKey:
    anime_doc_id: str
    season: int | None
    resolution: str
    audio: AudioType
    season_part: int | None = None
    entry_id: int | None = None


class StorageChannelService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self.cfg = container.config.storage_channel

    @property
    def _client(self):
        # Levi (downloader bot) owns the database channel and must be an admin
        # there. Fall back to the global admin_client when the pipeline isn't
        # wired (e.g. tests, or NekoFetch's single-bot BotManager).
        pm = getattr(self._c, "pipeline_manager", None)
        client = (getattr(pm, "levi", None) if pm else None) or getattr(self._c, "admin_client", None)
        if not self.cfg.enabled or self.cfg.channel_id == 0 or client is None:
            raise FeatureDisabled("storage_channel")
        return client

    def header_text(self, *, title: str, season: int | None, resolution: str,
                    audio: AudioType, episode_from: int | None = None,
                    episode_to: int | None = None,
                    content_type: str = "Season",
                    season_part: int | None = None,
                    alt_titles: list[str] | None = None,
                    audio_langs: list[str] | None = None) -> str:
        """Build the caption posted before a storage pack's files.

        Two bold lines (operator spec)::

            ➠ TAKOPI'S ORIGINAL SIN : SEASON 1
            ➠ 480p [DUAL ∽ ENG + JPN]

        The title line is shortened to Telegram's ~38-char single-line budget by
        :func:`~nekofetch.services.bot_naming.build_pack_caption` — dropping
        ``SEASON 1`` → ``S1``, then swapping the title for its shortest synonym,
        then an acronym, in that order. ``content_type`` ("Season", "OVA", "ONA",
        "Movie", "Special") selects the season label; ``alt_titles`` are the
        AniList English/native/synonym strings the shortener may fall back to.

        Setting ``storage_channel.header_template`` to a non-empty *legacy*
        template restores the old single-line ``{title} — …`` rendering, so the
        new caption is opt-out via config.
        """
        from nekofetch.services.bot_naming import build_pack_caption

        # Legacy escape hatch: a template beginning with "{title}" (the old
        # inline form) means the operator explicitly wants the flat header. The
        # new default (empty / "{caption}") uses the two-line builder.
        tmpl = (self.cfg.header_template or "").strip()
        ct_low = (content_type or "").lower()
        if ct_low == "movie" and self.cfg.movie_header_template:
            tmpl = self.cfg.movie_header_template
        elif ct_low in ("ova", "ona", "special") and self.cfg.special_header_template:
            tmpl = self.cfg.special_header_template

        use_legacy = bool(tmpl) and "{caption}" not in tmpl and "{title}" in tmpl
        if not use_legacy:
            return build_pack_caption(
                title, season=season, season_part=season_part,
                resolution=resolution, audio=audio, content_type=content_type,
                alt_titles=alt_titles, audio_langs=audio_langs,
            )

        branding = BrandingService(self._c)
        return templates.render(
            tmpl,
            title=title,
            season=(season if season is not None else "—"),
            resolution=resolution,
            language=_LANG_LABELS.get(audio, audio.value),
            episode_from=episode_from or "",
            episode_to=episode_to or "",
            content_type=content_type,
            group=branding.group,
        )

    # ── ingestion: assisted indexing ──
    async def index_pack(
        self,
        key: PackKey,
        *,
        title: str,
        start_message_id: int,
        end_message_id: int,
        channel_id: int | None = None,
    ) -> StoragePack:
        """Record a pack from content already in the channel.

        Enumerates messages in ``[start_message_id, end_message_id]``, keeps media as the
        ordered file list, and treats a sticker as the end marker.
        """
        client = self._client
        channel_id = channel_id or self.cfg.channel_id

        file_ids: list[int] = []
        header_id: int | None = None
        header_caption: str | None = None
        for mid in range(start_message_id, end_message_id + 1):
            try:
                msg = await client.get_messages(channel_id, mid)
            except Exception:  # noqa: BLE001 - deleted/missing id in range
                continue
            if msg is None or getattr(msg, "empty", False):
                continue
            if msg.document or msg.video or msg.audio:
                file_ids.append(mid)
            elif (getattr(msg, "text", None) or getattr(msg, "caption", None)) and header_id is None and not file_ids:
                header_id = mid
                header_caption = getattr(msg, "text", None) or getattr(msg, "caption", None)
            # stickers/other are treated as markers and skipped

        return await self._persist(
            key, title=title, channel_id=channel_id,
            header_message_id=header_id,
            start_message_id=file_ids[0] if file_ids else start_message_id,
            end_message_id=end_message_id,
            file_message_ids=file_ids, caption=header_caption,

            ingest_method="indexed",
        )

    # ── ingestion: automated upload ──
    async def upload_pack(
        self,
        key: PackKey,
        *,
        title: str,
        file_paths: list[Path],
        episode_from: int | None = None,
        episode_to: int | None = None,
        content_type: str = "Season",
        thumb: Path | None = None,
        alt_titles: list[str] | None = None,
        on_progress=None,
        file_meta: list[dict] | None = None,
        audio_langs: list[str] | None = None,
    ) -> StoragePack:
        """Post header, upload files in order, post the end sticker; record the range.

        ``content_type`` controls the ``{content_type}`` template variable in the
        header ("Season", "OVA", "ONA", "Movie", "Special").
        ``thumb`` (when present) is the request's poster, attached to every document
        so the files show a proper cover in Telegram instead of a blank icon.
        ``alt_titles`` are AniList synonym/native strings the caption builder may
        fall back to when the full title overflows the 38-char line budget.
        ``on_progress(done, total, meta)`` (when present) receives live upload byte
        counts for the CURRENT FILE (not the pack), plus a ``meta`` dict tagging the
        file's episode/resolution/audio + index — so ACTIVE TASKS renders a per-file
        upload bar exactly like the per-episode download card. ``file_meta`` is a
        parallel list (one dict per ``file_paths`` entry) supplying that identity."""
        client = self._client
        channel_id = self.cfg.channel_id
        thumb_arg = str(thumb) if thumb and thumb.exists() else None

        header_caption = self.header_text(
            title=title, season=key.season, resolution=key.resolution,
            audio=key.audio, episode_from=episode_from, episode_to=episode_to,
            content_type=content_type, season_part=key.season_part,
            alt_titles=alt_titles, audio_langs=audio_langs,
        )
        header = await client.send_message(channel_id, header_caption)
        n_files = len(file_paths)
        file_ids: list[int] = []
        for idx, path in enumerate(file_paths):
            # Per-file identity for the progress card (episode/resolution/audio +
            # "file i of n"), so the upload walks file-by-file like the download.
            meta = dict((file_meta or [{}] * n_files)[idx] or {})
            meta.setdefault("file_index", idx + 1)
            meta.setdefault("file_total", n_files)
            meta.setdefault("season", key.season)
            meta.setdefault("season_part", key.season_part)
            prog_cb = None
            if on_progress is not None:
                async def prog_cb(current, total, _meta=meta):  # noqa: ANN001
                    # Report THIS file's own bytes (resets to 0 per file) — the
                    # card shows the current episode's transfer, not a pack sum.
                    await on_progress(current, total, _meta)

            sent = await client.send_document(
                channel_id, str(path), thumb=thumb_arg, progress=prog_cb,
            )
            file_ids.append(sent.id)

        end_id = file_ids[-1] if file_ids else header.id
        if self.cfg.end_sticker_id:
            sticker = await client.send_sticker(channel_id, self.cfg.end_sticker_id)
            end_id = sticker.id

        return await self._persist(
            key, title=title, channel_id=channel_id,
            header_message_id=header.id,
            start_message_id=file_ids[0] if file_ids else header.id,
            end_message_id=end_id,
            file_message_ids=file_ids,
            ingest_method="uploaded",
            episode_from=episode_from, episode_to=episode_to, caption=header_caption,
            audio_langs=audio_langs,
        )

    async def _persist(self, key: PackKey, **fields) -> StoragePack:
        async with session_scope(self._c.pg_sessionmaker) as session:
            existing = (
                await session.execute(
                    select(StoragePack).where(
                        StoragePack.anime_doc_id == key.anime_doc_id,
                        StoragePack.season == key.season,
                        StoragePack.season_part == key.season_part,
                        StoragePack.resolution == key.resolution,
                        StoragePack.audio == key.audio,
                        StoragePack.entry_id == key.entry_id,
                    )
                )
            ).scalar_one_or_none()
            file_ids = fields.get("file_message_ids") or []
            data = dict(
                anime_doc_id=key.anime_doc_id, anime_title=fields["title"],
                season=key.season, season_part=key.season_part,
                resolution=key.resolution, audio=key.audio,
                channel_id=fields["channel_id"],
                header_message_id=fields.get("header_message_id"),
                start_message_id=fields["start_message_id"],
                end_message_id=fields["end_message_id"],
                file_message_ids=file_ids, file_count=len(file_ids),
                caption=fields.get("caption") or fields.get("header_caption"),
                episode_from=fields.get("episode_from"), episode_to=fields.get("episode_to"),
                entry_id=key.entry_id,
                ingest_method=fields.get("ingest_method"),
                audio_langs=fields.get("audio_langs"),
            )
            if existing is None:
                pack = StoragePack(**data)
                session.add(pack)
            else:
                existing_ids = list(existing.file_message_ids or [])
                ef, et = existing.episode_from, existing.episode_to
                nf, nt = fields.get("episode_from"), fields.get("episode_to")
                # Episode-keyed merge is only safe when BOTH sides carry a clean
                # contiguous episode range (file[i] == episode base+i). Uploaded
                # packs do; indexed packs / movies don't (episode_from is None),
                # so those fall back to the historical append.
                can_merge_by_ep = (
                    ef is not None and et is not None
                    and nf is not None and nt is not None
                    and len(existing_ids) == (et - ef + 1)
                    and len(file_ids) == (nt - nf + 1)
                )
                if can_merge_by_ep:
                    # Map episode number -> message id; the new upload WINS on any
                    # overlap, so a reprocess overwrites the same episodes in place
                    # instead of stacking a second copy. Non-overlapping ranges
                    # (disk-space chunking, eps 1-50 then 51-100) still concatenate.
                    by_ep = {ef + i: mid for i, mid in enumerate(existing_ids)}
                    by_ep.update({nf + i: mid for i, mid in enumerate(file_ids)})
                    lo, hi = min(ef, nf), max(et, nt)
                    merged = [by_ep[e] for e in range(lo, hi + 1) if e in by_ep]
                    existing.episode_from = lo
                    existing.episode_to = hi
                else:
                    merged = existing_ids + file_ids
                    existing.episode_to = fields.get("episode_to")
                existing.file_message_ids = merged
                existing.file_count = len(merged)
                existing.end_message_id = fields["end_message_id"]
                if fields.get("caption") is not None or fields.get("header_caption") is not None:
                    value = fields.get("caption") or fields.get("header_caption")
                    existing.caption = value
                # Keep the original header/start from the first upload.
                existing.ingest_method = fields.get("ingest_method")
                # Refresh real languages when this upload carries them; never
                # clobber a previously-probed value with a null (an untagged
                # reprocess must not erase good languages).
                if fields.get("audio_langs"):
                    existing.audio_langs = fields.get("audio_langs")
                pack = existing
            await session.flush()
            session.expunge(pack)
            log.info("storage.pack.persisted", anime=key.anime_doc_id, season=key.season,
                     res=key.resolution, files=pack.file_count,
                     method=fields.get("ingest_method"))
            return pack

    # ── lookup & delivery ──
    async def update_header_caption(self, pack_id: int, caption: str) -> StoragePack | None:
        """Persist an entry caption across sibling packs and edit live headers.

        A storage entry can have several resolution/audio packs, but the operator
        edits one logical header. Keep those sibling rows synchronized and update
        every live header. Indexed headers may be media messages (caption rather
        than text), so fall back to ``edit_message_caption`` when Telegram rejects
        the text edit.
        """
        clean = (caption or "").strip()
        if not clean:
            raise ValueError("caption must not be empty")

        headers: list[tuple[int, int, int]] = []
        async with session_scope(self._c.pg_sessionmaker) as session:
            selected = await session.get(StoragePack, pack_id)
            if selected is None or not selected.enabled:
                return None
            siblings = list((await session.execute(
                select(StoragePack).where(
                    StoragePack.anime_doc_id == selected.anime_doc_id,
                    StoragePack.season == selected.season,
                    StoragePack.season_part == selected.season_part,
                    StoragePack.entry_id == selected.entry_id,
                    StoragePack.enabled.is_(True),
                )
            )).scalars().all())
            for pack in siblings:
                pack.caption = clean
                if pack.header_message_id:
                    headers.append((pack.channel_id, pack.header_message_id, pack.id))
            await session.flush()
            session.expunge_all()

        if headers:
            client = self._client
            for channel_id, message_id, sibling_id in headers:
                try:
                    await client.edit_message_text(
                        channel_id, message_id, clean,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as text_exc:  # noqa: BLE001 - media headers use captions
                    try:
                        await client.edit_message_caption(
                            channel_id, message_id, clean,
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as caption_exc:  # noqa: BLE001 - DB remains authoritative
                        log.warning(
                            "storage.header_caption.telegram_edit_failed",
                            pack=sibling_id, text_error=str(text_exc),
                            caption_error=str(caption_exc),
                        )
        # Return the selected row's detached, updated representation. It is
        # deliberately reconstructed from the persisted values rather than
        # relying on a session-expunged ORM instance after sibling updates.
        selected.caption = clean
        await self._sync_distribution_backup(selected, clean)
        return selected

    async def _sync_distribution_backup(self, selected: StoragePack, caption: str) -> None:
        """Update any durable distribution snapshot for this logical entry."""
        from nekofetch.infrastructure.database.postgres.models import ChannelContentBackup

        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (await session.execute(
                select(ChannelContentBackup).where(
                    ChannelContentBackup.scope == "distribution",
                    ChannelContentBackup.channel_key == selected.anime_doc_id,
                )
            )).scalar_one_or_none()
            if row is None or not row.cards:
                return
            changed = False
            for card in row.cards:
                if card.get("kind") not in ("season_card", "movie_card"):
                    continue
                same_entry = (
                    selected.entry_id is not None
                    and card.get("anilist_id") == selected.entry_id
                )
                same_tv_slot = (
                    card.get("season") == selected.season
                    and card.get("season_part") == selected.season_part
                )
                if same_entry or same_tv_slot:
                    card["caption"] = caption
                    changed = True
            if changed:
                row.cards = list(row.cards)

    async def list_packs(self, *, limit: int = 30) -> list[StoragePack]:
        """Return enabled packs for the Levi caption editor."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            packs = list((await session.execute(
                select(StoragePack)
                .where(StoragePack.enabled.is_(True))
                .order_by(StoragePack.anime_title, StoragePack.season,
                         StoragePack.season_part, StoragePack.resolution)
                .limit(limit)
            )).scalars().all())
            for pack in packs:
                session.expunge(pack)
            return packs

    async def find_pack(self, key: PackKey) -> StoragePack | None:
        async with session_scope(self._c.pg_sessionmaker) as session:
            pack = (
                await session.execute(
                    select(StoragePack).where(
                        StoragePack.anime_doc_id == key.anime_doc_id,
                        StoragePack.season == key.season,
                        StoragePack.season_part == key.season_part,
                        StoragePack.resolution == key.resolution,
                        StoragePack.audio == key.audio,
                        StoragePack.entry_id == key.entry_id,
                        StoragePack.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if pack is not None:
                session.expunge(pack)
            return pack

    async def deliver(self, pack: StoragePack, to_chat_id: int) -> list[int]:
        """Copy a pack's messages to ``to_chat_id``. Returns sent message ids.

        Honors protect_content and the header/sticker inclusion settings. The caller is
        responsible for temporary-link gating and scheduling auto-delete of the returned
        message ids.
        """
        client = self._client
        protect = self._c.config.distribution.protect_content
        sent_ids: list[int] = []

        ids: list[int] = []
        if self.cfg.include_header_in_delivery and pack.header_message_id:
            ids.append(pack.header_message_id)
        ids.extend(pack.file_message_ids or list(range(pack.start_message_id, pack.end_message_id + 1)))
        if self.cfg.include_sticker_in_delivery:
            ids.append(pack.end_message_id)

        for mid in ids:
            try:
                copied = await client.copy_message(
                    chat_id=to_chat_id,
                    from_chat_id=pack.channel_id,
                    message_id=mid,
                    protect_content=protect,
                )
                sent_ids.append(copied.id)
            except Exception as exc:  # noqa: BLE001 - skip individual failures
                log.warning("storage.deliver.skip", message_id=mid, error=str(exc))
        log.info("storage.delivered", pack=pack.id, to=to_chat_id, count=len(sent_ids))
        return sent_ids

    @staticmethod
    def key_from(anime_doc_id: str, season: int | None, resolution: str,
                 audio: AudioType, *, season_part: int | None = None,
                 entry_id: int | None = None) -> PackKey:
        return PackKey(anime_doc_id, season, resolution, audio,
                       season_part=season_part, entry_id=entry_id)
