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
                    alt_titles: list[str] | None = None) -> str:
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
                alt_titles=alt_titles,
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
        for mid in range(start_message_id, end_message_id + 1):
            try:
                msg = await client.get_messages(channel_id, mid)
            except Exception:  # noqa: BLE001 - deleted/missing id in range
                continue
            if msg is None or getattr(msg, "empty", False):
                continue
            if msg.document or msg.video or msg.audio:
                file_ids.append(mid)
            elif msg.text and header_id is None and not file_ids:
                header_id = mid
            # stickers/other are treated as markers and skipped

        return await self._persist(
            key, title=title, channel_id=channel_id,
            header_message_id=header_id,
            start_message_id=file_ids[0] if file_ids else start_message_id,
            end_message_id=end_message_id,
            file_message_ids=file_ids,
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
    ) -> StoragePack:
        """Post header, upload files in order, post the end sticker; record the range.

        ``content_type`` controls the ``{content_type}`` template variable in the
        header ("Season", "OVA", "ONA", "Movie", "Special").
        ``thumb`` (when present) is the request's poster, attached to every document
        so the files show a proper cover in Telegram instead of a blank icon.
        ``alt_titles`` are AniList synonym/native strings the caption builder may
        fall back to when the full title overflows the 38-char line budget.
        ``on_progress(done, total)`` (when present) receives live upload byte counts
        for the whole pack, so ACTIVE TASKS can render an upload bar + speed."""
        client = self._client
        channel_id = self.cfg.channel_id
        thumb_arg = str(thumb) if thumb and thumb.exists() else None

        header = await client.send_message(
            channel_id,
            self.header_text(title=title, season=key.season, resolution=key.resolution,
                             audio=key.audio, episode_from=episode_from, episode_to=episode_to,
                             content_type=content_type, season_part=key.season_part,
                             alt_titles=alt_titles),
        )
        # Upload byte accounting across the whole pack so the progress bar reflects
        # the pack, not each individual file resetting to 0.
        sizes = [p.stat().st_size if p.exists() else 0 for p in file_paths]
        pack_total = sum(sizes)
        uploaded_before = 0

        file_ids: list[int] = []
        for idx, path in enumerate(file_paths):
            prog_cb = None
            if on_progress is not None:
                base = uploaded_before

                async def prog_cb(current, total, _base=base):  # noqa: ANN001
                    await on_progress(_base + current, pack_total)

            sent = await client.send_document(
                channel_id, str(path), thumb=thumb_arg, progress=prog_cb,
            )
            uploaded_before += sizes[idx]
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
            episode_from=episode_from, episode_to=episode_to,
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
                episode_from=fields.get("episode_from"), episode_to=fields.get("episode_to"),
                entry_id=key.entry_id,
                ingest_method=fields.get("ingest_method"),
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
                # Keep the original header/start from the first upload.
                existing.ingest_method = fields.get("ingest_method")
                pack = existing
            await session.flush()
            session.expunge(pack)
            log.info("storage.pack.persisted", anime=key.anime_doc_id, season=key.season,
                     res=key.resolution, files=pack.file_count,
                     method=fields.get("ingest_method"))
            return pack

    # ── lookup & delivery ──
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
