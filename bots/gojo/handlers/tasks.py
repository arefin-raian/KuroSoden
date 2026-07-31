"""Gojo task handlers — REUSES NekoFetch's publishing infrastructure.

Key principle: Gojo does NOT reimplement publishing. It delegates to:
  • MainChannelService.publish() — generates and posts to the main channel.
  • IndexChannelService.refresh_letter() — updates the A-Z index.
  • PublishingService.publish() — the full publish orchestration.
  • BotOrchestratorService — recreates bots for channel recovery.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from kurosoden.shared import gojo_voice as V
from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.ui.artwork import pick_artwork
from nekofetch.ui.components import cb, keyboard
from nekofetch.ui.screens import Screen, send_screen

log = get_logger(__name__)

STATE_EDIT_CAPTION = "gojo:await_caption_edit"
STATE_SCHEDULE = "gojo:await_schedule"
STATE_EDIT_FOOTER = "gojo:await_footer_edit"
STATE_CHANGE_MAIN = "gojo:await_new_main_channel"
STATE_UPDATES_REVIEW = "gojo:await_updates_review"
STATE_UPDATES_EDIT = "gojo:await_updates_edit"
STATE_CHANGE_INDEX = "gojo:await_new_index_channel"
# Index slot-management free-text steps. Each stashes the target slot's sort_order.
STATE_IDX_SLOT_CAPTION = "gojo:await_idx_slot_caption"
STATE_IDX_SLOT_IMAGE = "gojo:await_idx_slot_image"
STATE_IDX_SLOT_BUTTONS = "gojo:await_idx_slot_buttons"


def _publish_keyboard(code: str):
    """The review card's action row — publish now / silent / schedule / edit."""
    return keyboard(
        [(V.BTN_PUBLISH_NOW, cb("gojo", "publish_confirm", code)),
         (V.BTN_PUBLISH_SILENT, cb("gojo", "publish_silent", code))],
        [(V.BTN_SCHEDULE, cb("gojo", "publish_schedule", code)),
         (V.BTN_EDIT_CAPTION, cb("gojo", "publish_edit", code))],
        [(V.BTN_CANCEL, cb("gojo", "home"))],
    )


def _flatten_update_rows(results) -> list[dict]:
    """Flatten ``CheckResult``s into serializable review rows (FSM-storable).

    Keeps the whole ``NewEntry`` shape so the submit step can rebuild each entry
    without re-walking the franchise graph. Shared by the manual ``/updates``
    flow and the scheduled monthly notify.
    """
    return [
        {
            "doc": r.anime_doc_id, "title": r.title,
            "aid": e.anilist_id, "fmt": e.format, "t": e.english_title,
            "season": e.season_number, "eps": e.episode_count,
            "rel": e.relation,
        }
        for r in results for e in r.new_entries
    ]


def updates_review_markup(rows: list[dict]):
    """Build the review keyboard: one ✖ per entry, then Submit / Edit / Cancel.

    Each entry uses the official AniList name (``t``) and carries its index in
    the callback so a tap removes exactly that row before re-rendering. Module
    level so the scheduled monthly notify reuses the exact manual-flow UI.
    """
    btn_rows = [
        [(V.remove_entry_label(r["t"]), cb("gojo", "updates_drop", str(i)))]
        for i, r in enumerate(rows)
    ]
    if rows:
        btn_rows.append([(V.BTN_SUBMIT, cb("gojo", "updates_submit"))])
    # Add-entries: hand the admin the current list to edit as free text so they
    # can drop lines AND type new titles the sweep didn't surface.
    btn_rows.append([(V.BTN_EDIT_LIST, cb("gojo", "updates_edit"))])
    btn_rows.append([(V.BTN_CANCEL, cb("gojo", "home"))])
    return keyboard(*btn_rows)


async def render_updates_review(edit_target: Message, rows: list[dict]) -> None:
    """Render/refresh the review card in place (edits the given message)."""
    if not rows:
        await edit_target.edit_text(V.UPDATES_NONE, parse_mode=ParseMode.HTML)
        return
    listing = "\n".join(f"⦿ {r['t']}" for r in rows)
    await edit_target.edit_text(
        f"{V.updates_found(len(rows))}\n\n<pre>{listing}</pre>",
        parse_mode=ParseMode.HTML,
        reply_markup=updates_review_markup(rows),
    )


# ── Index management markup ───────────────────────────────────────────────────
# A slot's callbacks carry its ``sort_order`` (stable) rather than a list index,
# so a concurrent shift can't retarget the wrong post.

def _index_slots_markup(slots: list[dict]):
    """One button per index slot, tagged by kind, then a Change-Index / Home row.

    ``letter`` slots show their label, ``reserved`` an empty-slot marker, and
    ``repurposed`` a lock so the operator sees which posts auto-indexing leaves
    alone. Tapping a slot opens its action panel."""
    icon = {"letter": "🔤", "reserved": "▫️", "repurposed": "🔒"}
    btn_rows = []
    for s in slots:
        tag = s["label"] or ("Reserved" if s["kind"] == "reserved" else "Repurposed")
        btn_rows.append([(
            f"{icon.get(s['kind'], '•')} #{s['order']} · {tag}",
            cb("gojo", "idx_slot", str(s["order"])),
        )])
    btn_rows.append([(V.BTN_CHANGE_INDEX, cb("gojo", "change_index"))])
    btn_rows.append([(V.BTN_HOME, cb("gojo", "home"))])
    return keyboard(*btn_rows)


def _index_slot_actions_markup(order: int):
    """Edit-text / replace-image / edit-buttons + repurpose toggle for one slot."""
    return keyboard(
        [(V.BTN_IDX_EDIT_TEXT, cb("gojo", "idx_edit", "caption", str(order)))],
        [(V.BTN_IDX_EDIT_IMAGE, cb("gojo", "idx_edit", "image", str(order)))],
        [(V.BTN_IDX_EDIT_BUTTONS, cb("gojo", "idx_edit", "buttons", str(order)))],
        [(V.BTN_IDX_MARK_REPURPOSED, cb("gojo", "idx_repurpose", str(order), "on")),
         (V.BTN_IDX_MARK_RESERVED, cb("gojo", "idx_repurpose", str(order), "off"))],
        [(V.BTN_IDX_BACK, cb("gojo", "index")), (V.BTN_HOME, cb("gojo", "home"))],
    )


