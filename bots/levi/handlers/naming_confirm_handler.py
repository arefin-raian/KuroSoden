"""Levi side of the interactive filename / caption confirm gate.

The download **worker** (headless) posts a confirm card, arms a chat-scoped reply
marker, and BLOCK-POLLS a Redis flag (see ``nekofetch.services.naming_confirm``).
This module is the bot-side counterpart that consumes the admin's answer and
releases the worker:

* **✅ Use it**  (``levi|nmuse|{job}|{kind}``) — accept the computed default:
  write the ``__use__`` sentinel, clear the awaiting flag, disarm, tidy the card.
* **✏️ Edit**   (``levi|nmedit|{job}|{kind}``) — swap the buttons to Cancel and
  nudge the admin to send their corrected text back. The marker stays armed.
* **❌ Cancel** (``levi|nmcancel|{job}|{kind}``) — restore the Use it / Edit row
  without releasing the worker (nothing changes).
* **text reply** (group=16) — when a marker with state ``levi_confirm_{kind}`` is
  live in this chat, the next text message IS the edit: write it to the value key,
  clear the awaiting flag, disarm, edit the card.

Everything is chat-scoped (via ``channel_reply``), so it works both in a DM with
Levi and from the anonymous Control Center channel. The text consumer sits in its
own handler group (16) so it never collides with the review flow's group=4/5/6/7/
10/12/13/14 handlers or pack-caption's group=15.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from nekofetch.bots.channel_reply import disarm as _disarm_reply
from nekofetch.bots.channel_reply import peek as _peek_reply
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import (
    safe_redis_delete,
    safe_redis_get,
    safe_redis_set,
)
from nekofetch.services.naming_confirm import (
    _USE_DEFAULT,
    await_key,
    ddlmap_data_key,
    value_key,
)
from nekofetch.ui.components import cb

log = get_logger(__name__)

# Text-reply kinds this handler consumes. ``name``/``caption`` capture a free-text
# edit; ``ddlmap`` captures ``<file#> S<season>`` override lines for the DDL
# franchise-mapping confirm (a different grammar — handled in its own branch).
_KINDS = {"name", "caption", "ddlmap"}


def _choice_kb(job_id: int, kind: str) -> InlineKeyboardMarkup:
    """The resting keyboard: accept the default, or start an edit.

    Rebuilt (not stored) so it matches the worker's original card exactly — same
    callbacks the confirm gate posts in ``naming_confirm.confirm``."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Use it", callback_data=cb("levi", "nmuse", job_id, kind)),
        InlineKeyboardButton("✏️ Edit", callback_data=cb("levi", "nmedit", job_id, kind)),
    ]])


def _cancel_kb(job_id: int, kind: str) -> InlineKeyboardMarkup:
    """The editing keyboard: while we await the typed value, the only action is to
    back out. Tapping it restores :func:`_choice_kb`."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=cb("levi", "nmcancel", job_id, kind)),
    ]])


def _card_key(job_id: int, kind: str) -> str:
    return f"nf:job:{job_id}:{kind}_card"


async def _release(redis, job_id: int, kind: str, value: str) -> None:
    """Hand the admin's choice to the blocked worker.

    Write the value first, THEN clear the awaiting flag — the worker only reads
    the value once it sees the flag gone, so this ordering guarantees it never
    wakes to an empty value."""
    await safe_redis_set(redis, value_key(job_id, kind), value,
                         label="naming_confirm.set_value", ex=15 * 60)
    await safe_redis_delete(redis, await_key(job_id, kind),
                            label="naming_confirm.release")


# ── DDL mapping-confirm helpers (kind "ddlmap") ───────────────────────────────

def _rebuild_ddl_mapping(data: dict, overrides: dict[int, int]):
    """Re-run ``build_torrent_mapping`` from the stashed working set + season
    overrides. Returns a ``TorrentMapping`` (or None). The franchise structure is
    reconstructed from the stored mapping entries (they carry season/part/episode
    counts) — no AniList re-walk, mirroring the torrent flow's rebuild."""
    from nekofetch.services.franchise_flow import FranchiseMapping
    from nekofetch.services.torrent_mapping import TorrentMapping, build_torrent_mapping

    md = data.get("mapping")
    ordered = data.get("ordered_files")
    if not md or not ordered:
        return None
    prev = TorrentMapping.from_dict(md)
    franchise = FranchiseMapping(
        anime_doc_id="", root_title="",
        entries=[e.franchise_entry for e in prev.entries],
    )
    # JSON stringified the int anilist_id keys when the working set was stashed —
    # coerce them back so build_torrent_mapping's title-match tier keeps working
    # on the manual re-run exactly as it did on the auto build.
    raw_titles = data.get("episode_titles") or {}
    ep_titles: dict[int, list] = {}
    for k, v in raw_titles.items():
        try:
            ep_titles[int(k)] = v
        except (TypeError, ValueError):
            continue
    return build_torrent_mapping(
        ordered, franchise, episode_titles=ep_titles or None,
        season_overrides=overrides or None)


