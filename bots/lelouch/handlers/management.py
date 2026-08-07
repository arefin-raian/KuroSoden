"""Lelouch management surface — the admin-pool control plane.

Owns every ``mg|`` callback behind Command's *Manage Ranks*, *Availability*, and
*Working Hours* cards, plus the render helpers those three entry points (routed
from ``app.py``'s ``_menu``) call. All state flows through
:class:`ManagementService`, so this module is pure Telegram I/O + composition.

Callback grammar (all ``mg|``):
  roster                     → the pool overview
  adm|<id>                   → one admin's control card
  addlist / addid|<id>       → muster an env-admin into the pool
  rm|<id>                    → discharge from the pool
  bot|<id>|<stage>           → toggle a stage on/off for that admin
  wt|<id>|<+1|-1>            → nudge assignment weight
  av|<id>                    → flip availability (on ⇄ off the field)
  brk|<id> / endbrk|<id>     → grant / end a 1-hour leave
  hrs|<id>                   → open the working-hours picker
  sethrs|<id>|<s>|<e> / clrhrs|<id>
  mode|<normal|catch-up|paused>  → set campaign tempo

Reassignment lives on Levi/Senku/Gojo's task cards (they know the stuck code);
:meth:`ManagementService.reassign` is the shared primitive.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery

from nekofetch.core.container import Container
from nekofetch.domain.enums import Role
from nekofetch.ui.artwork import pick_artwork
from nekofetch.ui.components import cb
from nekofetch.ui.screens import Message, card, send_screen

from kurosoden.shared import lelouch_voice as V
from kurosoden.shared.management_service import STAGES, ManagementService
from kurosoden.shared.request_gate import VALID_MODES, get_mode, set_mode

import structlog

log = structlog.get_logger(__name__)

BOT = "lelouch"
# Preset UTC working windows offered by the hours picker.
_HOUR_PRESETS = [(0, 8), (8, 16), (16, 24), (22, 6)]


def _svc(container: Container) -> ManagementService:
    return ManagementService(container.pg_sessionmaker)


def _art():
    return pick_artwork("lelouch")


async def req_wrk_rows(container: Container, limit: int = 20):
    """Combined REQ + WRK rows for the owner's Manage REQ/WRK console.

    Returns ``(rows, total)`` where each row is ``(label, callback_data)``
    and the label's leading code makes REQ vs WRK visible (``REQ-1073 …`` /
    ``WRK-42 …``). REQ rows come from ``RequestService.list_active``; WRK
    rows from :meth:`WorkService.list_open`. Both detail cards are reached
    through the same ``mg|reqdet|<code>`` callback — the handler branches on
    the ``WRK-`` prefix and routes destructive actions to ``WorkService``
    instead of ``RequestService``.
    """
    from kurosoden.shared.work_service import WorkService
    from nekofetch.services.request_service import RequestService

    reqs = await RequestService(container).list_active(limit=limit)
    works = await WorkService(container.pg_sessionmaker).list_open(limit=limit)
    rows: list[tuple[str, str]] = []
    for r in reqs:
        st = r.status.value if hasattr(r.status, "value") else str(r.status)
        label = f"{V.esc(r.code)} · {V.esc((r.anime_title or '')[:22])} [{st}]"
        rows.append((label, cb("mg", "reqdet", r.code)))
    for w in works:
        label = (f"{V.esc(w.code)} · {V.esc((w.anime_title or '')[:22])} "
                 f"[{V.esc(w.status)}]")
        rows.append((label, cb("mg", "reqdet", w.code)))
    return rows, len(reqs) + len(works)


def _staff(obj) -> bool:
    user = getattr(obj, "nf_user", None)
    if user is None:
        return False
    try:
        return Role(user.role) in (Role.STAFF, Role.ADMIN)
    except Exception:  # noqa: BLE001
        return False


# ── Render helpers (called from app.py's _menu entry points too) ──────────────

async def render_manage(client: Client, container: Container, chat_id: int,
                        old_msg: Message | None) -> None:
    admins = await _svc(container).list_admins()
    rows = [[(V.esc(a.name or str(a.telegram_id))[:24], cb("mg", "adm", a.telegram_id))]
            for a in admins]
    rows.append([(V.BTN_ADD_ADMIN, cb("mg", "addlist"))])
    rows.append([(V.BTN_BACK_ADMIN, cb(BOT, "admin"))])
    await send_screen(client, chat_id,
                      card(V.manage_roster(admins), image=_art(), bot_name=BOT,
                           buttons=rows), old_msg=old_msg)


async def render_availability(client: Client, container: Container, chat_id: int,
                              old_msg: Message | None) -> None:
    admins = await _svc(container).list_admins()
    rows = []
    for a in admins:
        glyph = "🔴" if not a.is_available else ("☕" if a.on_break else "🟢")
        rows.append([(f"{glyph} {V.esc(a.name or str(a.telegram_id))[:22]}",
                      cb("mg", "av", a.telegram_id))])
    rows.append([(V.BTN_BACK_ADMIN, cb(BOT, "admin"))])
    await send_screen(client, chat_id,
                      card(V.availability_board(admins), image=_art(), bot_name=BOT,
                           buttons=rows), old_msg=old_msg)


async def render_hours(client: Client, container: Container, chat_id: int,
                       old_msg: Message | None) -> None:
    admins = await _svc(container).list_admins()
    mode = await get_mode(container)
    rows = [[(V.BTN_MODE_NORMAL, cb("mg", "mode", "normal")),
             (V.BTN_MODE_CATCHUP, cb("mg", "mode", "catch-up")),
             (V.BTN_MODE_PAUSED, cb("mg", "mode", "paused"))]]
    for a in admins:
        rows.append([(f"🕰 {V.esc(a.name or str(a.telegram_id))[:22]}",
                      cb("mg", "hrs", a.telegram_id))])
    rows.append([(V.BTN_BACK_ADMIN, cb(BOT, "admin"))])
    await send_screen(client, chat_id,
                      card(V.hours_board(admins, mode), image=_art(), bot_name=BOT,
                           buttons=rows), old_msg=old_msg)


async def _render_detail(client: Client, container: Container, chat_id: int,
                         admin_id: int, old_msg: Message | None) -> None:
    v = await _svc(container).get_admin(admin_id)
    if v is None:
        await render_manage(client, container, chat_id, old_msg)
        return
    # Stage coverage toggles: ✓ when assigned.
    stage_row = [
        (f"{'✓' if s in v.assigned_bots else '＋'} {V.stage_label(s)}",
         cb("mg", "bot", admin_id, s))
        for s in STAGES
    ]
    rows = [stage_row[:2], stage_row[2:]]
    rows.append([(V.BTN_WEIGHT_DOWN, cb("mg", "wt", admin_id, -1)),
                 (V.BTN_WEIGHT_UP, cb("mg", "wt", admin_id, 1))])
    avail_btn = ((V.BTN_TAKE_OFF, cb("mg", "av", admin_id)) if v.is_available
                 else (V.BTN_PUT_ON, cb("mg", "av", admin_id)))
    brk_btn = ((V.BTN_END_BREAK, cb("mg", "endbrk", admin_id)) if v.on_break
               else (V.BTN_BREAK, cb("mg", "brk", admin_id)))
    rows.append([avail_btn, brk_btn])
    rows.append([(V.BTN_SET_HOURS, cb("mg", "hrs", admin_id)),
                 (V.BTN_REMOVE_ADMIN, cb("mg", "rm", admin_id))])
    if v.active_tasks > 0:
        rows.append([("↪️ Reassign a task", cb("mg", "reasgn", admin_id))])
    rows.append([(V.BTN_BACK_MANAGE, cb("mg", "roster"))])
    await send_screen(client, chat_id,
                      card(V.manage_admin_detail(v), image=_art(), bot_name=BOT,
                           buttons=rows), old_msg=old_msg)


def register(client: Client, container: Container) -> None:
    """Wire the management control plane onto the Pyrogram client."""

    async def _guard(q: CallbackQuery) -> bool:
        if q.message is None:
            await q.answer()
            return False
        if not _staff(q):
            await q.answer("🔒 Command is staff only.", show_alert=True)
            return False
        return True

    def _parts(q: CallbackQuery) -> list[str]:
        return q.data.split("|")

    # ── Roster / detail navigation ────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^mg\|roster$"))
    async def _roster(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        await q.answer()
        await render_manage(client, container, q.message.chat.id, q.message)

    @client.on_callback_query(filters.regex(r"^mg\|adm\|"))
    async def _detail(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        await q.answer()
        admin_id = int(_parts(q)[2])
        await _render_detail(client, container, q.message.chat.id, admin_id, q.message)

    # ── Muster / discharge ────────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^mg\|addlist$"))
    async def _addlist(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        await q.answer()
        env_admins = list(getattr(container.env, "admin_ids", []) or [])
        existing = {a.telegram_id for a in await _svc(container).list_admins()}
        candidates = [a for a in env_admins if a not in existing]
        if not candidates:
            caption = (
                f"{V.ICON} <b>All hands accounted for.</b>\n\n"
                "Every configured admin is already in the pool. Add more IDs to "
                "<code>ADMIN_IDS</code> to expand the ranks.\n\n"
                "<i>A commander works with the army he has.</i>"
            )
            rows = [[(V.BTN_BACK_MANAGE, cb("mg", "roster"))]]
        else:
            caption = (
                f"{V.ICON} <b>Muster a rank</b>\n\n"
                "These configured admins aren't in the pool yet. Tap one to bring "
                "them in — they'll cover no stages until you assign them.\n\n"
                "<i>Choose who fights, and where.</i>"
            )
            # Show each candidate by NAME, not raw id — everyone has a name, not
            # everyone a username. Best-effort lookup via the bot; the callback
            # still carries the id (that's what we write).
            rows = []
            for aid in candidates:
                label = str(aid)
                try:
                    u = await client.get_users(aid)
                    label = (u.first_name or "") or (u.username or str(aid))
                except Exception:  # noqa: BLE001 — fall back to id if unreachable
                    pass
                rows.append([(f"➕ {V.esc(label)[:28]}", cb("mg", "addid", aid))])
            rows.append([(V.BTN_BACK_MANAGE, cb("mg", "roster"))])
        await send_screen(client, q.message.chat.id,
                          card(caption, image=_art(), bot_name=BOT, buttons=rows),
                          old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|addid\|"))
    async def _addid(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        admin_id = int(_parts(q)[2])
        # Best-effort name lookup for a friendlier roster.
        name = None
        try:
            u = await client.get_users(admin_id)
            name = (u.first_name or "") or (u.username or None)
        except Exception:  # noqa: BLE001
            pass
        await _svc(container).ensure_admin(admin_id, name=name)
        await q.answer("Mustered.")
        await _render_detail(client, container, q.message.chat.id, admin_id, q.message)

    @client.on_callback_query(filters.regex(r"^mg\|rm\|"))
    async def _remove(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        admin_id = int(_parts(q)[2])
        await _svc(container).remove_admin(admin_id)
        await q.answer("Discharged.")
        await render_manage(client, container, q.message.chat.id, q.message)

    # ── Stage coverage / weight ───────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^mg\|bot\|"))
    async def _toggle_bot(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        _, _, aid, stage = _parts(q)
        await _svc(container).toggle_bot(int(aid), stage)
        await q.answer()
        await _render_detail(client, container, q.message.chat.id, int(aid), q.message)

    @client.on_callback_query(filters.regex(r"^mg\|wt\|"))
    async def _weight(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        _, _, aid, delta = _parts(q)
        svc = _svc(container)
        v = await svc.get_admin(int(aid))
        if v is not None:
            await svc.set_weight(int(aid), v.weight + int(delta))
        await q.answer()
        await _render_detail(client, container, q.message.chat.id, int(aid), q.message)

    # ── Availability / breaks ─────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^mg\|av\|"))
    async def _avail(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        admin_id = int(_parts(q)[2])
        v = await _svc(container).toggle_available(admin_id)
        await q.answer("On the field." if (v and v.is_available) else "Stood down.")
        # Return to whichever board they came from — detail if that's where the
        # tap originated (the button lives on both). Re-render the detail card;
        # it always has a back-link to the roster/availability boards.
        await _render_detail(client, container, q.message.chat.id, admin_id, q.message)

    @client.on_callback_query(filters.regex(r"^mg\|brk\|"))
    async def _break(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        admin_id = int(_parts(q)[2])
        v = await _svc(container).schedule_break(admin_id, hours=1.0, reason="leave")
        await q.answer("Leave granted (1h).")
        if v is not None:
            await send_screen(
                client, q.message.chat.id,
                card(V.break_scheduled(v.name or str(admin_id), 1.0), image=_art(),
                     bot_name=BOT,
                     buttons=[[(V.BTN_END_BREAK, cb("mg", "endbrk", admin_id))],
                              [(V.BTN_BACK_MANAGE, cb("mg", "adm", admin_id))]]),
                old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|endbrk\|"))
    async def _end_break(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        admin_id = int(_parts(q)[2])
        await _svc(container).clear_breaks(admin_id)
        await q.answer("Back on the field.")
        await _render_detail(client, container, q.message.chat.id, admin_id, q.message)

    # ── Working hours ─────────────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^mg\|hrs\|"))
    async def _hours(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        admin_id = int(_parts(q)[2])
        v = await _svc(container).get_admin(admin_id)
        name = V.esc(v.name or str(admin_id)) if v else str(admin_id)
        caption = (
            f"{V.ICON} <b>Working hours — {name}</b>\n\n"
            "Bound this soldier to a UTC window, or set them always-on. The "
            "assignment engine and the idle nudge both honour it.\n\n"
            "<i>Never rouse a resting blade.</i>"
        )
        rows = [
            [(f"{s:02d}–{e:02d} UTC", cb("mg", "sethrs", admin_id, s, e))]
            for s, e in _HOUR_PRESETS
        ]
        rows.append([(V.BTN_CLEAR_HOURS, cb("mg", "clrhrs", admin_id))])
        rows.append([(V.BTN_BACK_MANAGE, cb("mg", "adm", admin_id))])
        await send_screen(client, q.message.chat.id,
                          card(caption, image=_art(), bot_name=BOT, buttons=rows),
                          old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|sethrs\|"))
    async def _set_hours(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        _, _, aid, s, e = _parts(q)
        await _svc(container).set_hours(int(aid), int(s), int(e))
        await q.answer(f"Hours set: {int(s):02d}–{int(e):02d} UTC.")
        await _render_detail(client, container, q.message.chat.id, int(aid), q.message)

    @client.on_callback_query(filters.regex(r"^mg\|clrhrs\|"))
    async def _clear_hours(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        admin_id = int(_parts(q)[2])
        await _svc(container).set_hours(admin_id, None, None)
        await q.answer("Always on.")
        await _render_detail(client, container, q.message.chat.id, admin_id, q.message)

    # ── Campaign mode (tempo) ─────────────────────────────────────────────────
    @client.on_callback_query(filters.regex(r"^mg\|mode\|"))
    async def _mode(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        mode = _parts(q)[2]
        mode = mode if mode in VALID_MODES else "normal"
        await set_mode(container, mode)
        # Pausing the campaign also closes the request gate — the two are one
        # gesture from the admin's view.
        if mode == "paused":
            from kurosoden.shared.request_gate import set_requests_open
            await set_requests_open(container, False)
        await q.answer(f"Tempo: {mode}.")
        await render_hours(client, container, q.message.chat.id, q.message)

    # ── Reassignment ──────────────────────────────────────────────────────────
    # Move a stuck request off one admin onto another who covers the same stage.
    # Flow: reasgn|<from_id> → pick a task → reto|<code>|<stage>|<from_id> → pick
    # a target → redo|<code>|<stage>|<to_id> → reassign + notify both admins.
    @client.on_callback_query(filters.regex(r"^mg\|reasgn\|"))
    async def _reassign_pick_task(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        from_id = int(_parts(q)[2])
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine
        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        tasks = await engine.get_active_tasks(from_id)
        if not tasks:
            await q.answer("No active tasks to move.", show_alert=True)
            await _render_detail(client, container, q.message.chat.id, from_id, q.message)
            return
        await q.answer()
        rows = [
            [(f"↪️ {V.esc(t.request_code)} · {V.stage_label(t.stage)}",
              cb("mg", "reto", t.request_code, t.stage, from_id))]
            for t in tasks[:10]
        ]
        rows.append([(V.BTN_BACK_MANAGE, cb("mg", "adm", from_id))])
        caption = (
            f"{V.ICON} <b>Reassign a task</b>\n\n"
            "Pick the order to hand off. I'll show you who else covers its "
            "stage.\n\n"
            "<i>A stalled piece is a wasted one.</i>"
        )
        await send_screen(client, q.message.chat.id,
                          card(caption, bot_name=BOT, buttons=rows),
                          old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|reto\|"))
    async def _reassign_pick_target(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        _, _, code, stage, from_id = _parts(q)
        from_id = int(from_id)
        svc = _svc(container)
        # Candidates: available admins on that stage, excluding the current owner.
        candidates = [a for a in await svc.list_admins(stage=stage)
                      if a.telegram_id != from_id and a.is_available]
        if not candidates:
            await q.answer("No other admin covers that stage.", show_alert=True)
            return
        await q.answer()
        rows = [
            [(f"{V.esc(a.name or str(a.telegram_id))[:24]} "
              f"({a.active_tasks} active)", cb("mg", "redo", code, stage, a.telegram_id))]
            for a in candidates
        ]
        rows.append([(V.BTN_BACK_MANAGE, cb("mg", "reasgn", from_id))])
        caption = (
            f"{V.ICON} <b>Hand <code>{V.esc(code)}</code> to…</b>\n\n"
            f"These soldiers cover the {V.stage_label(stage)} stage. Tap one to "
            "transfer the order.\n\n"
            "<i>Choose the steadier hand.</i>"
        )
        await send_screen(client, q.message.chat.id,
                          card(caption, bot_name=BOT, buttons=rows),
                          old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|redo\|"))
    async def _reassign_commit(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        _, _, code, stage, to_id = _parts(q)
        to_id = int(to_id)
        svc = _svc(container)
        await svc.reassign(code, stage, to_id)
        target = await svc.get_admin(to_id)
        name = (target.name if target else None) or str(to_id)
        await q.answer("Order reissued.")
        # Best-effort ping to the new owner so they know work landed.
        try:
            await client.send_message(
                to_id,
                f"{V.ICON} <b>An order lands on you.</b>\n\n"
                f"<code>{V.esc(code)}</code> is now yours at the "
                f"{V.stage_label(stage)} stage. Open your tasks and press on.",
            )
        except Exception:  # noqa: BLE001
            pass
        await send_screen(
            client, q.message.chat.id,
            card(V.reassigned(code, name), bot_name=BOT,
                 buttons=[[(V.BTN_BACK_MANAGE, cb("mg", "roster"))]]),
            old_msg=q.message)

    # ── Request lifecycle (OWNER-ONLY): re-fetch fresh OR delete a request ──────
    # Distinct from the stage-reassign above (which moves an assignment between
    # admins on the SAME request). Here the owner can restart a request under a
    # brand-new ticket, or delete it outright — both tear down the old rows/files
    # and DM the requester. Gated to the owner because they're destructive.
    def _is_owner(obj) -> bool:
        from nekofetch.services.auth_service import AuthService
        user = getattr(obj, "nf_user", None)
        if user is None:
            return False
        try:
            return AuthService(container).is_owner(user)
        except Exception:  # noqa: BLE001
            return False

    async def _owner_guard(q: CallbackQuery) -> bool:
        if q.message is None:
            await q.answer()
            return False
        if not _is_owner(q):
            await q.answer("🔒 That's the owner's console.", show_alert=True)
            return False
        return True

    def _req_service():
        from nekofetch.services.request_service import RequestService
        return RequestService(container)

    async def _render_requests(client: Client, chat_id: int,
                               old_msg: CallbackQuery | None) -> None:
        rows, total = await req_wrk_rows(container, limit=20)
        buttons = [[(label, cb_data)] for label, cb_data in rows]
        buttons.append([(V.BTN_BACK_ADMIN, cb(BOT, "admin"))])
        caption = (
            f"{V.ICON} <b>Manage REQ/WRK</b>\n\n"
            f"<b>{total}</b> in flight — <b>REQ</b> tickets and <b>WRK</b> work "
            f"items. Tap one to re-fetch/delete it or cancel the work item.\n\n"
            "<i>A commander prunes the line so the charge stays clean.</i>"
        )
        await send_screen(client, chat_id,
                          card(caption, image=_art(), bot_name=BOT, buttons=buttons),
                          old_msg=old_msg)

    @client.on_callback_query(filters.regex(r"^mg\|reqs"))
    async def _requests_list(_: Client, q: CallbackQuery) -> None:
        if not await _owner_guard(q):
            return
        await q.answer()
        await _render_requests(client, q.message.chat.id, q.message)

    @client.on_callback_query(filters.regex(r"^mg\|reqdet\|"))
    async def _request_detail(_: Client, q: CallbackQuery) -> None:
        if not await _owner_guard(q):
            return
        code = _parts(q)[2]
        # WRK rows route to the work-item console (cancel only); REQ rows keep
        # the full re-fetch/delete lifecycle.
        if code.startswith("WRK-"):
            await _work_detail(client, q, code)
            return
        try:
            req = await _req_service().get(code)
        except Exception:  # noqa: BLE001 — vanished mid-navigation
            await q.answer("That request is gone.", show_alert=True)
            await _render_requests(client, q.message.chat.id, q.message)
            return
        await q.answer()
        st = req.status.value if hasattr(req.status, "value") else str(req.status)
        caption = (
            f"{V.ICON} <b>{V.esc(req.anime_title or code)}</b>\n\n"
            f"<b>ticket:</b> <code>{V.esc(code)}</code>\n"
            f"<b>status:</b> {V.esc(st)}\n\n"
            "<b>♻️ Reassign fresh</b> — tear this down and re-fetch it under a "
            "NEW ticket (the requester is told).\n"
            "<b>🗑 Delete</b> — remove it entirely; it stops for every stage and "
            "the requester is told it was removed.\n\n"
            "<i>Both are irreversible.</i>"
        )
        rows = [
            [("♻️ Reassign fresh", cb("mg", "refresh", code))],
            [("🗑 Delete request", cb("mg", "purge", code))],
            [("↩️ Back", cb("mg", "reqs", 0))],
        ]
        await send_screen(client, q.message.chat.id,
                          card(caption, bot_name=BOT, buttons=rows),
                          old_msg=q.message)

    async def _work_detail(client: Client, q: CallbackQuery, code: str) -> None:
        """Detail card for a WRK row: status + a working cancel action."""
        from kurosoden.shared.work_service import WorkService

        svc = WorkService(container.pg_sessionmaker)
        w = next((item for item in await svc.list_open(limit=200)
                  if item.code == code), None)
        if w is None:
            await q.answer("That work item is gone.", show_alert=True)
            await _render_requests(client, q.message.chat.id, q.message)
            return
        await q.answer()
        caption = (
            f"{V.ICON} <b>{V.esc(w.anime_title or code)}</b>\n\n"
            f"<b>code:</b> <code>{V.esc(code)}</code>\n"
            f"<b>stage:</b> {V.esc(w.stage)}\n"
            f"<b>status:</b> {V.esc(w.status)}\n\n"
            "<b>🗑 Cancel work item</b> — remove it from the queue; it stops "
            "for every stage.\n\n"
            "<i>This cannot be undone.</i>"
        )
        rows = [
            [("🗑 Cancel work item", cb("mg", "wrkcancel", code))],
            [("↩️ Back", cb("mg", "reqs", 0))],
        ]
        await send_screen(client, q.message.chat.id,
                          card(caption, bot_name=BOT, buttons=rows),
                          old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|wrkcancel\|"))
    async def _work_cancel(_: Client, q: CallbackQuery) -> None:
        if not await _owner_guard(q):
            return
        code = _parts(q)[2]
        from kurosoden.shared.work_service import WorkService

        ok = await WorkService(container.pg_sessionmaker).cancel(code)
        await q.answer("Work item cancelled." if ok else "Nothing to cancel.")
        await _render_requests(client, q.message.chat.id, q.message)

    @client.on_callback_query(filters.regex(r"^mg\|refresh\|"))
    async def _request_refresh_confirm(_: Client, q: CallbackQuery) -> None:
        if not await _owner_guard(q):
            return
        code = _parts(q)[2]
        await q.answer()
        caption = (
            f"{V.ICON} <b>Re-fetch <code>{V.esc(code)}</code> fresh?</b>\n\n"
            "This deletes the old ticket, its jobs, files, and any uploaded "
            "packs, then creates a NEW ticket and restarts the download from "
            "scratch. The requester is notified of the new ticket.\n\n"
            "<i>Prefetched metadata is kept.</i>"
        )
        rows = [
            [("✅ Yes, re-fetch", cb("mg", "refreshok", code))],
            [("↩️ Cancel", cb("mg", "reqdet", code))],
        ]
        await send_screen(client, q.message.chat.id,
                          card(caption, bot_name=BOT, buttons=rows),
                          old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|refreshok\|"))
    async def _request_refresh_commit(_: Client, q: CallbackQuery) -> None:
        if not await _owner_guard(q):
            return
        code = _parts(q)[2]
        await q.answer("Re-fetching…")
        try:
            result = await _req_service().reassign_fresh(code)
        except Exception as exc:  # noqa: BLE001
            log.warning("lelouch.request.reassign_failed", code=code, error=str(exc))
            await send_screen(
                client, q.message.chat.id,
                card(f"{V.ICON} <b>Couldn't re-fetch <code>{V.esc(code)}</code>.</b>\n\n"
                     f"<code>{V.esc(str(exc))[:200]}</code>",
                     bot_name=BOT,
                     buttons=[[("↩️ Back", cb("mg", "reqs", 0))]]),
                old_msg=q.message)
            return
        new_code = result.get("new_code", "?")
        caption = (
            f"{V.ICON} <b>Restarted.</b>\n\n"
            f"<code>{V.esc(code)}</code> was cleared and re-fetched as "
            f"<code>{V.esc(new_code)}</code>. The download stage has been "
            f"assigned and the requester notified.\n\n"
            "<i>Fresh line, fresh charge.</i>"
        )
        await send_screen(client, q.message.chat.id,
                          card(caption, bot_name=BOT,
                               buttons=[[("↩️ Back", cb("mg", "reqs", 0))]]),
                          old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|purge\|"))
    async def _request_purge_confirm(_: Client, q: CallbackQuery) -> None:
        if not await _owner_guard(q):
            return
        code = _parts(q)[2]
        await q.answer()
        caption = (
            f"{V.ICON} <b>Delete <code>{V.esc(code)}</code> entirely?</b>\n\n"
            "This removes the request, its jobs, files, and any uploaded packs, "
            "and stops it for every stage. The requester is told it was removed.\n\n"
            "<i>This cannot be undone.</i>"
        )
        rows = [
            [("🗑 Yes, delete", cb("mg", "purgeok", code))],
            [("↩️ Cancel", cb("mg", "reqdet", code))],
        ]
        await send_screen(client, q.message.chat.id,
                          card(caption, bot_name=BOT, buttons=rows),
                          old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^mg\|purgeok\|"))
    async def _request_purge_commit(_: Client, q: CallbackQuery) -> None:
        if not await _owner_guard(q):
            return
        code = _parts(q)[2]
        await q.answer("Deleting…")
        try:
            await _req_service().delete_request(code)
        except Exception as exc:  # noqa: BLE001
            log.warning("lelouch.request.delete_failed", code=code, error=str(exc))
            await send_screen(
                client, q.message.chat.id,
                card(f"{V.ICON} <b>Couldn't delete <code>{V.esc(code)}</code>.</b>\n\n"
                     f"<code>{V.esc(str(exc))[:200]}</code>",
                     bot_name=BOT,
                     buttons=[[("↩️ Back", cb("mg", "reqs", 0))]]),
                old_msg=q.message)
            return
        caption = (
            f"{V.ICON} <b>Removed.</b>\n\n"
            f"<code>{V.esc(code)}</code> and everything under it is gone. The "
            f"requester has been notified.\n\n"
            "<i>The line is clean.</i>"
        )
        await send_screen(client, q.message.chat.id,
                          card(caption, bot_name=BOT,
                               buttons=[[("↩️ Back", cb("mg", "reqs", 0))]]),
                          old_msg=q.message)
