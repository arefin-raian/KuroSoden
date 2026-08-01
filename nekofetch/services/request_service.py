"""Request service — the public request workflow.

Creates requests with human-friendly codes (``REQ-1048``), reports queue position,
and lists a user's requests. Honors the ``request_system`` feature toggle.
"""

from __future__ import annotations

from dataclasses import dataclass

from nekofetch.core.constants import REQUEST_PREFIX
from nekofetch.core.container import Container
from nekofetch.core.exceptions import FeatureDisabled, NotFound
from nekofetch.domain.enums import AudioType, DownloadScope, RequestStatus
from nekofetch.infrastructure.database.postgres.models import Request
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.infrastructure.repositories.request_repo import RequestRepository
from nekofetch.infrastructure.repositories.user_repo import UserRepository


@dataclass(slots=True)
class RequestReceipt:
    code: str
    position: int
    status: str


@dataclass(slots=True)
class RequestStats:
    """Real request counters for the Command / Board panels.

    ``pending`` deliberately means *awaiting staff review* only (status PENDING) —
    not "in the pipeline". Most requests are accepted immediately, so the headline
    figure operators actually care about is ``total``; the pipeline breakdown
    (working / published / rejected) is what fills the detailed Board.
    """

    total: int = 0
    pending: int = 0            # awaiting review (status PENDING)
    working: int = 0            # queued → downloading → processing → ready
    published: int = 0
    rejected: int = 0           # rejected (incl. duplicate-of-existing)
    failed: int = 0


