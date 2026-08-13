"""Lazy WRK -> Request bridge — makes a work item processable on Levi's board.

The batch/redo flows normally create a ``WorkItem`` (WRK-N) *and* a bridged
``Request`` (REQ-N) plus a Levi assignment in one go. That bridge can fail
silently (a swallowed exception leaves the WRK row with no request), and works
created before the bridge existed are orphaned by definition. This module is the
safety net: when an admin opens a WRK code from Levi's task list it resolves the
durable request — reusing an existing non-terminal request for the same anime
when one exists (legacy reconciliation), otherwise creating the bridge fresh —
so the shared review/download flow always has a real request to work with.
"""

from __future__ import annotations

from nekofetch.core.logging import get_logger

log = get_logger(__name__)

# Request statuses that mean the title's pipeline is done — never reuse one.
_TERMINAL_STATUSES = ("published", "rejected", "failed")


async def ensure_work_request(container, work_code: str, admin_id: int):
    """Resolve ``work_code`` (WRK-N) to its bridged Request, bridging on demand.

    Steps, in order:

    1. Load the work item; unknown codes return ``None``.
    2. If it already carries a ``request_code`` link, return that request.
    3. Legacy reconcile: link to the newest non-terminal request for the same
       anime (by ``anime_doc_id`` or franchise ``anilist_id``) when one exists.
    4. Fresh bridge: create a QUEUED ``Request`` mirroring the batch bridge
       (owner's User row resolved by telegram id), link the work item to it,
       claim the work for ``admin_id``, and assign the Levi stage so it shows
       up like any other download task.

    Returns the Request row (detached) or ``None``. Best-effort throughout: a
    partial failure never crashes the caller — the work simply stays unbridged
    and can be retried by opening it again.
    """
    from sqlalchemy import select

    from nekofetch.core.constants import REQUEST_PREFIX
    from nekofetch.domain.enums import DownloadScope, RequestStatus
    from nekofetch.infrastructure.database.postgres.models import Request
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.repositories.request_repo import RequestRepository
    from nekofetch.infrastructure.repositories.user_repo import UserRepository
    from kurosoden.shared.admin_assignment import AdminAssignmentEngine
    from kurosoden.shared.work_service import WorkService

    svc = WorkService(container.pg_sessionmaker)
    work = await svc.get(work_code)
    if work is None:
        return None

    async with session_scope(container.pg_sessionmaker) as session:
        repo = RequestRepository(session)

        # 2. Already linked.
        if work.request_code:
            req = await repo.get_by_code(work.request_code)
            return req

        # 3. Legacy: reuse a live request for the same anime.
        doc = work.anime_doc_id
        if not doc:
            aid = (work.franchise_data or {}).get("anilist_id")
            doc = f"{aid}" if aid is not None else None
        if doc:
            existing = (
                await session.execute(
                    select(Request).where(
                        Request.anime_doc_id == doc,
                        Request.status.notin_(_TERMINAL_STATUSES),
                    ).order_by(Request.id.desc()).limit(1)
                )
            ).scalars().first()
            if existing is not None:
                await svc.link(work_code, existing.code, _session=session)
                await svc.claim(work_code, admin_id, _session=session)
                await session.commit()
                return existing

        # 4. Fresh bridge — mirror the batch/redo bridge exactly.
        submitter = await UserRepository(session).get_or_create(
            admin_id, username=None, first_name=None)
        await session.flush()
        seq = await repo.next_sequence()
        code = f"{REQUEST_PREFIX}-{seq}"
        fr = dict(work.franchise_data or {})
        req = Request(
            code=code,
            user_id=submitter.id,
            anime_doc_id=work.anime_doc_id,
            anime_title=work.anime_title,
            source="",            # batch-style: admin picks the source at review
            source_ref="",
            scope=DownloadScope.ENTIRE_SERIES.value,
            season=None,
            episodes=None,
            franchise_data=fr,
            status=RequestStatus.QUEUED,
        )
        await repo.add(req)
        await session.flush()
        await svc.link(work_code, code, _session=session)
        await svc.claim(work_code, admin_id, _session=session)
        await session.flush()

    # Assign the Levi stage outside the creation transaction (matching the
    # single-request path): one failure never rolls back the bridge itself, and
    # the assignment row is what makes it visible on the downloader's board.
    try:
        result = await AdminAssignmentEngine(container.pg_sessionmaker).assign(
            code, "levi", preferred_admin=admin_id)
        if result is None:
            from kurosoden.shared.management_service import ManagementService

            await ManagementService(container.pg_sessionmaker).reassign(
                code, "levi", admin_id)
    except Exception as exc:  # noqa: BLE001 — recovery sweep still catches it
        log.warning("work_bridge.assign_failed", code=code, error=str(exc)[:200])

    async with session_scope(container.pg_sessionmaker) as session:
        return await RequestRepository(session).get_by_code(code)
