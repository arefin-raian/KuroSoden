"""Levi task handlers — the downloader's task list.

Levi does NOT reimplement the download flow. The real machinery — source pick,
website coverage report, seeders-ranked torrent picker, franchise-entry mapping,
and queueing — lives in NekoFetch's admin ``review`` handler, which
``register_all`` mounts onto this same client. This module owns the visible task
cards and drops confirmed source choices into that shared machinery.

So the flow the user sees is:

    Open a task  →  Levi-native request card
                 →  Pick source (Website / Torrent / Telegram-manual)
                 →  Read the source report / seeders list
                 →  Pick which franchise entries to pull
                 →  It queues; the background worker downloads + processes.
"""

from __future__ import annotations

import asyncio
import html
import json
from urllib.parse import quote

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, Message

from kurosoden.shared import levi_voice as V
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.ui.artwork import (
    ensure_anime_art,
    key_for_franchise,
    next_anime_art,
    pick_artwork,
)
from nekofetch.ui.components import cb, keyboard
from nekofetch.ui.screens import Screen, send_screen

log = get_logger(__name__)
LEVI_COMMANDS = ["start", "help", "tasks", "packcaptions", "settings"]


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _requester_label(req) -> str:
    user = getattr(req, "user", None)
    if user is None:
        return "unknown"
    name = getattr(user, "first_name", None) or getattr(user, "username", None) or "user"
    telegram_id = getattr(user, "telegram_id", None)
    return f"{name} ({telegram_id})" if telegram_id is not None else name


def _franchise_line(franchise: dict | None) -> str:
    fr = franchise or {}
    bits = []
    for key, label in (
        ("franchise_seasons", "season"),
        ("franchise_movies", "movie"),
        ("franchise_ovas", "OVA"),
        ("franchise_onas", "ONA"),
        ("franchise_specials", "special"),
    ):
        count = int(fr.get(key) or 0)
        if count:
            suffix = "" if count == 1 or label in {"OVA", "ONA"} else "s"
            bits.append(f"{count} {label}{suffix}")
    episodes = int(fr.get("franchise_episodes") or 0)
    if episodes:
        bits.append(f"{episodes} eps")
    return " · ".join(bits) if bits else "single entry"


def _expected_episodes(franchise: dict | None) -> int | None:
    fr = franchise or {}
    for key in ("franchise_episodes", "episodes", "episode_count"):
        val = fr.get(key)
        try:
            if val:
                return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _request_card(req, *, offered: bool = False) -> str:
    title = (req.franchise_data or {}).get("title") or req.anime_title
    header = "Optional download detail" if offered else "Download detail"
    body = (
        "Quiet-hour offer. Accept it if you're taking the cut now; reject it and the "
        "ladder moves without marking the request dead."
        if offered else
        "Report first if you want the source readout. Begin now if the job is clear."
    )
    return (
        f"{V.ICON} <b>{header}</b>\n\n"
        f"<blockquote><b>{_esc(title)}</b>\n"
        f"<code>{_esc(req.code)}</code>\n"
        f"<b>Requester:</b> {_esc(_requester_label(req))}\n"
        f"<b>Contents:</b> {_esc(_franchise_line(req.franchise_data))}\n"
        f"<b>Status:</b> {_esc(getattr(req.status, 'value', req.status))}</blockquote>\n\n"
        f"<i>{body}</i>"
    )


def _source_label(name: str) -> str:
    return {
        "kickassanime": "KickAss Anime",
        "anikoto": "AniKoto",
        "miruro": "Miruro",
        "anizone": "AniZone",
        "nyaa": "Nyaa",
        "telegram": "Telegram",
    }.get(name, name.title())


