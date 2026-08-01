"""Lelouch request handlers — REUSES NekoFetch's existing request flow.

Key design principle: **We do NOT rewrite NekoFetch's AniList search logic.**
We import the module-level helper functions that the admin bot already uses
and register the same FSM-driven flow with Lelouch-specific additions:

  • Duplicate detection before accepting (dedup across main/dist/in-progress).
  • One-request-at-a-time limit for regular users (configurable).
  • Admin batch request support for staff.
  • Admin assignment to the downloader stage after submission.

The actual AniList search, franchise confirmation, TMDB enrichment, version
picker, and submission logic is ALL reused from the existing codebase.
"""

from __future__ import annotations

import html
import asyncio
from datetime import UTC

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

# ── Lelouch-specific additions ───────────────────────────────────────────────
from kurosoden.shared.admin_assignment import AdminAssignmentEngine
from kurosoden.shared.dedup import DedupService

# ── REUSED from NekoFetch's existing admin handler ───────────────────────────
# These are module-level functions in the admin bot's request handler.
# We import and use them directly — zero code duplication.
from nekofetch.bots.admin.handlers.requests import (
    _media_to_franchise_dict,
    apply_franchise_totals,
    enrich_with_tmdb,
)
from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.exceptions import NekoFetchError
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import DownloadScope, RequestStatus
from nekofetch.localization.messages import M, t
from nekofetch.ui.artwork import (
    anime_art_key,
    ensure_anime_art,
    key_for_franchise,
    next_anime_art,
    pick_artwork,
    seed_anime_art,
)
from nekofetch.ui.components import lock_buttons
from nekofetch.ui.progress import SPINNER, animate_until
from nekofetch.ui.screens import (
    Screen,
    ask_title,
    choose_version,
    confirm_franchise,
    request_received,
    retry_title,
    send_screen,
)

log = get_logger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _esc_q(text: str) -> str:
    """Escape a user-supplied query for safe inclusion in HTML captions."""
    return html.escape(text or "", quote=False)


async def _seed_anime_art(container: Container, franchise: dict,
                          search_title: str) -> None:
    """Seed the per-anime artwork pool from this franchise's TMDB gallery.

    Called once at franchise confirmation. Also persists the fetched URLs onto
    ``franchise["backdrops"]`` so the SAME art can be re-seeded downstream (Levi,
    Senku, Gojo) straight from the stored request — no extra TMDB calls needed.
    """
    key = key_for_franchise(franchise, title=search_title)
    try:
        urls = await container.tmdb.backdrops(search_title, limit=8)
    except Exception:  # noqa: BLE001 — artwork is decorative
        urls = []
    # Fold in whatever single backdrop we already resolved for the confirm card.
    single = franchise.get("_backdrop_url") or franchise.get("banner_url")
    ordered = ([single] if single else []) + [u for u in urls if u != single]
    if ordered:
        franchise["backdrops"] = ordered
        seed_anime_art(key, ordered)


STATE_NAME = "req:await_name"
STATE_FRANCHISE = "req:franchise"
LELOUCH_COMMANDS = ["start", "help", "myrequests", "admin", "settings", "batch", "cleardatabase"]


# ── Registration ─────────────────────────────────────────────────────────────