def register(client: Client, container: Container) -> None:
    fsm = FSM(container.redis, bot="gojo")

    async def _render_tasks(chat_id: int, user_id: int,
                            *, old_msg: Message | None = None) -> None:
        """Render the real, actionable task list (offers + assigned).

        Shared by the ``/tasks`` command AND the ``gojo|tasks`` / ``gojo|publish``
        inline buttons (from /start and the Senku→Gojo handoff card). Before this
        existed, those buttons fell through to app.py's catch-all which showed
        static help text with only a Back button — the "publishing shows nothing
        to post" bug."""
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.infrastructure.repositories.request_repo import RequestRepository

        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        active = await engine.get_active_tasks(user_id)
        offers = await engine.get_pending_offers(user_id)

        if not active and not offers:
            await send_screen(
                client, chat_id,
                Screen(
                    caption=V.TASKS_EMPTY,
                    image=pick_artwork("gojo"),
                    keyboard=keyboard([(V.BTN_HOME, cb("gojo", "home"))]),
                ),
                old_msg=old_msg,
            )
            return

        lines = [V.tasks_title(len(active) + len(offers)), ""]
        rows: list[list[tuple[str, str]]] = []
        for a in offers[:5]:
            title = a.request_code
            try:
                async with session_scope(container.pg_sessionmaker) as s:
                    req = await RequestRepository(s).get_by_code(a.request_code)
                    if req:
                        title = req.anime_title
            except Exception:
                pass
            lines.append(
                f"<blockquote><b>Offer</b>\n{V.task_row(a.request_code, title)}</blockquote>"
            )
            rows.append([(f"Accept - {title}"[:60],
                          cb("gojo", "offer", "accept", a.request_code))])
            rows.append([(f"Reject - {title}"[:60],
                          cb("gojo", "offer", "reject", a.request_code))])
        if active:
            lines.append("\n<b>Assigned</b>")
        for a in active[:10]:
            title = a.request_code
            try:
                async with session_scope(container.pg_sessionmaker) as s:
                    req = await RequestRepository(s).get_by_code(a.request_code)
                    if req:
                        title = req.anime_title
            except Exception:
                pass
            lines.append(
                V.task_row(a.request_code, title, in_progress=a.status == "in_progress")
            )
            rows.append([
                (f"Publish · {title}"[:60], cb("gojo", "publish_open", a.request_code)),
            ])
        rows.append([(V.BTN_HOME, cb("gojo", "home"))])
        await send_screen(
            client, chat_id,
            Screen(
                caption="\n".join(lines),
                image=pick_artwork("gojo"),
                keyboard=keyboard(*rows),
            ),
            old_msg=old_msg,
        )

    # ── /tasks — View assigned publishing tasks ───────────────────────────────
    @client.on_message(filters.command("tasks"))
    async def _tasks(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        await _render_tasks(message.chat.id, message.from_user.id)

    # ── gojo|tasks / gojo|publish inline buttons → the SAME actionable list ────
    # These MUST be registered before app.py's `^gojo\|` catch-all (register_all
    # runs first), so a tap on Tasks/Publish shows real tasks, not static help.
    @client.on_callback_query(filters.regex(r"^gojo\|tasks$"))
    async def _cb_tasks(_: Client, q: CallbackQuery) -> None:
        if q.from_user is None or q.message is None:
            await q.answer()
            return
        await q.answer()
        await _render_tasks(q.message.chat.id, q.from_user.id, old_msg=q.message)

    @client.on_callback_query(filters.regex(r"^gojo\|publish$"))
    async def _cb_publish_list(_: Client, q: CallbackQuery) -> None:
        if q.from_user is None or q.message is None:
            await q.answer()
            return
        await q.answer()
        await _render_tasks(q.message.chat.id, q.from_user.id, old_msg=q.message)


    # ── Callback handlers (registered ONCE, not dynamically) ────────────────
    @client.on_callback_query(filters.regex(r"^gojo\|offer\|"))
    async def _offer_cb(_: Client, q: CallbackQuery) -> None:
        if q.from_user is None:
            await q.answer()
            return
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        parts = (q.data or "").split("|", 3)
        action = parts[2] if len(parts) > 2 else ""
        code = parts[3] if len(parts) > 3 else ""
        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        if action == "accept":
            result = await engine.accept_offer(code, "gojo", q.from_user.id)
            await q.answer("Accepted." if result else "Offer expired.", show_alert=result is None)
            if result and q.message is not None:
                await _review_for_publish(client, container, q.message, code, fsm)
        elif action == "reject":
            ok = await engine.reject_offer(code, "gojo", q.from_user.id)
            await q.answer("Rejected." if ok else "Offer expired.", show_alert=not ok)
        else:
            await q.answer()

    @client.on_callback_query(filters.regex(r"^gojo\|publish_open\|"))
    async def _publish_open(_: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        code = (q.data or "").split("|", 2)[2]
        await q.answer()
        await _review_for_publish(client, container, q.message, code, fsm)

    @client.on_callback_query(filters.regex(r"^gojo\|publish_confirm\|"))
    async def _cb_publish(_: Client, q: CallbackQuery) -> None:
        _, _, code = q.data.split("|", 2)
        await q.answer("Publishing...")
        # "Publish Now" honours the configured default (some operators prefer
        # silent posts as the norm); the separate "Silent" button always forces it.
        silent = bool(getattr(container.config.main_channel, "silent_default", False))
        await _execute_publish(client, container, q.message, code, silent=silent)

    @client.on_callback_query(filters.regex(r"^gojo\|publish_silent\|"))
    async def _cb_publish_silent(_: Client, q: CallbackQuery) -> None:
        _, _, code = q.data.split("|", 2)
        await q.answer("Publishing silently...")
        await _execute_publish(client, container, q.message, code, silent=True)

    @client.on_callback_query(filters.regex(r"^gojo\|publish_edit\|"))
    async def _cb_edit(_: Client, q: CallbackQuery) -> None:
        _, _, code = q.data.split("|", 2)
        await q.answer()
        await fsm.set(q.from_user.id, STATE_EDIT_CAPTION, request_code=code)
        await q.message.reply(V.EDIT_CAPTION_PROMPT, parse_mode=ParseMode.HTML)

    @client.on_callback_query(filters.regex(r"^gojo\|publish_schedule\|"))
    async def _cb_schedule(_: Client, q: CallbackQuery) -> None:
        _, _, code = q.data.split("|", 2)
        await q.answer()
        await fsm.set(q.from_user.id, STATE_SCHEDULE, request_code=code)
        tz_name = await _admin_tz(container, q.from_user.id)
        # Prompt in the admin's own timezone, then show the combined queue so they
        # don't stack a post on top of someone else's.
        await q.message.reply(
            V.schedule_prompt(_tz_label(tz_name)), parse_mode=ParseMode.HTML,
        )
        await _show_schedule_queue(container, q.message, tz_name)

    # ── Universal footer edit — /footer or the gojo|edit_footer button ────────
    async def _arm_footer(user_id: int, reply_to: Message) -> None:
        await fsm.set(user_id, STATE_EDIT_FOOTER)
        await reply_to.reply(V.FOOTER_EDIT_PROMPT, parse_mode=ParseMode.HTML)

    @client.on_message(filters.command("footer"))
    async def _footer_cmd(_: Client, message: Message) -> None:
        if message.from_user:
            await _arm_footer(message.from_user.id, message)

    @client.on_callback_query(filters.regex(r"^gojo\|edit_footer$"))
    async def _cb_footer(_: Client, q: CallbackQuery) -> None:
        await q.answer()
        await _arm_footer(q.from_user.id, q.message)

    # ── Backup — snapshot every main-channel post to durable hosts ────────────
    async def _run_backup(reply_to: Message) -> None:
        from nekofetch.services.backup_service import BackupService

        note = await reply_to.reply(V.BACKUP_RUNNING, parse_mode=ParseMode.HTML)
        stats = await BackupService(container).backup_all()
        await note.edit_text(
            V.backup_done(stats.backed_up, stats.posts, stats.images_mirrored),
            parse_mode=ParseMode.HTML,
        )

    @client.on_message(filters.command("backup"))
    async def _backup_cmd(_: Client, message: Message) -> None:
        await _run_backup(message)

    @client.on_callback_query(filters.regex(r"^gojo\|backup$"))
    async def _cb_backup(_: Client, q: CallbackQuery) -> None:
        await q.answer("Backing up…")
        await _run_backup(q.message)

    # ── Change main channel — restore all posts to a new channel from backup ──
    @client.on_callback_query(filters.regex(r"^gojo\|change_main$"))
    async def _cb_change_main(_: Client, q: CallbackQuery) -> None:
        await q.answer()
        await fsm.set(q.from_user.id, STATE_CHANGE_MAIN)
        await q.message.reply(V.CHANGE_MAIN_PROMPT, parse_mode=ParseMode.HTML)

    # ── Change index channel — rebuild the whole index on a new channel ───────
    @client.on_callback_query(filters.regex(r"^gojo\|change_index$"))
    async def _cb_change_index(_: Client, q: CallbackQuery) -> None:
        await q.answer()
        await fsm.set(q.from_user.id, STATE_CHANGE_INDEX)
        await q.message.reply(V.CHANGE_INDEX_PROMPT, parse_mode=ParseMode.HTML)

    # ── Index management — list slots, edit text/image/buttons ────────────────
    async def _render_index_slots(edit_target: Message) -> None:
        from nekofetch.services.index_channel_service import IndexChannelService

        slots = await IndexChannelService(container).list_slots()
        await edit_target.edit_text(
            V.index_slots_body(slots), parse_mode=ParseMode.HTML,
            reply_markup=_index_slots_markup(slots),
        )

    @client.on_callback_query(filters.regex(r"^gojo\|index$"))
    async def _cb_index(_: Client, q: CallbackQuery) -> None:
        await q.answer()
        from nekofetch.services.index_channel_service import IndexChannelService

        slots = await IndexChannelService(container).list_slots()
        note = await q.message.reply(V.INDEX_INTRO, parse_mode=ParseMode.HTML)
        await note.edit_text(
            V.index_slots_body(slots), parse_mode=ParseMode.HTML,
            reply_markup=_index_slots_markup(slots),
        )

    @client.on_callback_query(filters.regex(r"^gojo\|idx_slot\|"))
    async def _cb_idx_slot(_: Client, q: CallbackQuery) -> None:
        await q.answer()
        try:
            order = int(q.data.rsplit("|", 1)[1])
        except (ValueError, IndexError):
            return
        await q.message.edit_text(
            V.index_slot_detail(order), parse_mode=ParseMode.HTML,
            reply_markup=_index_slot_actions_markup(order),
        )

    @client.on_callback_query(filters.regex(r"^gojo\|idx_edit\|"))
    async def _cb_idx_edit(_: Client, q: CallbackQuery) -> None:
        # gojo|idx_edit|<what>|<order>  where what ∈ caption|image|buttons
        parts = q.data.split("|")
        if len(parts) < 4:
            await q.answer()
            return
        what, order = parts[2], parts[3]
        state = {"caption": STATE_IDX_SLOT_CAPTION, "image": STATE_IDX_SLOT_IMAGE,
                 "buttons": STATE_IDX_SLOT_BUTTONS}.get(what)
        if state is None:
            await q.answer()
            return
        await q.answer()
        await fsm.set(q.from_user.id, state, slot_order=int(order))
        await q.message.reply(V.index_edit_prompt(what), parse_mode=ParseMode.HTML)

    @client.on_callback_query(filters.regex(r"^gojo\|idx_repurpose\|"))
    async def _cb_idx_repurpose(_: Client, q: CallbackQuery) -> None:
        # gojo|idx_repurpose|<order>|<on|off>
        parts = q.data.split("|")
        if len(parts) < 4:
            await q.answer()
            return
        order, flag = int(parts[2]), parts[3] == "on"
        from nekofetch.services.index_channel_service import IndexChannelService

        ok = await IndexChannelService(container).mark_slot_repurposed(order, flag)
        await q.answer("Updated." if ok else "Slot not found.", show_alert=not ok)
        await _render_index_slots(q.message)

    # ── Update check — detect-only sweep + edit-before-submit ─────────────────
    _updates_review_markup = updates_review_markup
    _render_updates_review = render_updates_review

    async def _run_update_check(reply_to: Message, user_id: int) -> None:
        from nekofetch.services.maintenance_service import MaintenanceService

        note = await reply_to.reply(V.UPDATES_RUNNING, parse_mode=ParseMode.HTML)
        results = await MaintenanceService(container).scan_updates()
        if not results:
            await note.edit_text(V.UPDATES_NONE, parse_mode=ParseMode.HTML)
            return
        rows = _flatten_update_rows(results)
        await fsm.set(user_id, STATE_UPDATES_REVIEW, rows=rows)
        await _render_updates_review(note, rows)

    @client.on_message(filters.command("updates"))
    async def _updates_cmd(_: Client, message: Message) -> None:
        if message.from_user:
            await _run_update_check(message, message.from_user.id)

    @client.on_callback_query(filters.regex(r"^gojo\|check_updates$"))
    async def _cb_check_updates(_: Client, q: CallbackQuery) -> None:
        await q.answer("Sweeping…")
        await _run_update_check(q.message, q.from_user.id)

    @client.on_callback_query(filters.regex(r"^gojo\|updates_drop\|"))
    async def _cb_updates_drop(_: Client, q: CallbackQuery) -> None:
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_UPDATES_REVIEW:
            await q.answer("This list expired — run the check again.", show_alert=True)
            return
        rows = data.get("rows", [])
        try:
            idx = int(q.data.rsplit("|", 1)[1])
        except (ValueError, IndexError):
            await q.answer()
            return
        if 0 <= idx < len(rows):
            dropped = rows.pop(idx)
            await fsm.update(q.from_user.id, rows=rows)
            await q.answer(V.entry_dropped(dropped["t"]))
        else:
            await q.answer()
        if not rows:
            await fsm.clear(q.from_user.id)
        await _render_updates_review(q.message, rows)

    @client.on_callback_query(filters.regex(r"^gojo\|updates_edit$"))
    async def _cb_updates_edit(_: Client, q: CallbackQuery) -> None:
        """Arm the free-text edit step: show the current list as editable text.

        The admin copies the block, trims lines, and/or adds new titles (one per
        line, official AniList English title). We stash the current rows so a
        kept line matches back to its already-resolved entry and only genuinely
        new lines hit AniList — see ``_fsm_text``'s ``STATE_UPDATES_EDIT`` arm."""
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_UPDATES_REVIEW:
            await q.answer("This list expired — run the check again.", show_alert=True)
            return
        await q.answer()
        rows = data.get("rows", [])
        await fsm.set(q.from_user.id, STATE_UPDATES_EDIT, rows=rows)
        listing = "\n".join(r["t"] for r in rows)
        await q.message.reply(
            f"{V.UPDATES_EDIT_PROMPT}\n\n<pre>{listing}</pre>",
            parse_mode=ParseMode.HTML,
        )

    @client.on_callback_query(filters.regex(r"^gojo\|updates_submit$"))
    async def _cb_updates_submit(_: Client, q: CallbackQuery) -> None:
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_UPDATES_REVIEW:
            await q.answer("Nothing to submit.", show_alert=True)
            return
        await q.answer("Submitting…")
        rows = data.get("rows", [])
        await fsm.clear(q.from_user.id)
        from nekofetch.services.update_check_service import NewEntry, UpdateCheckService

        svc = UpdateCheckService(container)
        # Rebuild NewEntry objects grouped by anime, then commit each group.
        by_doc: dict[str, tuple[str, list[NewEntry]]] = {}
        for r in rows:
            title, entries = by_doc.setdefault(r["doc"], (r["title"], []))
            entries.append(NewEntry(
                anilist_id=r["aid"], format=r["fmt"], english_title=r["t"],
                season_number=r["season"], episode_count=r["eps"],
                relation=r.get("rel", ""),
            ))
        made = 0
        for doc, (title, entries) in by_doc.items():
            made += await svc.create_requests_for(doc, title, entries)
        await q.message.reply(V.updates_submitted(made), parse_mode=ParseMode.HTML)

    # ── Ban check — probe every channel; recover the ones that are down ───────
    async def _run_ban_check(reply_to: Message) -> None:
        from nekofetch.services.maintenance_service import MaintenanceService

        note = await reply_to.reply(V.BANCHECK_RUNNING, parse_mode=ParseMode.HTML)
        result = await MaintenanceService(container).probe_channels()
        await note.edit_text(
            V.ban_check_result(len(result.banned), result.checked),
            parse_mode=ParseMode.HTML,
        )
        if not result.banned:
            return
        # Recover each down channel through the same path /recover uses: Senku
        # rebuilds the entity, then every backed-up post is restored to it. The
        # main channel has no anime_doc_id — it goes through change-main instead,
        # so we only auto-recover distribution channels here.
        from nekofetch.services.bot_orchestrator import BotOrchestratorService

        orch = BotOrchestratorService(container)
        for probe in result.banned:
            if not probe.anime_doc_id:
                continue
            try:
                info = await orch.recreate_bot(probe.anime_doc_id)
                if info:
                    await reply_to.reply(
                        V.ban_recovered(probe.name, info.username or info.name),
                        parse_mode=ParseMode.HTML,
                    )
            except Exception as exc:  # noqa: BLE001
                await reply_to.reply(
                    V.ban_recover_failed(probe.name, str(exc)),
                    parse_mode=ParseMode.HTML,
                )

    @client.on_message(filters.command("bancheck"))
    async def _bancheck_cmd(_: Client, message: Message) -> None:
        await _run_ban_check(message)

    @client.on_callback_query(filters.regex(r"^gojo\|check_banned$"))
    async def _cb_check_banned(_: Client, q: CallbackQuery) -> None:
        await q.answer("Probing…")
        await _run_ban_check(q.message)

    # ── Stats — catalog coverage + backup/schedule/maintenance dashboard ──────
    @client.on_callback_query(filters.regex(r"^gojo\|stats$"))
    async def _cb_stats(_: Client, q: CallbackQuery) -> None:
        await q.answer("Crunching…")
        from nekofetch.services.stats_service import StatsService

        note = await q.message.reply(V.STATS_RUNNING, parse_mode=ParseMode.HTML)
        try:
            data = await StatsService(container).gojo_dashboard()
            body = StatsService.dashboard_message(data)
        except Exception as exc:  # noqa: BLE001 — stats must never hard-fail the panel
            log.warning("gojo.stats.failed", error=str(exc))
            await note.edit_text(V.STATS_FAILED, parse_mode=ParseMode.HTML)
            return
        await note.edit_text(
            body, parse_mode=ParseMode.HTML,
            reply_markup=keyboard([(V.BTN_HOME, cb("gojo", "home"))]),
        )

    @client.on_message(filters.command("stats"))
    async def _stats_cmd(_: Client, message: Message) -> None:
        from nekofetch.services.stats_service import StatsService

        note = await message.reply(V.STATS_RUNNING, parse_mode=ParseMode.HTML)
        try:
            data = await StatsService(container).gojo_dashboard()
            body = StatsService.dashboard_message(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("gojo.stats.failed", error=str(exc))
            await note.edit_text(V.STATS_FAILED, parse_mode=ParseMode.HTML)
            return
        await note.edit_text(body, parse_mode=ParseMode.HTML)

    async def _apply_updates_edit(message: Message, prev_rows: list[dict]) -> None:
        """Apply a returned edit-list: keep survivors, resolve added titles.

        The admin sends back the block with lines removed and/or new titles
        added (one per line). A line whose title matches an existing row (case-
        insensitive) keeps that row's already-resolved entry verbatim. A genuinely
        new line is resolved via AniList and bound to the same anime as the list
        it was added to (single-anime lists are unambiguous; for a mixed list we
        bind an add to the first anime, since a manual add is almost always
        another entry of the franchise being reviewed). Unresolvable lines are
        reported and skipped. Re-renders the review so the admin can still submit.
        """
        lines = [ln.strip() for ln in (message.text or "").splitlines() if ln.strip()]
        if not lines:
            await fsm.clear(message.from_user.id)
            await message.reply(V.UPDATES_NONE, parse_mode=ParseMode.HTML)
            return

        by_title = {r["t"].casefold(): r for r in prev_rows}
        kept: list[dict] = []
        added_titles: list[str] = []
        for ln in lines:
            match = by_title.get(ln.casefold())
            if match is not None:
                kept.append(match)
            else:
                added_titles.append(ln)

        # Default anime binding for adds: the (single) anime under review.
        default_doc = prev_rows[0]["doc"] if prev_rows else None
        default_title = prev_rows[0]["title"] if prev_rows else None

        unresolved: list[str] = []
        if added_titles and default_doc is not None:
            for title in added_titles:
                try:
                    media = await container.anilist.search(title)
                except Exception as exc:  # noqa: BLE001 — a lookup miss is not fatal
                    log.warning("gojo.updates_edit.search_failed",
                                title=title, error=str(exc))
                    media = None
                if media is None:
                    unresolved.append(title)
                    continue
                fmt = (media.format or "TV").upper()
                is_tv = fmt in ("TV", "TV_SHORT")
                kept.append({
                    "doc": default_doc, "title": default_title,
                    "aid": media.id, "fmt": fmt,
                    "t": media.english or media.romaji or title,
                    "season": (media.franchise_seasons or None) if is_tv else None,
                    "eps": media.episodes,
                    "rel": "MANUAL",
                })
        elif added_titles:
            unresolved.extend(added_titles)

        # De-dup by AniList id, preserving order (a kept row + re-added line).
        seen: set[int] = set()
        deduped: list[dict] = []
        for r in kept:
            if r["aid"] in seen:
                continue
            seen.add(r["aid"])
            deduped.append(r)

        await fsm.set(message.from_user.id, STATE_UPDATES_REVIEW, rows=deduped)
        if unresolved:
            await message.reply(
                V.updates_unresolved(unresolved), parse_mode=ParseMode.HTML,
            )
        note = await message.reply(V.UPDATES_RUNNING, parse_mode=ParseMode.HTML)
        await _render_updates_review(note, deduped)

    # ── FSM text consumer — caption edit + schedule time ──────────────────────
    @client.on_message(filters.text & filters.private & ~filters.command(["cancel"]))
    async def _fsm_text(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, data = await fsm.get(message.from_user.id)
        code = data.get("request_code")
        if state == STATE_EDIT_CAPTION and code:
            from kurosoden.shared.settings_ui import parse_user_markup

            caption = parse_user_markup(message)
            await fsm.clear(message.from_user.id)
            await _execute_publish(
                client, container, message, code,
                silent=False, caption_override=caption,
            )
        elif state == STATE_UPDATES_EDIT:
            await _apply_updates_edit(message, data.get("rows", []))
        elif state == STATE_SCHEDULE and code:
            raw = (message.text or "").strip()
            tz_name = await _admin_tz(container, message.from_user.id)
            when_utc = _parse_schedule(raw, tz_name)
            if when_utc is None:
                await message.reply(V.schedule_bad_time(raw), parse_mode=ParseMode.HTML)
                return
            # Warn (but don't block) if the slot is crowded — the admin stays in
            # the schedule state so their next message is another time attempt.
            from nekofetch.services.schedule_service import ScheduleService

            clashes = await ScheduleService(container).collision_window(
                when_utc, exclude_code=code,
            )
            if clashes:
                rows = [(_to_tz(c.scheduled_at, tz_name), c.anime_title or c.request_code)
                        for c in clashes]
                await message.reply(
                    V.schedule_collision(rows, _tz_label(tz_name)),
                    parse_mode=ParseMode.HTML,
                )
                return
            await fsm.clear(message.from_user.id)
            await _schedule_publish(client, container, message, code, when_utc, tz_name)
        elif state == STATE_EDIT_FOOTER:
            from nekofetch.services.footer_service import FooterService

            from kurosoden.shared.settings_ui import parse_user_markup

            html = parse_user_markup(message)
            await fsm.clear(message.from_user.id)
            result = await FooterService(container).set_footer(html)
            await message.reply(
                V.footer_updated(result.ok, result.footers_rewritten,
                                 result.bots_bumped),
                parse_mode=ParseMode.HTML,
            )
        elif state == STATE_CHANGE_MAIN:
            raw = (message.text or "").strip()
            new_id = _parse_channel_id(raw)
            if new_id is None:
                await message.reply(V.change_main_bad(raw), parse_mode=ParseMode.HTML)
                return
            await fsm.clear(message.from_user.id)
            await _restore_to_channel(client, container, message, new_id)
        elif state == STATE_CHANGE_INDEX:
            raw = (message.text or "").strip()
            new_id = _parse_channel_id(raw)
            if new_id is None:
                await message.reply(V.change_main_bad(raw), parse_mode=ParseMode.HTML)
                return
            await fsm.clear(message.from_user.id)
            await _restore_index_to_channel(client, container, message, new_id)
        elif state == STATE_IDX_SLOT_CAPTION:
            from kurosoden.shared.settings_ui import parse_user_markup
            from nekofetch.services.index_channel_service import IndexChannelService

            order = int(data.get("slot_order", -1))
            html_cap = parse_user_markup(message)
            await fsm.clear(message.from_user.id)
            ok = await IndexChannelService(container).edit_slot_caption(order, html_cap)
            await message.reply(V.index_edit_result(ok, "caption"), parse_mode=ParseMode.HTML)
        elif state == STATE_IDX_SLOT_IMAGE:
            from nekofetch.services.index_channel_service import IndexChannelService

            order = int(data.get("slot_order", -1))
            image = _extract_image_ref(message)
            await fsm.clear(message.from_user.id)
            if not image:
                await message.reply(V.index_image_bad, parse_mode=ParseMode.HTML)
                return
            ok = await IndexChannelService(container).replace_slot_image(order, image)
            await message.reply(V.index_edit_result(ok, "image"), parse_mode=ParseMode.HTML)
        elif state == STATE_IDX_SLOT_BUTTONS:
            from nekofetch.services.index_channel_service import IndexChannelService

            order = int(data.get("slot_order", -1))
            buttons = _parse_button_lines(message.text or "")
            await fsm.clear(message.from_user.id)
            ok = await IndexChannelService(container).set_slot_buttons(order, buttons)
            await message.reply(
                V.index_buttons_result(ok, len(buttons)), parse_mode=ParseMode.HTML,
            )

    @client.on_message(filters.command("cancel"))
    async def _cancel(_: Client, message: Message) -> None:
        if message.from_user:
            await fsm.clear(message.from_user.id)
        await message.reply(f"{V.ICON} Cancelled.", parse_mode=ParseMode.HTML)

    # ── /publish — Review and publish flow ────────────────────────────────────
    @client.on_message(filters.command("publish"))
    async def _publish_cmd(_: Client, message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "<b>📰 Publish Review</b>\n\n"
                "Usage: <code>/publish REQ-XXXX</code>\n\n"
                "Shows the generated caption and thumbnail for review.\n"
                "You can edit the caption (Markdown or HTML) before publishing.",
                parse_mode=ParseMode.HTML,
            )
            return

        request_code = parts[1].strip()
        await _review_for_publish(client, container, message, request_code, fsm)

    # ── /recover — Channel recovery ───────────────────────────────────────────
    @client.on_message(filters.command("recover"))
    async def _recover_cmd(_: Client, message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "<b>🔄 Channel Recovery</b>\n\n"
                "Usage: <code>/recover REQ-XXXX</code>\n\n"
                "Detects and replaces banned distribution channels:\n"
                "• Replaces the distribution channel\n"
                "• Updates buttons in the main channel\n"
                "• Updates buttons in the index channel\n"
                "• Repairs every affected link",
                parse_mode=ParseMode.HTML,
            )
            return

        request_code = parts[1].strip()
        await _recover_channel(client, container, message, request_code)

    # ── /help ─────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("help"))
    async def _help(_: Client, message: Message) -> None:
        caption = (
            "<b>🔮 Gojo Satoru — Publisher</b>\n\n"
            "<i>The final step — your work goes live here.</i>\n\n"
            "<b>How it works</b>\n"
            "1. Distribution finishes → a task lands with you\n"
            "2. I build the main-channel post + franchise thumbnail\n"
            "3. Review the caption — edit it in Markdown/HTML\n"
            "4. Approve → I publish now or on a schedule\n"
            "5. The A–Z index updates itself\n\n"
            "<b>Commands</b>\n"
            "/tasks — What's waiting to publish\n"
            "/publish — Review &amp; publish a title\n"
            "/schedule — Publish at a set time\n"
            "/recover — Rebuild a banned channel + fix every button"
        )
        await send_screen(
            client, message.chat.id,
            Screen(caption=caption, image=pick_artwork("gojo"),
                   keyboard=keyboard([("◀ Back", cb("gojo", "home"))])),
        )

    # ── /settings ── handled by the shared human-friendly settings engine
    # (register_settings in handlers/__init__.py) under the gojo|set|… namespace.
    # A local /settings handler here would shadow it — see the app.py note.


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _review_for_publish(
    client: Client, container: Container, message: Message,
    request_code: str, fsm: FSM,
) -> None:
    """Show the caption/thumbnail for admin review before publishing."""
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.repositories.request_repo import RequestRepository

    async with session_scope(container.pg_sessionmaker) as session:
        req = await RequestRepository(session).get_by_code(request_code)
        if req is None:
            await message.reply(
                f"❌ <b>Request not found:</b> {request_code}",
                parse_mode=ParseMode.HTML,
            )
            return
        title = req.anime_title
        anime_doc_id = req.anime_doc_id

    await send_screen(
        client, message.chat.id,
        Screen(
            caption=V.review_card(title, request_code, anime_doc_id),
            image=pick_artwork("gojo"),
            keyboard=_publish_keyboard(request_code),
        ),
    )


async def _execute_publish(
    client: Client, container: Container, message: Message, request_code: str,
    *, caption_override: str | None = None, silent: bool = False,
) -> None:
    """Publish to the main channel and update the index.

    ``caption_override`` carries an admin-edited caption (already parsed to HTML
    with styling and line breaks preserved); ``silent`` posts without a channel
    notification. Both flow straight through ``PublishingService.publish``.
    """
    try:
        from nekofetch.services.publishing_service import PublishingService

        title = request_code
        try:
            from nekofetch.infrastructure.database.postgres.session import session_scope
            from nekofetch.infrastructure.repositories.request_repo import RequestRepository
            async with session_scope(container.pg_sessionmaker) as session:
                req = await RequestRepository(session).get_by_code(request_code)
                if req:
                    title = req.anime_title
        except Exception:  # noqa: BLE001 — title is decorative on the receipt
            pass

        await PublishingService(container).publish(
            request_code, caption_override=caption_override, silent=silent,
        )

        await send_screen(
            client, message.chat.id,
            Screen(caption=V.published(title, silent=silent),
                   image=pick_artwork("gojo"),
                   keyboard=keyboard([(V.BTN_TASKS, cb("gojo", "tasks"))])),
        )

        # Mark task as completed.
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine
        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        await engine.complete_task(request_code, "gojo")

        # Everything is live (main channel + distribution) — the prefetched
        # artwork on disk is no longer needed. Delete the local image bytes; the
        # hosted links stay in assets.json / the DB for any later rebuild.
        try:
            from nekofetch.services.metadata_prefetch import cleanup_local_assets

            _doc = None
            try:
                from nekofetch.infrastructure.database.postgres.session import session_scope
                from nekofetch.infrastructure.repositories.request_repo import RequestRepository
                async with session_scope(container.pg_sessionmaker) as session:
                    req = await RequestRepository(session).get_by_code(request_code)
                    if req:
                        _doc = req.anime_doc_id
            except Exception:  # noqa: BLE001
                pass
            await cleanup_local_assets(container, request_code, anime_doc_id=_doc)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            log.debug("gojo.publish.asset_cleanup_failed",
                      code=request_code, error=str(exc))

    except Exception as exc:
        log.warning("gojo.publish.failed", code=request_code, error=str(exc))
        # ``message`` may be a Message (has .reply) or a bare Chat (scheduled
        # fire) — resolve a chat id either way and send through the client.
        chat_id = getattr(message, "chat", message)
        chat_id = getattr(chat_id, "id", chat_id)
        await client.send_message(
            chat_id, V.fail(str(exc)[:300]), parse_mode=ParseMode.HTML,
        )


async def _recover_channel(
    client: Client, container: Container, message: Message, request_code: str,
) -> None:
    """Recover a banned or broken distribution channel."""
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.repositories.request_repo import RequestRepository

    async with session_scope(container.pg_sessionmaker) as session:
        req = await RequestRepository(session).get_by_code(request_code)
        if req is None:
            await message.reply(
                f"❌ <b>Request not found:</b> {request_code}",
                parse_mode=ParseMode.HTML,
            )
            return
        title = req.anime_title
        anime_doc_id = req.anime_doc_id

    if not anime_doc_id:
        await message.reply("❌ No anime ID found for this request.")
        return

    await message.reply(
        f"🔄 <b>Starting recovery</b> for <b>{title}</b>...\n\n"
        "<i>Checking distribution channels...</i>",
        parse_mode=ParseMode.HTML,
    )

    try:
        # Reuse NekoFetch's BotOrchestratorService for recreation.
        from nekofetch.services.bot_orchestrator import BotOrchestratorService

        orch = BotOrchestratorService(container)
        info = await orch.recreate_bot(anime_doc_id)

        if info:
            await message.reply(
                f"✅ <b>Channel Recovered!</b>\n\n"
                f"🎬 <b>Anime:</b> {title}\n"
                f"📺 <b>New entity:</b> @{info.username or info.name}\n\n"
                "<i>All buttons in the main channel and index have been updated.</i>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.reply(
                "⚠️ <b>Recovery incomplete</b>\n\n"
                "Could not recreate the distribution entity.\n"
                "Check the logs for details.",
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        await message.reply(
            f"❌ <b>Recovery failed:</b> {str(exc)[:300]}",
            parse_mode=ParseMode.HTML,
        )


# ── Scheduling ─────────────────────────────────────────────────────────────────


async def _admin_tz(container: Container, admin_id: int) -> str | None:
    """The scheduling admin's IANA timezone (None → global display default)."""
    try:
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        return await AdminAssignmentEngine(container.pg_sessionmaker).get_timezone(admin_id)
    except Exception:  # noqa: BLE001 — a lookup miss just falls back to global tz
        return None


def _tz_label(tz_name: str | None) -> str:
    from nekofetch.core.timefmt import tz_offset_label

    base = tz_name or "Asia/Dhaka"
    return f"{base} ({tz_offset_label(tz_name)})"


def _to_tz(dt: datetime, tz_name: str | None) -> str:
    from nekofetch.core.timefmt import to_tz

    return to_tz(dt, tz_name, with_label=False)


async def _show_schedule_queue(
    container: Container, message: Message, tz_name: str | None,
) -> None:
    """Reply with the combined pending-schedule table in the admin's timezone."""
    from nekofetch.services.schedule_service import ScheduleService

    pending = await ScheduleService(container).list_pending()
    rows = [(_to_tz(p.scheduled_at, tz_name), p.anime_title or p.request_code)
            for p in pending]
    await message.reply(
        V.schedule_table(rows, _tz_label(tz_name)), parse_mode=ParseMode.HTML,
    )


def _parse_schedule(raw: str, tz_name: str | None) -> datetime | None:
    """Parse ``YYYY-MM-DD HH:MM`` entered in the admin's timezone → aware UTC.

    Returns ``None`` if unparseable or already in the past (compared in UTC), so
    the caller can show the "bad time" prompt.
    """

    from nekofetch.core.timefmt import parse_local

    when_utc = parse_local(raw, tz_name)
    if when_utc is None:
        return None
    if when_utc <= datetime.now(UTC):
        return None
    return when_utc


async def _schedule_publish(
    client: Client, container: Container, message: Message,
    request_code: str, when_utc: datetime, tz_name: str | None,
) -> None:
    """Persist a durable scheduled publish that survives restarts.

    The row is the source of truth: ``ScheduleService.sweep_due`` (a 60s job in
    ``manager.py``) fires it via the same ``PublishingService`` path the buttons
    use, so a scheduled publish is byte-identical to an immediate one — just
    deferred, and never lost to a restart.
    """
    from nekofetch.services.schedule_service import ScheduleService

    title = request_code
    try:
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.infrastructure.repositories.request_repo import RequestRepository
        async with session_scope(container.pg_sessionmaker) as session:
            req = await RequestRepository(session).get_by_code(request_code)
            if req:
                title = req.anime_title
    except Exception:  # noqa: BLE001
        pass

    from nekofetch.core.timefmt import to_tz

    await ScheduleService(container).schedule(
        request_code, message.from_user.id, when_utc, anime_title=title,
    )
    when_text = to_tz(when_utc, tz_name)  # includes the "UTC+N" label
    await send_screen(
        client, message.chat.id,
        Screen(caption=V.scheduled(title, when_text),
               image=pick_artwork("gojo"),
               keyboard=keyboard([(V.BTN_TASKS, cb("gojo", "tasks"))])),
    )
    await _show_schedule_queue(container, message, tz_name)


def _parse_channel_id(raw: str) -> int | None:
    """Parse a channel id like ``-1001234567890`` into an int, or ``None``.

    Only numeric channel ids are accepted (not @usernames): restore uses the
    id directly with the admin client, and a public channel that got banned
    won't have a resolvable @handle anymore.
    """
    raw = (raw or "").strip()
    if not raw.lstrip("-").isdigit():
        return None
    return int(raw)


async def _restore_to_channel(
    client: Client, container: Container, message: Message, new_id: int,
) -> None:
    """Rebuild every backed-up main-channel post on ``new_id`` from the DB.

    No re-rendering: captions, mirrored images, buttons, and dividers all come
    from :class:`PublishedPostBackup`. Repoints the main-channel config + each
    ``ChannelPost`` at the new channel on success.
    """
    from nekofetch.services.backup_service import BackupService

    note = await message.reply(V.RESTORE_RUNNING, parse_mode=ParseMode.HTML)
    stats = await BackupService(container).restore_to_channel(new_id)
    await note.edit_text(
        V.restore_done(stats.restored, stats.total, stats.failed),
        parse_mode=ParseMode.HTML,
    )


async def _restore_index_to_channel(
    client: Client, container: Container, message: Message, new_id: int,
) -> None:
    """Rebuild the whole index channel on ``new_id`` from its wipe-proof backup.

    Reposts the poster + every slot verbatim, remaps each ``IndexSection`` message
    id, and repoints the index-channel config (id / username / poster id) so every
    t.me link and hyperlink follows. Used when the index channel itself is banned.
    """
    from nekofetch.services.backup_service import BackupService

    note = await message.reply(V.RESTORE_RUNNING, parse_mode=ParseMode.HTML)
    stats = await BackupService(container).restore_index(new_id)
    await note.edit_text(
        V.restore_done(stats.restored, stats.total, stats.failed),
        parse_mode=ParseMode.HTML,
    )


def _extract_image_ref(message: Message) -> str | None:
    """Pull an image reference from an admin's message for a slot-image swap.

    Accepts a sent photo/document (uses its Telegram ``file_id``) or a plain URL
    typed as text. Returns ``None`` when neither is present so the caller can
    reprompt.
    """
    if message.photo:
        return message.photo.file_id
    doc = getattr(message, "document", None)
    if doc is not None and (getattr(doc, "mime_type", "") or "").startswith("image/"):
        return doc.file_id
    text = (message.text or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return None


def _parse_button_lines(raw: str) -> list[tuple[str, str]]:
    """Parse ``Text | https://url`` lines into ``[(text, url), …]`` (one per row).

    Blank lines and lines without a ``|`` separator or a valid http(s) URL are
    skipped, so a stray line can't create a dead button. An empty result clears
    the slot's buttons.
    """
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        text, _, url = line.partition("|")
        text, url = text.strip(), url.strip()
        if text and (url.startswith("http://") or url.startswith("https://")):
            out.append((text, url))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Scheduled monthly maintenance jobs (wired onto the pipeline scheduler)
# ─────────────────────────────────────────────────────────────────────────────
# Both DM the on-shift Gojo admins rather than acting silently: the update sweep
# is detect-only (create=False) so a human reviews/trims/adds before anything is
# requested, and the ban check reports + auto-recovers down channels. Each is a
# closure over ``container`` so the pipeline manager can register it without the
# Gojo handler's ``register`` scope.

async def _gojo_admin_ids(container: Container) -> list[int]:
    """Telegram ids of admins who cover the Gojo (publishing) stage.

    Falls back to the ``.env`` owner/admin ids when the pool has no Gojo admin,
    so a scheduled notify is never silently dropped on a fresh deployment.
    """
    ids: list[int] = []
    try:
        from kurosoden.shared.management_service import ManagementService

        admins = await ManagementService(container.pg_sessionmaker).list_admins(
            stage="gojo",
        )
        ids = [a.telegram_id for a in admins]
    except Exception as exc:  # noqa: BLE001 — fall back to env owners below
        log.warning("gojo.sched.admin_lookup_failed", error=str(exc))
    if not ids:
        ids = list(getattr(container.env, "admin_ids", []) or [])
    # De-dup, preserve order.
    seen: set[int] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def make_monthly_update_notify_job(container: Container):
    """Build the scheduled monthly update-check job.

    Detect-only franchise sweep → DM each Gojo admin the **reviewable** list
    (same drop/edit/submit card as ``/updates``) with per-admin FSM state armed,
    so nothing is auto-created; the admin commits via Submit. A no-op when there
    are no new entries. Best-effort per admin — one failed DM never aborts the rest.
    """
    fsm = FSM(container.redis, bot="gojo")

    async def _tick() -> None:
        try:
            from nekofetch.services.maintenance_service import MaintenanceService

            from nekofetch.core.redis_safe import safe_redis_set

            # Stamp the sweep time so the Stats screen shows when it last ran,
            # even when the sweep finds nothing.
            await safe_redis_set(
                container.redis,
                "nf:maint:last_update_check",
                datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            )
            results = await MaintenanceService(container).scan_updates()
            if not results:
                log.info("gojo.sched.updates.none")
                return
            rows = _flatten_update_rows(results)
            if not rows:
                return
            mgr = getattr(container, "pipeline_manager", None)
            gojo = getattr(mgr, "gojo", None) if mgr else None
            if gojo is None:
                log.warning("gojo.sched.updates.no_client")
                return
            admin_ids = await _gojo_admin_ids(container)
            sent = 0
            for admin_id in admin_ids:
                try:
                    await fsm.set(admin_id, STATE_UPDATES_REVIEW, rows=rows)
                    note = await gojo.send_message(
                        admin_id, V.UPDATES_SCHEDULED_INTRO, parse_mode=ParseMode.HTML,
                    )
                    await render_updates_review(note, rows)
                    sent += 1
                except Exception as exc:  # noqa: BLE001 — one bad DM never stops the rest
                    log.warning("gojo.sched.updates.dm_failed",
                                admin=admin_id, error=str(exc)[:200])
            log.info("gojo.sched.updates.sent", admins=sent, entries=len(rows))
        except Exception as exc:  # noqa: BLE001 — a scheduler job must never crash the loop
            log.warning("gojo.sched.updates.tick_failed", error=str(exc)[:200])

    return _tick


def make_monthly_bancheck_job(container: Container):
    """Build the scheduled monthly ban-check job.

    Probes every channel; auto-recovers down distribution channels (Senku rebuild
    → verbatim restore from backup) and DMs the Gojo admins a summary. The main
    channel and index channel have no ``anime_doc_id`` — they need the manual
    Change-Main / Change-Index flow — so they're reported but not auto-recovered.
    """
    async def _tick() -> None:
        try:
            from nekofetch.services.maintenance_service import MaintenanceService

            from nekofetch.core.redis_safe import safe_redis_set
            from nekofetch.services.bot_orchestrator import BotOrchestratorService

            await safe_redis_set(
                container.redis,
                "nf:maint:last_ban_check",
                datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            )
            result = await MaintenanceService(container).probe_channels()
            if not result.banned:
                log.info("gojo.sched.bancheck.clear", checked=result.checked)
                return
            orch = BotOrchestratorService(container)
            recovered: list[str] = []
            for probe in result.banned:
                if not probe.anime_doc_id:
                    continue
                try:
                    info = await orch.recreate_bot(probe.anime_doc_id)
                    if info:
                        recovered.append(probe.name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("gojo.sched.bancheck.recover_failed",
                                anime=probe.anime_doc_id, error=str(exc)[:200])
            mgr = getattr(container, "pipeline_manager", None)
            gojo = getattr(mgr, "gojo", None) if mgr else None
            if gojo is not None:
                summary = V.bancheck_scheduled_summary(
                    result.checked, len(result.banned), recovered,
                )
                for admin_id in await _gojo_admin_ids(container):
                    try:
                        await gojo.send_message(admin_id, summary,
                                                parse_mode=ParseMode.HTML)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("gojo.sched.bancheck.dm_failed",
                                    admin=admin_id, error=str(exc)[:200])
            log.info("gojo.sched.bancheck.done", checked=result.checked,
                     banned=len(result.banned), recovered=len(recovered))
        except Exception as exc:  # noqa: BLE001 — a scheduler job must never crash the loop
            log.warning("gojo.sched.bancheck.tick_failed", error=str(exc)[:200])

    return _tick