def _coverage_line(cov, expected: int | None) -> str:
    if not getattr(cov, "available", False):
        note = getattr(cov, "note", "") or "no match"
        return f"• <b>{_source_label(cov.source)}</b> — unavailable. <i>{_esc(note)}</i>"
    total = int(getattr(cov, "total_episodes", 0) or 0)
    sub = int(getattr(cov, "sub_episodes", 0) or 0)
    dub = int(getattr(cov, "dub_episodes", 0) or 0)
    dual = int(getattr(cov, "dual_episodes", 0) or 0)
    if dual:
        audio = f"{dual} dual"
    elif sub and dub:
        audio = f"{sub} sub · {dub} dub"
    elif dub:
        audio = f"{dub} dub"
    else:
        audio = f"{sub or total} sub"
    coverage = f"{total} ep"
    if expected:
        coverage = f"{total}/{expected} ep"
    # The verdict answers ONE question: can this source give us a dual-audio pack?
    #   • native dual track covering the run        → already dual
    #   • separate sub AND dub tracks both present   → can be MADE dual (merge)
    #   • only one language                          → single track (no dual)
    # (Short of AniList's count → also flag it's partial.)
    if dual >= (expected or total or 1) and dual:
        verdict = "dual ✓ (native)"
    elif sub and dub:
        verdict = "dual-capable (sub+dub → merge)"
    elif dub:
        verdict = "single track · dub only"
    else:
        verdict = "single track · sub only"
    if expected and total < expected:
        verdict = f"{verdict} · partial ({total}/{expected})"
    return f"• <b>{_source_label(cov.source)}</b> — {coverage} · {audio} · <i>{verdict}</i>"


def _report_recommendation(report, torrent_rows: list[dict], expected: int | None) -> str:
    coverages = list(getattr(report, "coverages", []) or [])
    full_dual = [
        c for c in coverages
        if getattr(c, "available", False)
        and int(getattr(c, "dual_episodes", 0) or 0) >= (expected or 1)
    ]
    if full_dual:
        return (
            f"{V.ICON} <b>Levi's call:</b> Use "
            f"<b>{_source_label(full_dual[0].source)}</b>. Full dual-audio coverage is visible."
        )
    paired = [
        c for c in coverages
        if getattr(c, "available", False)
        and int(getattr(c, "sub_episodes", 0) or 0)
        and int(getattr(c, "dub_episodes", 0) or 0)
        and (not expected or int(getattr(c, "total_episodes", 0) or 0) >= expected)
    ]
    if paired:
        return (
            f"{V.ICON} <b>Levi's call:</b> Website route is workable through "
            f"<b>{_source_label(paired[0].source)}</b>. Sub and dub coverage both show up."
        )
    top_seeders = int(torrent_rows[0].get("seeders", 0)) if torrent_rows else 0
    if top_seeders >= 10:
        return (
            f"{V.ICON} <b>Levi's call:</b> Use <b>Torrent</b>. Websites do not show "
            f"a clean dual backup, and the top Nyaa release has {top_seeders} seeders."
        )
    if torrent_rows:
        return (
            f"{V.ICON} <b>Levi's call:</b> Torrent is weak here. Top seeders: "
            f"{top_seeders}. Pick the cleanest website or take Telegram manual backup."
        )
    return (
        f"{V.ICON} <b>Levi's call:</b> No torrent readout. "
        "Use website first, Telegram if it fails."
    )