class RequestService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def submit(
        self,
        *,
        telegram_id: int,
        source: str,
        source_ref: str,
        anime_title: str,
        scope: DownloadScope,
        season: int | None = None,
        episodes: list[int] | None = None,
        resolution: str | None = None,
        audio: AudioType | None = None,
        anime_doc_id: str | None = None,
        franchise_data: dict | None = None,
    ) -> RequestReceipt:
        if not self._c.config.features.request_system:
            raise FeatureDisabled("request_system")

        async with session_scope(self._c.pg_sessionmaker) as session:
            users = UserRepository(session)
            requests = RequestRepository(session)

            user = await users.get_by_telegram_id(telegram_id)
            if user is None:
                raise NotFound("user")

            seq = await requests.next_sequence()
            code = f"{REQUEST_PREFIX}-{seq}"
            req = Request(
                code=code,
                user_id=user.id,
                anime_doc_id=anime_doc_id,
                anime_title=anime_title,
                source=source,
                source_ref=source_ref,
                scope=scope.value,
                season=season,
                episodes=episodes,
                resolution=resolution,
                audio=audio,
                franchise_data=franchise_data,
                status=RequestStatus.PENDING,
            )
            await requests.add(req)
            await session.flush()
            position = await requests.pending_position(req.id)
            req.position = position
            receipt = RequestReceipt(code=code, position=position, status=req.status.value)

        from nekofetch.services.log_channel_service import LogChannelService

        logcc = LogChannelService(self._c)
        await logcc.event(
            "request", "submitted", code=code, anime=anime_title, user=telegram_id,
            scope=scope.value, season=season,
            source=source, episodes=episodes,
            franchise_seasons=franchise_data.get("franchise_seasons") if franchise_data else None,
            relations=len(franchise_data.get("relations", [])) if franchise_data else None,
        )
        # Operational control center: post an actionable request card so staff can
        # assign a source (Telegram / Website / Torrent) or reject — inline.
        from nekofetch.ui.typography import user_label

        await logcc.post_request_card(
            code=code, title=anime_title, by=user_label(user),
            scope=scope.value.replace("_", " ").title(),
        )
        return receipt

    async def list_pending(self, *, limit: int = 50) -> list[Request]:
        """Requests awaiting staff review (oldest first), detached for safe UI reads."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = await RequestRepository(session).list_by_status(
                RequestStatus.PENDING, limit=limit
            )
            for r in rows:
                session.expunge(r)
            return rows

    async def update_source(self, code: str, new_source: str) -> Request:
        """Update the source plugin assigned to a request."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            req.source = new_source
            await session.flush()
            title = req.anime_title
            session.expunge(req)

        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(self._c).event(
            "request", "source_assigned", code=code, anime=title, source=new_source
        )
        return req

    async def retry_episodes(
        self, code: str, episodes: list[int], *, new_source: str | None = None
    ) -> Request:
        """Re-queue a request for ONLY the given (previously stuck) episode numbers,
        optionally switching to a different source. The download worker filters by
        ``req.episodes``, so a fresh job re-attempts just those episodes without
        re-downloading the whole series."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            req.episodes = sorted(set(episodes)) or None
            if new_source:
                req.source = new_source
            req.status = RequestStatus.QUEUED
            await session.flush()
            title, source = req.anime_title, req.source
            session.expunge(req)
        from nekofetch.services.log_channel_service import LogChannelService
        await LogChannelService(self._c).event(
            "request", "retry", code=code, anime=title, source=source,
        )
        return req

    async def update_source_ref(self, code: str, source: str, source_ref: str) -> None:
        """Pin a request to a specific source + native ref (e.g. a chosen torrent)."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            req.source = source
            req.source_ref = source_ref

    async def reject(self, code: str) -> Request:
        """Mark a request rejected; logged to the log channel."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            req.status = RequestStatus.REJECTED
            await session.flush()
            title = req.anime_title
            session.expunge(req)

        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(self._c).event(
            "request", "rejected", code=code, anime=title
        )
        return req

    async def list_for_user(self, telegram_id: int, *, limit: int = 20) -> list[Request]:
        async with session_scope(self._c.pg_sessionmaker) as session:
            users = UserRepository(session)
            requests = RequestRepository(session)
            user = await users.get_by_telegram_id(telegram_id)
            if user is None:
                return []
            rows = await requests.list_for_user(user.id, limit=limit)
            for r in rows:
                session.expunge(r)
            return rows

    async def get(self, code: str) -> Request:
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            session.expunge(req)
            return req

    async def title_for(self, code: str) -> str:
        """Best-effort human title for a request code; falls back to the code."""
        try:
            async with session_scope(self._c.pg_sessionmaker) as session:
                req = await RequestRepository(session).get_by_code(code)
                return req.anime_title if req and req.anime_title else code
        except Exception:  # noqa: BLE001
            return code

    async def abandon(self, code: str) -> dict:
        """Tear a request all the way back down so a fresh source can be tried.

        Deletes, in order: any storage-channel packs (their channel messages +
        rows), all local work files + their DB rows, and the request's download
        jobs. The request itself is reset to PENDING (kept so its code/history
        survive). Live progress + stuck/skip/cancel flags are cleared. Returns a
        summary ``{title, files, packs}`` for the confirmation message.

        Destructive and irreversible — callers must confirm with the admin first.
        """
        from sqlalchemy import delete, select

        from nekofetch.infrastructure.database.postgres.models import (
            DownloadJob,
            MediaFile,
            StoragePack,
        )

        removed_files = 0
        removed_packs = 0
        title = code
        job_ids: list[int] = []
        work_folder: str | None = None

        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            title = req.anime_title or code
            anime_doc_id = req.anime_doc_id
            from nekofetch.core.parsing import clean_anilist_id
            doc_key = anime_doc_id or clean_anilist_id(req.source_ref)

            jobs = (await session.execute(
                select(DownloadJob).where(DownloadJob.request_id == req.id)
            )).scalars().all()
            job_ids = [j.id for j in jobs]

            files = (await session.execute(
                select(MediaFile).where(MediaFile.job_id.in_(job_ids))
            )).scalars().all() if job_ids else []

            packs = (await session.execute(
                select(StoragePack).where(StoragePack.anime_doc_id == doc_key)
            )).scalars().all() if doc_key else []

            # Purge storage-channel messages before dropping the rows, so we don't
            # orphan uploaded media in the channel.
            for pack in packs:
                await self._purge_pack_messages(pack)
                removed_packs += 1

            # Remove local work files best-effort; collect the folder to prune after.
            from pathlib import Path
            for mf in files:
                removed_files += 1
                if mf.local_path:
                    try:
                        p = Path(mf.local_path)
                        work_folder = work_folder or (p.parent.name if p.parent else None)
                        p.unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        pass

            if job_ids:
                await session.execute(delete(MediaFile).where(MediaFile.job_id.in_(job_ids)))
            for pack in packs:
                await session.delete(pack)
            for job in jobs:
                await session.delete(job)

            # Reset to PENDING so the request re-enters the source-pick flow.
            # ``source`` stays put (the column is NOT NULL); the next source pick
            # overwrites it via ``update_source``.
            req.status = RequestStatus.PENDING
            req.episodes = None
            await session.flush()

        # Prune the on-disk work directory for this title (best effort).
        try:
            import shutil
            from nekofetch.services.download_service import _safe_folder  # local import
            folder = None
            async with session_scope(self._c.pg_sessionmaker) as session:
                req = await RequestRepository(session).get_by_code(code)
                if req is not None:
                    folder = _safe_folder(req)
            if folder:
                work_dir = self._c.env.storage_path / "work" / folder
                if work_dir.exists():
                    shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

        # Clear live progress + worker flags for every job we removed.
        if self._c.redis:
            for jid in job_ids:
                try:
                    if self._c.progress:
                        await self._c.progress.delete(jid)
                    await self._c.redis.delete(
                        f"nf:job:{jid}:skip", f"nf:job:{jid}:cancel",
                        f"nf:job:{jid}:progressmsg",
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self._c.redis.delete(f"nf:stuck:{code}")
            except Exception:  # noqa: BLE001
                pass

        from nekofetch.services.log_channel_service import LogChannelService
        await LogChannelService(self._c).event(
            "admin", "abandoned", code=code, anime=title,
            files=removed_files, packs=removed_packs,
        )
        return {"title": title, "files": removed_files, "packs": removed_packs}

    async def _purge_pack_messages(self, pack) -> None:
        """Delete a storage pack's channel messages (header, files, end sticker).
        Best-effort — a missing message or disabled channel must not abort abandon."""
        client = getattr(self._c, "admin_client", None)
        if client is None or not pack.channel_id:
            return
        ids: list[int] = []
        if pack.header_message_id:
            ids.append(pack.header_message_id)
        if pack.file_message_ids:
            ids.extend(int(m) for m in pack.file_message_ids)
        elif pack.start_message_id and pack.end_message_id:
            ids.extend(range(pack.start_message_id, pack.end_message_id + 1))
        if not ids:
            return
        try:
            await client.delete_messages(pack.channel_id, ids)
        except Exception:  # noqa: BLE001
            # Fall back to one-by-one so a single un-deletable message doesn't
            # strand the rest.
            for mid in ids:
                try:
                    await client.delete_messages(pack.channel_id, mid)
                except Exception:  # noqa: BLE001
                    pass

    async def _purge_request_rows(self, session, req) -> dict:
        """Tear down everything downstream of a request row, in-session.

        Shared teardown for :meth:`delete_request` and :meth:`reassign_fresh`:
        deletes storage packs (channel messages + rows), MediaFile rows + local
        files, and DownloadJob rows for ``req``. Returns
        ``{"job_ids", "files", "packs", "work_folder"}`` for follow-up cleanup.
        Does NOT touch the Request row itself — the caller decides whether to
        delete it (full delete) or recreate a fresh one (reassign)."""
        from pathlib import Path

        from sqlalchemy import delete, select

        from nekofetch.core.parsing import clean_anilist_id
        from nekofetch.infrastructure.database.postgres.models import (
            DownloadJob,
            MediaFile,
            StoragePack,
        )

        doc_key = req.anime_doc_id or clean_anilist_id(req.source_ref)
        jobs = (await session.execute(
            select(DownloadJob).where(DownloadJob.request_id == req.id)
        )).scalars().all()
        job_ids = [j.id for j in jobs]
        files = (await session.execute(
            select(MediaFile).where(MediaFile.job_id.in_(job_ids))
        )).scalars().all() if job_ids else []
        packs = (await session.execute(
            select(StoragePack).where(StoragePack.anime_doc_id == doc_key)
        )).scalars().all() if doc_key else []

        for pack in packs:
            await self._purge_pack_messages(pack)

        removed_files = 0
        work_folder: str | None = None
        for mf in files:
            removed_files += 1
            if mf.local_path:
                try:
                    p = Path(mf.local_path)
                    work_folder = work_folder or (p.parent.name if p.parent else None)
                    p.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

        if job_ids:
            await session.execute(delete(MediaFile).where(MediaFile.job_id.in_(job_ids)))
        for pack in packs:
            await session.delete(pack)
        for job in jobs:
            await session.delete(job)

        return {
            "job_ids": job_ids, "files": removed_files,
            "packs": len(packs), "work_folder": work_folder,
        }

    async def _clear_assignments(self, session, code: str) -> None:
        """Delete the AdminAssignment rows for ``code`` so a purged/reassigned
        request leaves no dangling stage ownership. AdminAssignment keys on the
        request CODE (a string, no FK), so this cleanup is manual."""
        try:
            from sqlalchemy import delete
            from kurosoden.shared.admin_assignment import AdminAssignment
            await session.execute(
                delete(AdminAssignment).where(AdminAssignment.request_code == code)
            )
        except Exception:  # noqa: BLE001 — assignment table optional/absent
            pass

    async def _prune_work_dir(self, code: str) -> None:
        """Best-effort rmtree of a request's on-disk work folder + Redis flags."""
        try:
            import shutil
            from nekofetch.services.download_service import _safe_folder
            folder = None
            async with session_scope(self._c.pg_sessionmaker) as session:
                req = await RequestRepository(session).get_by_code(code)
                if req is not None:
                    folder = _safe_folder(req)
            if folder:
                work_dir = self._c.env.storage_path / "work" / folder
                if work_dir.exists():
                    shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    async def _clear_job_flags(self, code: str, job_ids: list[int]) -> None:
        if not self._c.redis:
            return
        for jid in job_ids:
            try:
                if self._c.progress:
                    await self._c.progress.delete(jid)
                await self._c.redis.delete(
                    f"nf:job:{jid}:skip", f"nf:job:{jid}:cancel",
                    f"nf:job:{jid}:progressmsg",
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._c.redis.delete(f"nf:stuck:{code}")
        except Exception:  # noqa: BLE001
            pass

    async def delete_request(self, code: str) -> dict:
        """Delete a request ENTIRELY — rows, files, packs, assignments — and tell
        the requester it was removed.

        Owner-only action (the caller enforces the gate). Everything downstream of
        the request is torn down (like :meth:`abandon`) AND the Request row itself
        is deleted, so it stops for every stage bot (Senku/Gojo). The original
        requester is DMed "your request was removed". Prefetched metadata under
        ``metadata/`` is intentionally left intact (it's keyed by anime_doc_id and
        cheap to reuse). Returns ``{title, files, packs, user_id}``."""
        title = code
        user_id = None
        job_ids: list[int] = []
        summary = {"files": 0, "packs": 0}
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            title = req.anime_title or code
            user_id = req.user_id
            summary = await self._purge_request_rows(session, req)
            job_ids = summary["job_ids"]
            await self._clear_assignments(session, code)
            await session.delete(req)
            await session.flush()

        await self._prune_work_dir(code)
        await self._clear_job_flags(code, job_ids)

        # Notify the original requester (resolve their telegram id from the DB user).
        await self._notify_user_removed(user_id, title, code)

        from nekofetch.services.log_channel_service import LogChannelService
        await LogChannelService(self._c).event(
            "admin", "request_deleted", code=code, anime=title,
            files=summary["files"], packs=summary["packs"],
        )
        return {"title": title, "files": summary["files"],
                "packs": summary["packs"], "user_id": user_id}

    async def reassign_fresh(self, code: str) -> dict:
        """Re-fetch a request under a BRAND-NEW ticket, discarding the old one.

        Owner-only action (the caller enforces the gate). Tears the old request all
        the way down (packs/messages, files, jobs, assignments) AND deletes the old
        Request row, then creates a NEW request (new ``REQ-####`` code) from the
        same anime/source/scope so the download pipeline restarts from scratch and
        re-assigns the ``levi`` stage. The requester is DMed the new ticket.
        Prefetched metadata (keyed by anime_doc_id) is reused. Returns
        ``{title, old_code, new_code, user_id}``."""
        from nekofetch.domain.enums import DownloadScope

        title = code
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            # Snapshot everything needed to mint the replacement before teardown.
            snap = {
                "user_id": req.user_id,
                "anime_doc_id": req.anime_doc_id,
                "anime_title": req.anime_title,
                "source": req.source,
                "source_ref": req.source_ref,
                "scope": req.scope,
                "season": req.season,
                "franchise_data": req.franchise_data,
            }
            title = req.anime_title or code
            summary = await self._purge_request_rows(session, req)
            job_ids = summary["job_ids"]
            await self._clear_assignments(session, code)
            await session.delete(req)
            await session.flush()

            # Mint the fresh request in the SAME transaction so a crash can't leave
            # the old one deleted with no replacement.
            requests = RequestRepository(session)
            seq = await requests.next_sequence()
            new_code = f"{REQUEST_PREFIX}-{seq}"
            try:
                scope_val = DownloadScope(snap["scope"])
            except (ValueError, TypeError):
                scope_val = DownloadScope.ENTIRE_SERIES
            new_req = Request(
                code=new_code,
                user_id=snap["user_id"],
                anime_doc_id=snap["anime_doc_id"],
                anime_title=snap["anime_title"],
                source=snap["source"],
                source_ref=snap["source_ref"],
                scope=scope_val.value,
                season=snap["season"],
                episodes=None,
                franchise_data=snap["franchise_data"],
                status=RequestStatus.QUEUED,
            )
            await requests.add(new_req)
            await session.flush()

        await self._prune_work_dir(code)
        await self._clear_job_flags(code, job_ids)

        # Re-assign the download stage for the new ticket (fresh offer/duty).
        try:
            from kurosoden.shared.admin_assignment import AdminAssignmentEngine
            await AdminAssignmentEngine(self._c.pg_sessionmaker).assign(new_code, "levi")
        except Exception as exc:  # noqa: BLE001 — recovery sweep will still pick it up
            from nekofetch.core.logging import get_logger
            get_logger(__name__).warning(
                "request.reassign_fresh.assign_failed", code=new_code, error=str(exc))

        await self._notify_user_requeued(snap["user_id"], title, code, new_code)

        from nekofetch.services.log_channel_service import LogChannelService
        await LogChannelService(self._c).event(
            "admin", "request_reassigned", code=code, new_code=new_code, anime=title,
        )
        return {"title": title, "old_code": code, "new_code": new_code,
                "user_id": snap["user_id"]}

    async def _telegram_id_for(self, user_id) -> int | None:
        """Resolve a DB user_id → their telegram id for a user-facing DM."""
        if user_id is None:
            return None
        try:
            from nekofetch.infrastructure.database.postgres.models import User
            async with session_scope(self._c.pg_sessionmaker) as session:
                u = await session.get(User, user_id)
                return u.telegram_id if u else None
        except Exception:  # noqa: BLE001
            return None

    async def _notify_user_removed(self, user_id, title: str, code: str) -> None:
        tid = await self._telegram_id_for(user_id)
        if tid is None:
            return
        from nekofetch.services.notification_service import NotificationService
        await NotificationService(self._c).request_removed(tid, title, code)

    async def _notify_user_requeued(self, user_id, title: str,
                                    old_code: str, new_code: str) -> None:
        tid = await self._telegram_id_for(user_id)
        if tid is None:
            return
        from nekofetch.services.notification_service import NotificationService
        await NotificationService(self._c).request_requeued(tid, title, old_code, new_code)

    async def update_franchise_data(self, code: str, data: dict) -> None:
        """Replace the franchise_data JSON blob for a request.

        Used to attach AniZone slug mappings (or other source-specific metadata)
        after the franchise map confirmation step.
        """
        async with session_scope(self._c.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            if req is None:
                raise NotFound(code)
            req.franchise_data = data

    async def list_active(self, *, limit: int = 30) -> list[Request]:
        """In-flight requests for the owner's Manage-Requests console.

        Anything past acceptance and not yet published/rejected — i.e. the
        requests an owner might want to re-fetch fresh or delete: QUEUED,
        DOWNLOADING, PROCESSING, READY, APPROVED, FAILED. Newest first, detached
        for safe UI reads."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        statuses = (
            RequestStatus.QUEUED, RequestStatus.DOWNLOADING,
            RequestStatus.PROCESSING, RequestStatus.READY,
            RequestStatus.APPROVED, RequestStatus.FAILED,
        )
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (await session.execute(
                select(Request)
                .where(Request.status.in_(statuses))
                .order_by(Request.id.desc())
                .limit(limit)
                .options(selectinload(Request.user))
            )).scalars().all()
            session.expunge_all()
            return list(rows)

    async def stats(self) -> RequestStats:
        """Real request counters, grouped for the Command / Board panels.

        One GROUP BY over ``requests`` — cheap enough to call on every panel open.
        The pipeline buckets fold the several in-flight statuses into one
        ``working`` figure so the Board reads plainly instead of listing every
        internal status.
        """
        async with session_scope(self._c.pg_sessionmaker) as session:
            by_status = await RequestRepository(session).counts_by_status()
        working_statuses = (
            RequestStatus.QUEUED, RequestStatus.DOWNLOADING,
            RequestStatus.PROCESSING, RequestStatus.READY,
            RequestStatus.APPROVED,
        )
        return RequestStats(
            total=sum(by_status.values()),
            pending=by_status.get(RequestStatus.PENDING, 0),
            working=sum(by_status.get(s, 0) for s in working_statuses),
            published=by_status.get(RequestStatus.PUBLISHED, 0),
            rejected=by_status.get(RequestStatus.REJECTED, 0),
            failed=by_status.get(RequestStatus.FAILED, 0),
        )
