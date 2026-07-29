"""Batch request handler — submit multiple anime in a single action.

Flow:
  1. ``/batch`` command (staff+) or the "Batch Request" button → styled prompt.
  2. User sends comma-separated titles.
  3. Each title is resolved through AniList (same discovery path as single requests):
     - Unambiguous → staged immediately with full franchise_data.
     - Ambiguous (multiple adaptations) → paginated version picker, one title at
       a time, so many ambiguous titles stay manageable.
     - Not found → skipped with a notice.
  4. Confirmation card lists everything with franchise detail.
  5. On confirm → each title submitted as a separate request via RequestService,
     carrying the correct priority band (owner=10, admin=100) so the queue drains
     owner-first, then admin-FIFO.

Reuses the exact same helpers (``_media_to_franchise_dict``,
``apply_franchise_totals``, ``enrich_with_tmdb``), FSM patterns, ``show()``
rendering with rotating artwork, and ``paginate()`` from the existing codebase
so it feels native.
"""

from __future__ import annotations

import html as _html

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, Message

from nekofetch.bots.admin.handlers.requests import (
    _media_to_franchise_dict,
    apply_franchise_totals,
    enrich_with_tmdb,
)
from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.exceptions import NekoFetchError
from nekofetch.domain.enums import DownloadScope, Role
from nekofetch.localization.messages import M, t
from nekofetch.services.auth_service import AuthService
from nekofetch.ui.artwork import pick_artwork
from nekofetch.ui.components import cb, keyboard, lock_buttons, paginate
from nekofetch.ui.progress import SPINNER, animate_until
from nekofetch.ui.screens import Screen, send_screen

# ── FSM states ──
STATE_BATCH_PROMPT = "batch:await_titles"
STATE_BATCH_CLARIFY = "batch:clarify"
STATE_BATCH_CONFIRM = "batch:confirm"

# How many version-picker buttons per page (titles with multiple adaptations).
_CLARIFY_PAGE_SIZE = 6


def _esc(text: str) -> str:
    """HTML-escape user-facing text for safe rendering."""
    return _html.escape(text or "", quote=False)


def _franchise_detail(franchise_data: dict) -> str:
    """A short human-readable summary of a resolved franchise for list rows."""
    seasons = franchise_data.get("franchise_seasons", 0) or 0
    movies = franchise_data.get("franchise_movies", 0) or 0
    ovas = franchise_data.get("franchise_ovas", 0) or 0
    specials = franchise_data.get("franchise_specials", 0) or 0
    fmt = franchise_data.get("format") or "TV"
    parts = [fmt]
    if seasons:
        parts.append(f"{seasons} season{'s' if seasons != 1 else ''}")
    if movies:
        parts.append(f"{movies} movie{'s' if movies != 1 else ''}")
    if ovas:
        parts.append(f"{ovas} OVA{'s' if ovas != 1 else ''}")
    if specials:
        parts.append(f"{specials} special{'s' if specials != 1 else ''}")
    return t(M.SEP_DOT).join(parts)


