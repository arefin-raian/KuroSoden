"""Download worker — executes queued jobs through authorized sources.

Runs as a background loop (started by the bot manager / scheduler). For each job it:

1. claims the next queued job (respecting ``concurrent_downloads``),
2. resolves the request's source + episode variants,
3. downloads each variant resumably, publishing live progress to Redis and Postgres,
4. records :class:`MediaFile` rows, then advances the request to PROCESSING,
5. retries with backoff on failure, preserving ``resume_state``.

Byte-moving itself is delegated to the source's ``download`` (e.g. ``LocalFileSource``).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.parsing import clean_anilist_id
from nekofetch.core.redis_safe import (
    safe_redis_delete,
    safe_redis_get,
    safe_redis_mget,
    safe_redis_set,
)
from nekofetch.domain.enums import AudioType, JobStatus, RequestStatus
from nekofetch.infrastructure.database.postgres.models import DownloadJob, MediaFile
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.infrastructure.database.redis.progress import ProgressSnapshot
from nekofetch.infrastructure.repositories.queue_repo import QueueRepository
from nekofetch.infrastructure.repositories.request_repo import RequestRepository
from nekofetch.sources._diagnostics import classify as _classify

log = get_logger(__name__)

# Conservative per-episode size estimates used when variant metadata is unavailable.
# Actual sizes vary, but these ensure we don't over-commit disk space.
_ESTIMATED_BYTES_PER_RES: dict[str, int] = {
    "360p": 200_000_000,
    "480p": 400_000_000,
    "540p": 600_000_000,
    "720p": 800_000_000,
    "1080p": 1_500_000_000,
    "2160p": 4_000_000_000,
}
_DISK_BUFFER_BYTES = 1_000_000_000  # 1 GB safety margin


def _is_torrent_ref(ref: str) -> bool:
    """True when a ``source_ref`` is a nyaa/torrent descriptor (magnet, torrent
    URL/path, or info-hash) rather than a website native id. Such refs are only
    meaningful to the nyaa source — never pass them to a website source."""
    if not ref:
        return False
    if ref.startswith("magnet:"):
        return True
    try:
        obj = json.loads(ref)
    except (TypeError, ValueError):
        return False
    if not isinstance(obj, dict):
        return False
    return any(k in obj for k in
               ("magnet", "torrent_url", "torrent_path", "info_hash", "file_index"))


class _SkipEpisode(Exception):
    """Raised internally when an admin Stops the currently-downloading episode."""


class _AbortSourceAttempt(BaseException):
    """Raised internally when an admin backs out of the current source attempt.

    This differs from ``_CancelJob``: the download job stops, but the request is
    returned to APPROVED so Levi can pick another source without owner action.
    """


class _CancelJob(BaseException):
    """Raised internally when an admin Cancels the entire job.

    Deliberately a ``BaseException`` so the per-unit ``except Exception`` guards
    (which isolate ordinary download failures) don't swallow it — a Cancel must
    propagate all the way up to ``_process_job`` and tear the whole job down.
    """


class DownloadWorker:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._sem = asyncio.Semaphore(container.config.downloads.concurrent_downloads)
        self._running = False
        self._tasks: set[asyncio.Task] = set()

    async def run_forever(self, poll_interval: float = 2.0) -> None:
        self._running = True
        await self.recover_on_startup()
        log.info("download.worker.start", concurrency=self._c.config.downloads.concurrent_downloads)
        while self._running:
            job_id = await self._claim_next()
            if job_id is None:
                await asyncio.sleep(poll_interval)
                continue
            task = asyncio.create_task(self._guarded(job_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        """Stop the poll loop and cancel all in-flight download tasks.

        Awaits each cancelled task so that by the time this returns the worker
        has fully wound down — no background task is still writing to the DB or
        downloading a file. This is the guarantee :class:`DatabaseClearService`
        needs before truncating tables, and that ``PipelineManager.stop`` needs
        for a clean shutdown.
        """
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _claim_next(self) -> int | None:
        async with session_scope(self._c.pg_sessionmaker) as session:
            repo = QueueRepository(session)
            job = await repo.next_queued()
            if job is None:
                return None
            job.status = JobStatus.RUNNING
            job.attempts += 1
            return job.id

    async def _guarded(self, job_id: int) -> None:
        async with self._sem:
            try:
                await self._process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._handle_failure(job_id, exc)

    def _chunk_episodes(
        self, episodes: list, resolution: str, folder: str,
    ) -> list[list]:
        """Split episodes so each chunk fits in available disk space.

        Uses ``shutil.disk_usage`` on the work folder, reserves a 1 GB buffer,
        and divides episodes into chunks that (conservatively) won't blow out
        the disk.  When space is plentiful a single chunk covering all episodes
        is returned — the quality-first loop behaves exactly as before.
        """

        storage = Path(self._c.env.storage_path)
        check_dir = storage / "work" / folder
        check_dir.mkdir(parents=True, exist_ok=True)

        try:
            free = shutil.disk_usage(check_dir).free
        except OSError:
            # Can't probe — trust that space exists and don't chunk.
            return [list(episodes)]

        usable = max(0, free - _DISK_BUFFER_BYTES)
        est = _ESTIMATED_BYTES_PER_RES.get(resolution, 500_000_000)

        if usable <= 0:
            # Extremely tight — one episode at a time.
            log.warning("download.disk_full", resolution=resolution,
                        free_mb=free // 1_000_000)
            return [[ep] for ep in episodes]

        per_chunk = max(1, int(usable // est))
        if per_chunk >= len(episodes):
            return [list(episodes)]  # single chunk — common case

        chunks: list[list] = []
        for i in range(0, len(episodes), per_chunk):
            chunks.append(list(episodes[i:i + per_chunk]))

        log.info("download.disk_space_chunking", resolution=resolution,
                 episodes=len(episodes), chunks=len(chunks),
                 free_mb=free // 1_000_000, per_chunk=per_chunk)
        return chunks

    async def _process_job(self, job_id: int) -> None:
        cfg = self._c.config.downloads
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            req = await RequestRepository(session).get(job.request_id)
            job.started_at = _now()
            session.expunge(req)  # detach so we can use it after the session closes

        # Resolve the source chain (preferred site first; both sites for website
        # requests so dual-audio can cross-source). The primary drives the episode
        # list and all non-dual downloads.
        chain = await self._resolve_chain(req)
        source, episodes = chain[0]
        if req.season is not None:
            episodes = [e for e in episodes if e.season == req.season]
        if req.episodes:
            episodes = [e for e in episodes if e.number in set(req.episodes)]

        # Strip extras (NCOP, NCED, etc.) when the franchise data tells us how
        # many main episodes to expect and no explicit episode list was given.
        # Extras are only downloaded when the user specifically selected them.
        if not req.episodes:
            expected = int((req.franchise_data or {}).get("franchise_episodes") or 0)
            if expected > 0:
                main = []
                for ep in episodes:
                    try:
                        kind = json.loads(ep.source_ref).get("kind", "episode")
                    except Exception:
                        kind = "episode"
                    if kind == "episode":
                        main.append(ep)
                if len(main) >= expected:
                    episodes = main[:expected]
                elif main:
                    episodes = main

        # Apply torrent mapping: filter to included files and override season/episode numbers.
        torrent_mapping_dict = (req.franchise_data or {}).get("_torrent_mapping")
        if torrent_mapping_dict:
            from nekofetch.services.torrent_mapping import TorrentMapping
            from nekofetch.sources.base import Episode
            tm = TorrentMapping.from_dict(torrent_mapping_dict)
            ep_override: dict[int, tuple[int, int]] = {}
            for tme in tm.included_entries:
                fe = tme.franchise_entry
                for fa in tme.files:
                    if fa.episode_number is not None:
                        ep_override[fa.file_index] = (fe.season_number, fa.episode_number)
            if ep_override:
                remapped = []
                for ep in episodes:
                    try:
                        fidx = json.loads(ep.source_ref).get("file_index")
                    except Exception:
                        fidx = None
                    if fidx is not None and fidx in ep_override:
                        s, n = ep_override[fidx]
                        remapped.append(Episode(
                            source_ref=ep.source_ref, season=s, number=n, title=ep.title,
                        ))
                if remapped:
                    episodes = remapped

        audios = self._target_audios(req)
        folder = _safe_folder(req)
        code = req.code
        title = req.anime_title
        await self._clear_skip(job_id)
        await self._clear_source_abort(job_id)

        # Quality-first order (least → most): 360p → 480p → 720p → 1080p.
        # Probe the first episode to discover available resolutions.
        try:
            variants = await source.get_variants(episodes[0].source_ref)
            available = {v.resolution for v in variants}
        except Exception:
            available = set()
        resolutions = self._resolutions_to_fetch(req, available)
        resolutions.sort(
            key=lambda r: int(r.rstrip("p")) if r.rstrip("p").isdigit() else 0
        )

        all_failed: list[dict] = []

        try:
            for resolution in resolutions:
                if await self._source_abort_requested(job_id):
                    raise _AbortSourceAttempt()
                if await self._cancel_requested(job_id):
                    raise _CancelJob()

                chunks = self._chunk_episodes(episodes, resolution, folder)

                for chunk in chunks:
                    if await self._source_abort_requested(job_id):
                        raise _AbortSourceAttempt()
                    if await self._cancel_requested(job_id):
                        raise _CancelJob()

                    # Download every episode in this chunk.
                    failed: list[dict] = []
                    for ep in chunk:
                        if await self._source_abort_requested(job_id):
                            raise _AbortSourceAttempt()
                        if await self._cancel_requested(job_id):
                            raise _CancelJob()
                        await self._download_episode(
                            job_id, req, source, chain, ep, audios, folder, cfg, failed,
                            resolution=resolution,
                        )

                    # Retry failed units for this chunk — fresh tokens/hosts.
                    remaining: list[dict] = []
                    for spec in failed:
                        if await self._source_abort_requested(job_id):
                            raise _AbortSourceAttempt()
                        if await self._cancel_requested(job_id):
                            raise _CancelJob()
                        retried = await self._retry_unit(
                            job_id, req, source, chain, spec, folder, cfg,
                        )
                        if not retried:
                            remaining.append(spec)
                    if remaining:
                        all_failed.extend(remaining)

                    # Cancel check before processing/uploading this chunk.
                    if await self._source_abort_requested(job_id):
                        await self._finalize_source_aborted(job_id)
                        return
                    if await self._cancel_requested(job_id):
                        await self._finalize_cancelled(job_id)
                        return

                    # Process + upload this chunk — frees disk space before
                    # the next chunk (or next resolution tier) downloads.
                    await self._process_and_upload_quality(job_id, code, title)

        except _CancelJob:
            await self._finalize_cancelled(job_id)
            return
        except _AbortSourceAttempt:
            await self._finalize_source_aborted(job_id)
            return

        # Final cancel check before completing the job.
        if await self._source_abort_requested(job_id):
            await self._finalize_source_aborted(job_id)
            return
        if await self._cancel_requested(job_id):
            await self._finalize_cancelled(job_id)
            return

        # ── Fail-fast: a job that produced ZERO MediaFiles must NOT proceed ──
        # Without this guard the pipeline runs processing → upload → publish on
        # nothing, posts "downloaded"/"complete" log events, sends a
        # download-complete notification, and triggers BotOrchestrator —
        # creating an empty bot. The user sees a fake success. This guard
        # short-circuits the run: the raised RuntimeError is caught by
        # _guarded and routed to _handle_failure, producing a real failure
        # notification. Partial success (some succeeded, some failed) still
        # proceeds to _mark_partial + _finalize_after_qualities below.
        #
        # The DB check itself is delegated to :meth:`_has_recorded_files` so it
        # is testable in isolation (returns None on DB error, True/False otherwise).
        have_files = await self._has_recorded_files(job_id)
        if have_files is False:
            raise RuntimeError(
                f"download produced 0 files for {title!r} (code={code}, "
                f"{len(all_failed)} units failed); aborting job "
                f"instead of running upload/publish on nothing"
            )

        if all_failed:
            await self._mark_partial(job_id, all_failed)
        await self._finalize_after_qualities(job_id, code, title)

    async def _download_episode(self, job_id, req, source, chain, ep, audios, folder,
                                cfg, failed: list, resolution: str | None = None) -> None:
        """Download every resolution/audio unit for ONE episode, recording (never
        raising) failures so the caller keeps going through the rest of the series.

        When ``resolution`` is passed, only that resolution is fetched — used by
        the quality-first loop to download one tier at a time."""
        try:
            variants = await source.get_variants(ep.source_ref)
        except Exception as exc:  # noqa: BLE001
            kind, reason = _classify(exc)
            log.warning("download.variants.failed", season=ep.season, episode=ep.number,
                        failure=kind.value, reason=reason)
            failed.append(self._spec(ep, None, None, dual=False))
            return
        if getattr(source, "name", "") == "local":
            # Operator-owned library (also the manual-upload target): ingest exactly
            # the files present — every laid-down resolution/audio — with no
            # acquisition-policy filtering. Everything downstream (record → process →
            # storage → publish) is identical to any other source.
            for variant in variants:
                if resolution is not None and variant.resolution != resolution:
                    continue
                spec = self._spec(ep, variant.resolution, variant.audio, dual=False)
                try:
                    if await self._already_have(job_id, ep, variant.resolution, variant.audio):
                        continue
                    if not await self._run_unit(job_id, req, source, ep, variant, folder, cfg):
                        failed.append(spec)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    kind, reason = _classify(exc)
                    log.warning("download.unit.failed", season=ep.season, episode=ep.number,
                                resolution=variant.resolution,
                                audio=variant.audio.value if variant.audio else None,
                                failure=kind.value, reason=reason)
                    failed.append(spec)
            return
        available = {v.resolution for v in variants}
        wanted = self._resolutions_to_fetch(req, available)
        if resolution is not None:
            wanted = [r for r in wanted if r == resolution]
        for resolution in wanted:
            has_native_dual = any(
                v.audio == AudioType.DUAL_AUDIO and v.resolution == resolution for v in variants
            )
            # Collapse SUBBED/DUBBED requests to DUAL_AUDIO when the source offers
            # the same URL muxed with both language tracks. Without this mapping,
            # requesting only one language on a dual-audio source makes every
            # ``_select_variant`` exact match fail — the loop silently ``continue``s
            # and the fail-fast guard fires with "0 files / 0 units failed".
            # See ``_resolve_audio_targets`` docstring for full rationale.
            target_audios = self._resolve_audio_targets(audios, variants, resolution)
            for audio in target_audios:
                dual = audio == AudioType.DUAL_AUDIO and not has_native_dual
                spec = self._spec(ep, resolution, audio, dual=dual)
                try:
                    if dual:
                        await self._acquire_dual(chain, req, ep, resolution, folder, job_id, cfg)
                        continue
                    variant = _select_variant(
                        variants, resolution, audio,
                        self._c.config.acquisition.require_english_subs,
                    )
                    if variant is None:
                        continue
                    # Resume: if a prior run already downloaded this exact unit and
                    # the file is still on disk, skip it (e.g. eps 1-9 done → ep 10).
                    if await self._already_have(job_id, ep, resolution, audio):
                        continue
                    if not await self._run_unit(job_id, req, source, ep, variant, folder, cfg):
                        failed.append(spec)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    kind, reason = _classify(exc)
                    log.warning("download.unit.failed", season=ep.season, episode=ep.number,
                                resolution=resolution, audio=audio.value,
                                failure=kind.value, reason=reason)
                    failed.append(spec)

    @staticmethod
    def _spec(ep, resolution, audio, *, dual: bool) -> dict:
        return {"ep_ref": ep.source_ref, "season": ep.season, "number": ep.number,
                "title": ep.title, "resolution": resolution,
                "audio": audio.value if audio is not None else None, "dual": dual}

    @staticmethod
    def _resolve_audio_targets(
        wanted: list[AudioType],
        variants,
        resolution: str,
    ) -> list[AudioType]:
        """Collapse SUBBED/DUBBED requests to DUAL_AUDIO when the source can deliver
        both tracks natively in one muxed file.

        Why: multiple web sources (notably AniZone's ``seiryuu.vid-cdn.xyz``)
        advertise a single ``DUAL_AUDIO`` :class:`VideoVariant` from a master
        playlist that encodes BOTH Japanese and English audio renditions under one
        URL. When the request only wants one language (e.g. ``audios=[SUBBED]``),
        the exact-match filter in ``_select_variant`` finds zero matches and the
        loop silently ``continue``s every unit — never reaching ``_run_unit``,
        never logging ``download.unit.error``, so the fail-fast guard fires with
        ``download produced 0 files (... 0 units failed)`` and the operator gets
        no actionable signal beyond the silent zero.

        The fix is to detect native dual-audio availability at the **call site**
        of the audio loop and replace any single-track audio request with an
        equivalent ``DUAL_AUDIO`` request, deduped so we still get a single
        download pass per ``(episode, resolution)`` — fetching the same URL twice
        for ``[SUBBED, DUBBED]`` would be wasteful, and the resulting mux keeps
        BOTH language tracks so users see neither language get lost.

        Single-language requests on a non-dual source pass through unchanged
        (returns a copy of ``wanted``); a pure ``DUAL_AUDIO`` request on a
        dual-capable source also passes through unchanged.
        """
        has_native_dual = any(
            v.audio == AudioType.DUAL_AUDIO and v.resolution == resolution
            for v in variants
        )
        if not has_native_dual:
            return list(wanted)
        result: list[AudioType] = []
        for a in wanted:
            mapped = (
                AudioType.DUAL_AUDIO
                if a in (AudioType.SUBBED, AudioType.DUBBED)
                else a
            )
            if mapped not in result:
                result.append(mapped)
        return result

    async def _run_unit(self, job_id, req, source, ep, variant, folder, cfg) -> bool:
        """Download a single variant under a Stop watcher. True on success; False if
        it failed or was Stopped (so it lands on the retry list)."""
        dest = (
            self._c.env.storage_path / "work" / folder
            / f"S{ep.season:02d}E{ep.number:03d}_{variant.resolution}_{variant.audio.value}"
              f".{variant.container or 'mkv'}"
        )
        on_progress = self._make_progress(job_id, ep, variant, cfg)
        on_retry = self._make_retry(job_id, ep, variant, cfg)
        try:
            result = await self._download_watched(job_id, source, variant, dest,
                                                  on_progress, cfg, on_retry)
        except _SkipEpisode:
            log.info("download.episode.stopped", season=ep.season, episode=ep.number)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            kind, reason = _classify(exc)
            log.warning("download.unit.error", season=ep.season, episode=ep.number,
                        failure=kind.value, reason=reason)
            return False
        await self._record_file(job_id, req, ep, variant, dest, result)
        return True

    async def _download_watched(self, job_id, source, variant, dest, on_progress, cfg,
                                on_retry=None) -> dict:
        """Run the (retrying) download as a sub-task while polling the Stop flag, so
        an admin can stop the CURRENT episode without killing the whole job."""
        task = asyncio.ensure_future(self._download_with_retry(
            source, variant, dest, on_progress, cfg.retry_attempts, cfg.retry_backoff_seconds,
            on_retry=on_retry,
        ))
        while True:
            cancel, abort_source, skip = await self._control_flags(job_id)
            if cancel or abort_source or skip:
                task.cancel()
                if skip:
                    await self._clear_skip(job_id)
                if abort_source:
                    await self._clear_source_abort(job_id)
                try:
                    await task
                except BaseException:  # noqa: BLE001 - swallow whatever the cancel surfaces
                    pass
                if cancel:
                    raise _CancelJob()
                if abort_source:
                    raise _AbortSourceAttempt()
                raise _SkipEpisode()
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except TimeoutError:
                continue

    def _make_progress(self, job_id, ep, variant, cfg):
        """Build the rolling-window progress callback for one unit."""
        st = {"last": 0.0, "win_t": time.monotonic(), "win_done": 0}

        async def on_progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - st["last"] < cfg.progress_update_interval_seconds:
                return
            dt = max(now - st["win_t"], 1e-6)
            speed = max(done - st["win_done"], 0) / dt
            st["win_t"], st["win_done"], st["last"] = now, done, now
            pct = (done / total * 100) if total else 0.0
            eta = int((total - done) / speed) if speed > 0 else None
            if self._c.progress:
                try:
                    await self._c.progress.set(ProgressSnapshot(
                        job_id=job_id, status=JobStatus.RUNNING.value, progress=pct,
                        speed_bps=speed, downloaded_bytes=done, total_bytes=total,
                        current_episode=ep.number, eta_seconds=eta,
                        resolution=variant.resolution, audio=variant.audio.value,
                        label=(
                            f"S{ep.season:02d}E{ep.number:03d} "
                            f"{variant.resolution} {variant.audio.value}"
                        ),
                    ))
                except Exception:  # noqa: BLE001
                    # Progress is cosmetic telemetry — a Redis hiccup (e.g. an
                    # Upstash read timeout) must NEVER fail an episode that is
                    # actually downloading fine.
                    log.debug("download.progress_redis_blip", job_id=job_id)
        return on_progress

    def _make_retry(self, job_id, ep, variant, cfg):
        """Build the on-retry callback fired between auto-retry attempts for one unit.

        Publishes a snapshot marking WHICH attempt is now in flight and a human
        reason, so the live card can render "🔁 Retrying 2/3 · connection reset"
        instead of silently stalling."""
        async def on_retry(attempt: int, reason: str | None) -> None:
            if not self._c.progress:
                return
            try:
                await self._c.progress.set(ProgressSnapshot(
                    job_id=job_id, status=JobStatus.RUNNING.value,
                    current_episode=ep.number, season=ep.season,
                    resolution=variant.resolution, audio=variant.audio.value,
                    stage="Retrying",
                    retry_attempt=attempt, retry_max=cfg.retry_attempts,
                    retry_reason=reason,
                    label=(
                        f"S{ep.season:02d}E{ep.number:03d} "
                        f"{variant.resolution} {variant.audio.value}"
                    ),
                ))
            except Exception:  # noqa: BLE001 - telemetry, never fail the download
                log.debug("download.retry_redis_blip", job_id=job_id)
        return on_retry

    async def _retry_unit(self, job_id, req, source, chain, spec, folder, cfg) -> bool:
        """Retry one failed unit ONCE with freshly re-extracted metadata."""
        from nekofetch.sources.base import Episode

        ep = Episode(source_ref=spec["ep_ref"], season=spec["season"],
                     number=spec["number"], title=spec.get("title"))
        try:
            if spec.get("dual"):
                await self._acquire_dual(chain, req, ep, spec["resolution"], folder, job_id, cfg)
                return True
            if not spec.get("resolution") or not spec.get("audio"):
                return False
            variants = await source.get_variants(spec["ep_ref"])  # fresh tokens/hosts
            variant = _select_variant(
                variants, spec["resolution"], AudioType(spec["audio"]),
                self._c.config.acquisition.require_english_subs,
            )
            if variant is None:
                return False
            return await self._run_unit(job_id, req, source, ep, variant, folder, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            kind, reason = _classify(exc)
            log.warning("download.retry.failed", season=spec["season"], episode=spec["number"],
                        failure=kind.value, reason=reason)
            return False

    async def _mark_partial(self, job_id: int, remaining: list) -> None:
        """Record the episodes that couldn't be downloaded and post an actionable
        attention card (Retry / Switch-source / Provide-file). The job still
        completes — the series ships with what succeeded."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            req = await RequestRepository(session).get(job.request_id) if job else None
            if job is not None:
                state = dict(job.resume_state or {})
                state["partial_failures"] = remaining
                job.resume_state = state
            code = req.code if req else ""
            title = req.anime_title if req else ""
            source = req.source if req else ""
        # Keep per-episode audio so the card says exactly WHICH version failed
        # (sub vs dub), not just an episode number.
        seen: set = set()
        failures: list[dict] = []
        for s in remaining:
            n = s.get("number")
            if n is None or (n, s.get("audio")) in seen:
                continue
            seen.add((n, s.get("audio")))
            failures.append({"ep": n, "audio": s.get("audio")})
        log.warning("download.job.partial", job_id=job_id,
                    stuck=[(f["ep"], f["audio"]) for f in failures], source=source)
        from nekofetch.services.log_channel_service import LogChannelService
        await LogChannelService(self._c).post_attention_card(
            code=code, title=title, failures=failures, source=source,
            alt_source=_alternate_source(source),
        )

    async def ingest_provided_file(self, code: str, episode: int, src_path) -> None:
        """Ingest an admin-provided file for a stuck episode: record it as a verified
        MediaFile, push it to the storage channel, then remove the local temp so we
        don't keep the hand-delivered copy around."""

        from nekofetch.core.exceptions import NotFound
        from nekofetch.infrastructure.database.postgres.models import DownloadJob

        src_path = Path(src_path)
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            job = (await session.execute(
                select(DownloadJob).where(DownloadJob.request_id == req.id)
                .order_by(DownloadJob.id.desc())
            )).scalars().first()
            job_id = job.id if job else None
            anime_doc_id = _safe_anime_doc_id(req)
            audio = req.audio or AudioType.SUBBED
            season = req.season or 1
            folder = _safe_folder(req)
        ext = src_path.suffix.lstrip(".") or "mkv"
        resolution = _provided_resolution_bucket(src_path)
        dest = (self._c.env.storage_path / "work" / folder
                / f"S{season:02d}E{int(episode):03d}_{resolution}_manual.{ext}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src_path.resolve() != dest.resolve():
            src_path.replace(dest)
        import hashlib
        async with session_scope(self._c.pg_sessionmaker) as session:
            session.add(MediaFile(
                job_id=job_id, anime_doc_id=anime_doc_id, season=season,
                episode=int(episode), resolution=resolution, audio=audio,
                local_path=str(dest), size_bytes=dest.stat().st_size,
                checksum=hashlib.sha256(dest.read_bytes()).hexdigest(),
                container=ext, verified=True,
            ))
        from nekofetch.services.publishing_service import PublishingService
        await PublishingService(self._c).upload_to_storage(code)
        try:
            dest.unlink()                 # delete the provided file after ingesting it
        except OSError:
            log.debug("download.cleanup.unlink_failed", path=str(dest))
        log.info("download.manual.ingested", code=code, episode=episode)

    # ── Stop-current-episode flag (set from ACTIVE TASKS) ────────────────────────
    @staticmethod
    def _skip_key(job_id: int) -> str:
        return f"nf:job:{job_id}:skip"

    async def _control_flags(self, job_id: int) -> tuple[bool, bool, bool]:
        """Read cancel / source-abort / skip in ONE Redis round-trip.

        The hot poll loop (``_download_watched``, ~1s) previously issued three
        separate GETs, each with its own timeout — on a high-latency/remote
        Redis (e.g. Upstash from a home connection) those stacked into repeated
        ``redis.timeout`` warnings and starved the poll. One MGET is a single
        timed call instead of three. Returns ``(cancel, abort_source, skip)``;
        any blip yields all-``False`` so the download simply keeps going.
        """
        vals = await safe_redis_mget(
            self._c.redis,
            [self._cancel_key(job_id), self._source_abort_key(job_id), self._skip_key(job_id)],
            label="download.control_flags",
        )
        return bool(vals[0]), bool(vals[1]), bool(vals[2])

    async def _skip_requested(self, job_id: int) -> bool:
        # Hot path polled every ~1s by ``_download_watched``. A bare ``get``
        # here would wedge the worker for the full Upstash timeout on every
        # blip — use the safe wrapper so a blip just returns ``False`` (no
        # skip) and the worker keeps ticking.
        return bool(await safe_redis_get(self._c.redis, self._skip_key(job_id),
                                          label="download.skip_requested"))

    async def _clear_skip(self, job_id: int) -> None:
        if self._c.redis:
            await safe_redis_delete(self._c.redis, self._skip_key(job_id),
                                    label="download.clear_skip")

    async def request_skip(self, job_id: int) -> None:
        """Public: signal the worker to Stop the currently-downloading episode (it
        finishes the rest of the series, then retries this one at the end)."""
        if self._c.redis:
            # ``ex=300`` TTL is critical: a stale skip flag committed RIGHT BEFORE
            # a worker crash would be honored on the next ``recover_on_startup``
            # pass and would force a freshly-restarted download loop onto a
            # phantom-Skip branch. The TTL bounds the risk window to 5 min,
            # by which time the worker would have either consumed the flag or
            # crashed again with a different redacted set of state.
            await safe_redis_set(self._c.redis, self._skip_key(job_id), "1",
                                  label="download.request_skip", ex=300)

    # ── Abort-current-source flag ────────────────────────────────────────────────
    @staticmethod
    def _source_abort_key(job_id: int) -> str:
        return f"nf:job:{job_id}:source_abort"

    async def _source_abort_requested(self, job_id: int) -> bool:
        return bool(await safe_redis_get(
            self._c.redis,
            self._source_abort_key(job_id),
            label="download.source_abort_requested",
        ))

    async def _clear_source_abort(self, job_id: int) -> None:
        if self._c.redis:
            await safe_redis_delete(
                self._c.redis,
                self._source_abort_key(job_id),
                label="download.clear_source_abort",
            )

    async def request_source_abort(self, job_id: int) -> None:
        """Public: stop this job's current source attempt without failing the request."""
        if self._c.redis:
            await safe_redis_set(
                self._c.redis,
                self._source_abort_key(job_id),
                "1",
                label="download.request_source_abort",
                ex=300,
            )

    # ── Cancel-whole-job flag ────────────────────────────────────────────────────
    @staticmethod
    def _cancel_key(job_id: int) -> str:
        return f"nf:job:{job_id}:cancel"

    async def _cancel_requested(self, job_id: int) -> bool:
        # Hot path polled every ~1s by ``_download_watched``. See
        # ``_skip_requested`` for the blip rationale.
        return bool(await safe_redis_get(self._c.redis, self._cancel_key(job_id),
                                          label="download.cancel_requested"))

    async def _clear_cancel(self, job_id: int) -> None:
        if self._c.redis:
            await safe_redis_delete(self._c.redis, self._cancel_key(job_id),
                                    label="download.clear_cancel")

    async def _finalize_cancelled(self, job_id: int) -> None:
        """Tear a cancelled job down cleanly: mark it CANCELLED (so it leaves the
        active list), drop its live progress, and clear its control flags."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            title = code = ""
            if job is not None:
                job.status = JobStatus.CANCELLED
                job.finished_at = _now()
                req = await RequestRepository(session).get(job.request_id)
                if req is not None:
                    req.status = RequestStatus.FAILED
                    title, code = req.anime_title, req.code
        if self._c.progress:
            await self._c.progress.delete(job_id)
        await self._clear_skip(job_id)
        await self._clear_cancel(job_id)
        log.info("download.job.cancelled", job_id=job_id)
        from nekofetch.services.log_channel_service import LogChannelService
        await LogChannelService(self._c).event("error", "cancelled", job=job_id,
                                                anime=title, code=code)

    # ── startup recovery / resume ────────────────────────────────────────────────
    async def _finalize_source_aborted(self, job_id: int) -> None:
        """Stop this job but leave the request alive for another source pick."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            title = code = ""
            if job is not None:
                job.status = JobStatus.CANCELLED
                job.finished_at = _now()
                req = await RequestRepository(session).get(job.request_id)
                if req is not None:
                    req.status = RequestStatus.APPROVED
                    title, code = req.anime_title, req.code
        if self._c.progress:
            await self._c.progress.delete(job_id)
        await self._clear_skip(job_id)
        await self._clear_source_abort(job_id)
        log.info("download.source_aborted", job_id=job_id)
        from nekofetch.services.log_channel_service import LogChannelService
        await LogChannelService(self._c).event(
            "queue",
            "source_aborted",
            job=job_id,
            anime=title,
            code=code,
        )

    async def recover_on_startup(self) -> None:
        """At process start NOTHING is downloading yet, so any job left RUNNING/PAUSED
        was orphaned by a crash or kill. Re-queue it so the worker resumes (the loop
        skips already-downloaded episodes), and clear its stale live-progress so
        ACTIVE TASKS reflects reality instead of a phantom 'downloading' row.

        When resume is disabled, orphans are failed outright rather than left hanging.
        """
        async with session_scope(self._c.pg_sessionmaker) as session:
            repo = QueueRepository(session)
            orphaned = await repo.by_status(JobStatus.RUNNING, JobStatus.PAUSED)
            ids = [j.id for j in orphaned]
            resume = self._c.config.downloads.resume_interrupted
            for job in orphaned:
                if resume:
                    job.status = JobStatus.QUEUED          # picked up + resumed
                else:
                    job.status = JobStatus.FAILED
                    job.error = "interrupted (worker restarted)"
        for jid in ids:
            if self._c.progress:
                await self._c.progress.delete(jid)
            await self._clear_skip(jid)
            await self._clear_cancel(jid)
            await self._clear_source_abort(jid)
        if ids:
            log.info("download.recover", orphaned=ids, resume=resume)

    async def _has_recorded_files(self, job_id: int) -> bool | None:
        """Did this job record ANY MediaFile rows?

        Returns:
        - True   → at least one MediaFile row exists for the job.
        - False  → zero MediaFile rows — every attempt failed; the pipeline
                   must NOT advance past downloads (it would otherwise run
                   processing → upload → publish on nothing and create an
                   empty bot).
        - None   → the DB check itself failed (transient infra blip);
                   treat as unknown and let the caller proceed rather than
                   fake-fail the job.

        Extracted from :meth:`_process_job` so it can be unit-tested in
        isolation (mock the session_scope / SQL execution) without spinning
        up a real Container + Postgres + Redis stack.
        """
        try:
            async with session_scope(self._c.pg_sessionmaker) as _sess:
                return (await _sess.execute(
                    select(MediaFile.id)
                    .where(MediaFile.job_id == job_id).limit(1)
                )).scalars().first() is not None
        except Exception as exc:  # noqa: BLE001
            log.warning("download.has_recorded_files.check_failed", error=str(exc))
            return None

    async def _already_have(self, job_id, ep, resolution, audio) -> bool:
        """True if this exact unit is safely accounted for and needn't re-download.

        "Accounted for" means one of:
          * the file was already uploaded to storage (``published=True``), or
          * a DB row exists AND its file is still on disk.

        A DB row whose file is GONE from disk and was never uploaded is an
        orphan (e.g. a power-loss mid-encode wiped the working set before upload)
        — returning True there is what caused a resumed job to skip the
        re-download, find no files, and silently "complete" without uploading.
        In that case we return False so the unit is fetched again.
        """
        try:
            async with session_scope(self._c.pg_sessionmaker) as session:
                row = (await session.execute(
                    select(MediaFile).where(
                        MediaFile.job_id == job_id, MediaFile.season == ep.season,
                        MediaFile.episode == ep.number, MediaFile.resolution == resolution,
                        MediaFile.audio == audio,
                    )
                )).scalars().first()
                if row is None:
                    return False
                if row.published:
                    return True  # already in storage — never re-download
                local = row.local_path
            # Row exists but not uploaded → only trust it if the file survives.
            if local and await asyncio.to_thread(Path(local).exists):
                return True
            return False
        except Exception:  # noqa: BLE001 - on error, just re-download (safe) not fail
            return False

    async def _resolve_chain(self, req) -> list[tuple]:
        """Resolve a request to a priority-ordered chain of ``(source, episodes)``.

        Requests carry an AniList discovery ref (``anilist:<id>``), so we search
        each candidate source by verified title (English + Romaji) to find its
        native id, then list episodes. For a website priority chain
        (``anikoto>kickassanime``) BOTH sites are resolved when available — that is
        what lets dual-audio acquisition pull sub from one and dub from the other.
        Raises ``NotFound`` only when nothing resolves.
        """
        from nekofetch.core.exceptions import NotFound
        from nekofetch.sources._match import find_verified_match
        from nekofetch.sources.registry import _ALIASES

        fr = req.franchise_data or {}
        title = fr.get("english") or fr.get("title") or req.anime_title
        # Verify against English + Romaji so we never grab the wrong season/title.
        match_titles = [t for t in (fr.get("english") or req.anime_title,
                                    fr.get("title"), fr.get("romaji")) if t]

        # ── AniZone slug override ──
        # When the admin mapped AniZone slugs (via the slug-mapping prompt), use
        # those exact slugs to fetch episodes — bypass the AniList title match
        # which fails for AniZone's non-standard title format.
        anizone_slugs: dict | None = fr.get("_anizone_slugs")
        anizone_failed = False  # track whether the slug block already tried + failed
        if anizone_slugs and "anizone" in (req.source or ""):
            last_err = None
            try:
                src = self._c.sources.get("anizone")
                all_episodes: list = []
                for _idx, sdata in sorted(anizone_slugs.items(), key=lambda x: int(x[0])):
                    slug = sdata.get("slug", "")
                    # Strip accidental /anime/ prefix defensively.
                    slug = re.sub(r'^anime/', '', slug)
                    ep_type = sdata.get("ep_type")
                    if slug:
                        eps = await src.get_episodes(slug, ep_type=ep_type)
                        if eps:
                            all_episodes.extend(eps)
                            log.info("download.anizone_slug.resolved", slug=slug,
                                     ep_type=ep_type, episodes=len(eps))
                if all_episodes:
                    log.info("download.source.resolved", source="anizone",
                             episodes=len(all_episodes))
                    return [(src, all_episodes)]
                # Slug block ran but got 0 episodes — surface the real failure.
                last_err = "anizone: slug returned 0 episodes"
                anizone_failed = True
            except Exception as exc:
                log.warning("download.anizone_slug.failed", error=str(exc))
                last_err = f"anizone: {exc}"
                anizone_failed = True

        raw = req.source or ""
        if ">" in raw:                          # website priority chain
            names = [_ALIASES.get(tok.strip(), tok.strip()) for tok in raw.split(">")]
        else:
            try:
                names = [self._c.sources.resolve(raw).name]
            except Exception:
                names = []
        names = [
            n for n in names
            if n and n != "anilist" and not (anizone_failed and n == "anizone")
        ]

        chain: list[tuple] = []
        last_err: str | None = last_err if anizone_failed else None
        for name in names:
            try:
                src = self._c.sources.get(name)
            except Exception:
                continue
            try:
                ref = req.source_ref
                # A torrent/magnet source_ref is nyaa-specific — it must NEVER be
                # handed to a website source (kickassanime/anikoto/miruro), which
                # would try to fetch a garbage `https://…/{"magnet":…}` URL and
                # dead-end the whole chain. For any non-nyaa source, discard the
                # pinned ref and fall back to a verified title match.
                if ref and name != "nyaa" and _is_torrent_ref(ref):
                    ref = None
                if not ref or ref.startswith("anilist:"):
                    stub = await find_verified_match(src, match_titles)
                    if not stub:
                        last_err = f"{name}: no confident title match"
                        continue
                    ref = stub.source_ref
                episodes = await src.get_episodes(ref)
                if episodes:
                    log.info("download.source.resolved", source=name, episodes=len(episodes))
                    chain.append((src, episodes))
                else:
                    last_err = f"{name}: no episodes"
            except Exception as exc:  # noqa: BLE001
                log.warning("download.source.failed", source=name, error=str(exc))
                last_err = f"{name}: {exc}"
        if not chain:
            raise NotFound(f"no source could provide episodes for {title!r} ({last_err})")
        return chain

    async def _best_variant(self, chain, ep_number: int, audio, resolution: str | None = None):
        """First ``(source, variant, ep_ref)`` across the chain that offers ``audio``
        for ``ep_number`` — this is what enables cross-source acquisition."""
        for src, eps in chain:
            match = next((e for e in eps if e.number == ep_number), None)
            if not match:
                continue
            try:
                variants = await src.get_variants(match.source_ref)
            except Exception:
                continue
            if resolution:
                v = next(
                    (x for x in variants if x.audio == audio and x.resolution == resolution),
                    None,
                )
                if v is None:
                    continue
            else:
                v = next((x for x in variants if x.audio == audio), None)
            if v is not None:
                return src, v, match.source_ref
        return None

    async def _acquire_dual(self, chain, req, ep, resolution, folder, job_id, cfg) -> None:
        """Deliver BOTH languages for an episode, in the best available shape.

        Strategy, in order of preference:
          1. sub+dub on the SAME source and the same cut → remux into one dual file;
          2. sub+dub available (possibly cross-source) → keep as separate files;
          3. only one audio available → deliver it and flag the gap to staff.
        The goal is "both languages delivered" — one file or two doesn't matter.
        """
        from nekofetch.sources._dualaudio import merge_dual
        from nekofetch.sources.base import VideoVariant

        base = self._c.env.storage_path / "work" / folder
        base.mkdir(parents=True, exist_ok=True)
        stem = f"S{ep.season:02d}E{ep.number:03d}_{resolution}"
        sub_dest = base / f"{stem}_Sub.mkv"
        dub_dest = base / f"{stem}_Dub.mkv"
        a, b, c = cfg.retry_attempts, cfg.retry_backoff_seconds, None  # retry args

        sub = await self._best_variant(chain, ep.number, AudioType.SUBBED, resolution)
        dub = await self._best_variant(chain, ep.number, AudioType.DUBBED, resolution)

        # 1) same source + same cut → one merged dual file.
        if sub and dub and sub[0] is dub[0] and hasattr(sub[0], "dual_audio_plan"):
            try:
                plan = await sub[0].dual_audio_plan(sub[2], resolution=resolution)
            except Exception:
                plan = {}
            if plan.get("mergeable"):
                sr = await self._download_with_retry(sub[0], sub[1], sub_dest, c, a, b)
                dr = await self._download_with_retry(dub[0], dub[1], dub_dest, c, a, b)
                dual_dest = base / f"{stem}_dual_audio.mkv"
                if await merge_dual(sub_dest, dub_dest, dual_dest):
                    dual_v = VideoVariant(source_ref="", resolution=resolution,
                                          audio=AudioType.DUAL_AUDIO, container="mkv")
                    size = dual_dest.stat().st_size if dual_dest.exists() else 0
                    await self._record_file(job_id, req, ep, dual_v, dual_dest, {"bytes": size})
                    sub_dest.unlink(missing_ok=True)
                    dub_dest.unlink(missing_ok=True)
                    log.info("dualaudio.merged", episode=ep.number)
                    return
                # merge failed → keep the two we already downloaded.
                await self._record_file(job_id, req, ep, sub[1], sub_dest, sr)
                await self._record_file(job_id, req, ep, dub[1], dub_dest, dr)
                return

        # 2/3) download each available audio (cross-source if needed), separately.
        got_sub = got_dub = False
        if sub:
            r = await self._download_with_retry(sub[0], sub[1], sub_dest, c, a, b)
            await self._record_file(job_id, req, ep, sub[1], sub_dest, r)
            got_sub = True
        if dub:
            r = await self._download_with_retry(dub[0], dub[1], dub_dest, c, a, b)
            await self._record_file(job_id, req, ep, dub[1], dub_dest, r)
            got_dub = True

        if got_sub and got_dub:
            log.info("dualaudio.separate", episode=ep.number,
                     sub_src=sub[0].name, dub_src=dub[0].name)
        elif got_sub or got_dub:
            await self._notify_audio_gap(req, ep, "dub" if got_sub else "sub")
        else:
            await self._notify_audio_gap(req, ep, "both")

    async def _notify_audio_gap(self, req, ep, missing: str) -> None:
        """Flag to staff that an audio track was unavailable so they can decide
        (accept the partial result, or reassign the source)."""
        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(self._c).event(
            "admin", "audio_unavailable", code=req.code, anime=req.anime_title,
            episode=ep.number, missing=missing,
        )

    def _target_audios(self, req) -> list[AudioType]:
        """Resolve the audio types (subbed/dubbed) to acquire for a request.

        When unspecified, fans out into the configured languages
        (english -> DUBBED, japanese -> SUBBED).
        """
        acq = self._c.config.acquisition
        if req.audio:
            return [req.audio]
        return [a for a in (_audio_for_language(lang) for lang in acq.languages) if a]

    def _resolutions_to_fetch(self, req, available: set[str]) -> list[str]:
        """Which resolutions to actually download for this episode.

        A request that pins a resolution gets exactly that (if the source has it).
        Otherwise we grab every mandatory target (1080p/720p/480p) the source
        offers, and for any missing target we take the first available alternate
        from its fallback ladder — so 480p degrades to 540p or 360p instead of
        being skipped. Order/dupes are preserved/removed so each tier is fetched once.
        """
        if req.resolution:
            return [req.resolution] if req.resolution in available else []
        acq = self._c.config.acquisition
        wanted: list[str] = []
        for target in acq.target_resolutions:
            pick = target if target in available else next(
                (fb for fb in acq.resolution_fallbacks.get(target, []) if fb in available),
                None,
            )
            if pick and pick not in wanted:
                wanted.append(pick)
        return wanted

    async def _download_with_retry(
        self, source, variant, dest, on_progress, attempts, backoff, on_retry=None
    ) -> dict:
        resume_state: dict | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await source.download(
                    variant, dest, on_progress=on_progress, resume_state=resume_state
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _kind, reason = _classify(exc)
                log.warning("download.retry", attempt=attempt, error=str(exc))
                resume_state = (
                    {"partial": True}
                    if self._c.config.downloads.resume_interrupted
                    else None
                )
                if attempt >= attempts:
                    raise
                # Announce the NEXT attempt (attempt+1) before backing off, so the
                # live card reflects the retry while we sleep.
                if on_retry is not None:
                    try:
                        await on_retry(attempt + 1, reason)
                    except Exception:  # noqa: BLE001 - telemetry only
                        pass
                await asyncio.sleep(backoff * attempt)
        return {}

    async def _record_file(self, job_id, req, ep, variant, dest, result) -> None:
        actual_path = result.get("path") or str(dest)
        async with session_scope(self._c.pg_sessionmaker) as session:
            # Drop any stale orphan row for this exact unit before inserting — a
            # re-download on resume (file was gone from disk, never uploaded)
            # must not leave a duplicate MediaFile behind. Only un-published rows
            # are cleared; a published row means it's already in storage and
            # _already_have would have skipped the re-download entirely.
            stale = (await session.execute(
                select(MediaFile).where(
                    MediaFile.job_id == job_id, MediaFile.season == ep.season,
                    MediaFile.episode == ep.number,
                    MediaFile.resolution == variant.resolution,
                    MediaFile.audio == variant.audio,
                    MediaFile.published.is_(False),
                )
            )).scalars().all()
            for row in stale:
                await session.delete(row)
            session.add(
                MediaFile(
                    job_id=job_id,
                    anime_doc_id=_safe_anime_doc_id(req),
                    season=ep.season,
                    episode=ep.number,
                    resolution=variant.resolution,
                    audio=variant.audio,
                    original_name=ep.title,
                    local_path=actual_path,
                    size_bytes=int(result.get("bytes", 0)),
                    checksum=result.get("checksum"),
                    container=variant.container,
                    verified=False,
                )
            )

    async def _process_and_upload_quality(
        self, job_id: int, code: str, title: str,
    ) -> None:
        """Run the processing pipeline + storage upload for ONE quality tier.

        Called after every episode of a resolution has been downloaded.  Only
        files that exist on disk are touched — files from other quality tiers
        (not yet downloaded, or already uploaded and cleaned up) are naturally
        skipped by the pipeline stages and storage upload.

        Raises ``_CancelJob`` when the pipeline detects an admin Cancel.
        """
        # ── Chunk-level clean skip: if this chunk produced no usable files ──
        # (all its episodes failed), skip processing + upload entirely. The
        # job-level fail-fast at the end of _process_job surfaces the overall
        # failure if no other chunks produced files either; otherwise the
        # remaining chunks run their own pass. This guard avoids firing noisy
        # "Verifying"/"Metadata"/"Uploading" log events on zero files.
        #
        # NOTE: `upload_to_storage` deletes local files after a successful
        # upload, so for a multi-quality series the next pass can briefly see
        # zero on-disk files even though some tiers already uploaded — that's
        # expected and the next tier's download re-populates them.
        try:
            async with session_scope(self._c.pg_sessionmaker) as _csess:
                _paths = (await _csess.execute(
                    select(MediaFile.local_path).where(MediaFile.job_id == job_id)
                )).scalars().all()
            has_files = False
            for path in _paths:
                if path and await asyncio.to_thread(Path(path).exists):
                    has_files = True
                    break
            if not has_files:
                log.warning(
                    "download.chunk_skip.no_files",
                    job_id=job_id, anime=title,
                )
                return
        except Exception as exc:  # noqa: BLE001 - DB hiccup mustn't block the pipeline
            log.warning("download.chunk_skip.check_failed", error=str(exc))

        from nekofetch.services.log_channel_service import LogChannelService
        from nekofetch.services.processing.pipeline import (
            ProcessingPipeline,
        )
        from nekofetch.services.processing.pipeline import (
            _CancelJob as PipelineCancelJob,
        )
        from nekofetch.services.publishing_service import PublishingService

        try:
            ctx = await ProcessingPipeline(self._c).run_for_job(job_id)

            if code:
                await self._push_stage(job_id, title, "Uploading", 0.0)
                await LogChannelService(self._c).event(
                    "processing", "uploading", job=job_id, anime=title,
                )
                await PublishingService(self._c).upload_to_storage(
                    code, on_progress=self._upload_progress(job_id, title),
                )

            await LogChannelService(self._c).event(
                "processing", "quality_stored", job=job_id,
                anime=title, notes=len(ctx.notes),
            )
        except PipelineCancelJob as exc:
            raise _CancelJob() from exc
        except Exception as exc:  # noqa: BLE001
            log.error("download.quality.processing_failed",
                      job_id=job_id, error=str(exc))
            raise

    async def _finalize_after_qualities(
        self, job_id: int, code: str, title: str,
    ) -> None:
        """Wrap up the job after every quality tier has been downloaded,
        processed, and uploaded.  Handles status updates, user notifications,
        and auto-publishing (when approval is not required)."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            if job is not None:
                job.progress = 100.0
            req = await RequestRepository(session).get(
                job.request_id,
            ) if job else None
            if req is not None:
                req.status = RequestStatus.PROCESSING
            user_id = req.user_id if req else None

        needs_approval = self._c.config.processing.require_approval_before_publish
        log.info("download.job.all_qualities_done", job_id=job_id)

        from nekofetch.services.log_channel_service import LogChannelService
        from nekofetch.services.notification_service import NotificationService

        if user_id:
            await NotificationService(self._c).download_complete(
                user_id, title, code,
            )
        await LogChannelService(self._c).event(
            "download", "downloaded", job=job_id, anime=title, code=code,
        )

        await LogChannelService(self._c).event(
            "processing", "complete", job=job_id, anime=title,
        )
        await self._finalize_complete(job_id)

        # Auto-publish when approval is not required.
        if not needs_approval and code:
            from nekofetch.services.publishing_service import PublishingService

            try:
                await PublishingService(self._c).publish(code)
            except Exception as exc:  # noqa: BLE001
                log.warning("download.auto_publish.failed", job_id=job_id,
                            error=str(exc))
                await LogChannelService(self._c).event(
                    "error", "auto_publish_failed", job=job_id,
                    error=str(exc)[:300],
                )

        if user_id:
            await NotificationService(self._c).processing_complete(
                user_id, title, code, needs_approval=needs_approval,
            )

        # ── Pipeline handoff hook (optional) ────────────────────────────────
        # In multi-bot deployments (Kuro Sōden) the container carries an async
        # ``on_download_complete(code, title)`` hook. The download stage owns
        # nothing past this point, so firing the hook here hands the request to
        # the next stage (distribution). Standalone NekoFetch leaves it unset.
        hook = getattr(self._c, "on_download_complete", None)
        if hook is not None and code:
            try:
                await hook(code, title)
            except Exception as exc:  # noqa: BLE001 - handoff must never fail the job
                log.warning("download.handoff_hook.failed", job_id=job_id,
                            code=code, error=str(exc))

    async def _finalize_complete(self, job_id: int) -> None:
        """Mark the job COMPLETED only after download + processing + DB upload — and
        clear its live progress so it leaves ACTIVE TASKS."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            if job is not None:
                job.status = JobStatus.COMPLETED
                job.progress = 100.0
                job.finished_at = _now()
        if self._c.progress:
            try:
                await self._c.progress.delete(job_id)
            except Exception:  # noqa: BLE001
                log.debug("download.progress_cleanup.failed", job_id=job_id)
        log.info("download.job.complete", job_id=job_id)

    async def _mark_failed(self, job_id: int, error: str) -> None:
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.error = error[:500]
        if self._c.progress:
            try:
                await self._c.progress.delete(job_id)
            except Exception:  # noqa: BLE001
                log.debug("download.progress_cleanup.failed", job_id=job_id)

    async def _push_stage(self, job_id: int, title: str, stage: str, pct: float) -> None:
        """Publish a coarse stage marker so ACTIVE TASKS shows post-download stages
        (Verifying / Metadata / Uploading …) with a bar, not just downloads."""
        if not self._c.progress:
            return
        try:
            await self._c.progress.set(ProgressSnapshot(
                job_id=job_id, status=JobStatus.RUNNING.value, progress=pct,
                stage=stage, label=title,
            ))
        except Exception:  # noqa: BLE001
            pass

    def _upload_progress(self, job_id: int, title: str):
        """Rolling-window progress callback for the storage upload — same speed/ETA
        treatment as downloads, tagged with the 'Uploading' stage."""
        st = {"last": 0.0, "win_t": time.monotonic(), "win_done": 0}

        async def on_progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - st["last"] < 0.5:
                return
            dt = max(now - st["win_t"], 1e-6)
            speed = max(done - st["win_done"], 0) / dt
            st["win_t"], st["win_done"], st["last"] = now, done, now
            pct = (done / total * 100) if total else 0.0
            eta = int((total - done) / speed) if speed > 0 else None
            if self._c.progress:
                try:
                    await self._c.progress.set(ProgressSnapshot(
                        job_id=job_id, status=JobStatus.RUNNING.value, progress=pct,
                        speed_bps=speed, downloaded_bytes=done, total_bytes=total,
                        eta_seconds=eta, stage="Uploading", label=title,
                    ))
                except Exception:  # noqa: BLE001
                    pass
        return on_progress

    async def _handle_failure(self, job_id: int, exc: Exception) -> None:
        async with session_scope(self._c.pg_sessionmaker) as session:
            job = await session.get(DownloadJob, job_id)
            if job is None:
                return
            job.status = JobStatus.FAILED
            job.error = str(exc)
            req = await RequestRepository(session).get(job.request_id)
            user_id = req.user_id if req else None
            title = req.anime_title if req else ""
            code = req.code if req else ""
        log.error("download.job.failed", job_id=job_id, error=str(exc))
        if user_id:
            from nekofetch.services.notification_service import NotificationService
            await NotificationService(self._c).download_failed(user_id, title, code, str(exc))
        from nekofetch.services.log_channel_service import LogChannelService

        logcc = LogChannelService(self._c)
        await logcc.event(
            "error", "download_failed", job=job_id, error=str(exc),
            anime=title, code=code,
        )
        await logcc.post_failure_card(code=code, title=title, stage="download", error=str(exc))
        # Clean up the request card's divider sticker — failed jobs leave them
        # orphaned in the channel otherwise.
        if code:
            try:
                await logcc.clear_request_markers(code)
            except Exception:
                pass


_WEBSITE_SOURCES = ("anikoto", "anizone", "kickassanime", "miruro")


def _provided_resolution_bucket(path: Path) -> str:
    """Classify a hand-provided episode into the release quality bucket.

    The low slot accepts 360p, 480p, or 540p and is stored as 360p so a 480p
    backup does not get re-encoded just to satisfy the label.
    """
    import re
    import subprocess

    text = path.name.lower()
    match = re.search(r"(?<!\d)(2160|1440|1080|720|540|480|360)p?(?!\d)", text)
    height = int(match.group(1)) if match else 0
    if not height:
        try:
            probe = subprocess.run(  # noqa: ASYNC221 - rare manual fallback probe
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=height", "-of", "default=nw=1:nk=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            height = int((probe.stdout or "").strip() or 0)
        except Exception:  # noqa: BLE001
            height = 0
    if height in {360, 480, 540}:
        return "360p"
    if height >= 1000:
        return "1080p"
    if height >= 650:
        return "720p"
    if height > 0:
        return f"{height}p"
    return "source"


def _alternate_source(source: str) -> str | None:
    """The other website source to offer as a switch target, or None when the
    request isn't on a website source (torrent/telegram have no peer)."""
    s = (source or "").lower()
    present = [w for w in _WEBSITE_SOURCES if w in s]
    if not present:
        return None
    primary = present[0]  # first token in a "a>b" chain is the current primary
    return next((w for w in _WEBSITE_SOURCES if w != primary), None)


def _safe_anime_doc_id(req) -> str:
    """A short identifier safe for the VARCHAR(48) ``anime_doc_id`` column.

    Priority: ``req.anime_doc_id`` (if already set and short enough) → a cleaned
    AniList id extracted from ``source_ref`` → the request code. We NEVER fall
    back to the raw ``source_ref`` because nyaa/torrent sources store a full
    JSON metadata blob there that overflows the column.
    """
    _MAX_DOC_ID_LEN = 48
    doc = getattr(req, "anime_doc_id", None)
    if doc and len(str(doc)) <= _MAX_DOC_ID_LEN:
        return str(doc)
    # Extract ``anilist:<id>`` → ``<id>`` when source_ref is that form.
    cleaned = clean_anilist_id(getattr(req, "source_ref", None))
    if cleaned and len(cleaned) <= _MAX_DOC_ID_LEN:
        return cleaned
    # Last resort: the request code (always short, always unique).
    return getattr(req, "code", None) or "unknown"


def _safe_folder(req) -> str:
    """A filesystem-safe work folder name for a request (no colons/slashes)."""
    import re

    base = _safe_anime_doc_id(req) or "work"
    return re.sub(r"[^\w.\-]+", "_", str(base)).strip("_") or "work"


def _audio_for_language(language: str) -> AudioType | None:
    return {
        "english": AudioType.DUBBED,
        "japanese": AudioType.SUBBED,
        "dual": AudioType.DUAL_AUDIO,
        "dual_audio": AudioType.DUAL_AUDIO,
        "hindi": AudioType.MULTI,
        "multi": AudioType.MULTI,
    }.get(language.lower())


def _select_variant(variants, resolution, audio, require_english_subs: bool):
    """Pick the variant matching resolution + audio, preferring English subtitles.

    Returns None when the exact combo isn't offered (so unavailable combos are skipped).
    """
    cands = list(variants)
    if resolution:
        cands = [v for v in cands if v.resolution == resolution]
    if audio is not None:
        cands = [v for v in cands if v.audio == audio]
    if not cands:
        return None
    if require_english_subs:
        with_en = [
            v for v in cands
            if not v.subtitles or any("en" in s.lower() for s in v.subtitles)
        ]
        if with_en:
            cands = with_en
    return cands[0]


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
