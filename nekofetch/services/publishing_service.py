"""Publishing service — the approval gate before content becomes user-visible.

Lists requests in READY state, and publishes / reprocesses / cancels them. Publishing
marks the request's files visible and (in a full build) deploys them to the bound
distribution bot.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.exceptions import NotFound
from nekofetch.domain.enums import RequestStatus
from nekofetch.infrastructure.database.postgres.models import DownloadJob, MediaFile, Request
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.infrastructure.repositories.request_repo import RequestRepository


def _as_int(value) -> int | None:
    """Coerce a possibly-string id to ``int``; ``None`` when absent/non-numeric.

    ``franchise_data["anilist_id"]`` is stored as ``str(media.id)`` at the
    request-creation sites, but ``StoragePack.entry_id`` is an INTEGER column.
    Feeding the raw string into the pack lookup raises
    ``operator does not exist: integer = character varying`` and aborts the whole
    storage upload — so every id that reaches a pack query goes through here.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class ApprovalSummary:
    code: str
    title: str
    files: int
    resolution: str | None
    audio: str | None
    has_thumbnail: bool


class PublishingService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def list_ready(self, *, limit: int = 10) -> list[ApprovalSummary]:
        async with session_scope(self._c.pg_sessionmaker) as session:
            reqs = (
                await session.execute(
                    select(Request).where(Request.status == RequestStatus.READY).limit(limit)
                )
            ).scalars().all()
            out: list[ApprovalSummary] = []
            for req in reqs:
                files = await self._files_for_request(session, req.id)
                first = files[0] if files else None
                out.append(
                    ApprovalSummary(
                        code=req.code,
                        title=req.anime_title,
                        files=len(files),
                        resolution=first.resolution if first else None,
                        audio=(first.audio.value if first and first.audio else None),
                        has_thumbnail=any(
                            f.local_path and f.local_path.endswith(".thumb.jpg") for f in files
                        ),
                    )
                )
            return out

    async def _files_for_request(self, session, request_id: int) -> list[MediaFile]:
        job_ids = (
            await session.execute(
                select(DownloadJob.id).where(DownloadJob.request_id == request_id)
            )
        ).scalars().all()
        if not job_ids:
            return []
        return list(
            (await session.execute(select(MediaFile).where(MediaFile.job_id.in_(job_ids))))
            .scalars()
            .all()
        )

    async def upload_to_storage(self, code: str, *, on_progress=None) -> int:
        """Upload a request's processed files to the storage (DB) channel as packs.

        This is **automatic** — it runs straight after processing, independent of
        the main-channel publish/approval gate. Putting verified files into the
        database channel is just part of the pipeline; "publishing" (posting to the
        main channel, index, etc.) is a separate, deliberate action.
        """
        from pathlib import Path

        verify_on = self._c.config.processing.verify_files
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            files = await self._files_for_request(session, req.id)
            # Upload every file that exists on disk. The verify GATE only applies when
            # verification is actually enabled — otherwise files are never flagged
            # verified and NOTHING would ever reach the DB channel (the bug where DB
            # uploads "didn't consistently happen").
            files = [
                f for f in files
                if f.local_path and Path(f.local_path).exists()
                and (f.verified or not verify_on)
            ]
            from nekofetch.services.download_service import _safe_anime_doc_id
            anime_doc_id = _safe_anime_doc_id(req)
            title = req.anime_title
            # An admin-confirmed caption title (from the pre-upload confirm card)
            # overrides the pack caption's title line for this request. Line 2 of
            # the caption (resolution/audio) is always auto-derived per pack, so we
            # only override the TITLE stem. Best-effort: any job for this request
            # that recorded a ``caption_title`` in its resume_state wins.
            try:
                jobs = (await session.execute(
                    select(DownloadJob).where(DownloadJob.request_id == req.id)
                )).scalars().all()
                for j in jobs:
                    ct = (j.resume_state or {}).get("caption_title")
                    if ct:
                        title = ct
                        break
            except Exception:  # noqa: BLE001 — override is optional
                pass
            # Extract the AniList entry ID from franchise_data so storage packs
            # track which specific entry (season/movie/OVA) the files belong to.
            # ``anilist_id`` is stored as a STRING at the request-creation sites
            # (``str(media.id)``), but ``StoragePack.entry_id`` is an INTEGER
            # column. Passing the raw string into the pack lookup query produces
            # ``operator does not exist: integer = character varying`` and the
            # ENTIRE storage upload fails (uploaded=0). Coerce to ``int | None``.
            fd = req.franchise_data or {}
            req_entry_id: int | None = _as_int(
                fd.get("anilist_id") or fd.get("season_anilist_id")
            )
            snapshot = [
                {"season": f.season, "season_part": f.season_part,
                 "episode": f.episode, "resolution": f.resolution,
                 "audio": f.audio, "path": f.local_path,
                 "original_name": f.original_name,
                 "entry_id": req_entry_id}
                for f in files
            ]

        uploaded_paths = await self._upload_packs(
            anime_doc_id, title, snapshot, on_progress=on_progress,
        )

        # Delete every file that was CONFIRMED uploaded — individually, so a
        # partial failure still frees the disk of everything that made it up
        # (only the genuinely-failed files are kept for reprocessing). When ALL
        # files uploaded we additionally sweep the now-empty work/manual/library
        # dirs so nothing (stray temp files, empty trees) is left behind.
        all_paths = {s["path"] for s in snapshot if s.get("path")}
        confirmed = [s for s in snapshot if s.get("path") in uploaded_paths]
        if confirmed:
            self._delete_uploaded_files(confirmed)
        if all_paths and uploaded_paths >= all_paths:
            self._cleanup_local_files(snapshot, code=code, title=title)
        else:
            from nekofetch.core.logging import get_logger
            get_logger(__name__).warning(
                "publish.storage.incomplete_keeping_files",
                code=code, title=title,
                uploaded=len(uploaded_paths), total=len(all_paths),
                kept=len(all_paths - uploaded_paths),
                storage_enabled=self._c.config.storage_channel.enabled,
            )

        from nekofetch.services.log_channel_service import LogChannelService

        uploaded_count = len([s for s in snapshot if s.get("path") in uploaded_paths])
        await LogChannelService(self._c).event(
            "download", "stored", code=code, anime=title, files=uploaded_count,
        )
        return uploaded_count

    def _delete_uploaded_files(self, confirmed: list[dict]) -> None:
        """Delete each individually-confirmed uploaded file from disk.

        Runs even on a PARTIAL upload, so every tier that made it to the storage
        channel is freed immediately (only genuinely-failed files linger). Empty
        parent trees are swept separately by :meth:`_cleanup_local_files` on a
        full success."""
        from pathlib import Path

        from nekofetch.core.logging import get_logger

        log = get_logger(__name__)
        removed = 0
        for item in confirmed:
            p = item.get("path")
            if not p:
                continue
            try:
                Path(p).unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                log.debug("storage.cleanup.unlink_failed", path=p, error=str(exc))
        log.info("storage.cleanup.uploaded_removed", removed=removed)

    def _cleanup_local_files(self, snapshot: list[dict], *, code: str, title: str) -> None:
        """Delete every local file for a request after a successful storage upload.

        Covers all three places files can live so nothing is left behind:
          * ``work/<folder>``           — the processed outputs that were uploaded
          * ``work/_manual/<code>``     — a manual upload's raw intake
          * ``library/<slug>``          — a manual upload's renamed copies (staging)
        The manual paths simply don't exist for non-manual sources, so removing them
        is a harmless no-op there.
        """
        import shutil
        from pathlib import Path

        from nekofetch.core.logging import get_logger
        from nekofetch.sources.local import _slug

        log = get_logger(__name__)
        targets: set[Path] = set()

        for item in snapshot:
            p = item.get("path")
            if not p:
                continue
            fp = Path(p)
            # Only a "work/<folder>" directory is a safe rmtree target; otherwise
            # just remove the individual file.
            if fp.parent.parent.name == "work":
                targets.add(fp.parent)
            else:
                fp.unlink(missing_ok=True)

        storage = Path(self._c.env.storage_path)
        if code:
            targets.add(storage / "work" / "_manual" / code)
        if title:
            targets.add(storage / "library" / _slug(title))

        for d in targets:
            shutil.rmtree(d, ignore_errors=True)
        log.info("storage.cleanup.done", code=code, removed=len(targets))

    async def publish(
        self,
        code: str,
        *,
        caption_override: str | None = None,
        silent: bool = False,
    ) -> int:
        """Make stored content user-visible: wait for thumbnails → create bot
        → post to main channel + index.

        ``caption_override`` / ``silent`` flow straight through to
        :meth:`MainChannelService.publish` so Gojo's review card can publish an
        admin-edited caption and/or suppress the channel notification.

        New flow per operator feedback:
          1. Mark the request PUBLISHED (file.published=True, DB row updated).
          2. Call ThumbnailOrchestratorService so the bot/cards use
             admin-generated thumbnails (logo/poster/bg → Playwright render).
             Polls the workflow state and either waits it out OR marks the
             pipeline ready once the admin clicks "Skip Custom Thumbnails".
             Has a hard timeout so an absent admin can't block bot creation.
          3. Create distribution bot (or channel if bot limit is exhausted).
          4. Post to the main channel (the Download button now has the bot
             username); main channel uses the FIRST season's generated
             thumbnail as its post photo.
          5. Index + stats + notification fan-out.
        """
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            user_id = req.user_id
            files = await self._files_for_request(session, req.id)
            for f in files:
                f.published = True
            req.status = RequestStatus.PUBLISHED
            count = len(files)
            from nekofetch.services.download_service import _safe_anime_doc_id
            anime_doc_id = _safe_anime_doc_id(req)
            title = req.anime_title
            first = next((f for f in files if f.local_path), None)
            res = first.resolution if first else None
            aud = first.audio.value if first and first.audio else None
            # Franchise-update requests (created by UpdateCheckService) carry an
            # ``update_entry`` flag + their own anilist_id. When set, this entry
            # extends an already-published title: we update its distribution
            # channel in place instead of creating a new one / reposting main.
            fd = req.franchise_data or {}
            is_update_entry = bool(fd.get("update_entry"))
            update_anilist_id = fd.get("anilist_id")

        from nekofetch.services.analytics_service import AnalyticsService

        await AnalyticsService(self._c).record(
            "publish", anime_doc_id=anime_doc_id, data={"code": code, "files": count}
        )

        # Step 1: Wait for the thumbnail generation step to complete (or time
        # out / be skipped). The orchestrator surfaces the generated thumbnail
        # URL map to BotContentService + MainChannelService downstream.
        # No-op when the thumbnail_channel feature is disabled in config —
        # the orchestrator short-circuits and the rest of the pipeline uses
        # AniList posters throughout.
        #
        # All THREE gates must be on — if telegraph_access_token is empty,
        # :class:`ThumbnailChannelService.add_to_queue` short-circuits without
        # writing workflow entries, leaving ``is_complete()`` permanently
        # False. Without this guard we would burn the full 10-minute timeout
        # before falling back to AniList posters on every misconfigured host.
        if (
            self._c.config.features.thumbnail_generation
            and self._c.config.thumbnail_channel.enabled
            and self._c.config.thumbnail_channel.telegraph_access_token
        ):
            try:
                await self._wait_for_thumbnails(anime_doc_id, title)
            except Exception as exc:  # noqa: BLE001 - never block publish on thumb step
                from nekofetch.core.logging import get_logger
                get_logger(__name__).warning(
                    "publish.thumbnails.wait.failed",
                    anime=anime_doc_id, error=str(exc),
                )

        # Franchise-update branch: this entry extends an already-published
        # title. Append its card to the existing distribution channel in place
        # and STOP — no new bot/channel, no main-channel repost, no index
        # reshuffle (the title is already listed). Everything below (log/stats/
        # notify) still runs so the update is observable.
        if is_update_entry:
            try:
                from kurosoden.shared.senku_publisher import SenkuPublisher

                ids = [int(update_anilist_id)] if update_anilist_id is not None else None
                await SenkuPublisher(self._c).update_distribution_channel(
                    self._c.admin_client, anime_doc_id, ids,
                )
            except Exception as exc:  # noqa: BLE001 — never fail an update publish
                from nekofetch.core.logging import get_logger
                get_logger(__name__).warning(
                    "publish.channel_update.failed",
                    anime=anime_doc_id, error=str(exc),
                )
        else:
            # Step 2: Create distribution bot (if auto-create is enabled and feature is on).
            if self._c.config.features.distribution_bots and self._c.config.bot.auto_create_on_publish:
                from nekofetch.services.bot_orchestrator import BotOrchestratorService

                await BotOrchestratorService(self._c).ensure_bot_for_anime(anime_doc_id)
                # Snapshot the freshly-generated channel pack into a wipe-proof
                # backup so a later ban can restore it verbatim. Best-effort — a
                # capture hiccup must never fail an otherwise-successful publish.
                try:
                    from nekofetch.services.backup_service import BackupService

                    await BackupService(self._c).record_distribution_channel(anime_doc_id)
                except Exception as exc:  # noqa: BLE001
                    from nekofetch.core.logging import get_logger
                    get_logger(__name__).warning(
                        "publish.backup.distribution.failed",
                        anime=anime_doc_id, error=str(exc),
                    )

            # Step 3: Post to main channel (uses first season's generated thumbnail).
            from nekofetch.services.index_channel_service import IndexChannelService
            from nekofetch.services.main_channel_service import MainChannelService

            await MainChannelService(self._c).publish(
                anime_doc_id, caption_override=caption_override, silent=silent,
            )
            await IndexChannelService(self._c).refresh_letter(
                IndexChannelService.letter_of(title)
            )
            # Snapshot the main-channel post + the index sections (best-effort).
            try:
                from nekofetch.services.backup_service import BackupService

                bsvc = BackupService(self._c)
                await bsvc.backup_one(anime_doc_id)
                await bsvc.record_index()
            except Exception as exc:  # noqa: BLE001
                from nekofetch.core.logging import get_logger
                get_logger(__name__).warning(
                    "publish.backup.main_index.failed",
                    anime=anime_doc_id, error=str(exc),
                )

        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(self._c).event(
            "publish", "approved", code=code, anime=title, files=count,
            audio=aud, resolution=res,
        )

        # Refresh database stats (pinned message in index channel). Best-effort:
        # a stats hiccup must never fail an otherwise-successful publish.
        try:
            from nekofetch.services.stats_service import StatsService

            await StatsService(self._c).refresh()
        except Exception as exc:  # noqa: BLE001
            from nekofetch.core.logging import get_logger

            get_logger(__name__).warning("publish.stats_refresh.failed", error=str(exc))

        if user_id:
            from nekofetch.services.notification_service import NotificationService
            await NotificationService(self._c).request_published(user_id, title, code)
        return count

    async def _wait_for_thumbnails(self, anime_doc_id: str, title: str) -> None:
        """Bridge from the storage upload step to bot creation — queues and waits.

        Builds the per-entry thumbnail request list from the franchise walk
        (AniList) and TMDB metadata, then drives the orchestrator's polling
        loop. Skips generation entirely when the franchise walk returned zero
        entries (Telegram-source titles that don't have AniList relations).

        Emits a ``download.thumbnail_requested`` event so the Control Center
        shows admins that new thumb work is pending.
        """
        from nekofetch.core.logging import get_logger
        from nekofetch.services.bot_content import BotContentService
        from nekofetch.services.thumbnail_orchestrator_service import (
            ThumbnailOrchestratorService,
        )

        log = get_logger(__name__)

        # Re-use bot_content's franchise walk to build the entries list with
        # the same anilist_id we want thumbnails for. Avoids re-implementing
        # the AniList BFS in two places.
        bcs = BotContentService(self._c)
        try:
            meta = await bcs._gather_metadata(anime_doc_id)
            franchise = await bcs._walk_franchise(anime_doc_id, meta)
        except Exception as exc:  # noqa: BLE001 - walk failures shouldn't block publish
            log.debug("publish.franchise_walk.failed",
                      anime=anime_doc_id, error=str(exc))
            return

        tv_entries = franchise.get("tv", []) or []
        extra_entries = franchise.get("extras", []) or []
        if not tv_entries and not extra_entries:
            log.info("publish.thumbnails.skipped.no_entries",
                     anime=anime_doc_id, title=title)
            return

        # Use the shared helper so the labels and AniList IDs line up with
        # :meth:`BotContentService._queue_for_thumbnails`. One source of truth
        # for the per-entry shape.
        entries = BotContentService.build_thumbnail_entries(franchise)

        orch = ThumbnailOrchestratorService(self._c)
        await orch.request_thumbnails(anime_doc_id, str(title), entries)
        completed = await orch.wait_for_thumbnails(anime_doc_id)
        log.info(
            "publish.thumbnails.wait.result",
            anime=anime_doc_id,
            generated=completed,
            skipped=(not completed),
        )

    async def _upload_packs(self, anime_doc_id: str, title: str, files: list[dict],
                            *, on_progress=None) -> set[str]:
        """Group published files by (season, resolution, audio, entry_id) and upload each as a pack.

        Returns the set of local file paths that were CONFIRMED uploaded to the
        storage channel, so the caller only cleans up what actually shipped.
        """
        from nekofetch.core.logging import get_logger
        _log = get_logger(__name__)

        if not self._c.config.storage_channel.enabled:
            # Loud, not silent: a disabled storage channel means NOTHING reaches
            # the database channel — the operator must know why, and files must
            # NOT be cleaned up (the caller checks the returned set).
            _log.warning("publish.storage_channel_disabled",
                         anime=anime_doc_id, title=title, files=len(files),
                         hint="set storage_channel.enabled + channel_id to upload packs")
            return set()
        if not files:
            return set()
        from pathlib import Path

        from nekofetch.core.exceptions import FeatureDisabled
        from nekofetch.services.storage_channel_service import StorageChannelService

        storage = StorageChannelService(self._c)
        groups: dict[tuple, list[dict]] = {}
        for f in files:
            groups.setdefault((f.get("season"), f.get("season_part"), f["resolution"], f["audio"], f.get("entry_id")), []).append(f)

        from nekofetch.services.processing.stages import (
            POSTER_THUMB_NAME,
            _content_type_label,
        )

        # Post packs smallest-resolution-first (480p → 720p → 1080p) within each
        # season, so the database channel reads in ascending quality order. A
        # missing/garbage resolution token sorts last. Season/part lead the key
        # so a multi-season upload still groups each season's tiers together.
        def _res_rank(res: str) -> int:
            digits = "".join(ch for ch in (res or "") if ch.isdigit())
            return int(digits) if digits else 10_000

        ordered_groups = sorted(
            groups.items(),
            key=lambda kv: (
                kv[0][0] if kv[0][0] is not None else -1,   # season
                kv[0][1] if kv[0][1] is not None else -1,   # season_part
                _res_rank(kv[0][2]),                          # resolution asc
            ),
        )

        # Per-entry AniList poster map, resolved once from the prefetched
        # franchise walk. Each pack (S1/S2/S3/OVA) gets ITS OWN cover — AniList
        # has per-installment covers TMDB frequently lacks, and it matches the
        # franchise structure exactly. Falls back to the shared TMDB poster.jpg.
        entry_posters = await self._anilist_entry_posters(anime_doc_id, files)
        # Shorter title candidates (English/native/synonyms) the caption builder
        # falls back to when the full title overflows the 38-char line. Resolved
        # once from the prefetched AniList cache; empty when absent.
        alt_titles = await self._anilist_alt_titles(anime_doc_id)

        uploaded_paths: set[str] = set()
        for (season, season_part, resolution, audio, entry_id), items in ordered_groups:
            if not resolution or audio is None:
                continue
            items.sort(key=lambda x: (x["episode"] or 0))
            episodes = [i["episode"] for i in items if i["episode"] is not None]
            # Content type via the shared classifier (Season / Movie / OVA / ONA /
            # Special), refined by a filename hint so an extra reads naturally in
            # the header instead of a blanket "Special". Keeps header + filename in
            # lock-step (both go through processing.stages).
            name_hint = items[0].get("original_name") or items[0].get("path") or ""
            ct = _content_type_label(season, len(items), name_hint)
            # This pack's OWN AniList poster (keyed by entry_id, else season), fit
            # to a Telegram thumbnail beside the media files. Falls back to the
            # shared TMDB poster.jpg the thumbnail stage wrote when AniList had
            # nothing for this entry.
            dest_dir = Path(items[0]["path"]).parent
            poster = await self._pack_poster(
                entry_posters, entry_id, season, dest_dir,
                anime_doc_id=anime_doc_id,
            )
            if poster is None:
                poster = next(
                    (p for i in items
                     if (p := Path(i["path"]).with_name(POSTER_THUMB_NAME)).exists()),
                    None,
                )

            # ── Resume guard: skip a pack already fully in the channel ──
            # A pack persists its StoragePack row only AFTER all its files ship,
            # so a row that already covers every episode we have on disk means
            # this pack completed on a prior run (e.g. before a power-loss). Skip
            # the re-upload (which would orphan the old messages + duplicate the
            # files) and just mark these paths clean so they get tidied up. A
            # pack that crashed mid-upload has NO row (or a partial range) → it
            # falls through and re-uploads from the beginning, as intended.
            pack_key = storage.key_from(
                anime_doc_id, season, resolution, audio,
                season_part=season_part, entry_id=entry_id,
            )
            try:
                existing_pack = await storage.find_pack(pack_key)
            except Exception:  # noqa: BLE001 — lookup failure → upload (safe)
                existing_pack = None
            if existing_pack is not None:
                want_eps = {i["episode"] for i in items if i["episode"] is not None}
                ef, et = existing_pack.episode_from, existing_pack.episode_to
                have_eps = (set(range(ef, et + 1))
                            if ef is not None and et is not None else set())
                complete = (
                    (want_eps and want_eps.issubset(have_eps))
                    or (not want_eps and (existing_pack.file_count or 0) >= len(items))
                )
                if complete:
                    _log.info("publish.pack.already_stored.skip",
                              anime=anime_doc_id, season=season,
                              resolution=resolution, files=len(items))
                    uploaded_paths.update(i["path"] for i in items if i.get("path"))
                    continue

            try:
                await storage.upload_pack(
                    pack_key,
                    title=title,
                    file_paths=[Path(i["path"]) for i in items],
                    episode_from=min(episodes) if episodes else None,
                    episode_to=max(episodes) if episodes else None,
                    content_type=ct,
                    thumb=poster,
                    alt_titles=alt_titles,
                    on_progress=on_progress,
                )
                # Pack persisted → these files are safely in the channel.
                uploaded_paths.update(i["path"] for i in items if i.get("path"))
            except FeatureDisabled:
                _log.warning("publish.storage_channel_disabled_midway",
                             anime=anime_doc_id, season=season, resolution=resolution)
                return uploaded_paths
            except Exception as exc:  # noqa: BLE001 - one pack failing shouldn't abort publish
                _log.warning("publish.upload_pack.failed",
                             season=season, resolution=resolution, error=str(exc))
        return uploaded_paths

    async def _anilist_alt_titles(self, anime_doc_id: str) -> list[str]:
        """Shorter title candidates for the pack caption's fit-to-38 shortener.

        Sourced from the FRANCHISE ROOT (base series) — not the confirmed
        installment — so a sequel request never seeds "Kisekoi 2" as the name.
        Reads the prefetched ``anilist.json``: the ROOT entry's English / romaji /
        titles, plus the search blob's synonyms, all filtered to English/Latin
        script (no Filipino/Thai/Korean names). Empty when the cache is absent."""
        from nekofetch.core.logging import get_logger
        _log = get_logger(__name__)
        out: list[str] = []
        try:
            from nekofetch.services.bot_naming import is_latin_script, root_titles
            from nekofetch.services.metadata_prefetch import load_cached

            blob = await load_cached(self._c, anime_doc_id, "anilist",
                                     anime_doc_id=anime_doc_id)
            if not blob:
                return out
            root = root_titles(blob)          # ROOT english/romaji/titles (Latin-only)
            search = blob.get("search") or {}
            seen: set[str] = set()

            def _add(v):
                if v and is_latin_script(v) and v not in seen:
                    seen.add(v)
                    out.append(v)

            _add(root.get("english"))
            _add(root.get("romaji"))
            for v in root.get("titles") or []:
                _add(v)
            # Synonyms are only cached for the confirmed media (root synonyms
            # aren't walked) — still useful as extra SHORT candidates, Latin-only.
            for v in (search.get("synonyms") or []):
                _add(v)
        except Exception as exc:  # noqa: BLE001 — cache/parse issue → no alts
            _log.debug("publish.anilist_alt_titles.failed",
                       anime=anime_doc_id, error=str(exc))
        return out

    async def _anilist_entry_posters(
        self, anime_doc_id: str, files: list[dict],
    ) -> dict:
        """Map each franchise installment to its AniList cover URL.

        Reads the prefetched ``anilist.json`` (written at acceptance) so no live
        AniList call happens here. Returns two lookups merged into one dict::

            {("id", anilist_id): cover_url, ("season", n): cover_url}

        keyed by BOTH the entry's AniList id (matches a pack's ``entry_id``) and
        its chronological season index (fallback when a pack has no entry_id).
        Empty dict when the cache is absent — the caller then uses the shared
        TMDB poster."""
        from nekofetch.core.logging import get_logger
        _log = get_logger(__name__)
        out: dict = {}
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            blob = await load_cached(self._c, anime_doc_id, "anilist",
                                     anime_doc_id=anime_doc_id)
            if not blob:
                return out
            walk = blob.get("franchise") or {}
            # walk_franchise_full serializes to {id: FranchiseEntry-dict}; the
            # values carry anilist_id + cover_url + start_date (for ordering).
            entries = list(walk.values()) if isinstance(walk, dict) else list(walk or [])
            # TV entries in chronological order → season index (1-based).
            def _sk(e):
                sd = (e or {}).get("start_date") or {}
                return (sd.get("year") or 9999, sd.get("month") or 99,
                        sd.get("day") or 99)
            tv = sorted(
                [e for e in entries
                 if (e or {}).get("format") in ("TV", "TV_SHORT")],
                key=_sk,
            )
            for e in entries:
                aid = (e or {}).get("anilist_id")
                cov = (e or {}).get("cover_url")
                if aid is not None and cov:
                    out[("id", int(aid))] = cov
            for idx, e in enumerate(tv, start=1):
                cov = (e or {}).get("cover_url")
                if cov:
                    out[("season", idx)] = cov
        except Exception as exc:  # noqa: BLE001 — cache/parse issue → TMDB fallback
            _log.debug("publish.anilist_posters.failed",
                       anime=anime_doc_id, error=str(exc))
        return out

    async def _pack_poster(
        self, entry_posters: dict, entry_id, season, dest_dir,
        *, anime_doc_id: str | None = None,
    ):
        """Resolve + fit this pack's AniList poster; return a Path or ``None``.

        Offline-first: prefers the cover already downloaded + mirrored at
        prefetch time (``anilist_images.json`` local file, else a hosted
        backup) for this pack's ``entry_id`` — no live AniList fetch. Falls back
        to the franchise-walk cover URL only on a cache miss. Matches by
        ``entry_id`` (AniList id) first, then season index, and fits the result
        to Telegram's 320×320 thumbnail box under a per-entry filename so
        S1/S2/OVA posters never clobber each other. ``None`` when there's no
        AniList cover for this pack — the caller then uses the shared poster."""
        from pathlib import Path

        from nekofetch.core.logging import get_logger
        _log = get_logger(__name__)

        eid = _as_int(entry_id)
        tag = eid if eid is not None else (f"s{season}" if season is not None else "x")
        dest = Path(dest_dir) / f"poster_anilist_{tag}.jpg"
        if dest.exists() and dest.stat().st_size > 0:
            return dest

        # Prefer the prefetched, already-mirrored cover (local disk or a hosted
        # backup) so we don't re-download from AniList on every publish.
        ref = None
        if anime_doc_id and eid is not None:
            try:
                from nekofetch.services.metadata_prefetch import resolve_cached_cover

                ref = await resolve_cached_cover(
                    self._c, anime_doc_id, anilist_id=eid,
                    anime_doc_id=anime_doc_id,
                )
            except Exception as exc:  # noqa: BLE001
                _log.debug("publish.pack_poster.cache_miss",
                           entry=eid, error=str(exc))
        # Cache miss → the franchise-walk cover URL (by entry id, then season).
        if not ref:
            if eid is not None:
                ref = entry_posters.get(("id", eid))
            if ref is None and season is not None:
                ref = entry_posters.get(("season", int(season)))
        if not ref:
            return None

        try:
            from nekofetch.services.processing.stages import ThumbnailStage

            if await ThumbnailStage._fit_thumb(ref, dest):
                return dest
        except Exception as exc:  # noqa: BLE001
            _log.debug("publish.pack_poster.fit_failed", ref=ref, error=str(exc))
        return None

    async def reprocess(self, code: str) -> None:
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            job = (
                await session.execute(
                    select(DownloadJob).where(DownloadJob.request_id == req.id)
                )
            ).scalars().first()
            job_id = job.id if job else None
        if job_id is not None:
            from nekofetch.services.processing.pipeline import ProcessingPipeline

            await ProcessingPipeline(self._c).run_for_job(job_id)

    async def cancel(self, code: str) -> None:
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            req.status = RequestStatus.REJECTED