def register(client: Client, container: Container) -> None:
    auth = AuthService(container)
    fsm = FSM(container.redis, bot="admin")
    L = container.localizer.get

    def _allowed(message_or_q) -> bool:
        user = getattr(message_or_q, "nf_user", None)
        if not user:
            return False
        role = auth.role_of(user)
        return role in (Role.STAFF, Role.ADMIN)

    def _is_owner(message_or_q) -> bool:
        user = getattr(message_or_q, "nf_user", None)
        return bool(user and auth.is_owner(user))

    def _priority_label(is_owner: bool) -> str:
        return L(M.BATCH_PRIORITY_OWNER) if is_owner else L(M.BATCH_PRIORITY_ADMIN)

    # ── /batch command ──────────────────────────────────────────────────────
    @client.on_message(filters.command("batch"))
    async def _batch_cmd(_: Client, message: Message) -> None:
        if not _allowed(message):
            await message.reply(L(M.ACCESS_DENIED), parse_mode=ParseMode.HTML)
            return
        await _show_prompt(message, message.from_user.id)

    # ── "Batch Request" button from the welcome screen ──────────────────────
    @client.on_callback_query(filters.regex(r"^batch\|new$"))
    async def _batch_new(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await q.answer()
        await _show_prompt(q.message, q.from_user.id)

    async def _show_prompt(chat_msg: Message, user_id: int) -> None:
        """Show the styled batch prompt with rotating artwork."""
        await fsm.set(user_id, STATE_BATCH_PROMPT)
        screen = Screen(
            caption=L(M.BATCH_PROMPT),
            image=pick_artwork(),
            keyboard=keyboard([
                (L(M.BTN_CANCEL), cb("batch", "cancel")),
            ]),
        )
        await send_screen(client, chat_msg.chat.id, screen, old_msg=chat_msg)

    def _get_user_id(msg_or_q) -> int:
        if isinstance(msg_or_q, CallbackQuery):
            return msg_or_q.from_user.id
        return msg_or_q.from_user.id if msg_or_q.from_user else 0

    # ── Text handler — parse comma-separated titles ─────────────────────────
    @client.on_message(
        filters.text & filters.private & ~filters.command(["start", "help", "cancel", "reload", "cleardownloads", "resetoverrides", "batch"]),
        group=3,
    )
    async def _batch_text(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, _data = await fsm.get(message.from_user.id)
        if state != STATE_BATCH_PROMPT:
            return  # not in batch mode — let the single-request handler take it
        if not _allowed(message):
            return

        raw = (message.text or "").strip()
        if not raw:
            return

        titles = [t.strip() for t in raw.split(",") if t.strip()]
        if not titles:
            screen = Screen(
                caption=L(M.BATCH_EMPTY),
                image=pick_artwork(),
                keyboard=keyboard([(L(M.BTN_CANCEL), cb("batch", "cancel"))]),
            )
            await send_screen(client, message.chat.id, screen, old_msg=message)
            return

        # Start the resolution process
        await _resolve_titles(message, titles)

    # ── Core resolution loop ────────────────────────────────────────────────
    async def _resolve_titles(src_msg: Message, titles: list[str]) -> None:
        """Resolve each title through AniList, staging resolved ones and
        queuing ambiguous ones for clarification."""
        user_id = _get_user_id(src_msg)

        def _frame(f: str) -> str:
            return L(M.BATCH_PROCESSING, n=len(titles)) + f"\n\n{f}"

        resolved: list[dict] = []       # franchise_data dicts ready to submit
        ambiguous: list[dict] = []      # {query, versions} needing clarification
        skipped: list[str] = []         # titles that couldn't be found

        async def _resolve_all() -> tuple[list[dict], list[dict], list[str]]:
            for title in titles:
                try:
                    # AniList search — same path as the single-request flow
                    media = await container.anilist.search(title)
                except Exception:
                    skipped.append(title)
                    continue
                if media is None:
                    # TMDB fallback
                    try:
                        tmdb_result = await container.tmdb.search(title)
                    except Exception:
                        skipped.append(title)
                        continue
                    if tmdb_result is None:
                        skipped.append(title)
                        continue
                    # Build minimal franchise data from TMDB
                    tmdb_url = f"https://www.themoviedb.org/{tmdb_result.media_type}/{tmdb_result.id}"
                    franchise_data = {
                        "title": tmdb_result.title,
                        "english": tmdb_result.title,
                        "romaji": None,
                        "year": tmdb_result.year,
                        "format": tmdb_result.media_type.upper(),
                        "status": None,
                        "score": tmdb_result.rating,
                        "studio": None,
                        "genres": tmdb_result.genres,
                        "synopsis": tmdb_result.overview,
                        "synopsis_url": tmdb_url,
                        "franchise_episodes": tmdb_result.episodes,
                        "franchise_seasons": tmdb_result.seasons or 1,
                        "franchise_movies": 0,
                        "franchise_ovas": 0,
                        "franchise_onas": 0,
                        "franchise_specials": 0,
                        "relations": [],
                        "anilist_id": str(tmdb_result.id),
                        "anilist_url": tmdb_url,
                        "cover_url": tmdb_result.poster_url,
                        "banner_url": tmdb_result.backdrop_url,
                        "_source": "tmdb",
                        "_query": title,
                    }
                    resolved.append(franchise_data)
                    continue

                try:
                    # Check for multiple adaptations via SeriesResolver
                    resolution = await container.series_resolver.resolve(title)
                except Exception:
                    skipped.append(title)
                    continue
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
                    ambiguous.append({
                        "query": title,
                        "versions": versions,
                        "base_franchise": franchise_data,
                    })
                else:
                    # Single match — full-graph totals + TMDB enrichment
                    await apply_franchise_totals(container, franchise_data)
                    backdrop = await enrich_with_tmdb(
                        container, franchise_data,
                        franchise_data.get("english") or title,
                    )
                    franchise_data["_backdrop_url"] = backdrop
                    franchise_data["_query"] = title
                    resolved.append(franchise_data)
            return resolved, ambiguous, skipped

        msg = await src_msg.reply(_frame(SPINNER[0]), parse_mode=ParseMode.HTML)
        resolved, ambiguous, skipped = await animate_until(msg, _resolve_all(), _frame)

        # If there are ambiguous titles, enter the clarification flow
        if ambiguous:
            await fsm.set(
                user_id, STATE_BATCH_CLARIFY,
                resolved=resolved,
                ambiguous=ambiguous,
                ambiguous_index=0,
                skipped=skipped,
                page=0,
            )
            await _render_clarify(msg, user_id, 0)
            return

        # No ambiguous titles — go straight to confirmation
        if not resolved:
            screen = Screen(
                caption=L(M.BATCH_EMPTY),
                image=pick_artwork(),
                keyboard=keyboard([(L(M.BTN_BACK), cb("home"))]),
            )
            await send_screen(client, msg.chat.id, screen, old_msg=msg)
            return

        await fsm.set(
            user_id, STATE_BATCH_CONFIRM,
            resolved=resolved,
            skipped=skipped,
        )
        await _render_confirm(msg, user_id)

    # ── Clarification flow (version picker for ambiguous titles) ────────────
    async def _render_clarify(msg: Message, user_id: int, page: int) -> None:
        _, data = await fsm.get(user_id)
        ambiguous = data.get("ambiguous", [])
        idx = data.get("ambiguous_index", 0)
        resolved_count = len(data.get("resolved", []))
        pending_count = len(ambiguous) - idx

        if idx >= len(ambiguous):
            # All ambiguities resolved — go to confirmation
            resolved = data.get("resolved", [])
            if not resolved:
                await fsm.clear(user_id)
                screen = Screen(
                    caption=L(M.BATCH_EMPTY),
                    image=pick_artwork(),
                    keyboard=keyboard([(L(M.BTN_BACK), cb("home"))]),
                )
                await send_screen(client, msg.chat.id, screen, old_msg=msg)
                return
            await fsm.set(user_id, STATE_BATCH_CONFIRM,
                          resolved=resolved, skipped=data.get("skipped", []))
            await _render_confirm(msg, user_id)
            return

        current = ambiguous[idx]
        query = current["query"]
        versions = current["versions"]

        # Build the version picker — same pattern as the single-request flow
        header = L(M.BATCH_CLARIFY_HEADER,
                   resolved=resolved_count, pending=pending_count)
        body = L(M.BATCH_AMBIGUOUS, query=_esc(query))

        # Paginate version buttons
        items = [
            (L(M.BATCH_VERSION_PICK,
               title=_esc(v["title"])[:42]),
             cb("batch", "vpick", str(idx), str(v.get("id", i))))
            for i, v in enumerate(versions)
        ]
        kb = paginate(items, page=page, nav_action="batch|vpage",
                      page_size=_CLARIFY_PAGE_SIZE)

        # Add skip + cancel rows (must use InlineKeyboardButton, not tuples,
        # because paginate() returns InlineKeyboardMarkup whose rows are already
        # Button objects — appending raw tuples would crash Pyrogram at send time).
        kb.inline_keyboard.append([
            InlineKeyboardButton(L(M.BATCH_VERSION_SKIP),
                                 callback_data=cb("batch", "vskip", str(idx))),
            InlineKeyboardButton(L(M.BTN_CANCEL),
                                 callback_data=cb("batch", "cancel")),
        ])

        caption = f"{header}\n\n{body}"
        screen = Screen(caption=caption, image=pick_artwork(), keyboard=kb)
        await send_screen(client, msg.chat.id, screen, old_msg=msg)

    @client.on_callback_query(filters.regex(r"^batch\|vpage"))
    async def _vpage(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await q.answer()
        page = int(q.data.split("|")[-1])
        await fsm.update(q.from_user.id, page=page)
        await _render_clarify(q.message, q.from_user.id, page)

    @client.on_callback_query(filters.regex(r"^batch\|vpick\|"))
    async def _vpick(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await lock_buttons(q)
        _, data = await fsm.get(q.from_user.id)
        parts = q.data.split("|")
        idx = int(parts[2])
        picked_id = parts[3]

        ambiguous = data.get("ambiguous", [])
        if idx >= len(ambiguous):
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return

        current = ambiguous[idx]
        versions = current["versions"]
        chosen = next(
            (v for v in versions if str(v.get("id")) == picked_id),
            versions[0],
        )
        chosen_anilist_id = chosen.get("anilist_id") or chosen.get("id")

        # Refetch full media data for the chosen version
        try:
            refetched = await container.anilist._fetch_full(int(chosen_anilist_id))
        except (ValueError, TypeError):
            refetched = None

        if refetched:
            franchise_data = _media_to_franchise_dict(refetched)
        else:
            franchise_data = {
                "title": chosen.get("title", current["query"]),
                "anilist_id": str(chosen_anilist_id),
                "genres": [], "relations": [], "_source": "anilist",
            }

        franchise_data["title"] = chosen.get(
            "title", franchise_data.get("title", current["query"])
        )
        await apply_franchise_totals(container, franchise_data)
        search_title = franchise_data.get("english") or franchise_data["title"]
        backdrop = await enrich_with_tmdb(container, franchise_data, search_title)
        franchise_data["_backdrop_url"] = backdrop
        franchise_data["_query"] = current["query"]

        # Add to resolved, advance to next ambiguous title
        resolved = data.get("resolved", [])
        resolved.append(franchise_data)
        next_idx = idx + 1
        await fsm.update(q.from_user.id, resolved=resolved, ambiguous_index=next_idx)
        await q.answer()
        await _render_clarify(q.message, q.from_user.id, 0)

    @client.on_callback_query(filters.regex(r"^batch\|vskip\|"))
    async def _vskip(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await lock_buttons(q)
        _, data = await fsm.get(q.from_user.id)
        idx = int(q.data.split("|")[2])
        ambiguous = data.get("ambiguous", [])
        if idx < len(ambiguous):
            skipped = data.get("skipped", [])
            skipped.append(ambiguous[idx]["query"])
            await fsm.update(q.from_user.id, skipped=skipped)
        next_idx = idx + 1
        await fsm.update(q.from_user.id, ambiguous_index=next_idx)
        await q.answer()
        await _render_clarify(q.message, q.from_user.id, 0)

    # ── Confirmation card ───────────────────────────────────────────────────
    async def _render_confirm(msg: Message, user_id: int) -> None:
        _, data = await fsm.get(user_id)
        resolved = data.get("resolved", [])
        skipped = data.get("skipped", [])

        lines = [L(M.BATCH_CONFIRM_TITLE), "", L(M.BATCH_CONFIRM_INTRO), ""]

        for fr in resolved:
            title = fr.get("title") or fr.get("_query", "Unknown")
            detail = _franchise_detail(fr)
            lines.append(L(M.BATCH_CONFIRM_ROW, title=_esc(title), detail=detail))

        if skipped:
            lines.append("")
            for s in skipped:
                lines.append(L(M.BATCH_SKIPPED, title=_esc(s)))

        lines += ["", L(M.BATCH_CONFIRM_SUMMARY, n=len(resolved))]

        # Show which priority band will be used
        # (determined at submit time by the submitter's identity)
        kb = keyboard(
            [(L(M.BATCH_CONFIRM_BTN), cb("batch", "submit")),
             (L(M.BATCH_CANCEL_BTN), cb("batch", "cancel"))],
            [(L(M.BTN_BACK), cb("home"))],
        )
        screen = Screen(caption="\n".join(lines), image=pick_artwork(), keyboard=kb)
        await send_screen(client, msg.chat.id, screen, old_msg=msg)

    @client.on_callback_query(filters.regex(r"^batch\|submit$"))
    async def _submit(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await lock_buttons(q)
        _, data = await fsm.get(q.from_user.id)
        resolved = data.get("resolved", [])
        if not resolved:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return

        from nekofetch.services.request_service import RequestService

        user_id = q.from_user.id
        is_owner = _is_owner(q)
        svc = RequestService(container)
        results: list[str] = []
        failures: list[str] = []

        for fr in resolved:
            title = fr.get("title", fr.get("_query", "Unknown"))
            anilist_id = fr.get("anilist_id")
            source = fr.get("_source", "anilist")
            query = fr.get("_query", title)

            franchise_json = {
                "anilist_id": anilist_id,
                "source": source,
                "query": query,
                "title": title,
                "year": fr.get("year"),
                "format": fr.get("format"),
                "franchise_episodes": fr.get("franchise_episodes"),
                "franchise_seasons": fr.get("franchise_seasons"),
                "franchise_movies": fr.get("franchise_movies"),
                "franchise_ovas": fr.get("franchise_ovas"),
                "franchise_onas": fr.get("franchise_onas"),
                "franchise_specials": fr.get("franchise_specials"),
                "relations": fr.get("relations", []),
                "genres": fr.get("genres", []),
            }

            try:
                receipt = await svc.submit(
                    telegram_id=user_id,
                    source=source,
                    source_ref=f"anilist:{anilist_id}" if anilist_id else query,
                    anime_title=title,
                    scope=DownloadScope.ENTIRE_SERIES,
                    season=None,
                    episodes=None,
                    franchise_data=franchise_json,
                )
                results.append(
                    L(M.BATCH_SUBMIT_ROW, title=_esc(title),
                      code=receipt.code, pos=receipt.position)
                )
            except NekoFetchError as exc:
                detail = getattr(exc, "detail", None) or L(M.ERR_GENERIC)
                failures.append(
                    L(M.BATCH_SUBMIT_FAILED_ROW,
                      title=_esc(title), reason=detail)
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    L(M.BATCH_SUBMIT_FAILED_ROW,
                      title=_esc(title), reason=str(exc)[:120])
                )

        await fsm.clear(user_id)

        detail_lines = results + failures
        caption = L(M.BATCH_SUBMITTED, details="\n".join(detail_lines))
        # Show the priority band that was applied
        priority_line = _priority_label(is_owner)
        caption += f"\n\n<i>{priority_line}</i>"

        screen = Screen(
            caption=caption,
            image=pick_artwork(),
            keyboard=keyboard([(L(M.BTN_BACK), cb("home"))]),
        )
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    # ── Cancel ──────────────────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^batch\|cancel$"))
    async def _cancel(_: Client, q: CallbackQuery) -> None:
        await fsm.clear(q.from_user.id)
        await q.answer()
        screen = Screen(
            caption=L(M.CANCELLED),
            image=pick_artwork(),
            keyboard=keyboard([(L(M.BTN_BACK), cb("home"))]),
        )
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
