"""Lelouch batch flow — submit many titles into the request line at once.

This is Lelouch's own batch handler, distinct from the inherited NekoFetch admin
batch. Every confirmed title becomes a real :class:`Request` (QUEUED) — exactly
what the single-request flow produces — and is assigned to the downloader (Levi).
A batch entry is a request and nothing more: it does **not** also spawn a
``WorkItem``. Levi's task board reads ``AdminAssignment`` rows keyed on
``Request.code``, so the request alone surfaces the work; a parallel WRK row would
just be a redundant duplicate on the manage board.

Flow:
  1. ``/batch`` (staff+) or the ``batch|new`` button → styled prompt.
  2. Admin sends titles (comma- or newline-separated).
  3. Each title is resolved through :func:`resolve_franchise` (AniList →
     @acutebot → TMDB, franchise totals folded in). Resolver returns the single
     best match per title — there is no version-picker here; ambiguity is the
     single-request flow's concern. Titles that resolve to nothing are set aside.
  4. A review *carousel* parades each resolved title one card at a time. The
     admin approves or skips each, pages with ◀ ▶, and commits with "Commit the
     line".
  5. On commit, each approved entry becomes a QUEUED :class:`Request`, is assigned
     to Levi, and the selected downloader is DMed the same "New Download Task"
     card the single-request path sends (via
     :func:`kurosoden.shared.handoff.notify_stage_assignment`) — so a human sees
     each order as an actionable task, not a silent summary line.

State lives in Redis (:class:`FSM`) so the carousel survives restarts and works
across workers. The resolved franchise dicts are stored whole in the FSM bag so
paging never re-hits the providers.
"""

from __future__ import annotations

import asyncio
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.domain.enums import Role
from nekofetch.ui.artwork import pick_artwork
from nekofetch.ui.components import cb, lock_buttons
from nekofetch.ui.progress import SPINNER
from nekofetch.ui.screens import Screen, card, message_ref, send_screen

from nekofetch.bots.admin.handlers.requests import (
    _media_to_franchise_dict,
    apply_franchise_totals,
    enrich_with_tmdb,
)
from kurosoden.shared import lelouch_voice as V
from kurosoden.shared.franchise_resolver import (
    resolve_franchise,
    resolve_franchise_candidates,
)

import structlog

log = structlog.get_logger(__name__)

# ── FSM states ────────────────────────────────────────────────────────────────
STATE_BATCH_PROMPT = "lelouch_batch:await_titles"
STATE_BATCH_SELECT = "lelouch_batch:select"    # picking a franchise for an ambiguous title
STATE_BATCH_CONFIRM = "lelouch_batch:confirm"  # final approval card, ready to commit

# Franchises shown per page in the selection menu (user spec: 5 per page).
_FR_PAGE = 5

# Commands the batch text handler must never swallow.
_RESERVED = ["start", "help", "myrequests", "admin", "settings", "batch", "cleardatabase"]

BOT = "lelouch"


def _franchise_detail(fr: dict) -> str:
    """Short human summary of a resolved franchise for the review card."""
    seasons = fr.get("franchise_seasons") or 0
    movies = fr.get("franchise_movies") or 0
    ovas = fr.get("franchise_ovas") or 0
    specials = fr.get("franchise_specials") or 0
    parts = [fr.get("format") or "TV"]
    if fr.get("year"):
        parts.append(str(fr["year"]))
    if seasons:
        parts.append(f"{seasons} season{'s' if seasons != 1 else ''}")
    if movies:
        parts.append(f"{movies} movie{'s' if movies != 1 else ''}")
    if ovas:
        parts.append(f"{ovas} OVA{'s' if ovas != 1 else ''}")
    if specials:
        parts.append(f"{specials} special{'s' if specials != 1 else ''}")
    src = fr.get("_source")
    if src and src != "anilist":
        parts.append(f"via {src}")
    return " · ".join(parts)


