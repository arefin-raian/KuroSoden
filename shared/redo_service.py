"""Redo service — owner-triggered re-processing of an existing / published series.

The owner runs Lelouch's ``/redo`` command, picks a title, and confirms. Unlike a
normal request (which the dedup guard refuses when the anime already exists), a
redo is the deliberate exception: it re-runs the pipeline for corrupted files or a
bug that shipped before a fix.

The behaviour is **stage-aware** (see :meth:`RedoService.detect_state`):

* ``PUBLISHED`` — the distribution channel + posts exist. KEEP them; only wipe the
  storage packs + files + jobs, re-run, and RELINK the fresh packs' quality
  buttons into the existing season cards in place (``franchise_data["redo_relink"]``
  → :meth:`SenkuPublisher.relink_packs_in_place` at publish time).
* ``CHANNEL_WITHOUT_POSTS`` — the channel anchor remains but its posts are gone.
  Relinking would edit messages that no longer exist, so the redo full-wipes and
  re-queues WITHOUT ``redo_relink``: the normal Senku distribution request
  rebuilds the channel content from scratch.
* ``IN_PROGRESS_PREPUBLISH`` — mid-download / encode / thumbnail, no channel yet.
  Terminate the in-flight job, FULL wipe, notify whoever was on it, re-run fresh.
* ``ABSENT`` — nothing on record; behaves like a fresh work item.

This service is intentionally thin orchestration over existing primitives
(``RequestService.purge_all_for_anime``, ``QueueService.cancel``, the batch
work→request→assign bridge) so the Lelouch handler stays declarative and the
decisions are unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)


class RedoState(str, Enum):
    """Where the target anime sits in the pipeline — decides the redo strategy."""

    PUBLISHED = "published"                     # channel + posts exist → relink in place
    CHANNEL_WITHOUT_POSTS = "channel_without_posts"  # channel exists, posts gone → rebuild via Senku
    IN_PROGRESS_PREPUBLISH = "in_progress"      # mid-pipeline, no channel → full wipe
    ABSENT = "absent"                           # nothing on record → fresh work


@dataclass(slots=True)
class RedoPlan:
    """The detected state + everything the submit step needs to act on it."""

    state: RedoState
    anime_doc_id: str
    codes: list[str] = field(default_factory=list)      # existing request codes for the anime
    running_job_ids: list[int] = field(default_factory=list)  # jobs to cancel
    owner_admin_id: int | None = None                   # admin currently on the task (notify)
    current_stage: str | None = None                    # levi / senku / gojo
    published_season_ids: list[int] = field(default_factory=list)  # anilist ids already in a channel
    new_season_ids: list[int] = field(default_factory=list)

    @property
    def keep_channel(self) -> bool:
        """Published redo keeps the channel + posts; everything else full-wipes.

        ``CHANNEL_WITHOUT_POSTS`` full-wipes too: the channel's posts are gone,
        so the durable rows are stale and the pipeline must rebuild the
        distribution from a clean slate (a fresh Senku distribution request)."""
        return self.state is RedoState.PUBLISHED

    @property
    def recreate_distribution(self) -> bool:
        """True when the redo must rebuild the distribution posts through Senku."""
        return self.state is RedoState.CHANNEL_WITHOUT_POSTS


@dataclass(slots=True)
class MetadataRefreshResult:
    """What the post-redo metadata refresh (Task O) detected and changed.

    ``relinked_only`` is the quality-only-redo signal: True means no fact
    changed, so nothing was re-rendered or pushed — the relink that already
    ran was the only surface work needed.
    """

    changed_entries: list[int] = field(default_factory=list)   # entry anilist ids re-rendered
    main_changed: bool = False                                 # main-post thumbnail re-rendered
    captions_updated: int = 0                                  # live captions edited in place
    title_refreshed: bool = False                              # distribution channel renamed
    relinked_only: bool = True                                 # True → nothing re-rendered


class RedoService:
    def __init__(self, container: Container) -> None:
        self._c = container

    # ── state detection ──────────────────────────────────────────────────────

    async def detect_state(self, anime_doc_id: str) -> RedoPlan:
        """Classify the anime's pipeline state and gather the ids submit() needs.

        Published is decided by a durable ``DistributionBot`` channel OR a
        ``ChannelPost`` with a ``main_message_id`` — either means posts are live and
        must be preserved. Otherwise, any request row / running job means it's
        mid-pipeline; nothing at all means ABSENT.
        """
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import (
            ChannelLayout,
            ChannelPost,
            DistributionBot,
            DownloadJob,
            Request,
        )
        from nekofetch.infrastructure.database.postgres.session import session_scope

        codes: list[str] = []
        running: list[int] = []
        published = False
        published_ids: list[int] = []

        async with session_scope(self._c.pg_sessionmaker) as session:
            reqs = (await session.execute(
                select(Request).where(Request.anime_doc_id == anime_doc_id)
            )).scalars().all()
            codes = [r.code for r in reqs]
            req_ids = [r.id for r in reqs]

            if req_ids:
                jobs = (await session.execute(
                    select(DownloadJob).where(DownloadJob.request_id.in_(req_ids))
                )).scalars().all()
                # RUNNING/QUEUED/PAUSED jobs are in-flight and must be cancelled.
                from nekofetch.domain.enums import JobStatus
                running = [j.id for j in jobs if j.status in {
                    JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.PAUSED,
                }]

            bot = (await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.is_channel.is_(True),
                )
            )).scalar_one_or_none()
            post = (await session.execute(
                select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
            )).scalar_one_or_none()
            # "Posts exist" is decided by the durable card layout: the channel
            # anchor alone is not enough, because an admin may have deleted the
            # channel's content — in that case the redo must REBUILD the
            # distribution via Senku instead of relinking into missing posts.
            cards_exist = False
            if bot is not None:
                rows = (await session.execute(
                    select(ChannelLayout).where(
                        ChannelLayout.channel_bot_id == bot.id,
                        ChannelLayout.anilist_id.is_not(None),
                    )
                )).scalars().all()
                published_ids = sorted({
                    int(r.anilist_id) for r in rows if r.anilist_id is not None
                })
                cards_exist = any(r.tg_message_id is not None for r in rows)
            channel_exists = bot is not None and bool(bot.chat_id)

        if channel_exists and cards_exist:
            state = RedoState.PUBLISHED
        elif channel_exists:
            # Channel anchor remains but its cards are gone → recreate the
            # distribution posts through the normal Senku flow.
            state = RedoState.CHANNEL_WITHOUT_POSTS
        elif post is not None and post.main_message_id:
            # Main-channel post live but no distribution channel — keep it and
            # let the relink no-op (legacy edge, unchanged behaviour).
            state = RedoState.PUBLISHED
        elif codes or running:
            state = RedoState.IN_PROGRESS_PREPUBLISH
        else:
            state = RedoState.ABSENT

        owner_admin_id, current_stage = await self._current_owner(codes)
        return RedoPlan(
            state=state, anime_doc_id=anime_doc_id, codes=codes,
            running_job_ids=running, owner_admin_id=owner_admin_id,
            current_stage=current_stage, published_season_ids=published_ids,
        )

    async def _current_owner(self, codes: list[str]) -> tuple[int | None, str | None]:
        """Find the admin (and stage) currently holding any of these request codes,
        so submit() can DM them that their task is being redone. Best-effort."""
        if not codes:
            return None, None
        try:
            from sqlalchemy import select

            from kurosoden.shared.admin_assignment import (
                ACTIVE_STATUSES,
                AdminAssignment,
            )
            from nekofetch.infrastructure.database.postgres.session import session_scope

            async with session_scope(self._c.pg_sessionmaker) as session:
                row = (await session.execute(
                    select(AdminAssignment).where(
                        AdminAssignment.request_code.in_(codes),
                        AdminAssignment.status.in_(ACTIVE_STATUSES),
                    ).order_by(AdminAssignment.updated_at.desc())
                )).scalars().first()
            if row is not None:
                return row.admin_telegram_id, row.stage
        except Exception as exc:  # noqa: BLE001 — notify is best-effort
            log.debug("redo.owner_lookup_failed", error=str(exc)[:200])
        return None, None

    # ── submit ───────────────────────────────────────────────────────────────

    async def submit(
        self, owner_id: int, title: str, anime_doc_id: str, franchise_data: dict,
    ) -> RedoPlan:
        """Run the full redo: detect → terminate → notify → clean → re-queue.

        Returns the :class:`RedoPlan` (state + new-season info) so the caller can
        render an accurate receipt. Never raises for a partial-cleanup hiccup — the
        re-queue always runs so the work isn't lost.
        """
        plan = await self.detect_state(anime_doc_id)

        # 1. Terminate any in-flight download so it can't keep writing files we're
        #    about to purge (worker aborts at its next checkpoint).
        if plan.running_job_ids:
            from nekofetch.services.queue_service import QueueService
            qs = QueueService(self._c)
            for jid in plan.running_job_ids:
                try:
                    await qs.cancel(jid)
                except Exception as exc:  # noqa: BLE001
                    log.warning("redo.cancel_failed", job_id=jid, error=str(exc)[:200])

        # 2. Notify whoever was on the task (before we wipe their assignment).
        await self._notify_current_owner(plan, title)

        # 3. Clean per plan — published keeps the channel/posts, else full wipe.
        #    For a published redo we DEFER deleting the old storage messages: the
        #    pack rows go (so the fresh re-download inserts cleanly), but the
        #    channel messages stay live so the existing posts' quality buttons
        #    keep working during the re-download. The captured refs ride on
        #    franchise_data so the download finalizer deletes them right after the
        #    new packs upload (then relinks).
        deferred_messages: list = []
        try:
            from nekofetch.services.request_service import RequestService
            result = await RequestService(self._c).purge_all_for_anime(
                anime_doc_id, keep_channel=plan.keep_channel,
                defer_pack_messages=plan.keep_channel,
            )
            deferred_messages = (result or {}).get("deferred_messages") or []
        except Exception as exc:  # noqa: BLE001 — never block the re-queue on cleanup
            log.error("redo.purge_failed", anime=anime_doc_id, error=str(exc)[:300])

        # 4. Re-queue as work (WorkItem + QUEUED Request + levi assignment), tagging
        #    franchise_data so the pipeline knows this is a redo.
        fr = dict(franchise_data or {})
        fr["redo"] = True
        if plan.keep_channel:
            # Published: publish step must RELINK the fresh packs into the existing
            # season cards instead of creating a new channel / reposting.
            fr["redo_relink"] = True
            fr["redo_anime_doc_id"] = anime_doc_id
            # Old storage messages to delete AFTER the new upload (see step 3).
            if deferred_messages:
                fr["redo_old_messages"] = deferred_messages
        await self._requeue_as_work(owner_id, title, anime_doc_id, fr)

        # 5. Detect entries discovered during redo that are not already on the
        # channel. The handler presents the owner with a Yes/No choice (K5); the
        # normal update request remains separate so choosing No is safe.
        plan.new_season_ids = await self._detect_new_seasons(
            plan, franchise_data,
        )

        return plan

    async def _requeue_as_work(
        self, owner_id: int, title: str, anime_doc_id: str, franchise_data: dict,
    ) -> str | None:
        """Create a WorkItem + QUEUED Request + levi assignment (batch bridge).

        Returns the new request code, or None on failure. Mirrors the batch commit
        bridge so the redo appears on Levi's board exactly like other work."""
        try:
            from nekofetch.core.constants import REQUEST_PREFIX
            from nekofetch.domain.enums import DownloadScope, RequestStatus
            from nekofetch.infrastructure.database.postgres.models import Request
            from nekofetch.infrastructure.database.postgres.session import session_scope
            from nekofetch.infrastructure.repositories.request_repo import (
                RequestRepository,
            )
            from nekofetch.infrastructure.repositories.user_repo import UserRepository
            from kurosoden.shared.admin_assignment import AdminAssignmentEngine
            from kurosoden.shared.management_service import ManagementService
            from kurosoden.shared.owner_seed import _owner_id
            from kurosoden.shared.work_service import WorkService
            from types import SimpleNamespace

            created_works = await WorkService(self._c.pg_sessionmaker).add_batch(
                owner_id,
                [{"anime_title": title, "anime_doc_id": anime_doc_id,
                  "franchise_data": franchise_data}],
            )

            async with session_scope(self._c.pg_sessionmaker) as session:
                repo = RequestRepository(session)
                # ``owner_id`` is a TELEGRAM id, but ``Request.user_id`` FKs
                # ``users.id`` (the internal PK). Resolve/create the owner's User
                # row and use its ``.id`` — passing the telegram id straight through
                # violates the FK and the whole redo silently fails to appear.
                owner_user = await UserRepository(session).get_or_create(
                    owner_id, username=None, first_name=None)
                await session.flush()
                seq = await repo.next_sequence()
                code = f"{REQUEST_PREFIX}-{seq}"
                req = Request(
                    code=code,
                    user_id=owner_user.id,
                    anime_doc_id=anime_doc_id,
                    anime_title=title,
                    source="",
                    source_ref="",
                    scope=DownloadScope.ENTIRE_SERIES.value,
                    season=None,
                    episodes=None,
                    franchise_data=franchise_data,
                    status=RequestStatus.QUEUED,
                )
                await repo.add(req)
                await session.flush()

            # Link the work item to its bridged request so the pipeline advances
            # the WRK row in step with this request (AdminAssignmentEngine
            # .complete_task), and the queue counts stop treating an in-flight
            # work as "open at download".
            if created_works:
                try:
                    await WorkService(self._c.pg_sessionmaker).link(
                        created_works[0].code, code,
                    )
                except Exception as exc:  # noqa: BLE001 — board visibility is unaffected
                    log.warning("redo.work_link_failed", code=code,
                                error=str(exc)[:200])

            try:
                result = await AdminAssignmentEngine(
                    self._c.pg_sessionmaker).assign(code, "levi")
                if result is None:
                    owner = _owner_id(self._c)
                    if owner is not None:
                        await ManagementService(self._c.pg_sessionmaker).reassign(
                            code, "levi", owner)
                        # ``reassign`` writes the row but returns nothing; synthesize
                        # the same shape ``assign`` would so the DM below still fires.
                        result = SimpleNamespace(
                            admin_telegram_id=owner, status="assigned",
                            assignment_mode="fallback")
                # The redo lands on Levi's board, but assign()/reassign() only write
                # the DB row — without this the assigned admin never gets told their
                # task was reopened (the exact "no assignment message" redo bug). Reuse
                # the same DM primitive the normal + batch intake use.
                if result is not None:
                    try:
                        from kurosoden.shared.handoff import notify_stage_assignment
                        await notify_stage_assignment(
                            self._c, "levi", result, code, title,
                            franchise_json=franchise_data,
                        )
                    except Exception as exc:  # noqa: BLE001 — DM best-effort
                        log.warning("redo.notify_failed", code=code,
                                    error=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001 — recovery sweep still catches it
                log.warning("redo.assign_failed", code=code, error=str(exc)[:200])
            return code
        except Exception as exc:  # noqa: BLE001
            log.error("redo.requeue_failed", anime=anime_doc_id, error=str(exc)[:300])
            return None

    # ── notifications ────────────────────────────────────────────────────────

    async def _notify_current_owner(self, plan: RedoPlan, title: str) -> None:
        """DM the admin who was working the task that it's being redone. Sent
        through the stage's own bot when possible (levi/senku/gojo), else Lelouch.
        Best-effort — a failed DM never blocks the redo."""
        if plan.owner_admin_id is None:
            return
        try:
            from kurosoden.shared import lelouch_voice as V
            mgr = getattr(self._c, "pipeline_manager", None)
            client = None
            if mgr is not None:
                client = (getattr(mgr, plan.current_stage or "", None)
                          or getattr(mgr, "levi", None)
                          or getattr(mgr, "lelouch", None))
            if client is None:
                return
            await client.send_message(
                plan.owner_admin_id, V.redo_task_notice(title, plan.current_stage),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("redo.notify_owner_failed", error=str(exc)[:200])

    async def _detect_new_seasons(
        self, plan: RedoPlan, franchise_data: dict,
    ) -> list[int]:
        """Compare the franchise's TV seasons to those already in the channel.

        Returns the AniList ids of seasons the franchise now has that the
        channel does not (K5). Detection only — no DM: the owner gets an
        explicit Yes/No choice from the handler instead.
        """
        if plan.state is not RedoState.PUBLISHED or not plan.published_season_ids:
            return []
        try:
            season_ids = self._franchise_tv_ids(franchise_data)
            new_ids = [a for a in season_ids if a not in set(plan.published_season_ids)]
            if new_ids:
                log.info("redo.new_seasons_detected",
                         anime=plan.anime_doc_id, count=len(new_ids))
            return new_ids
        except Exception as exc:  # noqa: BLE001
            log.debug("redo.new_season_detect_failed", error=str(exc)[:200])
            return []

    async def queue_update_for_new_seasons(
        self, owner_id: int, anime_doc_id: str, franchise_data: dict,
        new_season_ids: list[int],
    ) -> int:
        """K5 Yes path — queue the newly-discovered seasons as update entries.

        Each new season becomes an ``update_entry`` request (the existing K3
        update machinery: download the entry, append its season card via
        ``update_distribution_channel`` / ``_append_and_refooter``, and Gojo
        replies to the main post). K1's per-entry episode windowing is the
        remaining edge: the entry is downloaded with the full-series scope the
        update flow already uses. Returns how many updates were queued.
        """
        from nekofetch.domain.enums import DownloadScope
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.infrastructure.repositories.user_repo import UserRepository
        from nekofetch.services.request_service import RequestService

        entries = {e["anilist_id"]: e for e in self._franchise_entries(franchise_data)}
        title = self._display_title(franchise_data, [])
        # ``RequestService.submit`` resolves the owner's internal User row and
        # raises if it is missing — create it first (same bridge the batch
        # flow uses), so a fresh admin account can still use the update path.
        async with session_scope(self._c.pg_sessionmaker) as session:
            await UserRepository(session).get_or_create(
                owner_id, username=None, first_name=None,
            )
        queued = 0
        for aid in new_season_ids:
            entry = entries.get(int(aid))
            if entry is None:
                continue
            entry_title = entry.get("title") or title or anime_doc_id
            update_fd = {
                "anilist_id": int(aid),
                "format": entry.get("format") or "TV",
                "season": entry.get("season"),
                "episodes": entry.get("episodes"),
                "english": entry_title,
                "title": title or entry_title,
                "franchise_seasons": 1,
                "update_entry": True,
            }
            try:
                await RequestService(self._c).submit(
                    telegram_id=owner_id,
                    source="",
                    source_ref=f"anilist:{aid}",
                    anime_title=entry_title,
                    scope=DownloadScope.SEASON,
                    season=int(entry.get("season") or 1),
                    anime_doc_id=anime_doc_id,
                    franchise_data=update_fd,
                )
                queued += 1
            except Exception as exc:  # noqa: BLE001 — one bad entry never blocks the rest
                log.warning("redo.update_queue_failed", entry=aid,
                            error=str(exc)[:200])
        return queued

    @staticmethod
    def _franchise_tv_ids(franchise_data: dict) -> list[int]:
        """Best-effort list of TV-season anilist ids from a franchise dict. Looks at
        the franchise walk entries if present, else the relations list."""
        out: list[int] = []
        fr = franchise_data or {}
        walk = fr.get("franchise") or fr.get("entries")
        values = walk.values() if isinstance(walk, dict) else (walk or [])
        for raw in values:
            if not isinstance(raw, dict):
                continue
            fmt = (raw.get("format") or "").upper()
            if fmt and fmt not in ("TV", "TV_SHORT"):
                continue
            aid = raw.get("anilist_id") or raw.get("id")
            if aid is not None:
                try:
                    out.append(int(aid))
                except (ValueError, TypeError):
                    continue
        return out

    # ── Task O: redo metadata refresh ───────────────────────────────────────

    @staticmethod
    def _franchise_entries(franchise_data: dict) -> list[dict]:
        """Every franchise-walk entry that has an anilist id, normalized.

        Richer than :meth:`_franchise_tv_ids` (which only collects TV ids):
        returns the entry dicts the Task O diff needs — per-entry score for the
        franchise-average rating, episode counts for the E3 summary line, and
        season/season_part for rebuilding a card caption.
        """
        out: list[dict] = []
        fr = franchise_data or {}
        walk = fr.get("franchise") or fr.get("entries")
        values = walk.values() if isinstance(walk, dict) else (walk or [])
        for raw in values:
            if not isinstance(raw, dict):
                continue
            aid = raw.get("anilist_id") or raw.get("id")
            if aid is None:
                continue
            try:
                aid = int(aid)
            except (ValueError, TypeError):
                continue
            score = raw.get("score")
            try:
                score = float(score) if score is not None else None
            except (ValueError, TypeError):
                score = None
            out.append({
                "anilist_id": aid,
                "title": raw.get("title") or raw.get("english") or "",
                "format": raw.get("format") or raw.get("kind") or "",
                "season": raw.get("season"),
                "season_part": raw.get("season_part"),
                "episodes": raw.get("episodes") or raw.get("episode_count"),
                "score": score,
            })
        return out

    @staticmethod
    def _normalize_rating(value) -> int | None:
        """Collapse every rating shape to an int on a 0-100 scale.

        ``"82%"`` → 82, ``8.2`` (AniList 0-10) → 82, ``82`` → 82, ``None`` →
        None. Lets the Task O diff compare a persisted ``anilist_score`` against
        the freshly-derived franchise average without format gymnastics.
        """
        if value is None:
            return None
        if isinstance(value, str):
            value = value.replace("%", "").strip()
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value <= 10:
            return int(round(value * 10))
        return int(round(value))

    @classmethod
    def _diff_facts(cls, stored: dict, *, rating: str, languages: str) -> set[str]:
        """Which fact keys changed between a stored ``ThumbnailSource.fields``
        dict and the freshly-derived post-redo facts.

        Compares the language label and the rating (normalized to 0-100). The
        episode-count line lives in the captions, not the thumbnail source, so
        it is covered by the caption refresh that fires on any change — no
        separate stored key is required.
        """
        changed: set[str] = set()
        stored_lang = stored.get("language") or stored.get("languages")
        if stored_lang is not None and str(stored_lang).strip() != str(languages).strip():
            changed.add("language")
        stored_rating = (
            stored.get("rating")
            or stored.get("anilist_score")
            or stored.get("tmdb_rating")
        )
        if stored_rating is not None:
            old = cls._normalize_rating(stored_rating)
            new = cls._normalize_rating(rating)
            if old is not None and new is not None and old != new:
                changed.add("rating")
        return changed

    async def _fresh_packs(self, anime_doc_id: str) -> list:
        """All current ``StoragePack`` rows for the anime (fresh after redo)."""
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import StoragePack
        from nekofetch.infrastructure.database.postgres.session import session_scope

        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (await session.execute(
                select(StoragePack).where(StoragePack.anime_doc_id == anime_doc_id)
            )).scalars().all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    async def _stored_sources(self, anime_doc_id: str) -> list:
        """All ``ThumbnailSource`` rows for the anime, detached."""
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import ThumbnailSource
        from nekofetch.infrastructure.database.postgres.session import session_scope

        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (await session.execute(
                select(ThumbnailSource).where(
                    ThumbnailSource.anime_doc_id == anime_doc_id
                ).order_by(ThumbnailSource.id)
            )).scalars().all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    @staticmethod
    def _display_title(franchise_data: dict, packs) -> str:
        """The franchise's user-facing display title (English preferred)."""
        fr = franchise_data or {}
        search = fr.get("search") or {}
        title = (
            fr.get("title")
            or fr.get("english")
            or search.get("english")
            or search.get("romaji")
        )
        if not title and packs:
            title = packs[0].anime_title
        return str(title).strip() if title else ""

    async def refresh_metadata(
        self, anime_doc_id: str, franchise_data: dict,
    ) -> "MetadataRefreshResult":
        """Task O — push post-redo metadata changes to the LIVE published surfaces.

        The redo-relink publish path only re-points the quality buttons; it
        deliberately leaves card media + captions untouched. So when a redo
        changed the title's facts (a new language line because a dual-audio
        version shipped, a new franchise-average rating, a corrected display
        title), the live cards would keep showing the stale values. That is the
        owner's ORB case: old files deleted, dual-audio uploaded, links
        relinked, and the card/main-post facts refreshed — WITHOUT re-picking
        art, WITHOUT re-notifying subscribers.

        Flow: derive the new facts via the reused helpers (E3 episode-summary
        formatter, :func:`main_channel_service._avg_score_pct`, :func:
        `main_channel_service._language_summary`), diff each stored
        ``ThumbnailSource`` row against them, and only for rows whose facts
        actually changed: re-render with the SAME user-picked art, persist,
        push via the 9.1 propagation methods, and refresh the matching live
        caption + backup. A quality-only redo (identical facts) touches nothing
        beyond the relink that already ran.
        """
        from nekofetch.services.main_channel_service import (
            _avg_score_pct,
            _language_summary,
            format_episode_summary,
        )

        entries = self._franchise_entries(franchise_data)
        scores = [e["score"] for e in entries if e.get("score") is not None]
        rating = _avg_score_pct(scores)
        packs = await self._fresh_packs(anime_doc_id)
        languages = _language_summary(packs)
        # Episode-count line — feeds the main caption re-render below (the E3
        # formatter's output, reused verbatim).
        episodes = format_episode_summary(entries)

        sources = await self._stored_sources(anime_doc_id)
        result = MetadataRefreshResult()
        if not sources:
            log.debug("redo.metadata.no_sources", anime=anime_doc_id)
            return result

        # 1. Diff every stored source against the new facts.
        main_row = None
        for row in sources:
            stored = dict(row.fields or {})
            changed = self._diff_facts(stored, rating=rating, languages=languages)
            if row.anilist_id == -1:
                main_row = row
                if changed:
                    result.main_changed = True
            elif changed:
                result.changed_entries.append(int(row.anilist_id))
        # 1b. Channel-title refresh (Vanitas case) is independent of the fact
        # diff — a corrected display title renames the channel even when the
        # rating/language lines are untouched.
        new_title = self._display_title(franchise_data, packs)
        title_changed = bool(new_title) and await self._channel_title_differs(
            anime_doc_id, new_title,
        )
        if not result.main_changed and not result.changed_entries and not title_changed:
            log.info("redo.metadata.unchanged", anime=anime_doc_id,
                     episodes=episodes, rating=rating, languages=languages)
            return result  # relinked_only stays True
        result.relinked_only = False

        # 2. Re-render + persist + push each changed surface (SAME art).
        if main_row is not None and result.main_changed:
            pushed = await self._rerender_and_push(main_row, anime_doc_id, languages)
            log.info("redo.metadata.main", anime=anime_doc_id, pushed=pushed,
                     rating=rating, languages=languages)
        for aid in list(result.changed_entries):
            row = next((r for r in sources if int(r.anilist_id) == aid), None)
            if row is None:
                continue
            pushed = await self._rerender_and_push(row, anime_doc_id, languages)
            log.info("redo.metadata.entry", anime=anime_doc_id, entry=aid,
                     pushed=pushed, languages=languages)

        # 3. Refresh captions where the episode-count/language line changed.
        if result.main_changed:
            try:
                from nekofetch.services.main_channel_service import MainChannelService

                if await MainChannelService(self._c).refresh_caption(anime_doc_id):
                    result.captions_updated += 1
            except Exception as exc:  # noqa: BLE001 - redo survives a caption hiccup
                log.warning("redo.metadata.main_caption_failed",
                            anime=anime_doc_id, error=str(exc))
        for aid in list(result.changed_entries):
            try:
                caption = await self._rebuild_entry_caption(anime_doc_id, aid, entries)
                if caption:
                    from nekofetch.services.thumbnail_channel_service import (
                        ThumbnailChannelService,
                    )
                    if await ThumbnailChannelService(self._c).refresh_published_caption(
                        anime_doc_id, aid, caption,
                    ):
                        result.captions_updated += 1
            except Exception as exc:  # noqa: BLE001 - redo survives a caption hiccup
                log.warning("redo.metadata.entry_caption_failed",
                            anime=anime_doc_id, entry=aid, error=str(exc))

        # 4. Channel-title refresh (Vanitas case) when the display title changed.
        if title_changed:
            try:
                if await self._refresh_channel_title(anime_doc_id, new_title):
                    result.title_refreshed = True
            except Exception as exc:  # noqa: BLE001
                log.warning("redo.metadata.title_failed",
                            anime=anime_doc_id, error=str(exc))
        return result

    async def _channel_title_differs(self, anime_doc_id: str, new_title: str) -> bool:
        """True when the live distribution channel's stored name differs."""
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import DistributionBot
        from nekofetch.infrastructure.database.postgres.session import session_scope

        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.enabled.is_(True),
                    DistributionBot.is_channel.is_(True),
                ).order_by(DistributionBot.id.desc())
            )).scalars().first()
            if bot is None or not bot.chat_id:
                return False
            return bool(bot.name) and bot.name != new_title

    async def _franchise_avg_ring(self, anime_doc_id: str) -> int | None:
        """Franchise-average AniList score on the 0-100 ring scale, or None.

        Same source + math as the main-post caption rating
        (``main_channel_service._avg_score_pct``): the cached AniList walk's
        per-entry ``score`` (0-10), averaged and scaled to 0-100. Cache-only,
        best-effort — used to keep a redo's main-card ring consistent with a
        fresh publish."""
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            blob = await load_cached(self._c, anime_doc_id, "anilist",
                                     anime_doc_id=anime_doc_id)
            walk = (blob or {}).get("franchise")
            if not walk:
                return None
            vals = list(walk.values()) if isinstance(walk, dict) else list(walk)
            scores = [float(e["score"]) for e in vals
                      if isinstance(e, dict) and e.get("score") is not None]
            if not scores:
                return None
            avg = sum(scores) / len(scores)
            pct = avg if avg > 10 else avg * 10
            return int(round(pct))
        except Exception:  # noqa: BLE001 — averaging is best-effort
            return None

    async def _rerender_and_push(
        self, row, anime_doc_id: str, languages: str,
    ) -> bool:
        """Re-render one stored source with fresh facts + the SAME art, persist,
        and push the corrected image to its live surface (9.1 methods)."""
        from nekofetch.services.thumbnail_service import (
            ThumbnailRenderService,
            gather_thumbnail_fields,
            persist_thumbnail_source,
            render_fields,
        )

        stored = dict(row.fields or {})
        title = stored.get("title") or anime_doc_id
        # Surface-aware, matching a fresh publish: the main-channel card (-1) uses
        # the franchise-level TMDB synopsis + franchise-average AniList ring, while
        # a distribution entry card uses its own AniList per-entry synopsis.
        is_main = int(row.anilist_id) == -1
        fresh = await gather_thumbnail_fields(
            self._c, title, anime_doc_id, prefer_anilist_synopsis=not is_main)
        merged = {
            **stored,
            **fresh,
            "title": title,
            "language": languages,
            "logo_url": stored.get("logo_url"),
            "poster_url": stored.get("poster_url"),
            "bg_url": stored.get("bg_url"),
        }
        if is_main:
            avg = await self._franchise_avg_ring(anime_doc_id)
            if avg is not None:
                merged["anilist_score"] = avg
        renderer = ThumbnailRenderService()
        try:
            image_path = await renderer.render_thumbnail(**render_fields(merged))
        finally:
            try:
                await renderer.close()
            except Exception:  # noqa: BLE001
                pass
        if not image_path:
            return False
        anilist_id = None if int(row.anilist_id) == -1 else int(row.anilist_id)
        try:
            await persist_thumbnail_source(
                self._c, anime_doc_id, anilist_id, merged, image_path=image_path,
            )
        except Exception as exc:  # noqa: BLE001 - live push still matters
            log.warning("redo.metadata.persist_failed",
                        anime=anime_doc_id, entry=anilist_id, error=str(exc))
        if anilist_id is None:
            from nekofetch.services.main_channel_service import MainChannelService

            return await MainChannelService(self._c).refresh_thumbnail(
                anime_doc_id, str(image_path),
            )
        from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService

        return await ThumbnailChannelService(self._c).refresh_published_thumbnail(
            anime_doc_id, anilist_id, str(image_path),
        )

    async def _rebuild_entry_caption(
        self, anime_doc_id: str, anilist_id: int, entries: list[dict],
    ) -> str | None:
        """Rebuild one entry card's caption through the REAL card builder.

        Reuses :meth:`BotContentService._build_season_card` (the same builder
        the full publish uses) with the stored source facts + the fresh packs,
        so the language/quality/episode lines reflect the redo's output.
        """
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import ThumbnailSource
        from nekofetch.infrastructure.database.postgres.session import session_scope

        entry = next((e for e in entries if e.get("anilist_id") == anilist_id), None)
        if entry is None:
            return None
        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (await session.execute(
                select(ThumbnailSource).where(
                    ThumbnailSource.anime_doc_id == anime_doc_id,
                    ThumbnailSource.anilist_id == anilist_id,
                )
            )).scalar_one_or_none()
            if row is None:
                return None
            stored = dict(row.fields or {})
        meta = {
            "title": stored.get("title") or entry.get("title") or anime_doc_id,
            "romaji": stored.get("romaji_title") or "",
            "genres": list(stored.get("genres") or []),
            "synopsis": stored.get("synopsis") or "",
            "score": entry.get("score"),
            "poster_url": stored.get("poster_url"),
        }
        packs = [p for p in await self._fresh_packs(anime_doc_id)
                 if p.season == entry.get("season")]
        from nekofetch.services.bot_content import BotContentService

        caption, _image = BotContentService(self._c)._build_season_card(
            meta, int(entry.get("season") or 1), packs,
            season_part=entry.get("season_part"),
        )
        return caption

    async def _refresh_channel_title(
        self, anime_doc_id: str, new_title: str,
    ) -> bool:
        """Rename the distribution channel when its display title changed, then
        sweep Telegram's auto-posted "channel name changed" service notice
        (the ``_sweep_service_notices`` pattern from the warm-search cleanup).
        Returns True when the rename happened."""
        from sqlalchemy import select

        from nekofetch.infrastructure.database.postgres.models import DistributionBot
        from nekofetch.infrastructure.database.postgres.session import session_scope

        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.enabled.is_(True),
                    DistributionBot.is_channel.is_(True),
                ).order_by(DistributionBot.id.desc())
            )).scalars().first()
            if bot is None or not bot.chat_id:
                return False
            chat_id, old_name = bot.chat_id, bot.name
            bot_id = bot.id
        if not old_name or old_name == new_title:
            return False
        mgr = getattr(self._c, "pipeline_manager", None)
        client = getattr(mgr, "senku", None) if mgr is not None else None
        client = client or getattr(self._c, "admin_client", None)
        if client is None:
            return False
        try:
            await client.edit_chat_title(chat_id, new_title)
            from kurosoden.shared.senku_publisher import SenkuPublisher

            await SenkuPublisher._sweep_service_notices(client, chat_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("redo.title_edit_failed",
                        anime=anime_doc_id, error=str(exc))
            return False
        try:
            async with session_scope(self._c.pg_sessionmaker) as session:
                bot = await session.get(DistributionBot, bot_id)
                if bot is not None:
                    bot.name = new_title
        except Exception as exc:  # noqa: BLE001
            log.warning("redo.title_persist_failed",
                        anime=anime_doc_id, error=str(exc))
        return True
