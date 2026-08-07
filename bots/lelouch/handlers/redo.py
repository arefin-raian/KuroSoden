"""Lelouch redo handler — owner-only re-processing of published titles.

The ``/redo`` command lets the owner trigger a stage-aware re-run of an anime
that already finished the pipeline. It works like ``/request`` (type name →
AniList search → confirm "this is it / not it") but submits as WORK (not a
request), bypasses the dedup guard (the whole point is to redo an existing
title), and calls :class:`RedoService.submit` to decide the cleanup strategy
from the pipeline stage (PUBLISHED keeps channel+posts, relinks fresh packs;
IN_PROGRESS_PREPUBLISH wipes fully; ABSENT behaves like a normal work item).

Design: mirrors ``requests.py`` AniList search flow with owner gating and a
distinct FSM namespace (``bot="lelouch_redo"``).
"""

from __future__ import annotations

import html

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.bots.admin.handlers.requests import (
    _media_to_franchise_dict,
    apply_franchise_totals,
    enrich_with_tmdb,
)
from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.services.auth_service import AuthService
from nekofetch.ui.artwork import (
    key_for_franchise,
    pick_artwork,
    seed_anime_art,
)
from nekofetch.ui.components import lock_buttons
from nekofetch.ui.progress import SPINNER, animate_until
from nekofetch.ui.screens import (
    Screen,
    choose_version,
    confirm_franchise,
    send_screen,
)

from kurosoden.shared import lelouch_voice as V

log = get_logger(__name__)

STATE_NAME = "redo:await_name"
STATE_FRANCHISE = "redo:franchise"


def _esc_q(text: str) -> str:
    return html.escape(text or "", quote=False)


async def _seed_anime_art(container: Container, franchise: dict, title: str) -> None:
    """Seed the per-anime artwork pool from TMDB gallery (same as requests.py)."""
    key = key_for_franchise(franchise, title=title)
    try:
        urls = await container.tmdb.backdrops(title, limit=8)
    except Exception:  # noqa: BLE001
        urls = []
    single = franchise.get("_backdrop_url") or franchise.get("banner_url")
    ordered = ([single] if single else []) + [u for u in urls if u != single]
    if ordered:
        franchise["backdrops"] = ordered
        seed_anime_art(key, ordered)


def _is_owner(message_or_query, container: Container) -> bool:
    """Check if the user is the owner (bypasses rate limits + access to /redo).

    Uses the ``nf_user`` object the auth middleware attaches to every update,
    matching the owner gate in ``bots/lelouch/app.py``.
    """
    user = getattr(message_or_query, "nf_user", None)
    if user is None:
        return False
    try:
        return AuthService(container).is_owner(user)
    except Exception:  # noqa: BLE001
        return False


