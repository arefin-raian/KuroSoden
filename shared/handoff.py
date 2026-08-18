"""Pipeline stage handoff and stage-specific staff notifications."""

from __future__ import annotations

import html
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.ui.components import cb

log = get_logger(__name__)


async def handoff_download_to_distribution(
    container: Container, code: str, title: str,
    *, already_relinked: bool = False,
) -> None:
    """Complete Levi and route the work to its next pipeline stage.

    Normal works are handed to Senku (distribution). Redo-relink and
    update-entry works SKIP the distribution stage entirely: the channel
    already exists, so the publish step relinks the fresh quality buttons
    (redo) or appends the new season card (update) in place.

    ``already_relinked`` means the download finalizer ran the redo relink inline
    (so the live card could show it + the merged completion card is honest); we
    then only complete the levi assignment here and skip re-publishing. After
    routing, we auto-advance the downloader to their next task card so they never
    open /tasks between jobs.
    """
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.repositories.request_repo import RequestRepository
    from nekofetch.services.publishing_service import PublishingService
    from kurosoden.shared.admin_assignment import (
        ACTIVE_STATUSES,
        AdminAssignment,
        AdminAssignmentEngine,
    )

    # A redo of a published title carries ``redo_relink``; an update entry
    # carries ``update_entry``. Both mean the distribution posts already exist
    # (or are appended in place) — never a fresh Senku channel build.
    async with session_scope(container.pg_sessionmaker) as session:
        req = await RequestRepository(session).get_by_code(code)
        fd = (req.franchise_data or {}) if req is not None else {}
        skip_distribution = bool(fd.get("redo_relink") or fd.get("update_entry"))
        # The downloader who owned this levi task — for the auto-advance card.
        row = (
            await session.execute(
                select(AdminAssignment).where(
                    AdminAssignment.request_code == code,
                    AdminAssignment.stage == "levi",
                    AdminAssignment.status.in_(ACTIVE_STATUSES),
                ).order_by(AdminAssignment.updated_at.desc())
            )
        ).scalars().first()
        levi_admin = row.admin_telegram_id if row is not None else None

    engine = AdminAssignmentEngine(container.pg_sessionmaker)
    try:
        await engine.complete_task(code, "levi")
    except Exception as exc:  # noqa: BLE001
        log.warning("handoff.levi_complete.failed", code=code, error=str(exc))

    if skip_distribution:
        is_redo = bool(fd.get("redo_relink"))
        # Redo/update: the distribution already exists. Relink/append in place —
        # unless the download finalizer already ran the redo relink inline.
        if not already_relinked:
            try:
                await PublishingService(container).publish(code)
            except Exception as exc:  # noqa: BLE001 — fall back to a manual Gojo retry
                log.warning("handoff.auto_publish.failed", code=code, error=str(exc))
                try:
                    assignment = await engine.assign(code, "gojo")
                except Exception as exc2:  # noqa: BLE001
                    log.warning("handoff.fallback_assign.failed", code=code,
                                error=str(exc2))
                    assignment = None
                if assignment is not None:
                    await notify_stage_assignment(
                        container, "gojo", assignment, code, title,
                    )
                await _advance_to_next_task(container, levi_admin, code)
                return
        # A REDO's completion is the merged "Redo complete + relinked" card the
        # Levi monitor paints — no separate DM. An UPDATE-entry has no merged
        # card, so it still gets its own "card appended" confirmation.
        if not is_redo:
            await _notify_auto_distribution_done(
                container, code, title, levi_admin, fd,
            )
        await _advance_to_next_task(container, levi_admin, code)
        return

    try:
        assignment = await engine.assign(code, "senku")
    except Exception as exc:  # noqa: BLE001
        log.warning("handoff.assign.failed", code=code, error=str(exc))
        assignment = None
    # A first-pass defer (None) usually means the only eligible admin carries a
    # stale ``skipped`` senku row (an expired offer) that blocks them for the rest
    # of the local day — so EVERY new senku task would silently vanish. Retry with
    # second_pass=True, which bypasses the skipped-block, before giving up. Without
    # this a solo operator stops receiving distribution tasks entirely.
    if assignment is None:
        try:
            assignment = await engine.assign(code, "senku", second_pass=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("handoff.assign.retry_failed", code=code, error=str(exc))
            assignment = None
    if assignment is None:
        log.info("handoff.deferred_or_unassigned", code=code, stage="senku")
    else:
        await notify_stage_assignment(container, "senku", assignment, code, title)

    # Auto-advance the downloader to their next task (all jobs, not just redo).
    await _advance_to_next_task(container, levi_admin, code)


async def _advance_to_next_task(
    container: Container, admin_id: int | None, just_done_code: str,
) -> None:
    """Push the downloader's NEXT single Levi task card, so they never open
    /tasks between jobs. No-op when the admin is unknown or the queue is empty."""
    if admin_id is None:
        return
    try:
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        active = await engine.get_active_tasks(admin_id, stage="levi")
        nxt = next(
            (a for a in active if a.request_code != just_done_code), None
        )
        if nxt is None:
            return
        next_title = await _title_for_code(container, nxt.request_code)
        await notify_stage_assignment(
            container, "levi", nxt, nxt.request_code, next_title,
        )
        log.info("handoff.auto_advance.sent", admin=admin_id,
                 code=nxt.request_code)
    except Exception as exc:  # noqa: BLE001 — advancing is best-effort, never fatal
        log.warning("handoff.auto_advance.failed", admin=admin_id, error=str(exc))


async def _title_for_code(container: Container, code: str) -> str:
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.repositories.request_repo import RequestRepository

    try:
        async with session_scope(container.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(code)
            return (req.anime_title if req else None) or code
    except Exception:  # noqa: BLE001
        return code


async def _notify_auto_distribution_done(
    container: Container,
    code: str,
    title: str,
    admin_id: int | None,
    franchise_data: dict,
) -> None:
    """Tell the downloader their redo/update completed without a Senku stage.

    Best-effort: a failed DM never affects the already-finished work.
    """
    if admin_id is None:
        return
    notifier = _stage_notifier(container, "levi")
    if notifier is None:
        return
    try:
        from kurosoden.shared import levi_voice as V

        if franchise_data.get("redo_relink"):
            text = V.redo_relinked(title, code)
        else:
            text = V.update_appended(title, code)
        await notifier.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        log.info("handoff.auto_done.notified", code=code, admin=admin_id)
    except Exception as exc:  # noqa: BLE001 — a completion DM never blocks the handoff
        log.warning("handoff.auto_done_notify.failed", code=code, error=str(exc))


async def handoff_distribution_to_publish(
    container: Container, code: str, title: str,
) -> None:
    """Complete Senku, assign Gojo, then send the publishing card."""
    try:
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        await engine.complete_task(code, "senku")
        assignment = await engine.assign(code, "gojo")
        # Same solo-operator guard as the senku handoff: a stale skipped gojo row
        # would otherwise defer every publish task silently. second_pass bypasses it.
        if assignment is None:
            assignment = await engine.assign(code, "gojo", second_pass=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("handoff.publish.assign.failed", code=code, error=str(exc))
        assignment = None
    if assignment is None:
        log.info("handoff.publish.deferred_or_unassigned", code=code, stage="gojo")
        return

    await notify_stage_assignment(container, "gojo", assignment, code, title)


async def notify_stage_assignment(
    container: Container,
    stage: str,
    assignment,
    code: str,
    title: str,
    *,
    requester: str | None = None,
    requester_id: int | None = None,
    franchise_json: dict | None = None,
) -> int:
    """Send one stage-specific assignment or offer card with this anime's backdrop."""
    admin_id = int(assignment.admin_telegram_id)
    notifier = _stage_notifier(container, stage)
    if notifier is None:
        log.warning("handoff.notify.no_notifier", code=code, stage=stage)
        return 0

    caption = _stage_caption(
        stage, assignment, code, title, requester, requester_id, franchise_json
    )
    keyboard = _stage_keyboard(stage, assignment, code)
    image = await _stage_art(container, stage, code, title, franchise_json)
    photo = str(image) if isinstance(image, Path) else image

    try:
        if photo is not None:
            await notifier.send_photo(
                admin_id, photo, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
        else:
            await notifier.send_message(
                admin_id, caption, parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
        log.info("handoff.notify.sent", code=code, stage=stage, admin=admin_id)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.warning("handoff.notify.photo_failed", code=code, stage=stage,
                    admin=admin_id, error=str(exc))
        try:
            await notifier.send_message(
                admin_id, caption, parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
            log.info("handoff.notify.sent_text", code=code, stage=stage, admin=admin_id)
            return 1
        except Exception as exc2:  # noqa: BLE001
            log.warning("handoff.notify.dm_failed", code=code, stage=stage,
                        admin=admin_id, error=str(exc2))
            return 0


def _stage_notifier(container: Container, stage: str):
    mgr = getattr(container, "pipeline_manager", None)
    if mgr is None:
        return None
    if stage == "levi":
        return getattr(mgr, "levi", None) or getattr(mgr, "lelouch", None)
    if stage == "senku":
        return getattr(mgr, "senku", None) or getattr(mgr, "levi", None)
    if stage == "gojo":
        return getattr(mgr, "gojo", None) or getattr(mgr, "senku", None)
    return None


def _stage_caption(
    stage: str,
    assignment,
    code: str,
    title: str,
    requester: str | None,
    requester_id: int | None,
    franchise_json: dict | None,
) -> str:
    escaped_title = html.escape(title or code)
    is_offer = getattr(assignment, "status", "assigned") == "offered"
    if stage == "levi":
        who = ""
        if requester_id is not None:
            who = (
                f"\n👤 <b>By</b> : {html.escape(requester or 'user')} "
                f"(<code>{requester_id}</code>)"
            )
        header = "⚔️ Levi Offer" if is_offer else "⚔️ New Download Task"
        body = (
            "You were active during quiet hours, so this is optional. "
            "Accept it or leave it for the next slot."
            if is_offer else
            "Assigned to download. Open Levi, pick the source, and cut the queue clean."
        )
        return (
            f"<blockquote><b>{header}</b>\n\n"
            f"<b>{escaped_title}</b>\n"
            f"<code>{code}</code> · {_franchise_bits(franchise_json or {})}"
            f"{who}</blockquote>\n\n"
            f"<blockquote><i>{body}</i></blockquote>"
        )
    if stage == "senku":
        header = "🧪 Senku Offer" if is_offer else "🧪 Ready for Distribution"
        body = (
            "This landed outside your slot while you were active. Accept to build it now, "
            "or reject and let the ladder move."
            if is_offer else
            "Downloaded, processed, and ready for the channel build. Open the wizard."
        )
        return f"<b>{header}</b>\n\n<b>{escaped_title}</b>\n<code>{code}</code>\n\n<i>{body}</i>"
    header = "🔮 Gojo Offer" if is_offer else "🔮 Ready to Publish"
    body = (
        "Optional publish review. Accept it if you are taking the slot now."
        if is_offer else
        f"Distribution is complete. Run <code>/publish {html.escape(code)}</code> "
        "to review the main-channel post and go live."
    )
    return f"<b>{header}</b>\n\n<b>{escaped_title}</b>\n<code>{code}</code>\n\n<i>{body}</i>"


def _stage_keyboard(stage: str, assignment, code: str) -> InlineKeyboardMarkup:
    is_offer = getattr(assignment, "status", "assigned") == "offered"
    if is_offer:
        rows = [[
            InlineKeyboardButton("Accept", callback_data=cb(stage, "offer", "accept", code)),
            InlineKeyboardButton("Reject", callback_data=cb(stage, "offer", "reject", code)),
        ]]
        if stage == "levi":
            rows.append([
                InlineKeyboardButton("⚔️ Open Offer", callback_data=cb("levi", "task", code))
            ])
        rows.append([InlineKeyboardButton("📋 Open Tasks", callback_data=cb(stage, "tasks"))])
        return InlineKeyboardMarkup(rows)
    if stage == "levi":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⚔️ Open Request", callback_data=cb("levi", "task", code))],
            [InlineKeyboardButton("📋 Open Download Tasks", callback_data=cb("levi", "tasks"))],
        ])
    elif stage == "senku":
        rows = [[
            InlineKeyboardButton(
                "🧪 Open Distribution",
                callback_data=cb("senku", "wiz", "open", code),
            ),
        ]]
    else:
        rows = [[
            InlineKeyboardButton(
                "📋 Open Publishing Tasks",
                callback_data=cb("gojo", "tasks"),
            ),
        ]]
    return InlineKeyboardMarkup(rows)


def _franchise_bits(franchise_json: dict) -> str:
    bits = []
    for key, label in (
        ("franchise_seasons", "season"),
        ("franchise_movies", "movie"),
        ("franchise_ovas", "OVA"),
    ):
        count = int(franchise_json.get(key) or 0)
        if count:
            plural = "" if count == 1 or label == "OVA" else "s"
            bits.append(f"{count} {label}{plural}")
    return " · ".join(bits) if bits else "single entry"


async def _stage_art(
    container: Container, stage: str, code: str, title: str, franchise_json: dict | None
) -> str | Path | None:
    if franchise_json:
        try:
            from nekofetch.ui.artwork import ensure_anime_art, key_for_franchise, next_anime_art

            art_title = franchise_json.get("title") or franchise_json.get("english") or title
            key = key_for_franchise(franchise_json, title=art_title)
            await ensure_anime_art(
                key,
                tmdb=container.tmdb,
                title=art_title,
                franchise=franchise_json,
                container=container,
                anime_doc_id=franchise_json.get("anime_doc_id")
                or (f"{franchise_json.get('anilist_id')}"
                    if franchise_json.get("anilist_id") else None),
            )
            return next_anime_art(key, fallback_bot=stage)
        except Exception as exc:  # noqa: BLE001
            log.warning("handoff.stage_art.failed", code=code, stage=stage, error=str(exc))
    return await _handoff_art(container, stage, code, title)


async def _handoff_art(
    container: Container, stage: str, code: str, title: str
) -> str | Path | None:
    """Resolve this anime's rotating artwork for the handoff card."""
    try:
        from nekofetch.services.request_service import RequestService
        from nekofetch.ui.artwork import ensure_anime_art, key_for_franchise, next_anime_art

        req = await RequestService(container).get(code)
        franchise = req.franchise_data or {}
        art_title = franchise.get("title") or req.anime_title or title
        key = key_for_franchise(franchise, title=art_title)
        doc_id = (franchise.get("anime_doc_id")
                  or (f"{franchise.get('anilist_id')}"
                      if franchise.get("anilist_id") else None))
        await ensure_anime_art(key, tmdb=container.tmdb, title=art_title,
                               franchise=franchise, container=container,
                               anime_doc_id=doc_id)
        return next_anime_art(key, fallback_bot=stage)
    except Exception as exc:  # noqa: BLE001
        log.warning("handoff.art.failed", code=code, error=str(exc))
        return None