def register(client: Client, container: Container) -> None:
    """Register Lelouch's handlers on the Pyrogram client.

    Reuses NekoFetch's AniList search + franchise confirmation flow entirely,
    with Lelouch-specific dedup, rate limiting, and admin assignment layered on.
    """
    fsm = FSM(container.redis, bot="lelouch")
    dedup = DedupService(container.pg_sessionmaker)
    assignment = AdminAssignmentEngine(container.pg_sessionmaker)

    # ── /start handler (already in app.py, but register the FSM trigger too) ──
    @client.on_callback_query(filters.regex(r"^req\|new"))
    async def _new(_: Client, q: CallbackQuery) -> None:
        from kurosoden.shared.join_gate import ensure_can_request

        # Force-join gate: requesting requires channel membership (staff bypass).
        is_staff = await _is_staff(q, container)
        if not await ensure_can_request(
            client, container, q.from_user.id, q.message.chat.id,
            is_staff=is_staff, old_msg=q.message,
        ):
            await q.answer()
            return
        await fsm.set(q.from_user.id, STATE_NAME)
        screen = ask_title()
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    # ── Text handler — anime search with dedup ────────────────────────────────
    @client.on_message(
        filters.text & filters.private & ~filters.command(LELOUCH_COMMANDS)
    )
    async def _text(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, _data = await fsm.get(message.from_user.id)
        # In either state a typed message is a (new) title to look up.
        if state in (STATE_NAME, STATE_FRANCHISE):
            await _search_with_dedup(message, message.text.strip())

    # ── Lelouch's enhanced search — dedup first, then AniList ─────────────────
    async def _search_with_dedup(message: Message, query: str) -> None:
        """Check dedup before delegating to NekoFetch's AniList search."""
        user_id = message.from_user.id

        # ── 0. Force-join + global gate + one-at-a-time limit (staff bypass) ──
        if not await _is_staff(message, container):
            from kurosoden.shared import lelouch_voice as V
            from kurosoden.shared.join_gate import ensure_can_request
            from kurosoden.shared.request_gate import requests_open

            # Force-join: typing a title also requires channel membership.
            if not await ensure_can_request(
                client, container, user_id, message.chat.id, is_staff=False,
            ):
                return
            if not await requests_open(container):
                await send_screen(
                    client, message.chat.id,
                    Screen(caption=V.REQUESTS_PAUSED, image=pick_artwork("lelouch")),
                )
                return
            if await _has_pending_request(user_id, container):
                active = await _active_request_title(user_id, container)
                await send_screen(
                    client, message.chat.id,
                    Screen(caption=V.limit_reached(active),
                           image=pick_artwork("lelouch")),
                )
                return

        # ── 1. Dedup check ───────────────────────────────────────────────
        result = await dedup.check(query)
        if result.exists:
            from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            from kurosoden.shared import lelouch_voice as V

            title = result.title or query
            # Offer a jump button when the title is already reachable. Main
            # channel post first (our primary surface now); distribution bot is
            # the secondary fallback only when there's no main-channel post.
            kb = None
            if result.source == "main_channel" and result.main_channel_link:
                caption = V.already_available(title)
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📺 Open in Main Channel",
                                         url=result.main_channel_link)]])
            elif result.source == "distribution" and result.bot_username:
                caption = V.already_available(title)
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🤖 Open Distribution Bot",
                                         url=f"https://t.me/{result.bot_username}")]])
            else:
                # Already in progress for someone else — reference + stage.
                caption = V.already_requested(
                    title, result.request_code or "—",
                    result.current_stage or "in progress",
                )

            # ONE card, on that anime's own artwork.
            art_key = anime_art_key(title=title)
            await ensure_anime_art(art_key, tmdb=container.tmdb, title=title)
            image = next_anime_art(art_key, fallback_bot="lelouch")
            await send_screen(
                client, message.chat.id,
                Screen(caption=caption, image=image, keyboard=kb),
            )
            return

        # ── 2. Delegate to NekoFetch's existing AniList search ───────────
        await _search_anilist(message, query)

    async def _is_staff(message: Message, container: Container) -> bool:
        """Check if the user is staff/admin (can bypass request limits)."""
        from nekofetch.domain.enums import Role

        user = getattr(message, "nf_user", None)
        if user is None:
            return False
        role = Role(user.role) if user else Role.USER
        return role in (Role.STAFF, Role.ADMIN)

    async def _has_pending_request(user_id: int, container: Container) -> bool:
        """Check if user already has an active request."""
        from nekofetch.services.request_service import RequestService

        rows = await RequestService(container).list_for_user(user_id, limit=5)
        active_statuses = {
            RequestStatus.PENDING,
            RequestStatus.APPROVED,
            RequestStatus.QUEUED,
            RequestStatus.DOWNLOADING,
            RequestStatus.PROCESSING,
            RequestStatus.READY,
        }
        for r in rows:
            status = r.status
            if isinstance(status, str):
                try:
                    status = RequestStatus(status)
                except ValueError:
                    continue
            if status in active_statuses:
                return True
        return False

    async def _active_request_title(user_id: int, container: Container) -> str | None:
        """Title of the user's most recent still-active request, for the
        limit-reached card. Best-effort — returns None on any hiccup."""
        try:
            from nekofetch.services.request_service import RequestService
            rows = await RequestService(container).list_for_user(user_id, limit=5)
        except Exception:  # noqa: BLE001
            return None
        for r in rows:
            status = r.status
            if hasattr(status, "value"):
                status = status.value
            if str(status) not in ("published", "rejected", "failed"):
                return r.anime_title
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # AniList search — IDENTICAL to NekoFetch's admin handler logic
    # (reuses the same module-level helpers for franchise totals + TMDB)
    # ──────────────────────────────────────────────────────────────────────────
    async def _search_anilist(message: Message, query: str) -> None:
        def _frame(f: str) -> str:
            return t(M.SEARCHING, query=_esc_q(query), frame=f)

        msg = await message.reply(_frame(SPINNER[0]), parse_mode=ParseMode.HTML)

        # --- 1. Resolve the title (AniList first) ---
        media = await animate_until(msg, container.anilist.search(query), _frame)
        if media is None:
            # AniList missed. Walk the rest of the unified provider chain
            # (Jikan/MAL is already fused into container.anilist, so this covers
            # @acutebot → TMDB) and normalize to the franchise-dict shape. Both
            # this single-request flow and the batch flow share this resolver.
            from kurosoden.shared.franchise_resolver import resolve_franchise

            franchise_data = await animate_until(
                msg, resolve_franchise(container, query), _frame
            )
            if franchise_data is None:
                await msg.edit_text(
                    t(M.SEARCH_NOT_FOUND, query=_esc_q(query)),
                    parse_mode=ParseMode.HTML,
                )
                return
            franchise_data["_query"] = query
            await fsm.set(
                message.from_user.id,
                STATE_FRANCHISE,
                franchise=franchise_data,
            )
            screen = confirm_franchise(franchise_data)
            msg = await send_screen(client, message.chat.id, screen, old_msg=msg)
            return

        # --- 2. Detect adaptations via SeriesResolver ---
        resolution = await container.series_resolver.resolve(query)
        franchise_data = _media_to_franchise_dict(media)

        if resolution.multiple:
            versions = [
                {
                    "title": e.title,
                    "id": str(e.anilist_id or media.id),
                    "anilist_id": str(e.anilist_id or media.id),
                    "format": e.format,
                    "year": None,
                    "episodes": None,
                    "aliases": e.aliases,
                }
                for e in resolution.entries
            ]
            await fsm.set(
                message.from_user.id,
                "req:versions",
                versions=versions,
                query=query,
                franchise=franchise_data,
            )
            screen = choose_version(query, versions)
            msg = await send_screen(client, message.chat.id, screen, old_msg=msg)
            return

        # --- 3. Single match — franchise totals + TMDB ---
        await apply_franchise_totals(container, franchise_data)
        backdrop_path = await enrich_with_tmdb(
            container, franchise_data, media.english or query
        )
        franchise_data["_backdrop_url"] = backdrop_path
        # Seed this anime's artwork rotation — every downstream card (receipt,
        # downloader wizard, admin pings) will pull different art from here.
        await _seed_anime_art(container, franchise_data, media.english or query)

        await fsm.set(
            message.from_user.id,
            STATE_FRANCHISE,
            franchise=franchise_data,
            query=query,
        )
        screen = confirm_franchise(franchise_data, backdrop_path=backdrop_path)
        msg = await send_screen(client, message.chat.id, screen, old_msg=msg)

    # ── Version picker callbacks ──────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^ver_pick\|"))
    async def _ver_pick(_: Client, q: CallbackQuery) -> None:
        await lock_buttons(q)
        _, args = q.data.split("|", 1)
        picked_id = args
        _, data = await fsm.get(q.from_user.id)
        versions = data.get("versions", [])
        query = data.get("query", "Anime")

        chosen = next(
            (v for v in versions if str(v.get("id")) == picked_id),
            versions[0],
        )
        chosen_anilist_id = chosen.get("anilist_id") or chosen.get("id")

        try:
            refetched = await container.anilist._fetch_full(int(chosen_anilist_id))
        except (ValueError, TypeError):
            refetched = None

        if refetched:
            franchise_data = _media_to_franchise_dict(refetched)
        else:
            franchise_data = {
                "title": chosen.get("title", query),
                "anilist_id": chosen_anilist_id,
                "_source": "anilist",
            }

        franchise_data["title"] = chosen.get(
            "title", franchise_data.get("title", query)
        )
        await apply_franchise_totals(container, franchise_data)
        search_title = franchise_data.get("english") or franchise_data["title"]
        backdrop_path = await enrich_with_tmdb(container, franchise_data, search_title)
        franchise_data["_backdrop_url"] = backdrop_path
        await _seed_anime_art(container, franchise_data, search_title)

        await fsm.set(
            q.from_user.id,
            STATE_FRANCHISE,
            franchise=franchise_data,
            query=query,
        )
        screen = confirm_franchise(franchise_data, backdrop_path=backdrop_path)
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    # ── Confirmation / rejection ──────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^series_yes\|"))
    async def _confirm(_: Client, q: CallbackQuery) -> None:
        await lock_buttons(q)
        _, data = await fsm.get(q.from_user.id)
        franchise_data = data.get("franchise", {})
        query = data.get("query", franchise_data.get("title", "Anime"))
        name = q.from_user.first_name if q.from_user else ""
        await q.answer()
        await _finalize(q.message, q.from_user.id, name, franchise_data, query=query)

    @client.on_callback_query(filters.regex(r"^series_no$"))
    async def _reject(_: Client, q: CallbackQuery) -> None:
        await fsm.set(q.from_user.id, STATE_NAME)
        screen = retry_title()
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^noop$"))
    async def _noop(_: Client, q: CallbackQuery) -> None:
        await q.answer()

    # ── Finalize — submit + assign ────────────────────────────────────────────
    async def _finalize(
        card_msg: Message,
        user_id: int,
        user_name: str,
        franchise_data: dict,
        *,
        query: str,
    ) -> None:
        from nekofetch.domain.enums import RequestStatus as RS
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.infrastructure.repositories.request_repo import RequestRepository
        from nekofetch.services.request_service import RequestService

        title = franchise_data.get("title", query)
        anilist_id = franchise_data.get("anilist_id")
        source = franchise_data.get("_source", "anilist")

        franchise_json = {
            "anilist_id": anilist_id,
            "source": source,
            "query": query,
            "title": title,
            "english": franchise_data.get("english"),
            "romaji": franchise_data.get("romaji"),
            "year": franchise_data.get("year"),
            "format": franchise_data.get("format"),
            "franchise_episodes": franchise_data.get("franchise_episodes"),
            "franchise_seasons": franchise_data.get("franchise_seasons"),
            "franchise_movies": franchise_data.get("franchise_movies"),
            "franchise_ovas": franchise_data.get("franchise_ovas"),
            "franchise_onas": franchise_data.get("franchise_onas"),
            "franchise_specials": franchise_data.get("franchise_specials"),
            "relations": franchise_data.get("relations", []),
            "genres": franchise_data.get("genres", []),
            "synonyms": franchise_data.get("synonyms", []),
            # Artwork rotation — persisted so Levi/Senku/Gojo re-seed the same
            # anime-specific gallery from the stored request, no re-fetch needed.
            "backdrops": franchise_data.get("backdrops", []),
            "_backdrop_url": franchise_data.get("_backdrop_url"),
            "cover_url": franchise_data.get("cover_url"),
            "banner_url": franchise_data.get("banner_url"),
        }

        try:
            receipt = await RequestService(container).submit(
                telegram_id=user_id,
                source=source,
                source_ref=f"anilist:{anilist_id}" if anilist_id else query,
                anime_title=title,
                scope=DownloadScope.ENTIRE_SERIES,
                season=None,
                episodes=None,
                # Key the request by its AniList id so every downstream stage
                # (thumbnail, publish) resolves the SAME metadata folder the
                # prefetch writes to (folder = anime_doc_id). Without this a
                # torrent request's anime_doc_id was None and _safe_anime_doc_id
                # fell back to the request CODE — a folder the prefetch never
                # wrote — so cached AniList/TMDB art was missed and the thumbnail
                # hit TMDB live every time.
                anime_doc_id=(f"{anilist_id}" if anilist_id else None),
                franchise_data=franchise_json,
            )
        except NekoFetchError as exc:
            await fsm.set(user_id, STATE_NAME)
            await send_screen(
                client,
                card_msg.chat.id,
                Screen(caption=t(exc.message_key), image=pick_artwork("lelouch")),
                old_msg=card_msg,
            )
            return

        await fsm.clear(user_id)

        # ── Prefetch ALL metadata NOW (accepted → cache once, read forever) ──
        # The request passed every criterion, so pay the AniList/Jikan/TMDB cost
        # a single time here, off the requester's critical path, and write it to
        # the request folder. Levi/Senku/Gojo then read the cached JSON instead
        # of re-hitting the shared public APIs (which is what rate-limited us).
        # Fire-and-forget: acceptance never waits on it, and it never fails the
        # request (the service swallows every error internally).
        try:
            from nekofetch.services.metadata_prefetch import MetadataPrefetchService

            _doc_id = (f"{anilist_id}" if anilist_id else None)
            asyncio.create_task(
                MetadataPrefetchService(container).prefetch(
                    receipt.code, _doc_id, franchise_json,
                )
            )
        except Exception as exc:  # noqa: BLE001 — prefetch must never block acceptance
            log.warning("lelouch.prefetch.spawn_failed",
                        code=receipt.code, error=str(exc))

        # (Assignment + admin notification happen after; the user never waits
        # on them.) The card image swaps to a fresh recurring artwork. The
        # receipt now carries the full detail: code, who + id, when, and a
        # summarized franchise breakdown.
        from datetime import datetime
        requested_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        art_key = key_for_franchise(franchise_json, title=title)
        await ensure_anime_art(art_key, franchise=franchise_json)
        screen = request_received(
            user_name, title, queue_pos=receipt.position,
            code=receipt.code, requester_id=user_id,
            requested_at=requested_at, franchise=franchise_json,
            image=next_anime_art(art_key, fallback_bot="lelouch"),
        )
        await send_screen(client, card_msg.chat.id, screen, old_msg=card_msg)

        # ── DB assignment (records who owns the download stage) ──
        # CRITICAL: a task row MUST exist or Levi's task list (which reads
        # AdminAssignment rows) shows "no download tasks" even though the request
        # sits QUEUED — the exact bug the owner-seed + this fallback close. If
        # ``assign`` finds no qualifying admin (nobody covers "levi", all off-hours
        # or on break) it returns None and writes NO row; we then pin the task to
        # the owner via ``reassign`` (which creates a row when none exists) so the
        # work is always visible to a human and never silently dropped.
        assignment_result = None
        deferred_for_quiet_hours = False
        try:
            result = await assignment.assign(receipt.code, "levi")
            assignment_result = result
            if result is None:
                deferred_for_quiet_hours = await assignment.has_quiet_candidates("levi")
            if result is None and not deferred_for_quiet_hours:
                from kurosoden.shared.management_service import ManagementService
                from kurosoden.shared.owner_seed import _owner_id

                owner_id = _owner_id(container)
                if owner_id is not None:
                    await ManagementService(container.pg_sessionmaker).reassign(
                        receipt.code, "levi", owner_id
                    )
                    from types import SimpleNamespace

                    assignment_result = SimpleNamespace(
                        admin_telegram_id=owner_id,
                        status="assigned",
                        assignment_mode="fallback",
                    )
                    log.warning("lelouch.assign.fallback_owner",
                                code=receipt.code, owner=owner_id)
                else:
                    log.error("lelouch.assign.no_owner_no_admin", code=receipt.code)
            async with session_scope(container.pg_sessionmaker) as session:
                repo = RequestRepository(session)
                req = await repo.get_by_code(receipt.code)
                if req is not None:
                    req.status = RS.QUEUED
        except Exception as exc:  # noqa: BLE001
            log.error("lelouch.assign.failed", code=receipt.code, error=str(exc))

        # ── Notify admins DIRECTLY by DM (no log channel in Kurosōden) ──────
        # This is the piece that was missing: the old code only wrote a DB row,
        # so nothing ever reached a human. We DM every configured admin via the
        # downloader bot (Levi) — the stage that acts next — falling back to
        # this (Lelouch) client if Levi isn't running.
        if deferred_for_quiet_hours:
            log.info("lelouch.assign.deferred_quiet_hours", code=receipt.code)
        else:
            await _notify_admins_new_request(receipt.code, title, user_name, user_id,
                                             franchise_json, assignment_result)

    async def _notify_admins_new_request(
        code: str, title: str, requester: str, requester_id: int,
        franchise_json: dict, assignment_result=None,
    ) -> None:
        """DM the selected downloader with a Levi-styled request card."""
        from types import SimpleNamespace

        from kurosoden.shared.handoff import notify_stage_assignment

        admin_ids = (
            [assignment_result.admin_telegram_id]
            if assignment_result is not None
            else list(getattr(container.env, "admin_ids", []) or [])
        )
        if not admin_ids:
            log.warning("lelouch.notify.no_admins", code=code)
            return

        sent = 0
        for admin_id in admin_ids:
            assignment = assignment_result or SimpleNamespace(
                admin_telegram_id=admin_id,
                status="assigned",
                assignment_mode="fallback",
            )
            sent += await notify_stage_assignment(
                container,
                "levi",
                assignment,
                code,
                title,
                requester=requester,
                requester_id=requester_id,
                franchise_json=franchise_json,
            )
        log.info("lelouch.notify.sent", code=code, admins=len(admin_ids), delivered=sent)

    # ── My Requests ───────────────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^req\|mine"))
    async def _mine(_: Client, q: CallbackQuery) -> None:
        from nekofetch.services.request_service import RequestService
        from nekofetch.ui.screens import my_requests as my_reqs_screen

        await q.answer()
        rows = await RequestService(container).list_for_user(q.from_user.id)
        name = q.from_user.first_name if q.from_user else ""
        if not rows:
            screen = my_reqs_screen(name, [])
            await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
            return
        req_list = [
            {"title": r.anime_title, "status": r.status} for r in rows[:10]
        ]
        screen = my_reqs_screen(name, req_list)
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