def _slim(fr: dict) -> dict:
    """Trim a franchise dict to what a WorkItem needs, so the FSM bag stays small.

    Redis holds the whole batch across paging; dropping the heavy synopsis/art
    fields keeps the JSON well under any sane value size while preserving every
    field the download stage re-derives franchise totals from.
    """
    keep = (
        "title", "english", "romaji", "year", "format", "studio", "genres",
        "franchise_episodes", "franchise_seasons", "franchise_movies",
        "franchise_ovas", "franchise_onas", "franchise_specials",
        "relations", "synonyms", "anilist_id", "anilist_url",
        "cover_url", "banner_url", "_source", "_query", "_backdrop_url",
    )
    return {k: fr.get(k) for k in keep if fr.get(k) is not None}


async def _commit_batch_requests(
    container: Container, submitter_telegram_id: int, keep: list[dict],
) -> list[tuple[str, str, dict]]:
    """Turn accepted batch entries into real requests + Levi assignments.

    Mirrors the single-request path exactly — one QUEUED :class:`Request` per
    entry (NO ``WorkItem``), each assigned to the downloader (Levi), and the
    selected admin DMed the same "New Download Task" card
    (:func:`notify_stage_assignment`) rather than a silent summary line.

    Returns ``(code, title, franchise_data)`` for every created request, kept
    aligned so the confirmation count, prefetch, and assignment cards agree.
    Raises only if the DB write itself fails (the caller shows the failure card);
    per-request assignment/notification is best-effort and never aborts the rest.
    """
    from types import SimpleNamespace

    from nekofetch.core.constants import REQUEST_PREFIX
    from nekofetch.domain.enums import DownloadScope, RequestStatus
    from nekofetch.infrastructure.database.postgres.models import Request
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.repositories.request_repo import RequestRepository
    from nekofetch.infrastructure.repositories.user_repo import UserRepository
    from kurosoden.shared.admin_assignment import AdminAssignmentEngine
    from kurosoden.shared.handoff import notify_stage_assignment
    from kurosoden.shared.management_service import ManagementService
    from kurosoden.shared.owner_seed import _owner_id

    bridged: list[tuple[str, str, dict]] = []
    async with session_scope(container.pg_sessionmaker) as session:
        repo = RequestRepository(session)
        # ``submitter_telegram_id`` is a TELEGRAM id, but ``Request.user_id`` FKs
        # ``users.id`` (the internal PK). Resolve/create the submitter's User row
        # once and use its ``.id`` — passing the telegram id straight through
        # violates the FK, so every batched Request INSERT fails and nothing
        # surfaces on Levi's board.
        submitter = await UserRepository(session).get_or_create(
            submitter_telegram_id, username=None, first_name=None)
        await session.flush()
        for keep_data in keep:
            title = (keep_data.get("anime_title")
                     or keep_data.get("title") or "").strip()
            if not title:
                continue
            seq = await repo.next_sequence()
            code = f"{REQUEST_PREFIX}-{seq}"
            fr = keep_data.get("franchise_data") or {}
            aid = fr.get("anilist_id")
            req = Request(
                code=code,
                user_id=submitter.id,
                anime_doc_id=f"{aid}" if aid else None,
                anime_title=title,
                source="",  # batch has no source yet (admin picks later)
                source_ref="",
                scope=DownloadScope.ENTIRE_SERIES.value,
                season=None,
                episodes=None,
                franchise_data=fr,
                status=RequestStatus.QUEUED,
            )
            await repo.add(req)
            bridged.append((code, title, fr))
        await session.flush()

    # Assign each request to Levi + DM the selected downloader the task card.
    # Done outside the creation session so each assign gets its own transaction
    # (matching the single-request path) and one failure never rolls back the
    # others.
    assignment = AdminAssignmentEngine(container.pg_sessionmaker)
    for code, title, fr in bridged:
        try:
            result = await assignment.assign(code, "levi")
            if result is None:
                # No qualifying admin (off-hours/on-break); fall back to the
                # owner so the task is always visible, never silently dropped.
                owner = _owner_id(container)
                if owner is not None:
                    await ManagementService(container.pg_sessionmaker).reassign(
                        code, "levi", owner
                    )
                    result = SimpleNamespace(
                        admin_telegram_id=owner,
                        status="assigned",
                        assignment_mode="fallback",
                    )
            if result is not None:
                await notify_stage_assignment(
                    container, "levi", result, code, title,
                    franchise_json=fr,
                )
        except Exception as exc:  # noqa: BLE001 — recovery sweep still catches it
            log.warning("lelouch.batch.assign_failed",
                        code=code, error=str(exc)[:200])

    return bridged


