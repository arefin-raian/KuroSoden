"""Thumbnail orchestrator \u2014 bridges storage upload and bot creation.

After :class:`PublishingService.upload_to_storage` runs, the user wants the
pipeline to:

  1. Queue thumbnail generation requests in :class:`ThumbnailChannelService`
     so an admin can pick logo / poster / background assets for every season
     and extra entry.
  2. Wait for the admin to mark every workflow entry ``status='done'`` \u2014
     each one's :attr:`WorkflowEntry.thumbnail_url` is what bot/channel cards
     (info, season 01, OVA 1, watch guide, etc.) carry as their image.
  3. If thumbnails aren't ready within a configurable timeout, the admin can
     click \u201cSkip Custom Thumbnails\u201d on the thumbnail channel to fall back to
     AniList / TMDB posters. The orchestrator surfaces that on the next
     poll cycle.
  4. After completion (or skip), return the rendered-thumbnail URL map so
     :class:`BotContentService.generate_posts` can swap them in, and so
     :class:`MainChannelService.publish` can take the first season's thumb
     as its backdrop.

This service does NOT poll forever \u2014 there is a sane default timeout. The
admin explicitly hitting Skip or letting the timeout expire always yields a
fallback path so the bot factory never blocks on thumbnail work indefinitely.

All Redis ops on the orchestrator hot path go through :mod:`core.redis_safe`
so an Upstash blip can't wedge the publish pipeline (the same wedging that
was visible on the apscheduler ``LogChannelService.refresh_active`` path on
2026-07-10).
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import safe_redis_get, safe_redis_set
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.infrastructure.database.postgres.models import DistributionBot
from nekofetch.sources.telegram.anilist import FranchiseEntry

log = get_logger(__name__)

# Redis keys \u2014 same shape as ThumbnailChannelService uses for workflow state, so
# the orchestrator can poll them without a circular service dependency.
_K_WORKFLOW = "nf:thumbcc:workflow:{anime_doc_id}"
_K_SKIP = "nf:thumbcc:skip:{anime_doc_id}"
_K_DONE_AT = "nf:thumbcc:done_at:{anime_doc_id}"

DEFAULT_TIMEOUT_SEC = 600.0   # 10 min before falling back to AniList posters
DEFAULT_POLL_SEC = 10.0       # how often to re-check workflow state


class ThumbnailOrchestratorService:
    """Drives the storage->thumbnail-request->bot-creation handoff."""

    def __init__(self, container: Container) -> None:
        self._c = container

    # \u2500\u2500 public surface \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    async def request_thumbnails(
        self, anime_doc_id: str, title: str, entries: list[dict],
    ) -> None:
        """Queue per-entry thumbnail requests in the thumbnail channel.

        ``entries`` is the already-built list from :meth:`BotContentService
        ._queue_for_thumbnails` (label, format, episode count, anilist_id,
        synopsis). Forwards to :meth:`ThumbnailChannelService.add_to_queue`.

        Idempotent: if a workflow already exists for ``anime_doc_id`` and every
        entry is ``done``, add_to_queue short-circuits and skips the UI reposts
        so a re-run of the pipeline doesn't spam the thumbnail channel.
        """
        from nekofetch.services.thumbnail_channel_service import (
            ThumbnailChannelService,
        )
        await ThumbnailChannelService(self._c).add_to_queue(
            anime_doc_id, title, entries,
        )
        log.info("thumbnail.orchestrator.requested",
                 anime=anime_doc_id, entries=len(entries))

    async def wait_for_thumbnails(
        self, anime_doc_id: str, *,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        poll_sec: float = DEFAULT_POLL_SEC,
    ) -> bool:
        """Block (poll) until every WorkflowEntry is ``done``, admin skipped, or timeout.

        Returns ``True`` when fully generated, ``False`` when fallback is in
        effect (admin skip OR timeout expiry \u2014 caller should then pull AniList
        posters, not generated thumbs).
        """
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while True:
            if await self.is_complete(anime_doc_id):
                return True
            if await self.admin_skipped(anime_doc_id):
                return False
            if asyncio.get_event_loop().time() >= deadline:
                log.warning("thumbnail.orchestrator.timeout",
                            anime=anime_doc_id, timeout=timeout_sec)
                return False
            await asyncio.sleep(poll_sec)

    async def is_complete(self, anime_doc_id: str) -> bool:
        """True when every workflow entry is done (thumbnails fully generated).

        Polled every ``poll_sec`` by :meth:`wait_for_thumbnails` \u2014 a hung read
        here would block the orchestrator for the full Upstash timeout during
        a blip, blocking the whole publish pipeline. The safe wrapper caps
        the read at ``_REDIS_READ_TIMEOUT_S`` so a blip falls through to
        ``False`` and the timeout/skip branches take over gracefully.
        """
        raw = await safe_redis_get(self._c.redis,
                                    _K_WORKFLOW.format(anime_doc_id=anime_doc_id),
                                    label="thumbnail.orchestrator.is_complete")
        if not raw:
            return False
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return False
        if not data:
            return False
        return all(e.get("status") == "done" for e in data)

    async def admin_skipped(self, anime_doc_id: str) -> bool:
        """True when the admin clicked \u201cSkip Custom Thumbnails\u201d on the channel UI."""
        return bool(await safe_redis_get(self._c.redis,
                                          _K_SKIP.format(anime_doc_id=anime_doc_id),
                                          label="thumbnail.orchestrator.admin_skipped"))

    async def mark_admin_skipped(self, anime_doc_id: str) -> None:
        """Called by :class:`ThumbnailChannelService.handle_callback` when the
        admin chooses to bypass the generation step (e.g. short on time).

        ``ex=86400`` TTL preserves the original behavior: the flag auto-cleans
        after 24h so a stale skip can't trap a future re-run of the pipeline
        for the same franchise indefinitely. ``safe_redis_set`` forwards the
        TTL atomically through ``redis.set(..., ex=ex)``.
        """
        await safe_redis_set(self._c.redis,
                              _K_SKIP.format(anime_doc_id=anime_doc_id), "1",
                              label="thumbnail.orchestrator.mark_admin_skipped",
                              ex=86400)

    async def get_generated_thumbnails(
        self, anime_doc_id: str,
    ) -> dict[int | None, str]:
        """Return ``{anilist_id: thumbnail_url}`` for every entry that's done.

        The database uses ``-1`` for a mapping-only/root entry, while workflow
        entries use ``None``. Normalize that sentinel at the service boundary so
        card builders can use the actual entry id consistently.
        """
        out: dict[int | None, str] = {}
        raw = await safe_redis_get(self._c.redis,
                                    _K_WORKFLOW.format(anime_doc_id=anime_doc_id),
                                    label="thumbnail.orchestrator.get_generated")
        if not raw:
            return out
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return out
        for e in data:
            if e.get("status") != "done":
                continue
            url = e.get("thumbnail_url")
            aid = e.get("anilist_id")
            if not url or aid is None:
                continue
            try:
                normalized_aid = int(aid)
            except (TypeError, ValueError):
                # Ignore malformed legacy workflow entries rather than
                # breaking the whole publish fallback path.
                continue
            out[None if normalized_aid == -1 else normalized_aid] = url
        return out

    async def get_first_season_thumbnail(
        self, anime_doc_id: str,
    ) -> str | None:
        """Return the FIRST workflow entry's thumbnail URL.

        The main channel post mirrors the first season thumbnail (per user
        spec: "the main channel thumbnail, which is essentially the first
        season thumbnail, just the info's changed a bit").
        """
        raw = await safe_redis_get(
            self._c.redis,
            _K_WORKFLOW.format(anime_doc_id=anime_doc_id),
            label="thumbnail.orchestrator.get_first_season",
        )
        if not raw:
            return None
        try:
            workflow = json.loads(raw)
        except (TypeError, ValueError):
            return None

        # ``index`` is the admin workflow order. The root/mapping-only entry
        # uses the -1 sentinel and is useful for the info card, but it is not a
        # season; never let it become the main channel's first-season image.
        candidates = []
        for entry in workflow:
            if (
                entry.get("status") != "done"
                or not entry.get("thumbnail_url")
                or entry.get("anilist_id") in (None, -1, "-1")
            ):
                continue
            try:
                index = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            candidates.append((index, entry))
        first = min(candidates, key=lambda item: item[0], default=None)
        return first[1].get("thumbnail_url") if first else None

    # \u2500\u2500 helpers \u2500\u2500 used by :class:`ThumbnailChannelService.handle_callback` \u2500\u2500\u2500\u2500\u2500

    async def emit_completion(self, anime_doc_id: str) -> None:
        """Mark the franchise ready for Stage 2 once ALL entries are done.

        :class:`ThumbnailChannelService.handle_generate` calls this every time
        a single entry flips to ``done`` \u2014 idempotent; only the LAST entry
        triggers a Redis write (the orchestrator polls every ``poll_sec``). We
        just stamp a timestamp so operators can see "when did the franchise
        finish generating" in logs.
        """
        if not await self.is_complete(anime_doc_id):
            return
        # Gate the success log on the Set return so a Redis blip doesn't leave
        # operators with a misleading "completed" message but no underlying
        # "done_at" key. The blip branch audits the gap; the next handle_generate
        # tick will retry idempotently.
        ok = await safe_redis_set(self._c.redis,
                                    _K_DONE_AT.format(anime_doc_id=anime_doc_id),
                                    str(asyncio.get_event_loop().time()),
                                    label="thumbnail.orchestrator.emit_completion")
        if ok:
            log.info("thumbnail.orchestrator.completed", anime=anime_doc_id)
        else:
            log.info("thumbnail.orchestrator.completed_skipped", anime=anime_doc_id)
