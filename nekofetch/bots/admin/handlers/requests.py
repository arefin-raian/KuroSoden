"""Phase 1 request flow — AniList-first discovery, franchise confirmation.

Source plugins must never perform discovery searches. Searching occurs
exclusively through AniList, with TMDB as a fallback for backdrops.

Workflow:
  1. User submits an anime name.
  2. Query AniList first — get full metadata + relation graph.
  3. If multiple adaptations exist (Hellsing vs Ultimate), present version picker.
  4. Otherwise show a rich confirmation card with franchise breakdown.
  5. On confirm → register a franchise-level request, forward to admin/log.
  6. TMDB fallback: search TV entries only when AniList finds nothing.
"""

from __future__ import annotations

import html

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.exceptions import NekoFetchError
from nekofetch.domain.enums import DownloadScope
from nekofetch.localization.messages import M, t
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


def _esc_q(text: str) -> str:
    """Escape a user-supplied query for safe inclusion in HTML captions."""
    return html.escape(text or "", quote=False)

STATE_NAME = "req:await_name"
STATE_FRANCHISE = "req:franchise"


def register(client: Client, container: Container) -> None:
    fsm = FSM(container.redis, bot="admin")

    @client.on_callback_query(filters.regex(r"^req\|new"))
    async def _new(_: Client, q: CallbackQuery) -> None:
        await fsm.set(q.from_user.id, STATE_NAME)
        screen = ask_title()
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    @client.on_message(filters.text & filters.private & ~filters.command(["start", "batch"]))
    async def _text(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, _data = await fsm.get(message.from_user.id)
        # In either state a typed message is a (new) title to look up — while a
        # confirmation card is shown, typing means "actually, search this instead".
        if state in (STATE_NAME, STATE_FRANCHISE):
            await _search_anilist(message, message.text.strip())

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 1 search — AniList only, no source plugin search
    # ──────────────────────────────────────────────────────────────────────────
    async def _search_anilist(message: Message, query: str) -> None:
        # Neutral, animated status — never expose the underlying providers.
        def _frame(f: str) -> str:
            return t(M.SEARCHING, query=_esc_q(query), frame=f)

        msg = await message.reply(_frame(SPINNER[0]), parse_mode=ParseMode.HTML)

        # --- 1. Resolve the title (AniList first, internally) ---
        media = await animate_until(msg, container.anilist.search(query), _frame)
        if media is None:
            # Internally fall back to TMDB — the user never sees a provider switch.
            tmdb_result = await animate_until(msg, container.tmdb.search(query), _frame)
            if tmdb_result is None:
                await msg.edit_text(t(M.SEARCH_NOT_FOUND, query=_esc_q(query)),
                                    parse_mode=ParseMode.HTML)
                return

            # TMDB fallback: build a minimal franchise info
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
                "_query": query,
            }
            await fsm.set(
                message.from_user.id, STATE_FRANCHISE,
                franchise=franchise_data,
            )
            screen = confirm_franchise(franchise_data)
            msg = await send_screen(client, message.chat.id, screen, old_msg=msg)
            return

        # --- 2. Detect adaptations via SeriesResolver ---
        resolution = await container.series_resolver.resolve(query)
        franchise_data = _media_to_franchise_dict(media)

        if resolution.multiple:
            # Show version picker — use 'id' key for choose_version compat
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
                message.from_user.id, "req:versions",
                versions=versions, query=query, franchise=franchise_data,
            )
            screen = choose_version(query, versions)
            msg = await send_screen(client, message.chat.id, screen, old_msg=msg)
            return

        # --- 3. Single match — full-graph franchise totals + TMDB English backdrop
        # and franchise synopsis. ---
        await apply_franchise_totals(container, franchise_data)
        backdrop_path = await enrich_with_tmdb(container, franchise_data, media.english or query)
        franchise_data["_backdrop_url"] = backdrop_path

        await fsm.set(
            message.from_user.id, STATE_FRANCHISE,
            franchise=franchise_data, query=query,
        )
        screen = confirm_franchise(franchise_data, backdrop_path=backdrop_path)
        msg = await send_screen(client, message.chat.id, screen, old_msg=msg)

    # ──────────────────────────────────────────────────────────────────────────
    # Version picker callbacks
    # ──────────────────────────────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^ver_pick\|"))
    async def _ver_pick(_: Client, q: CallbackQuery) -> None:
        await lock_buttons(q)  # neutralize the picker so the fetch can't double-fire
        _, args = q.data.split("|", 1)
        picked_id = args
        _, data = await fsm.get(q.from_user.id)
        versions = data.get("versions", [])
        query = data.get("query", "Anime")

        # Find the picked version by id
        chosen = next(
            (v for v in versions if str(v.get("id")) == picked_id),
            versions[0],
        )
        chosen_anilist_id = chosen.get("anilist_id") or chosen.get("id")

        # Refetch full media data for the chosen version using _fetch_full
        try:
            refetched = await container.anilist._fetch_full(int(chosen_anilist_id))
        except (ValueError, TypeError):
            refetched = None

        if refetched:
            franchise_data = _media_to_franchise_dict(refetched)
        else:
            # Fallback: build minimal franchise data from what we have
            franchise_data = {
                "title": chosen.get("title", query),
                "year": None,
                "format": chosen.get("format"),
                "status": None,
                "score": None,
                "studio": None,
                "genres": [],
                "synopsis": None,
                "franchise_episodes": None,
                "franchise_seasons": 1,
                "franchise_movies": 0,
                "franchise_ovas": 0,
                "franchise_onas": 0,
                "franchise_specials": 0,
                "relations": [],
                "anilist_id": chosen_anilist_id,
                "anilist_url": None,
                "cover_url": None,
                "banner_url": None,
                "_source": "anilist",
            }

        franchise_data["title"] = chosen.get("title", franchise_data.get("title", query))

        # Full-graph franchise totals + TMDB English backdrop / franchise synopsis.
        await apply_franchise_totals(container, franchise_data)
        search_title = franchise_data.get("english") or franchise_data["title"]
        backdrop_path = await enrich_with_tmdb(container, franchise_data, search_title)
        franchise_data["_backdrop_url"] = backdrop_path

        await fsm.set(
            q.from_user.id, STATE_FRANCHISE,
            franchise=franchise_data, query=query,
        )
        screen = confirm_franchise(franchise_data, backdrop_path=backdrop_path)
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^ver_pick_both$"))
    async def _ver_pick_both(_: Client, q: CallbackQuery) -> None:
        """'Both' — fold every listed adaptation into a SINGLE combined franchise
        request. The first adaptation seeds the card; the rest have their franchise
        totals summed in, their ids recorded, and the title joined, so one request
        represents the whole set (e.g. Hellsing + Hellsing Ultimate)."""
        await lock_buttons(q)
        _, data = await fsm.get(q.from_user.id)
        versions = data.get("versions", [])
        query = data.get("query", "Anime")
        if not versions:
            await q.answer()
            return

        base = versions[0]
        base_id = base.get("anilist_id") or base.get("id")
        try:
            refetched = await container.anilist._fetch_full(int(base_id))
        except (ValueError, TypeError):
            refetched = None
        if refetched:
            franchise_data = _media_to_franchise_dict(refetched)
        else:
            franchise_data = {
                "title": base.get("title", query), "anilist_id": str(base_id),
                "genres": [], "relations": [], "_source": "anilist",
            }
        await apply_franchise_totals(container, franchise_data)

        # Fold the remaining adaptations' totals/ids into the combined request.
        _TOTAL_FIELDS = (
            ("franchise_seasons", "seasons"), ("franchise_episodes", "episodes"),
            ("franchise_movies", "movies"), ("franchise_ovas", "ovas"),
            ("franchise_onas", "onas"), ("franchise_specials", "specials"),
        )
        combined_ids = [str(base_id)] if base_id else []
        titles = [base.get("title") or franchise_data.get("title")]
        for v in versions[1:]:
            vid = v.get("anilist_id") or v.get("id")
            titles.append(v.get("title"))
            if vid:
                combined_ids.append(str(vid))
            try:
                totals = await container.anilist.franchise_totals(int(vid))
            except Exception:
                continue
            for field, attr in _TOTAL_FIELDS:
                cur = franchise_data.get(field) or 0
                franchise_data[field] = (cur + (getattr(totals, attr, 0) or 0)) or None

        franchise_data["title"] = " + ".join(x for x in titles if x) or query
        franchise_data["combined_ids"] = combined_ids
        franchise_data["_combined"] = True

        search_title = franchise_data.get("english") or base.get("title") or franchise_data["title"]
        backdrop_path = await enrich_with_tmdb(container, franchise_data, search_title)
        franchise_data["_backdrop_url"] = backdrop_path

        await fsm.set(q.from_user.id, STATE_FRANCHISE, franchise=franchise_data, query=query)
        screen = confirm_franchise(franchise_data, backdrop_path=backdrop_path)
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    # ──────────────────────────────────────────────────────────────────────────
    # Confirmation / rejection
    # ──────────────────────────────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^series_yes\|"))
    async def _confirm(_: Client, q: CallbackQuery) -> None:
        await lock_buttons(q)  # disable the confirm card so it can't be submitted twice
        _, data = await fsm.get(q.from_user.id)
        franchise_data = data.get("franchise", {})
        query = data.get("query", franchise_data.get("title", "Anime"))
        name = q.from_user.first_name if q.from_user else ""
        await q.answer()
        await _finalize(q.message, q.from_user.id, name, franchise_data, query=query)

    @client.on_callback_query(filters.regex(r"^noop$"))
    async def _noop(_: Client, q: CallbackQuery) -> None:
        # Inert button (disabled control / pagination indicator) — just dismiss the
        # client-side spinner so a tap on a locked button feels intentional.
        await q.answer()

    @client.on_callback_query(filters.regex(r"^series_no$"))
    async def _reject(_: Client, q: CallbackQuery) -> None:
        await fsm.set(q.from_user.id, STATE_NAME)
        screen = retry_title()
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    # ──────────────────────────────────────────────────────────────────────────
    # Finalize — submit the franchise request
    # ──────────────────────────────────────────────────────────────────────────
    async def _finalize(
        card_msg: Message,
        user_id: int,
        user_name: str,
        franchise_data: dict,
        *,
        query: str,
    ) -> None:
        from nekofetch.services.request_service import RequestService

        title = franchise_data.get("title", query)
        anilist_id = franchise_data.get("anilist_id")
        source = franchise_data.get("_source", "anilist")

        # Build franchise_data JSON for the request record.
        # Store every title variant so website reports and source searches can
        # fall back to Romaji / synonyms when the English title doesn't match.
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
                franchise_data=franchise_json,
            )
        except NekoFetchError as exc:
            # Submission failed (e.g. duplicate) — surface the error, reset to retry.
            await fsm.set(user_id, STATE_NAME)
            await send_screen(
                client, card_msg.chat.id,
                Screen(caption=t(exc.message_key)), old_msg=card_msg,
            )
            return
        await fsm.clear(user_id)

        # Every request now flows through staff source-assignment (the control-center
        # request card). We never auto-queue here — a franchise request carries the
        # "anilist" discovery tag, which is not a downloadable source on its own.
        from datetime import datetime, timezone
        requested_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        screen = request_received(
            user_name, title, queue_pos=receipt.position,
            code=receipt.code, requester_id=user_id,
            requested_at=requested_at, franchise=franchise_json,
        )
        await send_screen(client, card_msg.chat.id, screen, old_msg=card_msg)

    # ──────────────────────────────────────────────────────────────────────────
    # My Requests
    # ──────────────────────────────────────────────────────────────────────────
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
        req_list = [{"title": r.anime_title, "status": r.status} for r in rows[:10]]
        screen = my_reqs_screen(name, req_list)
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)

    # ── Home (back/welcome navigation) ──
    @client.on_callback_query(filters.regex(r"^(home)$"))
    async def _home(_: Client, q: CallbackQuery) -> None:
        from nekofetch.domain.enums import Role
        from nekofetch.ui.screens import welcome as welcome_screen

        name = q.from_user.first_name or ""
        user = getattr(q, "nf_user", None)
        role = Role(user.role) if user else Role.USER
        screen = welcome_screen(
            name,
            is_staff=role in (Role.STAFF, Role.ADMIN),
            is_admin=role is Role.ADMIN,
        )
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()