def register(client: Client, container: Container) -> None:
    """Wire Lelouch's batch request flow onto the Pyrogram client."""
    fsm = FSM(container.redis, bot="lelouch_batch")

    # ── Role gate (staff or admin only — work items are an admin surface) ─────
    def _staff(obj) -> bool:
        user = getattr(obj, "nf_user", None)
        if user is None:
            return False
        try:
            return Role(user.role) in (Role.STAFF, Role.ADMIN)
        except Exception:  # noqa: BLE001 — unknown role string ⇒ not staff
            return False

    def _art():
        return pick_artwork("lelouch")

    # ── Entry: /batch command ─────────────────────────────────────────────────
    @client.on_message(filters.command("batch") & filters.private)
    async def _batch_cmd(_: Client, message: Message) -> None:
        if not _staff(message):
            return  # silently ignore — non-staff shouldn't know it exists
        await _prompt(message.chat.id, message.from_user.id, old_msg=None)

    # ── Entry: "Batch Work" button from the home/admin card ───────────────────
    @client.on_callback_query(filters.regex(r"^batch\|new$"))
    async def _batch_new(_: Client, q: CallbackQuery) -> None:
        if not _staff(q):
            await q.answer(V.UNKNOWN_ACTION, show_alert=True)
            return
        await q.answer()
        await _prompt(q.message.chat.id, q.from_user.id, old_msg=q.message)

    async def _prompt(chat_id: int, user_id: int, *, old_msg: Message | None) -> None:
        screen = card(
            V.BATCH_PROMPT, image=_art(), bot_name=BOT,
            buttons=[[(V.BTN_BATCH_CANCEL, cb("batch", "cancel"))]],
        )
        prompt = await send_screen(client, chat_id, screen, old_msg=old_msg)
        # Remember the prompt so the title-list step evolves THIS card in place.
        await fsm.set(user_id, STATE_BATCH_PROMPT,
                      prompt_msg_id=prompt.id, prompt_chat_id=prompt.chat.id)

    # ── Title intake (group=2 so it sits ahead of the single-request text
    #    handler; only fires while this user is in the batch prompt state) ──────
    @client.on_message(
        filters.text & filters.private & ~filters.command(_RESERVED),
        group=2,
    )
    async def _batch_text(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, data = await fsm.get(message.from_user.id)
        if state != STATE_BATCH_PROMPT:
            return  # not our turn — let the request handler take the message
        if not _staff(message):
            return
        raw = (message.text or "").strip()
        prompt = message_ref(client, data.get("prompt_chat_id") or message.chat.id,
                             data.get("prompt_msg_id"))
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            pass
        # Accept both commas and newlines as separators.
        titles = [
            t.strip()
            for chunk in raw.replace("\n", ",").split(",")
            if (t := chunk.strip())
        ]
        # De-dup while preserving order (an admin pasting a list often repeats).
        seen: set[str] = set()
        titles = [t for t in titles if not (t.lower() in seen or seen.add(t.lower()))]
        if not titles:
            await send_screen(
                client, message.chat.id,
                card(V.BATCH_EMPTY, image=_art(), bot_name=BOT,
                     buttons=[[(V.BTN_BATCH_CANCEL, cb("batch", "cancel"))]]),
                old_msg=prompt,
            )
            return
        await _resolve(message, titles, prompt=prompt)

    async def _hydrate(cand: dict, query: str) -> dict | None:
        """Turn a lightweight franchise candidate ({title, anilist_id, format})
        into a full, slimmed franchise dict — the same shape the single-request
        flow stages (``_media_to_franchise_dict`` → totals → TMDB backdrop)."""
        aid = cand.get("anilist_id")
        fr: dict | None = None
        if aid:
            try:
                media = await container.anilist._fetch_full(int(aid))
            except (ValueError, TypeError):
                media = None
            except Exception as exc:  # noqa: BLE001
                log.warning("lelouch.batch.hydrate_failed",
                            aid=aid, error=str(exc)[:200])
                media = None
            if media is not None:
                fr = _media_to_franchise_dict(media)
        if fr is None:
            # No AniList id (acutebot/TMDB candidate) — resolve by title instead.
            try:
                fr = await resolve_franchise(container, cand.get("title") or query)
            except Exception as exc:  # noqa: BLE001
                log.warning("lelouch.batch.hydrate_by_title_failed",
                            title=cand.get("title"), error=str(exc)[:200])
                fr = None
        if fr is None:
            return None
        # The picked franchise's display title always wins as the entry title.
        fr["title"] = cand.get("title") or fr.get("title") or query
        try:
            await apply_franchise_totals(container, fr)
        except Exception as exc:  # noqa: BLE001 — totals are best-effort
            log.debug("lelouch.batch.totals_failed", error=str(exc)[:200])
        try:
            backdrop = await enrich_with_tmdb(
                container, fr, fr.get("english") or fr["title"])
            fr["_backdrop_url"] = backdrop
        except Exception as exc:  # noqa: BLE001 — art is decorative
            log.debug("lelouch.batch.tmdb_failed", error=str(exc)[:200])
        fr["_query"] = query
        return fr

    async def _franchise_candidates(title: str) -> list[dict]:
        """The franchises a title could mean — using the SAME logic as Lelouch's
        single-request flow (``container.series_resolver.resolve``), so batch and
        request agree on what's one franchise vs. genuinely-distinct adaptations.

        SeriesResolver starts from the single best AniList hit and only splits into
        multiple entries for real alternate adaptations (a full TV remake, a
        substantial ≥2-ep OVA) — a special/recap/OVA of the SAME show collapses
        into ONE franchise (its extras are folded into the counts by
        ``apply_franchise_totals`` at hydrate). This is what stops "Vivy" from
        listing its special episode as a second franchise (the old
        ``resolve_franchise_candidates`` grouped the raw search page by title
        words, so siblings showed up separately). Returns the light
        ``{title, anilist_id, format}`` dicts the picker + ``_hydrate`` consume.

        Falls back to ``resolve_franchise_candidates`` only when the resolver finds
        nothing (a title AniList can't page but @acutebot/TMDB can)."""
        try:
            resolution = await container.series_resolver.resolve(title)
        except Exception as exc:  # noqa: BLE001
            log.warning("lelouch.batch.series_resolve_failed",
                        title=title, error=str(exc)[:200])
            resolution = None
        if resolution is not None and resolution.entries:
            return [
                {"title": e.title, "anilist_id": str(e.anilist_id) if e.anilist_id else None,
                 "format": e.format}
                for e in resolution.entries
            ]
        # AniList couldn't resolve it → the acutebot/TMDB-aware candidate path.
        return await resolve_franchise_candidates(container, title)

    async def _resolve(src: Message, titles: list[str], *, prompt=None) -> None:
        """Resolve each title to its franchise(s):

          • exactly one franchise  → auto-approve (hydrated straight away);
          • several franchises      → queued for a selection menu (one per title);
          • none                    → set aside as skipped.

        Then open the franchise selection menu (if any title was ambiguous) or go
        straight to the approval card."""
        user_id = src.from_user.id

        def _frame(f: str) -> str:
            return f"{V.batch_processing(len(titles))}\n\n{f}"

        async def _run() -> tuple[list[dict], list[dict], list[str]]:
            resolved: list[dict] = []      # finished franchise dicts (auto-approved)
            pending: list[dict] = []       # {query, candidates} awaiting a pick
            skipped: list[str] = []
            for title in titles:
                try:
                    cands = await _franchise_candidates(title)
                except Exception as exc:  # noqa: BLE001
                    log.warning("lelouch.batch.candidates_failed",
                                title=title, error=str(exc)[:200])
                    cands = []
                if not cands:
                    skipped.append(title)
                elif len(cands) == 1:
                    fr = await _hydrate(cands[0], title)
                    if fr:
                        resolved.append(_slim(fr))
                    else:
                        skipped.append(title)
                else:
                    pending.append({"query": title, "candidates": cands})
            return resolved, pending, skipped

        # Animate the resolve ON the prompt card (a photo card) when we have it;
        # else fall back to a fresh status message. Downstream cards replace this
        # same message in place.
        if prompt is not None:
            await send_screen(
                client, src.chat.id,
                card(_frame(SPINNER[0]), image=_art(), bot_name=BOT),
                old_msg=prompt,
            )
            msg = prompt
        else:
            msg = await src.reply(_frame(SPINNER[0]), parse_mode=ParseMode.HTML)
        resolved, pending, skipped = await _run()

        if not resolved and not pending:
            await fsm.clear(user_id)
            await send_screen(
                client, msg.chat.id,
                card(V.batch_none_found(skipped), image=_art(), bot_name=BOT,
                     buttons=[[(V.BTN_HOME, cb(BOT, "home"))]]),
                old_msg=msg,
            )
            return

        await fsm.set(
            user_id, STATE_BATCH_SELECT,
            resolved=resolved,
            pending=pending,
            pending_index=0,
            page=0,
            skipped=skipped,
        )
        if pending:
            await _render_select(msg, user_id)
        else:
            await _render_approval(msg, user_id)

    # ── Franchise selection menu (one ambiguous title at a time) ───────────────
    async def _render_select(msg: Message, user_id: int) -> None:
        _, data = await fsm.get(user_id)
        pending = data.get("pending", [])
        idx = data.get("pending_index", 0)
        if idx >= len(pending):
            # Every ambiguous title has been settled → the approval card.
            await _render_approval(msg, user_id)
            return

        page = max(0, data.get("page", 0))
        cur = pending[idx]
        query = cur.get("query", "")
        cands = cur.get("candidates", [])
        total_pages = max(1, (len(cands) + _FR_PAGE - 1) // _FR_PAGE)
        page = min(page, total_pages - 1)
        window = cands[page * _FR_PAGE:(page + 1) * _FR_PAGE]

        caption = V.batch_choose_franchise(query, idx + 1, len(pending))
        rows: list[list[tuple[str, str]]] = []
        for i, cand in enumerate(window):
            abs_i = page * _FR_PAGE + i
            fmt = cand.get("format")
            label = cand.get("title") or query
            if fmt:
                label = f"{label} · {fmt}"
            rows.append([(label[:60], cb("batch", "fpick", idx, abs_i))])

        # Pagination only when it's needed (> one page): paired arrows side by
        # side, same style. Otherwise just a Cancel button (user spec).
        if total_pages > 1:
            nav: list[tuple[str, str]] = []
            if page > 0:
                nav.append((V.BTN_FR_PREV, cb("batch", "fpage", idx, page - 1)))
            if page < total_pages - 1:
                nav.append((V.BTN_FR_NEXT, cb("batch", "fpage", idx, page + 1)))
            if nav:
                rows.append(nav)
        rows.append([(V.BTN_BATCH_CANCEL, cb("batch", "cancel"))])

        await send_screen(client, msg.chat.id,
                          card(caption, image=_art(), bot_name=BOT, buttons=rows),
                          old_msg=msg)

    @client.on_callback_query(filters.regex(r"^batch\|fpage\|"))
    async def _fpage(_: Client, q: CallbackQuery) -> None:
        if not _staff(q):
            await q.answer(V.UNKNOWN_ACTION, show_alert=True)
            return
        await q.answer()
        parts = q.data.split("|")
        page = int(parts[-1])
        await fsm.update(q.from_user.id, page=page)
        await _render_select(q.message, q.from_user.id)

    @client.on_callback_query(filters.regex(r"^batch\|fpick\|"))
    async def _fpick(_: Client, q: CallbackQuery) -> None:
        if not _staff(q):
            await q.answer(V.UNKNOWN_ACTION, show_alert=True)
            return
        await lock_buttons(q)
        user_id = q.from_user.id
        parts = q.data.split("|")
        title_idx = int(parts[2])
        cand_idx = int(parts[3])
        _, data = await fsm.get(user_id)
        pending = data.get("pending", [])
        if title_idx >= len(pending):
            await q.answer()
            await _render_select(q.message, user_id)
            return
        cur = pending[title_idx]
        cands = cur.get("candidates", [])
        chosen = cands[cand_idx] if 0 <= cand_idx < len(cands) else None

        resolved = data.get("resolved", [])
        skipped = list(data.get("skipped", []))
        if chosen is not None:
            fr = await _hydrate(chosen, cur.get("query", ""))
            if fr:
                resolved.append(_slim(fr))
            else:
                skipped.append(cur.get("query", "?"))
        else:
            skipped.append(cur.get("query", "?"))

        await fsm.update(
            user_id,
            resolved=resolved,
            skipped=skipped,
            pending_index=title_idx + 1,
            page=0,
        )
        await q.answer()
        await _render_select(q.message, user_id)

    # ── Approval card (single row: commit / stand down) ────────────────────────
    async def _render_approval(msg: Message, user_id: int) -> None:
        _, data = await fsm.get(user_id)
        resolved = data.get("resolved", [])
        skipped = data.get("skipped", [])

        if not resolved:
            await fsm.clear(user_id)
            await send_screen(
                client, msg.chat.id,
                card(V.batch_none_found(skipped), image=_art(), bot_name=BOT,
                     buttons=[[(V.BTN_HOME, cb(BOT, "home"))]]),
                old_msg=msg,
            )
            return

        # Move to the confirm state so a stray text message can't re-enter select.
        await fsm.set(user_id, STATE_BATCH_CONFIRM,
                      resolved=resolved, skipped=skipped)

        entries = [
            (fr.get("title") or fr.get("_query") or "Unknown", _franchise_detail(fr))
            for fr in resolved
        ]
        caption = V.batch_approval_summary(entries)
        # ONE row, two buttons: commit-and-send-down + stand down.
        rows = [[(V.BTN_BATCH_DONE, cb("batch", "commit")),
                 (V.BTN_BATCH_CANCEL, cb("batch", "cancel"))]]
        image = resolved[0].get("_backdrop_url") or resolved[0].get("banner_url") or _art()
        await send_screen(client, msg.chat.id,
                          card(caption, image=image, bot_name=BOT, buttons=rows),
                          old_msg=msg)

    @client.on_callback_query(filters.regex(r"^batch\|commit$"))
    async def _commit(_: Client, q: CallbackQuery) -> None:
        if not _staff(q):
            await q.answer(V.UNKNOWN_ACTION, show_alert=True)
            return
        await lock_buttons(q)
        user_id = q.from_user.id
        _, data = await fsm.get(user_id)
        resolved = data.get("resolved", [])
        skipped = list(data.get("skipped", []))

        # Persist the same canonical entry mapping as single requests. WorkItems
        # carry this franchise JSON into the Request bridge below, so split-season
        # episode boundaries survive the batch handoff too.
        keep: list[dict] = []
        from nekofetch.services.franchise_flow import FranchiseFlowService
        mapping_service = FranchiseFlowService(container)
        for fr in resolved:
            franchise = dict(fr)
            try:
                aid = franchise.get("anilist_id")
                doc_id = f"{aid}" if aid is not None else franchise.get("title") or "batch"
                franchise["entries"] = await mapping_service.persisted_entries(
                    franchise, doc_id,
                )
            except Exception as exc:  # noqa: BLE001 — keep batch acceptance resilient
                log.warning("lelouch.batch.franchise_entries.persist_failed",
                            title=franchise.get("title"), error=str(exc)[:200])
                franchise.setdefault("entries", [])
            keep.append({
                "anime_title": franchise.get("title") or franchise.get("_query"),
                "franchise_data": franchise,
            })

        await fsm.clear(user_id)

        if not keep:
            await send_screen(
                client, q.message.chat.id,
                card(V.BATCH_EMPTY, image=_art(), bot_name=BOT,
                     buttons=[[(V.BTN_HOME, cb(BOT, "home"))]]),
                old_msg=q.message,
            )
            await q.answer()
            return

        # ── Commit: one real Request per accepted title (NO WorkItem) ──
        # A batch entry IS a request, nothing more. Batch used to create a
        # WorkItem (WRK-N) *and* bridge it to a Request — but Levi's task board
        # reads AdminAssignment rows keyed on Request.code, so the request alone
        # already surfaces the work; the parallel WRK row was redundant (owner:
        # "just keep it as REQ, no need to make it work"). ``_commit_batch_requests``
        # mirrors the single-request path exactly: create QUEUED Request → assign
        # to Levi → DM the downloader the "New Download Task" card.
        try:
            bridged = await _commit_batch_requests(container, user_id, keep)
        except Exception as exc:  # noqa: BLE001
            log.error("lelouch.batch.commit_failed", error=str(exc)[:300])
            await send_screen(
                client, q.message.chat.id,
                card(f"{V.ICON} <b>The line wouldn't hold.</b>\n\n"
                     "Something failed committing the batch. Nothing was staged — "
                     "try again in a moment.",
                     image=_art(), bot_name=BOT,
                     buttons=[[(V.BTN_HOME, cb(BOT, "home"))]]),
                old_msg=q.message,
            )
            await q.answer()
            return

        if not bridged:
            await send_screen(
                client, q.message.chat.id,
                card(V.BATCH_EMPTY, image=_art(), bot_name=BOT,
                     buttons=[[(V.BTN_HOME, cb(BOT, "home"))]]),
                old_msg=q.message,
            )
            await q.answer()
            return

        from kurosoden.shared.access_gate import levi_link
        open_tasks = await levi_link(container)
        await send_screen(
            client, q.message.chat.id,
            card(V.batch_done(len(bridged), skipped), image=_art(), bot_name=BOT,
                 url_buttons=[[(V.BTN_OPEN_TASKS, open_tasks)]] if open_tasks else None,
                 buttons=[[(V.BTN_QUEUE, cb(BOT, "queue", 0)),
                           (V.BTN_HOME, cb(BOT, "home"))]]),
            old_msg=q.message,
        )
        await q.answer()
        log.info("lelouch.batch.committed", count=len(bridged),
                 by=getattr(q.from_user, "first_name", "") or "command")

        # Prefetch metadata for every accepted batch entry (same policy as the
        # single-request flow): cache AniList/Jikan/TMDB + mirrored artwork to
        # each request folder now, so later stages read from disk. Fire-and-
        # forget; the service swallows its own errors.
        try:
            from nekofetch.services.metadata_prefetch import MetadataPrefetchService

            svc = MetadataPrefetchService(container)
            for code, _title, fr in bridged:
                aid = (fr or {}).get("anilist_id")
                _doc = f"{aid}" if aid else None
                asyncio.create_task(svc.prefetch(code, _doc, fr or {}))
        except Exception as exc:  # noqa: BLE001 — prefetch never blocks the batch
            log.warning("lelouch.batch.prefetch_spawn_failed", error=str(exc)[:200])

    # ── Cancel ────────────────────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^batch\|cancel$"))
    async def _cancel(_: Client, q: CallbackQuery) -> None:
        await fsm.clear(q.from_user.id)
        await q.answer()
        await send_screen(
            client, q.message.chat.id,
            card(f"{V.ICON} <b>Stood down.</b>\n\n"
                 "The batch is scrapped — nothing was committed. Call it up again "
                 "whenever you're ready to move.",
                 image=_art(), bot_name=BOT,
                 buttons=[[(V.BTN_HOME, cb(BOT, "home"))]]),
            old_msg=q.message,
        )
