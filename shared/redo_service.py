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
        """Published redo keeps the channel + posts; everything else full-wipes."""
        return self.state is RedoState.PUBLISHED


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
            published = bool(
                (bot is not None and bot.chat_id)
                or (post is not None and post.main_message_id)
            )
            if bot is not None:
                rows = (await session.execute(
                    select(ChannelLayout.anilist_id).where(
                        ChannelLayout.channel_bot_id == bot.id,
                        ChannelLayout.anilist_id.is_not(None),
                    )
                )).scalars().all()
                published_ids = sorted({int(a) for a in rows if a is not None})

        if published:
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
        try:
            from nekofetch.services.request_service import RequestService
            await RequestService(self._c).purge_all_for_anime(
                anime_doc_id, keep_channel=plan.keep_channel,
            )
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
        await self._requeue_as_work(owner_id, title, anime_doc_id, fr)

        # 5. Detect entries discovered during redo that are not already on the
        # channel. The handler can present the owner with a Yes/No choice; the
        # normal update request remains separate so choosing No is safe.
        plan.new_season_ids = await self._detect_and_notify_new_seasons(
            plan, franchise_data, owner_id,
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

            await WorkService(self._c.pg_sessionmaker).add_batch(
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

            try:
                result = await AdminAssignmentEngine(
                    self._c.pg_sessionmaker).assign(code, "levi")
                if result is None:
                    owner = _owner_id(self._c)
                    if owner is not None:
                        await ManagementService(self._c.pg_sessionmaker).reassign(
                            code, "levi", owner)
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

    async def _detect_and_notify_new_seasons(
        self, plan: RedoPlan, franchise_data: dict, owner_id: int,
    ) -> list[int]:
        """Compare the franchise's TV seasons to those already in the channel.

        Returns new AniList ids and sends the existing informational notice. The
        caller may use the returned ids to offer an explicit update choice.
        """
        if plan.state is not RedoState.PUBLISHED or not plan.published_season_ids:
            return []
        try:
            season_ids = self._franchise_tv_ids(franchise_data)
            new_ids = [a for a in season_ids if a not in set(plan.published_season_ids)]
            if not new_ids:
                return []
            from kurosoden.shared import lelouch_voice as V
            mgr = getattr(self._c, "pipeline_manager", None)
            client = getattr(mgr, "lelouch", None) if mgr else None
            if client is not None:
                await client.send_message(
                    owner_id, V.redo_new_seasons_notice(len(new_ids)))
            log.info("redo.new_seasons_detected",
                     anime=plan.anime_doc_id, count=len(new_ids))
            return new_ids
        except Exception as exc:  # noqa: BLE001
            log.debug("redo.new_season_detect_failed", error=str(exc)[:200])
            return []

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