def _media_to_franchise_dict(media) -> dict:
    """Convert an AnilistMedia into the dict shape `confirm_franchise` expects."""
    english = media.english or (media.titles[0] if media.titles else "Unknown")
    return {
        "title": english,
        "english": english,
        "romaji": media.romaji,
        "year": media.year,
        "format": media.format,
        "status": media.status,
        "score": media.score,
        "studio": media.studio,
        "genres": media.genres,
        "synopsis": media.synopsis,
        "synopsis_url": media.anilist_url,
        "franchise_episodes": media.franchise_episodes,
        "franchise_seasons": media.franchise_seasons,
        "franchise_movies": media.franchise_movies,
        "franchise_ovas": media.franchise_ovas,
        "franchise_onas": media.franchise_onas,
        "franchise_specials": media.franchise_specials,
        "synonyms": media.synonyms,
        "relations": [
            {
                "relation": r.relation,
                "format": r.format,
                "status": r.status,
                "episodes": r.episodes,
                "titles": r.titles,
                "anilist_id": r.anilist_id,
            }
            for r in media.relations
        ],
        "anilist_id": str(media.id),
        "anilist_url": media.anilist_url,
        "cover_url": media.cover_url,
        "banner_url": media.banner_url,
        "_source": "anilist",
    }


async def apply_franchise_totals(container: Container, franchise_data: dict) -> None:
    """Replace the immediate-children breakdown with totals computed across the
    whole AniList relation graph (every season/cour/movie/OVA/special/ONA).

    Extracted to module level so the batch handler can reuse the exact same logic
    without duplicating it.
    """
    anilist_id = franchise_data.get("anilist_id")
    if not anilist_id:
        return
    try:
        totals = await container.anilist.franchise_totals(int(anilist_id))
    except Exception:
        return
    franchise_data.update(
        franchise_seasons=totals.seasons,
        # Never let the graph walk zero-out a known episode count — keep the
        # main entry's own count (set by _media_to_franchise_dict) as a floor.
        franchise_episodes=totals.episodes or franchise_data.get("franchise_episodes"),
        franchise_movies=totals.movies,
        franchise_ovas=totals.ovas,
        franchise_onas=totals.onas,
        franchise_specials=totals.specials,
        franchise_spinoffs=totals.spin_offs,
    )
    # Franchise RATING = average score across the released/canonical franchise
    # (not a single entry's number). Only overwrite the single-entry score when
    # the walk actually produced an average, so a walk miss keeps the root score.
    avg = totals.avg_score
    if avg is not None:
        franchise_data["franchise_score"] = round(avg, 1)


async def enrich_with_tmdb(container: Container, franchise_data: dict, search_title: str) -> str | None:
    """Layer TMDB **artwork** onto an AniList-built franchise dict.

    Division of labour (updated):
      * AniList/dataset own titles, relations, episode/season counts, structure,
        AND the info-card **synopsis** — the confirm card and distribution card
        captions read the per-title AniList synopsis already in ``franchise_data``.
      * TMDB owns only the English 16:9 **backdrop** here. (The main-channel POST
        still uses TMDB's overview for its own caption via ``MainChannelService``,
        which searches TMDB independently — this function no longer touches the
        synopsis, so it can't flatten every distribution card to the TMDB blurb.)

    Returns the backdrop URL (English-tagged when available) or ``None``.

    Extracted to module level so the batch handler can reuse the exact same logic.
    """
    try:
        match = await container.tmdb.search(search_title)
    except Exception:
        return None
    if not match:
        return None
    return match.backdrop_url