def _render_ddl_card(data: dict, mapping) -> str:
    """The mapping card body (per-entry present/to-encode qualities), using the
    encode config carried in the stashed working set so it matches the worker."""
    from nekofetch.ui.torrent_screens import format_torrent_mapping

    head = (
        "🧩 <b>Franchise mapping</b>\n"
        "Each entry shows the qualities present and what will be encoded to fill "
        "the gaps. To fix a mis-detected season, tap <b>Fix seasons</b> and send "
        "lines like <code>3 S2</code> (file #3 → Season 2).\n\n"
    )
    body = format_torrent_mapping(
        mapping,
        encode_heights=data.get("encode_heights") or [],
        fallbacks_cfg=data.get("fallbacks_cfg") or {},
    )
    return head + f"<pre>{body}</pre>"


def register(client: Client, container: Container) -> None:
    """Wire the confirm-card callbacks + the edited-text consumer."""

    @client.on_callback_query(filters.regex(r"^levi\|nmuse\|"))
    async def _use_default(client: Client, q: CallbackQuery) -> None:
        parts = q.data.split("|")
        if len(parts) < 4:
            return
        job_id, kind = int(parts[2]), parts[3]
        redis = container.redis
        if redis is not None:
            await _release(redis, job_id, kind, _USE_DEFAULT)
            await _disarm_reply(redis, q.message.chat.id)
        try:
            await q.message.edit_text(
                (q.message.text.html if q.message.text else "")
                + "\n\n<i>✅ Using this — continuing.</i>",
                parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001 — the edit is cosmetic
            pass
        try:
            await q.answer("Using it.")
        except Exception:  # noqa: BLE001
            pass

    @client.on_callback_query(filters.regex(r"^levi\|nmedit\|"))
    async def _prompt_edit(client: Client, q: CallbackQuery) -> None:
        parts = q.data.split("|")
        if len(parts) < 4:
            return
        job_id, kind = int(parts[2]), parts[3]
        what = "file name" if kind == "name" else "caption"
        # We're now awaiting a typed value, so the only meaningful action left is
        # to back out — swap the [Use it | Edit] row for a single Cancel button.
        try:
            await q.edit_message_reply_markup(reply_markup=_cancel_kb(job_id, kind))
        except Exception:  # noqa: BLE001 — button swap is cosmetic
            pass
        try:
            await q.answer(
                f"Copy the {what} above, edit it, and send it back to me.",
                show_alert=True)
        except Exception:  # noqa: BLE001
            pass

    @client.on_callback_query(filters.regex(r"^levi\|nmcancel\|"))
    async def _cancel_edit(client: Client, q: CallbackQuery) -> None:
        """Back out of an edit without changing anything — restore the choice row.

        The worker stays blocked on the awaiting flag (we do NOT release it), so
        the admin can still tap Use it or Edit again; nothing is lost."""
        parts = q.data.split("|")
        if len(parts) < 4:
            return
        job_id, kind = int(parts[2]), parts[3]
        try:
            await q.edit_message_reply_markup(reply_markup=_choice_kb(job_id, kind))
        except Exception:  # noqa: BLE001 — button swap is cosmetic
            pass
        try:
            await q.answer("Edit cancelled — the shown value stands.")
        except Exception:  # noqa: BLE001
            pass

    @client.on_callback_query(filters.regex(r"^levi\|ddlfix\|"))
    async def _ddl_fix_prompt(client: Client, q: CallbackQuery) -> None:
        """DDL mapping: 'Fix seasons' tapped — keep the worker blocked and nudge
        the admin to send ``<file#> S<season>`` override lines (consumed below).
        The marker stays armed; Proceed (nmuse) still ends the wait."""
        parts = q.data.split("|")
        if len(parts) < 3:
            return
        try:
            await q.answer(
                "Send lines like `3 S2` (file #3 → Season 2), one per fix. "
                "Then tap Proceed.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass

    @client.on_message(filters.text & ~filters.command(["start"]), group=16)
    async def _consume_edit(client: Client, message: Message) -> None:
        """Capture a text reply while a naming/caption/ddlmap marker is armed here."""
        redis = container.redis
        if redis is None:
            return
        state, data = await _peek_reply(redis, message.chat.id)
        if not state or not state.startswith("levi_confirm_"):
            return
        kind = state[len("levi_confirm_"):]
        if kind not in _KINDS:
            return
        job_id = data.get("job_id")
        if job_id is None:
            return
        job_id = int(job_id)

        if kind == "ddlmap":
            await _consume_ddlmap_fix(client, message, redis, job_id)
            return

        text = (message.text or "").strip()
        if not text:
            return

        await _release(redis, job_id, kind, text)
        await _disarm_reply(redis, message.chat.id)

        # The admin's edited value has been captured — delete their message so the
        # chat stays clean (the single evolving card is the only surface we keep).
        try:
            await message.delete()
        except Exception:  # noqa: BLE001 — best-effort tidy
            pass

        # Edit the confirm card in place so the admin sees their value took.
        ref = await safe_redis_get(redis, _card_key(job_id, kind),
                                   label="naming_confirm.cardref_read")
        if ref and ":" in ref:
            try:
                chat_s, msg_s = ref.split(":", 1)
                await client.edit_message_text(
                    int(chat_s), int(msg_s),
                    f"<b>Applied your edit</b>\n\n<code>{text}</code>",
                    parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001 — cosmetic
                pass
        log.info("levi.naming_confirm.edit_consumed", job_id=job_id, kind=kind)

    async def _consume_ddlmap_fix(client: Client, message: Message, redis, job_id: int) -> None:
        """Apply ``<file#> S<season>`` override lines to the DDL mapping, re-run
        it, and re-show the card in place. The worker stays blocked (marker armed,
        Proceed still ends it) — this only refines the mapping the worker will read
        back on Proceed."""
        import json
        import re

        text = (message.text or "").strip()
        raw = await safe_redis_get(redis, ddlmap_data_key(job_id),
                                   label="ddlmap.fix_read")
        try:
            data = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            return

        ordered = data.get("ordered_files") or []
        max_idx = len(ordered)
        overrides: dict[int, int] = {}
        applied: list[tuple[int, int]] = []
        for m in re.finditer(r"\b(\d+)\s+[sS]\s*(\d+)\b", text):
            file_no, season = int(m.group(1)), int(m.group(2))
            if 1 <= file_no <= max_idx:
                real_index = ordered[file_no - 1].get("index")
                if real_index is not None:
                    overrides[int(real_index)] = season
                    applied.append((file_no, season))

        try:
            await message.delete()
        except Exception:  # noqa: BLE001 — best-effort tidy
            pass
        if not applied:
            return

        mapping = _rebuild_ddl_mapping(data, overrides)
        if mapping is None:
            return
        # Persist the corrected mapping so Proceed (the worker) reads THIS one.
        data["mapping"] = mapping.to_dict()
        await safe_redis_set(redis, ddlmap_data_key(job_id), json.dumps(data),
                             label="ddlmap.fix_write", ex=15 * 60)

        ref = await safe_redis_get(redis, _card_key(job_id, "ddlmap"),
                                   label="ddlmap.fix_cardref")
        if ref and ":" in ref:
            from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            applied_str = ", ".join(f"#{n}→S{s}" for n, s in applied)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Proceed", callback_data=cb("levi", "nmuse", job_id, "ddlmap")),
                InlineKeyboardButton("🔧 Fix seasons", callback_data=cb("levi", "ddlfix", job_id)),
            ]])
            try:
                chat_s, msg_s = ref.split(":", 1)
                await client.edit_message_text(
                    int(chat_s), int(msg_s),
                    f"<i>Applied {applied_str}.</i>\n\n" + _render_ddl_card(data, mapping),
                    parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:  # noqa: BLE001 — cosmetic
                pass
        log.info("levi.ddlmap.fix_applied", job_id=job_id, fixes=len(applied))