def register(client: Client, container: Container) -> None:
    """Register Lelouch's /redo command + button handlers (owner-only)."""
    fsm = FSM(container.redis, bot="lelouch_redo")

    # ── /redo command ──────────────────────────────────────────────────────
    @client.on_message(filters.command("redo") & filters.private)
    async def _redo_cmd(_: Client, message: Message) -> None:
        if not _is_owner(message, container):
            await message.reply(V.OWNER_ONLY, parse_mode=ParseMode.HTML)
            return
        await fsm.set(message.from_user.id, STATE_NAME)
        await send_screen(
            client, message.chat.id,
            Screen(caption=V.REDO_ASK_TITLE, image=pick_artwork("lelouch")),
        )

    # ── Callback: redo button from admin panel ────────────────────────────
    @client.on_callback_query(filters.regex(r"^redo\|new"))
    async def _redo_new(_: Client, q: CallbackQuery) -> None:
        if not _is_owner(q, container):
            await q.answer(V.OWNER_ONLY, show_alert=True)
            return
        await fsm.set(q.from_user.id, STATE_NAME)
        await send_screen(
            client, q.message.chat.id,
            Screen(caption=V.REDO_ASK_TITLE, image=pick_artwork("lelouch")),
            old_msg=q.message,
        )
        await q.answer()

    # ── Text handler — AniList search (NO dedup guard) ────────────────────
    # group=3 so it runs independently of the request (group 0) and batch
    # (group 2) text handlers; it only acts when the owner is mid-/redo (FSM),
    # so it's inert for everyone else.
    @client.on_message(filters.text & filters.private, group=3)
    async def _text(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, _data = await fsm.get(message.from_user.id)
        if state in (STATE_NAME, STATE_FRANCHISE):
            if not _is_owner(message, container):
                return
            await _search_anilist(message, message.text.strip())

    # ── AniList search — identical to requests.py but NO dedup ────────────
    async def _search_anilist(message: Message, query: str) -> None:
        def _frame(f: str) -> str:
            from nekofetch.localization.messages import M, t
            return t(M.SEARCHING, query=_esc_q(query), frame=f)

        msg = await message.reply(_frame(SPINNER[0]), parse_mode=ParseMode.HTML)

        # 1. Resolve the title (AniList first)
        media = await animate_until(msg, container.anilist.search(query), _frame)
        if media is None:
            from kurosoden.shared.franchise_resolver import resolve_franchise

            franchise_data = await animate_until(
                msg, resolve_franchise(container, query), _frame
            )
            if franchise_data is None:
                from nekofetch.localization.messages import M, t
                await msg.edit_text(
                    t(M.SEARCH_NOT_FOUND, query=_esc_q(query)),
                    parse_mode=ParseMode.HTML,
                )
                return
            franchise_data["_query"] = query
            await fsm.set(
                message.from_user.id, STATE_FRANCHISE, franchise=franchise_data,
            )
            screen = confirm_franchise(franchise_data, namespace="redo")
            msg = await send_screen(client, message.chat.id, screen, old_msg=msg)
            return

        # 2. Detect adaptations
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
                message.from_user.id, "redo:versions",
                versions=versions, query=query, franchise=franchise_data,
            )
            screen = choose_version(
                query, versions, namespace="redo_ver", neither_ns="redo",
            )
            msg = await send_screen(client, message.chat.id, screen, old_msg=msg)
            return

        # 3. Single match — franchise totals + TMDB
        await apply_franchise_totals(container, franchise_data)
        backdrop_path = await enrich_with_tmdb(
            container, franchise_data, media.english or query
        )
        franchise_data["_backdrop_url"] = backdrop_path
        await _seed_anime_art(container, franchise_data, media.english or query)

        await fsm.set(
            message.from_user.id, STATE_FRANCHISE,
            franchise=franchise_data, query=query,
        )
        screen = confirm_franchise(
            franchise_data, backdrop_path=backdrop_path, namespace="redo",
        )
        msg = await send_screen(client, message.chat.id, screen, old_msg=msg)

    # ── Version picker ─────────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^redo_ver_pick\|"))
    async def _ver_pick(_: Client, q: CallbackQuery) -> None:
        if not _is_owner(q, container):
            await q.answer(V.OWNER_ONLY, show_alert=True)
            return
        await lock_buttons(q)
        _, args = q.data.split("|", 1)
        picked_id = args
        _, data = await fsm.get(q.from_user.id)
        versions = data.get("versions", [])
        query = data.get("query", "Anime")

        chosen = next(
            (v for v in versions if str(v.get("id")) == picked_id),
            versions[0] if versions else {},
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

        franchise_data["title"] = chosen.get("title", franchise_data.get("title", query))
        await apply_franchise_totals(container, franchise_data)
        backdrop_path = await enrich_with_tmdb(
            container, franchise_data, franchise_data.get("title") or query
        )
        franchise_data["_backdrop_url"] = backdrop_path
        await _seed_anime_art(container, franchise_data, franchise_data.get("title") or query)

        await fsm.set(
            q.from_user.id, STATE_FRANCHISE,
            franchise=franchise_data, query=query,
        )
        screen = confirm_franchise(
            franchise_data, backdrop_path=backdrop_path, namespace="redo",
        )
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^redo_ver_pick_both$"))
    async def _ver_pick_both(_: Client, q: CallbackQuery) -> None:
        # "Both" keeps the combined franchise resolved at search time — redo it
        # as a whole. Confirm with the already-stored franchise dict.
        if not _is_owner(q, container):
            await q.answer(V.OWNER_ONLY, show_alert=True)
            return
        await lock_buttons(q)
        _, data = await fsm.get(q.from_user.id)
        franchise_data = data.get("franchise", {})
        query = data.get("query", franchise_data.get("title", "Anime"))
        await fsm.set(
            q.from_user.id, STATE_FRANCHISE,
            franchise=franchise_data, query=query,
        )
        screen = confirm_franchise(franchise_data, namespace="redo")
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)

    # ── Confirm callbacks ──────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^redo_yes\|"))
    async def _yes(_: Client, q: CallbackQuery) -> None:
        if not _is_owner(q, container):
            await q.answer(V.OWNER_ONLY, show_alert=True)
            return
        await lock_buttons(q)
        await q.answer()
        await _finalize_redo(q)

    @client.on_callback_query(filters.regex(r"^redo_no$"))
    async def _no(_: Client, q: CallbackQuery) -> None:
        if not _is_owner(q, container):
            await q.answer(V.OWNER_ONLY, show_alert=True)
            return
        await lock_buttons(q)
        await fsm.set(q.from_user.id, STATE_NAME)
        await send_screen(
            client, q.message.chat.id,
            Screen(caption=V.REDO_RETRY_TITLE, image=pick_artwork("lelouch")),
            old_msg=q.message,
        )
        await q.answer()

    # ── Finalize redo ──────────────────────────────────────────────────────
    async def _finalize_redo(q: CallbackQuery) -> None:
        if not _is_owner(q, container):
            await q.answer(V.OWNER_ONLY, show_alert=True)
            return
        # By confirm time the franchise dict is already fully resolved (single
        # match, version pick, or "both"), so just read it — mirror requests.py.
        _, data = await fsm.get(q.from_user.id)
        franchise = data.get("franchise", {})
        query = data.get("query", "")

        title = franchise.get("title") or query
        anilist_id = franchise.get("anilist_id")
        if anilist_id is None:
            await send_screen(
                client, q.message.chat.id,
                Screen(caption="❌ Could not resolve an AniList ID for that title.",
                       image=pick_artwork("lelouch")),
                old_msg=q.message,
            )
            return
        anime_doc_id = str(anilist_id)

        # Kick off the stage-aware redo (terminate → notify → clean → re-queue).
        from kurosoden.shared.redo_service import RedoService

        plan = await RedoService(container).submit(
            q.from_user.id, title, anime_doc_id, franchise,
        )

        # Receipt card summarizing detected stage + action taken.
        state_label = {
            "published": "Published",
            "in_progress": "In Progress",
            "absent": "New",
        }.get(plan.state.value, plan.state.value.title())
        action = "relink packs in place" if plan.keep_channel else "full re-run"
        caption = V.redo_receipt(title, state_label, action)
        await send_screen(
            client, q.message.chat.id,
            Screen(caption=caption, image=pick_artwork("lelouch")),
            old_msg=q.message,
        )
        await fsm.clear(q.from_user.id)