def register(client: Client, container: Container) -> None:
    """Register Levi's task list — the entry point into the shared download flow."""

    _active_reports: dict[str, asyncio.Task] = {}

    async def _render_tasks(chat_id: int, admin_id: int,
                            old_msg: Message | None = None) -> None:
        """Build and send the assigned-tasks screen with one Open button per task."""
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine
        from kurosoden.shared.work_service import STAGE_DOWNLOAD, WorkService
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.infrastructure.repositories.request_repo import RequestRepository

        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        active = await engine.get_active_tasks(admin_id, stage="levi")
        offers = await engine.get_pending_offers(admin_id, stage="levi")

        # The work line is the shared download queue: works added via Lelouch's
        # batch (or a redo) that haven't been claimed yet. Reconcile links first
        # so a work whose request already moved past download stops showing here.
        wsvc = WorkService(container.pg_sessionmaker)
        try:
            await wsvc.reconcile_links()
        except Exception:  # noqa: BLE001 — the board must never blank on bookkeeping
            pass
        try:
            works = await wsvc.list_open(stage=STAGE_DOWNLOAD, limit=10)
        except Exception:  # noqa: BLE001 - works table optional/absent
            works = []

        if not active and not offers and not works:
            screen = Screen(
                caption=(
                    "<b>⚔️ No download tasks right now.</b>\n\n"
                    "When a request is routed to you, it shows up here — tap it to "
                    "pick a source and start the download."
                ),
                image=pick_artwork("levi"),
                keyboard=keyboard([("⇐ Back", cb("levi", "home"))]),
            )
            await send_screen(client, chat_id, screen, old_msg=old_msg)
            return

        # Resolve titles in one session pass so the list reads like anime, not codes.
        titles: dict[str, str] = {}
        try:
            async with session_scope(container.pg_sessionmaker) as s:
                repo = RequestRepository(s)
                for a in [*offers[:5], *active[:10]]:
                    req = await repo.get_by_code(a.request_code)
                    titles[a.request_code] = req.anime_title if req else a.request_code
        except Exception:  # noqa: BLE001 - fall back to codes; never blank the list
            pass

        lines = ["<b>⚔️ Your Download Tasks</b>", ""]
        rows: list[tuple[str, str]] = []
        if offers:
            lines.append("<b>Pending offers</b>")
            for a in offers[:5]:
                title = titles.get(a.request_code, a.request_code)
                lines.append(f"Offer  <b>{title}</b>  <code>{a.request_code}</code>")
                rows.append((f"⚔️ Review offer · {title[:26]}",
                             cb("levi", "task", a.request_code)))
            lines.append("")
        if active:
            lines.append("<b>Assigned</b>")
        for a in active[:10]:
            icon = "🔄" if a.status == "in_progress" else "⏳"
            title = titles.get(a.request_code, a.request_code)
            lines.append(f"{icon}  <b>{title}</b>  ·  <code>{a.request_code}</code>")
            rows.append((f"▶️ Open · {title[:28]}",
                         cb("levi", "task", a.request_code)))
        if works:
            if active:
                lines.append("")
            lines.append("<b>Work line</b>")
            for w in works:
                icon = "🛠️" if w.status == "open" else "🔨"
                lines.append(
                    f"{icon}  <b>{w.anime_title}</b>  ·  <code>{w.code}</code>"
                )
                rows.append((f"▶️ Open · {w.anime_title[:28]}",
                             cb("levi", "task", w.code)))

        lines += ["", "<i>Tap a task to pick a source and begin.</i>"]
        # One Open button per row, then a Back control.
        kb_rows = [[r] for r in rows]
        kb_rows.append([("⇐ Back", cb("levi", "home"))])
        screen = Screen(
            caption="\n".join(lines),
            image=pick_artwork("levi"),
            keyboard=keyboard(*kb_rows),
        )
        await send_screen(client, chat_id, screen, old_msg=old_msg)

    async def _load_request(code: str):
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.infrastructure.repositories.request_repo import RequestRepository

        async with session_scope(container.pg_sessionmaker) as session:
            return await RequestRepository(session).get_by_code(code)

    async def _has_pending_offer(admin_id: int, code: str) -> bool:
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        offers = await engine.get_pending_offers(admin_id, stage="levi")
        return any(a.request_code == code for a in offers)

    async def _anime_image(req) -> object:
        franchise = req.franchise_data or {}
        title = franchise.get("title") or req.anime_title
        art_key = key_for_franchise(franchise, title=title)
        await ensure_anime_art(art_key, tmdb=container.tmdb, title=title, franchise=franchise)
        return next_anime_art(art_key, fallback_bot="levi")

    async def _nyaa_rows(title: str) -> list[dict]:
        source = container.sources.get("nyaa")
        if source is None:
            return []
        try:
            stubs = await source.search(title)
        except Exception as exc:  # noqa: BLE001
            log.warning("levi.nyaa_report.failed", title=title, error=str(exc))
            return []
        rows: list[dict] = []
        for stub in stubs[:8]:
            try:
                info = json.loads(stub.source_ref)
            except (TypeError, json.JSONDecodeError):
                info = {}
            rows.append({
                "title": getattr(stub, "title", "") or info.get("title", "release"),
                "seeders": int(info.get("seeders") or 0),
                "size": info.get("size_text") or "",
                "dual": bool(info.get("dual_audio")),
            })
        return rows

    async def _build_report_caption(req) -> str:
        from nekofetch.services.website_report import build_website_report

        franchise = req.franchise_data or {}
        title = franchise.get("title") or req.anime_title
        expected = _expected_episodes(franchise)
        report = await build_website_report(
            container,
            title=title,
            franchise=franchise,
            skip_anizone=True,
        )
        torrents = await _nyaa_rows(title)
        search_url = f"https://anizone.to/anime?search={quote(title)}"

        lines = [
            f"{V.ICON} <b>Source Report</b>",
            "",
            f"<blockquote><b>{_esc(title)}</b>",
            f"<code>{_esc(req.code)}</code>",
            (
                f"<b>Expected:</b> {_esc(expected or 'unknown')} episode(s) · "
                f"{_esc(_franchise_line(franchise))}</blockquote>"
            ),
            "",
            "<b>Websites</b>",
        ]
        lines.extend(_coverage_line(c, expected) for c in report.coverages)
        lines += [
            (
                f"• <b>AniZone</b> — manual inspection. "
                f"<a href=\"{_esc(search_url)}\">Click here to view search results</a>; "
                "the structure is inconsistent, so a human has to verify the right entry."
            ),
            "",
            "<b>Torrent · Nyaa</b>",
        ]
        if torrents:
            lines.append(f"Found <b>{len(torrents)}</b> ranked release(s). Top seeders:")
            for row in torrents[:5]:
                badge = "dual" if row["dual"] else "single"
                lines.append(
                    f"• <b>{row['seeders']}S</b> · {_esc(row['size'])} · "
                    f"{badge} · {_esc(row['title'])[:90]}"
                )
        else:
            lines.append("No ranked Nyaa release came back.")
        lines += [
            "",
            "<b>Telegram Manual</b>",
            "Use it when a website episode is dirty or missing. Send files low to high "
            "(360/480 bucket, 720, 1080). If only 1080 arrives, processing derives the "
            "missing lower qualities; 480/540 can satisfy the 360 bucket without re-encode.",
            "",
            _report_recommendation(report, torrents, expected),
        ]
        return "\n".join(lines)

    async def _render_source_picker(
        chat_id: int,
        code: str,
        old_msg: Message | None = None,
    ) -> None:
        req = await _load_request(code)
        if req is None:
            await _render_tasks(chat_id, 0, old_msg=old_msg)
            return
        title = (req.franchise_data or {}).get("title") or req.anime_title
        caption = (
            f"{V.ICON} <b>Pick the route.</b>\n\n"
            f"<blockquote><b>{_esc(title)}</b>\n<code>{_esc(code)}</code></blockquote>\n\n"
            "Telegram is manual intake. Website lets you pick KickAss, AniKoto, Miruro, "
            "or AniZone. Torrent opens the Nyaa release board. Direct Link takes an "
            "archive link (zip/rar/7z) instead."
        )
        kb = keyboard(
            [(V.BTN_SRC_TELEGRAM, cb("levi", "telegram", code)),
             ("🌐 Website", cb("levi", "website", code))],
            [(V.BTN_SRC_TORRENT, cb("staff", "rsource", code, "torrent")),
             (V.BTN_SRC_DDL, cb("staff", "rsource", code, "ddl"))],
        )
        await send_screen(
            client,
            chat_id,
            Screen(caption=caption, image=await _anime_image(req), keyboard=kb),
            old_msg=old_msg,
        )

    async def _render_website_picker(
        chat_id: int,
        code: str,
        old_msg: Message | None = None,
    ) -> None:
        req = await _load_request(code)
        if req is None:
            await _render_tasks(chat_id, 0, old_msg=old_msg)
            return
        title = (req.franchise_data or {}).get("title") or req.anime_title
        caption = (
            f"{V.ICON} <b>Website source.</b>\n\n"
            f"<blockquote><b>{_esc(title)}</b>\n<code>{_esc(code)}</code></blockquote>\n\n"
            "Pick the primary source. Levi keeps the other compatible websites behind it "
            "as fallback for missing or dirty episodes."
        )
        kb = keyboard(
            [(V.BTN_SRC_KICKASS,
              cb("staff", "rsiteprio", code, "kickassanime")),
             (V.BTN_SRC_ANIKOTO,
              cb("staff", "rsiteprio", code, "anikoto"))],
            [("Miruro",
              cb("staff", "rsiteprio", code, "miruro")),
             (V.BTN_SRC_ANIZONE, cb("levi", "anizone", code))],
            [(V.BTN_BACK, cb("levi", "sources", code))],
        )
        await send_screen(
            client,
            chat_id,
            Screen(caption=caption, image=await _anime_image(req), keyboard=kb),
            old_msg=old_msg,
        )

    async def _render_anizone_card(
        chat_id: int,
        code: str,
        old_msg: Message | None = None,
    ) -> None:
        req = await _load_request(code)
        if req is None:
            await _render_tasks(chat_id, 0, old_msg=old_msg)
            return
        title = (req.franchise_data or {}).get("title") or req.anime_title
        search_url = f"https://anizone.to/anime?search={quote(title)}"
        caption = (
            f"{V.ICON} <b>AniZone needs hands.</b>\n\n"
            f"<blockquote><b>{_esc(title)}</b>\n<code>{_esc(code)}</code></blockquote>\n\n"
            f"<a href=\"{_esc(search_url)}\">Click here to view the search results</a>. "
            "Try title variations, open the correct entry, then use the mapping prompt. "
            "AniZone does not follow a clean pattern, so the bot waits for your verified slug."
        )
        kb = keyboard(
            [("Map AniZone Entry", cb("staff", "rsource", code, "website"))],
            [(V.BTN_BACK, cb("levi", "website", code))],
        )
        await send_screen(
            client,
            chat_id,
            Screen(caption=caption, image=await _anime_image(req), keyboard=kb),
            old_msg=old_msg,
        )

    async def _render_telegram_card(
        chat_id: int,
        code: str,
        old_msg: Message | None = None,
    ) -> None:
        req = await _load_request(code)
        if req is None:
            await _render_tasks(chat_id, 0, old_msg=old_msg)
            return
        title = (req.franchise_data or {}).get("title") or req.anime_title
        caption = (
            f"{V.ICON} <b>Telegram manual.</b>\n\n"
            f"<blockquote><b>{_esc(title)}</b>\n<code>{_esc(code)}</code></blockquote>\n\n"
            f"{V.TELEGRAM_NOTE}\n\n"
            "Send packs low to high. The 360 slot accepts 360, 480, or 540 without "
            "forcing a second encode."
        )
        kb = keyboard(
            [("Begin Manual Upload", cb("staff", "rtgmode", code, "manual"))],
            [(V.BTN_BACK, cb("levi", "sources", code))],
        )
        await send_screen(
            client,
            chat_id,
            Screen(caption=caption, image=await _anime_image(req), keyboard=kb),
            old_msg=old_msg,
        )

    async def _render_detail(
        chat_id: int,
        code: str,
        old_msg: Message | None = None,
        *,
        offered: bool = False,
    ) -> None:
        req = await _load_request(code)
        if req is None:
            screen = Screen(
                caption=f"{V.ICON} <b>Request not found.</b>\n\n<code>{_esc(code)}</code>",
                image=pick_artwork("levi"),
                keyboard=keyboard([("⇐ Back", cb("levi", "tasks"))]),
            )
            await send_screen(client, chat_id, screen, old_msg=old_msg)
            return

        if offered:
            kb = keyboard(
                [("Accept", cb("levi", "offer", "accept", code)),
                 ("Reject", cb("levi", "offer", "reject", code))],
                [("⇐ Tasks", cb("levi", "tasks"))],
            )
        else:
            kb = keyboard(
                [(V.BTN_REPORT, cb("levi", "report", code))],
                [("▶ Begin Now", cb("levi", "sources", code))],
            )
        screen = Screen(
            caption=_request_card(req, offered=offered),
            image=await _anime_image(req),
            keyboard=kb,
        )
        await send_screen(client, chat_id, screen, old_msg=old_msg)

    # ── /tasks — the assigned-task list ────────────────────────────────────
    @client.on_message(filters.command("tasks"))
    async def _tasks_cmd(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        await _render_tasks(message.chat.id, message.from_user.id)

    # ── levi|tasks — same list, from the inline menu ───────────────────────
    @client.on_callback_query(filters.regex(r"^levi\|tasks$"))
    async def _tasks_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None or q.from_user is None:
            await q.answer()
            return
        await q.answer()
        await _render_tasks(q.message.chat.id, q.from_user.id, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^levi\|task\|"))
    async def _task_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None or q.from_user is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        # A work item (WRK-N) from the shared work line may not have a bridged
        # request yet (legacy item, or a bridge that failed silently). Resolve
        # it lazily — reuse the anime's live request when one exists, otherwise
        # create the bridge — so the review/download flow always gets a REQ.
        if code.startswith("WRK-"):
            from kurosoden.shared.work_bridge import ensure_work_request

            req = await ensure_work_request(container, code, q.from_user.id)
            if req is None:
                await q.answer("Work item not found.", show_alert=True)
                await _render_tasks(
                    q.message.chat.id, q.from_user.id, old_msg=q.message,
                )
                return
            code = req.code
        offered = await _has_pending_offer(q.from_user.id, code)
        await q.answer()
        await _render_detail(q.message.chat.id, code, old_msg=q.message, offered=offered)

    @client.on_callback_query(filters.regex(r"^levi\|report\|"))
    async def _report_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        req = await _load_request(code)
        if req is None:
            await q.answer("Request not found.", show_alert=True)
            return
        await q.answer()

        cancel_key = f"{q.message.chat.id}:{code}"
        old_task = _active_reports.pop(cancel_key, None)
        if old_task and not old_task.done():
            old_task.cancel()

        back_kb = keyboard([(V.BTN_BACK, cb("levi", "reportcancel", code))])

        async def _do_report():
            return (
                await _build_report_caption(req),
                await _anime_image(req),
            )

        task = asyncio.create_task(_do_report())
        _active_reports[cancel_key] = task

        _ANIM_FRAMES = [
            f"{V.ICON} <b>Scanning sources.</b>",
            f"{V.ICON} <b>Scanning sources..</b>",
            f"{V.ICON} <b>Scanning sources...</b>",
        ]

        frame = 0
        try:
            while not task.done():
                caption = _ANIM_FRAMES[frame % len(_ANIM_FRAMES)]
                try:
                    await client.edit_message_caption(
                        q.message.chat.id, q.message.id,
                        caption=caption, parse_mode=ParseMode.HTML,
                        reply_markup=back_kb,
                    )
                except MessageNotModified:
                    pass
                except Exception:
                    pass
                frame += 1
                await asyncio.sleep(0.8)

            caption_text, image = await task
        except asyncio.CancelledError:
            return
        finally:
            _active_reports.pop(cancel_key, None)

        screen = Screen(
            caption=caption_text,
            image=image,
            keyboard=keyboard(
                [("Pick Source", cb("levi", "sources", code))],
                [(V.BTN_BACK, cb("levi", "task", code))],
            ),
        )
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^levi\|reportcancel\|"))
    async def _report_cancel_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        await q.answer()

        cancel_key = f"{q.message.chat.id}:{code}"
        task = _active_reports.pop(cancel_key, None)
        if task and not task.done():
            task.cancel()

        offered = await _has_pending_offer(q.from_user.id, code) if q.from_user else False
        await _render_detail(q.message.chat.id, code, old_msg=q.message, offered=offered)

    @client.on_callback_query(filters.regex(r"^levi\|sources\|"))
    async def _sources_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        await q.answer()
        await _render_source_picker(q.message.chat.id, code, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^levi\|website\|"))
    async def _website_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        await q.answer()
        await _render_website_picker(q.message.chat.id, code, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^levi\|telegram\|"))
    async def _telegram_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        await q.answer()
        await _render_telegram_card(q.message.chat.id, code, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^levi\|anizone\|"))
    async def _anizone_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        await q.answer()
        await _render_anizone_card(q.message.chat.id, code, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^levi\|offer\|"))
    async def _offer_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None or q.from_user is None:
            await q.answer()
            return
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        parts = (q.data or "").split("|", 3)
        action = parts[2] if len(parts) > 2 else ""
        code = parts[3] if len(parts) > 3 else ""
        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        if action == "accept":
            result = await engine.accept_offer(code, "levi", q.from_user.id)
            await q.answer("Accepted." if result else "Offer expired.", show_alert=result is None)
            if result:
                await _render_detail(q.message.chat.id, code, old_msg=q.message)
                return
        elif action == "reject":
            ok = await engine.reject_offer(code, "levi", q.from_user.id)
            await q.answer("Rejected." if ok else "Offer expired.", show_alert=not ok)
        else:
            await q.answer()
        await _render_tasks(q.message.chat.id, q.from_user.id, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^levi\|decline\|"))
    async def _decline_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None or q.from_user is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        if container.redis:
            await container.redis.set(f"nf:levi:decline:{q.from_user.id}", code, ex=1800)
        kb = keyboard(
            [("⇐ Back to request", cb("levi", "task", code))],
            [("⇐ Tasks", cb("levi", "tasks"))],
        )
        await q.answer()
        await send_screen(
            client,
            q.message.chat.id,
            Screen(
                caption=(
                    f"{V.ICON} <b>Reason required.</b>\n\n"
                    f"<code>{_esc(code)}</code>\n"
                    "Send the reason in one message. The owner gets it through Lelouch; "
                    "the request stays alive until the owner cancels or reassigns it."
                ),
                image=pick_artwork("levi"),
                keyboard=kb,
            ),
            old_msg=q.message,
        )

    @client.on_message(filters.text & ~filters.command(LEVI_COMMANDS))
    async def _decline_reason(_: Client, message: Message) -> None:
        if message.from_user is None or container.redis is None:
            return
        key = f"nf:levi:decline:{message.from_user.id}"
        code = await container.redis.get(key)
        if not code:
            return
        if isinstance(code, bytes):
            code = code.decode()
        await container.redis.delete(key)
        reason = (message.text or "").strip()
        if not reason:
            return
        req = await _load_request(code)
        title = (req.franchise_data or {}).get("title") if req else None
        title = title or (req.anime_title if req else code)
        owner_id = None
        try:
            from kurosoden.shared.owner_seed import _owner_id

            owner_id = _owner_id(container)
        except Exception:  # noqa: BLE001
            owner_id = None
        notifier = getattr(getattr(container, "pipeline_manager", None), "lelouch", None)
        if owner_id and notifier is not None:
            admin_name = _esc(
                message.from_user.first_name or message.from_user.username or "user"
            )
            await notifier.send_message(
                int(owner_id),
                (
                    "♟️ <b>Levi decline request</b>\n\n"
                    f"<b>Anime:</b> {_esc(title)}\n"
                    f"<b>Request:</b> <code>{_esc(code)}</code>\n"
                    f"<b>Admin:</b> {admin_name} (<code>{message.from_user.id}</code>)\n\n"
                    f"<blockquote>{_esc(reason)}</blockquote>\n\n"
                    "Decide whether to cancel the series or reassign it."
                ),
                parse_mode=ParseMode.HTML,
            )
        await message.reply_text(
            f"{V.ICON} <b>Sent to owner.</b>\n\nThe request is still alive.",
            parse_mode=ParseMode.HTML,
        )
