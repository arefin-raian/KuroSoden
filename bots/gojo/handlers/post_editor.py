"""Gojo post editor — edit published MAIN-channel and INDEX-channel posts.

The owner opens ``/editpost`` (or the home-board "📝 Edit Post" button) and picks
a surface:

  * **Main channel** — one post per franchise. Paginated title list → per-post
    menu → edit the caption, the buttons, the image, or regenerate the thumbnail
    card's values (rating / synopsis / genres / studio / title / logo·backdrop
    URLs). Every edit hits the LIVE post via ``MainChannelService`` (which acts
    as the channel admin — the Gojo bot itself can't edit an admin-authored post
    → ``MESSAGE_AUTHOR_REQUIRED``) and is mirrored into the wipe-proof
    ``published_post_backups`` row, so a later ban-restore ships the edited copy,
    never the stale one.
  * **Index channel** — the A–Z (+ ``#``) letter grid. Pick a letter → edit its
    text (HTML), replace its image, or set its buttons. Each edit drives the
    keyboard-safe ``IndexChannelService`` primitives AND re-snapshots the index
    backup (``record_index``) — the gap the old slot editor left, which let a
    restore revive pre-edit captions.

Design notes:
  * Callbacks live under the SHORT ``gojo|pe|…`` namespace (not ``postedit``) so
    ``gojo|pe|mp|<anime_doc_id>`` stays under Telegram's 64-byte callback cap
    even for a 48-char doc id; the thumbnail field is encoded as an INDEX into
    ``_EDITABLE`` for the same reason.
  * State for the text/photo capture steps uses an ISOLATED FSM namespace
    (``gojo_postedit``) so it can never clash with the publish wizard's
    ``FSM(redis, "gojo")``. The capture consumers register at groups 14 (text)
    and 16 (photo): every Gojo handler is group 0 and dispatched by registration
    order, so a group-0 consumer would be shadowed by ``tasks.py::_fsm_text``
    (which matches ALL private text) — a distinct group lets both run, and each
    gates on its own FSM.
  * The generic live-edit primitives (keyboard-preserve, ``.html`` caption) are
    the shared ``post_edit_ops`` module; the thumbnail value-edit core is reused
    verbatim from Senku's thumbnail editor (``_apply_field`` with ``-1`` = the
    main card), so both surfaces share one implementation.

Owner-only throughout (``is_owner``).
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import distinct, select

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import (
    ChannelPost,
    StoragePack,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.services.backup_service import BackupService
from nekofetch.services.index_channel_service import IndexChannelService
from nekofetch.services.main_channel_service import MainChannelService
from nekofetch.ui.components import cb, paginate
from nekofetch.ui.screens import Screen, card, message_ref, send_screen
from kurosoden.shared import post_edit_ops
from kurosoden.shared.access_gate import is_owner
from kurosoden.shared.settings_ui import parse_user_markup
# Reuse Senku's thumbnail value-edit core verbatim (main card == anilist_id -1).
from kurosoden.bots.senku.handlers.thumbnail_edit_senku import (
    _EDITABLE,
    _FIELD_LABELS,
    _MAIN_ANILIST,
    _apply_field,
    _entry_fields,
)

log = get_logger(__name__)

_BOT = "gojo"
_NS = "pe"  # short callback namespace → keeps gojo|pe|<act>|<doc> under 64 bytes

# FSM states (isolated namespace so a live publish wizard can't be clobbered).
_ST_MCAP = "gpe_main_caption"
_ST_MBTN = "gpe_main_buttons"
_ST_MIMG = "gpe_main_image"
_ST_MFIELD = "gpe_main_field"
_ST_ITEXT = "gpe_idx_text"
_ST_IIMG = "gpe_idx_image"
_ST_IBTN = "gpe_idx_buttons"

_MAIN_PAGE = 10   # 1 column (titles are long)
_IDX_PAGE = 24    # 3 columns × 8 rows

_TEXT_LIMIT = post_edit_ops.TEXT_LIMIT

_BTN_CANCEL = ("✖ Cancel", cb(_BOT, _NS, "x"))
_BTN_HOME = ("◀ Back", cb(_BOT, _NS, "home"))


def _fsm(container: Container) -> FSM:
    return FSM(container.redis, bot="gojo_postedit")


# ── Data helpers ─────────────────────────────────────────────────────────────

async def _main_posts(container: Container) -> list[tuple[str, str]]:
    """Published main-channel posts as ``(anime_doc_id, title)``, title-sorted.

    Only rows that actually have a live main message are listed (a bare
    ``ChannelPost`` with no ``main_message_id`` has nothing to edit)."""
    async with session_scope(container.pg_sessionmaker) as session:
        rows = (await session.execute(
            select(ChannelPost).where(ChannelPost.main_message_id.is_not(None))
        )).scalars().all()
        docs = [r.anime_doc_id for r in rows]
        titles: dict[str, str] = {}
        if docs:
            trows = (await session.execute(
                select(distinct(StoragePack.anime_doc_id), StoragePack.anime_title)
                .where(StoragePack.anime_doc_id.in_(docs))
            )).all()
            for doc, title in trows:
                if doc and doc not in titles and title:
                    titles[doc] = title
    out = [(d, titles.get(d, d)) for d in docs]
    out.sort(key=lambda t: (t[1] or "").lower())
    return out


async def _main_target(container: Container, anime_doc_id: str) -> tuple[int, int] | None:
    """``(chat_id, message_id)`` of a franchise's live main post, or None."""
    async with session_scope(container.pg_sessionmaker) as session:
        post = (await session.execute(
            select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
        )).scalar_one_or_none()
    if post is None or not post.main_message_id:
        return None
    chat_id = post.main_channel_id or container.config.main_channel.channel_id
    return int(chat_id), int(post.main_message_id)


async def _title_for(container: Container, anime_doc_id: str) -> str:
    async with session_scope(container.pg_sessionmaker) as session:
        row = (await session.execute(
            select(StoragePack.anime_title)
            .where(StoragePack.anime_doc_id == anime_doc_id).limit(1)
        )).first()
    return (row[0] if row else None) or anime_doc_id


async def _index_slots(container: Container) -> list[dict]:
    """Every editable index slot (has a message id), in ``sort_order``."""
    slots = await IndexChannelService(container).list_slots()
    return [s for s in slots if s.get("message_id")]


def _slot_label(slot: dict) -> str:
    """A short grid label for one index slot."""
    if slot["kind"] == "letter":
        return str(slot["label"] or slot["base_letter"] or "?")
    if slot["kind"] == "repurposed":
        return f"⟳{slot['order']}"
    return f"·{slot['order']}"  # reserved


def _parse_button_lines(raw: str) -> list[tuple[str, str]]:
    """Parse ``Label | https://url`` lines into ``[(label, url), …]``.

    One button per non-empty line; a line without a ``|`` or a usable URL is
    skipped. Mirrors the grammar the operator already knows from the old index
    editor and Senku's link editor."""
    out: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        label, url = line.split("|", 1)
        label, url = label.strip(), url.strip()
        if label and url.startswith(("http://", "https://", "tg://")):
            out.append((label, url))
    return out


def _markup_from_lines(rows: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    """One URL button per row (main-channel post buttons are simple links)."""
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, url=url)] for label, url in rows]
    )


# ── Screens ──────────────────────────────────────────────────────────────────

def _screen(caption: str, buttons=None, kb: InlineKeyboardMarkup | None = None) -> Screen:
    base = card(caption, bot_name=_BOT, buttons=buttons)
    if kb is not None:
        return Screen(caption=base.caption, image=base.image, keyboard=kb)
    return base


async def open_editor_home(
    client: Client, container: Container, target: Message | CallbackQuery,
) -> None:
    """The top-level surface picker: Main channel vs Index channel."""
    chat_id = (target.message.chat.id if isinstance(target, CallbackQuery)
               else target.chat.id)
    old = target.message if isinstance(target, CallbackQuery) else None
    caption = (
        "<b>📝 Edit a published post</b>\n\n"
        "Pick a surface to edit. Every change is written to the live post "
        "<i>and</i> the disaster-recovery backup, so a later restore keeps your edit.\n\n"
        "• <b>Main channel</b> — a franchise post's caption, buttons, image, or "
        "thumbnail values.\n"
        "• <b>Index channel</b> — a letter section's text, image, or buttons."
    )
    rows = [
        [("🖼 Main channel", cb(_BOT, _NS, "ml", "0")),
         ("🔤 Index channel", cb(_BOT, _NS, "il", "0"))],
        [_BTN_CANCEL],
    ]
    await send_screen(client, chat_id, _screen(caption, buttons=rows), old_msg=old)


async def _show_main_list(
    client: Client, container: Container, q: CallbackQuery, page: int,
) -> None:
    posts = await _main_posts(container)
    if not posts:
        await send_screen(
            client, q.message.chat.id,
            _screen("No published main-channel posts to edit yet.",
                    buttons=[[_BTN_HOME]]),
            old_msg=q.message)
        return
    items = [(title[:48], cb(_BOT, _NS, "mp", doc)) for doc, title in posts]
    kb = paginate(items, page=page, nav_action=cb(_BOT, _NS, "ml"),
                  page_size=_MAIN_PAGE, columns=1)
    kb.inline_keyboard.append([InlineKeyboardButton(_BTN_HOME[0],
                                                    callback_data=_BTN_HOME[1])])
    await send_screen(
        client, q.message.chat.id,
        _screen("<b>🖼 Main channel — pick a post</b>", kb=kb),
        old_msg=q.message)


async def _show_main_menu(
    client: Client, container: Container, q: CallbackQuery, anime_doc_id: str,
) -> None:
    """Per-post action menu, with the current live caption as a preview."""
    target = await _main_target(container, anime_doc_id)
    title = await _title_for(container, anime_doc_id)
    if target is None:
        await send_screen(
            client, q.message.chat.id,
            _screen(f"<b>{title}</b>\n\nThat post is no longer live to edit.",
                    buttons=[[("◀ Back", cb(_BOT, _NS, "ml", "0"))]]),
            old_msg=q.message)
        return
    chat_id, mid = target
    preview = ""
    try:
        live = await container.admin_client.get_messages(chat_id, mid)
        src = (getattr(live, "caption", None)
               if getattr(live, "caption", None) is not None
               else getattr(live, "text", None))
        preview = (str(src) if src else "")[:400]
    except Exception as exc:  # noqa: BLE001 — preview is best-effort
        log.debug("gojo.postedit.main_preview_failed", doc=anime_doc_id, error=str(exc))
    caption = f"<b>🖼 {title}</b>\n\n"
    if preview:
        caption += f"<blockquote>{_esc(preview)}</blockquote>\n\n"
    caption += "What do you want to change?"
    rows = [
        [("✏️ Caption", cb(_BOT, _NS, "mc", anime_doc_id)),
         ("🔘 Buttons", cb(_BOT, _NS, "mb", anime_doc_id))],
        [("🖼 Replace image", cb(_BOT, _NS, "mi", anime_doc_id)),
         ("🎨 Thumbnail", cb(_BOT, _NS, "mt", anime_doc_id))],
        [("◀ Back", cb(_BOT, _NS, "ml", "0"))],
    ]
    await send_screen(client, q.message.chat.id, _screen(caption, buttons=rows),
                      old_msg=q.message)


async def _show_thumb_grid(
    client: Client, container: Container, q: CallbackQuery, anime_doc_id: str,
) -> None:
    """The thumbnail value-edit grid for the MAIN card (anilist_id == -1)."""
    title = await _title_for(container, anime_doc_id)
    fields = await _entry_fields(container, anime_doc_id, _MAIN_ANILIST)
    if fields is None:
        await send_screen(
            client, q.message.chat.id,
            _screen(f"<b>🎨 {title}</b>\n\nThis post has no saved thumbnail card "
                    "to edit. Regenerate it from Senku's <code>/edit_thumbnail</code> first.",
                    buttons=[[("◀ Back", cb(_BOT, _NS, "mp", anime_doc_id))]]),
            old_msg=q.message)
        return
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for i, fkey in enumerate(_EDITABLE):
        pair.append((_FIELD_LABELS[fkey], cb(_BOT, _NS, "mf", anime_doc_id, str(i))))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("◀ Back", cb(_BOT, _NS, "mp", anime_doc_id))])
    await send_screen(
        client, q.message.chat.id,
        _screen(f"<b>🎨 {title} — thumbnail</b>\n\nPick a value to change. The card "
                "re-renders and the live post updates in place.", buttons=rows),
        old_msg=q.message)


async def _show_index_grid(
    client: Client, container: Container, q: CallbackQuery, page: int,
) -> None:
    slots = await _index_slots(container)
    if not slots:
        await send_screen(
            client, q.message.chat.id,
            _screen("The index channel has no editable sections.",
                    buttons=[[_BTN_HOME]]),
            old_msg=q.message)
        return
    items = [(_slot_label(s), cb(_BOT, _NS, "ip", str(s["order"]))) for s in slots]
    kb = paginate(items, page=page, nav_action=cb(_BOT, _NS, "il"),
                  page_size=_IDX_PAGE, columns=3)
    kb.inline_keyboard.append([InlineKeyboardButton(_BTN_HOME[0],
                                                    callback_data=_BTN_HOME[1])])
    await send_screen(
        client, q.message.chat.id,
        _screen("<b>🔤 Index channel — pick a section</b>\n\n"
                "<i>·N = reserved slot · ⟳N = repurposed</i>", kb=kb),
        old_msg=q.message)


async def _show_index_menu(
    client: Client, container: Container, q: CallbackQuery, order: int,
) -> None:
    slots = await _index_slots(container)
    slot = next((s for s in slots if int(s["order"]) == order), None)
    if slot is None:
        await send_screen(
            client, q.message.chat.id,
            _screen("That section is gone.",
                    buttons=[[("◀ Back", cb(_BOT, _NS, "il", "0"))]]),
            old_msg=q.message)
        return
    name = _slot_label(slot)
    caption = f"<b>🔤 Section {name}</b>\n\nWhat do you want to change?"
    if slot["kind"] == "letter":
        caption += ("\n\n<blockquote>⚠️ This is an auto-managed letter section — a "
                    "later auto-refresh can overwrite a hand-edited caption.</blockquote>")
    rows = [
        [("✏️ Text", cb(_BOT, _NS, "it", str(order))),
         ("🖼 Image", cb(_BOT, _NS, "ii", str(order)))],
        [("🔘 Buttons", cb(_BOT, _NS, "ib", str(order)))],
        [("◀ Back", cb(_BOT, _NS, "il", "0"))],
    ]
    await send_screen(client, q.message.chat.id, _screen(caption, buttons=rows),
                      old_msg=q.message)


def _esc(text: str) -> str:
    import html
    return html.escape(text)


# ── Prompt + capture ─────────────────────────────────────────────────────────

async def _arm_prompt(
    client: Client, container: Container, q: CallbackQuery, *,
    state: str, caption: str, back_cb: str, **data,
) -> None:
    """Render a capture prompt IN PLACE and arm the FSM to consume the reply."""
    if q.message.chat.type != ChatType.PRIVATE:
        await q.answer("Open Gojo in a private chat to edit posts.", show_alert=True)
        return
    if container.redis is None:
        await q.answer("Editing is temporarily unavailable.", show_alert=True)
        return
    await q.answer()
    prompt = await send_screen(
        client, q.message.chat.id,
        _screen(caption, buttons=[[("◀ Back", back_cb)], [_BTN_CANCEL]]),
        old_msg=q.message)
    await _fsm(container).set(
        q.from_user.id, state,
        prompt_msg_id=getattr(prompt, "id", None),
        prompt_chat_id=q.message.chat.id, **data,
    )


async def _result(
    client: Client, container: Container, message: Message, data: dict,
    ok: bool, text: str, *, back_cb: str,
) -> None:
    """Flip the prompt card in place to a result + a Back button; drop the reply."""
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 — cosmetic
        pass
    ref = message_ref(client, data.get("prompt_chat_id") or message.chat.id,
                      data.get("prompt_msg_id"))
    body = ("✅ " if ok else "⚠️ ") + text
    try:
        await send_screen(client, message.chat.id,
                          _screen(body, buttons=[[("◀ Back", back_cb)], [_BTN_HOME]]),
                          old_msg=ref)
    except Exception:  # noqa: BLE001
        await message.reply_text(body)


# ── Apply (main) ─────────────────────────────────────────────────────────────

async def _apply_main_caption(container: Container, doc: str, caption_html: str) -> tuple[bool, str]:
    target = await _main_target(container, doc)
    if target is None:
        return False, "That post is no longer live to edit."
    chat_id, mid = target
    ok, msg = await post_edit_ops.live_edit_caption(
        container.admin_client, chat_id, mid, caption_html,
        text_limit=_TEXT_LIMIT, log_event_prefix="gojo.main.caption")
    if not ok:
        return False, msg
    # Persist to the wipe-proof backup so a restore ships the edited caption.
    await BackupService(container).update_main_caption(doc, caption_html)
    return True, "Caption updated on the main post and the backup."


async def _apply_main_buttons(container: Container, doc: str, lines: str) -> tuple[bool, str]:
    target = await _main_target(container, doc)
    if target is None:
        return False, "That post is no longer live to edit."
    chat_id, mid = target
    parsed = _parse_button_lines(lines)
    markup = _markup_from_lines(parsed)
    ok, msg = await post_edit_ops.live_edit_buttons(
        container.admin_client, chat_id, mid, markup,
        log_event_prefix="gojo.main.buttons")
    if not ok:
        return False, msg
    await BackupService(container).update_main_buttons(doc, markup)
    n = len(parsed)
    return True, (f"Set {n} button{'s' if n != 1 else ''} on the main post and the backup."
                  if n else "Cleared the main post's buttons.")


async def _apply_main_image(container: Container, doc: str, image_path: str) -> tuple[bool, str]:
    # refresh_thumbnail edits media in place (keyboard + .html caption safe) and
    # refreshes the durable backup image — the whole image swap in one call.
    ok = await MainChannelService(container).refresh_thumbnail(doc, image_path)
    if not ok:
        return False, "Telegram rejected the image swap; the post is unchanged."
    return True, "Image replaced on the main post and the backup."


async def _apply_main_field(container: Container, doc: str, field: str, value: str) -> tuple[bool, str]:
    # Senku's shared value-edit core: render → persist ThumbnailSource → refresh
    # the live main post (anilist_id -1 routes to MainChannelService).
    ok, msg, trimmed = await _apply_field(container, doc, _MAIN_ANILIST, field, value)
    if trimmed:
        msg += " (synopsis trimmed to fit the card)."
    return ok, msg


# ── Apply (index) ────────────────────────────────────────────────────────────

async def _apply_index_text(container: Container, order: int, html_cap: str) -> tuple[bool, str]:
    ok = await IndexChannelService(container).edit_slot_caption(order, html_cap)
    if ok:
        await BackupService(container).record_index()
    return ok, ("Section text updated and the index backup refreshed." if ok
                else "Telegram rejected the text edit; the section is unchanged.")


async def _apply_index_image(container: Container, order: int, image: str) -> tuple[bool, str]:
    ok = await IndexChannelService(container).replace_slot_image(order, image)
    if ok:
        await BackupService(container).record_index()
    return ok, ("Section image replaced and the index backup refreshed." if ok
                else "Telegram rejected the image swap; the section is unchanged.")


async def _apply_index_buttons(container: Container, order: int, lines: str) -> tuple[bool, str]:
    parsed = _parse_button_lines(lines)
    ok = await IndexChannelService(container).set_slot_buttons(order, parsed)
    if ok:
        await BackupService(container).record_index()
    n = len(parsed)
    return ok, ((f"Set {n} button{'s' if n != 1 else ''} and refreshed the index backup."
                 if n else "Cleared the section's buttons and refreshed the backup.")
                if ok else "Telegram rejected the button edit; the section is unchanged.")


# ── Registration ─────────────────────────────────────────────────────────────

def register(client: Client, container: Container) -> None:
    """Register ``/editpost`` + the ``gojo|pe|…`` editor callbacks and consumers."""

    @client.on_message(filters.command("editpost") & filters.private)
    async def _command(_: Client, message: Message) -> None:
        if not is_owner(container, message):
            return
        await _fsm(container).clear(message.from_user.id)
        await open_editor_home(client, container, message)

    @client.on_callback_query(filters.regex(r"^gojo\|pe(\||$)"))
    async def _callback(_: Client, q: CallbackQuery) -> None:
        if q.message is None or not is_owner(container, q):
            await q.answer("Owner only.", show_alert=True)
            return
        try:
            await _dispatch(q)
        except (IndexError, ValueError):
            await q.answer("That button expired — reopen /editpost.", show_alert=True)

    async def _dispatch(q: CallbackQuery) -> None:
        parts = (q.data or "").split("|")
        action = parts[2] if len(parts) > 2 else "home"

        if action == "x":
            if container.redis is not None:
                await _fsm(container).clear(q.from_user.id)
            await q.answer("Cancelled.")
            try:
                await q.message.delete()
            except Exception:  # noqa: BLE001 — cosmetic
                pass
            return

        if action == "home":
            await q.answer()
            await open_editor_home(client, container, q)
            return

        # ── Main channel ──
        if action == "ml":
            await q.answer()
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            await _show_main_list(client, container, q, page)
            return
        if action == "mp":
            await q.answer()
            await _show_main_menu(client, container, q, parts[3])
            return
        if action == "mt":
            await q.answer()
            await _show_thumb_grid(client, container, q, parts[3])
            return
        if action == "mc":
            await _arm_prompt(
                client, container, q, state=_ST_MCAP, doc=parts[3],
                back_cb=cb(_BOT, _NS, "mp", parts[3]),
                caption=("<b>✏️ New caption</b>\n\nSend the full caption. HTML, "
                         "Telegram styling, or Markdown all work; the post's "
                         "buttons are kept."))
            return
        if action == "mb":
            await _arm_prompt(
                client, container, q, state=_ST_MBTN, doc=parts[3],
                back_cb=cb(_BOT, _NS, "mp", parts[3]),
                caption=("<b>🔘 New buttons</b>\n\nSend one button per line as "
                         "<code>Label | https://url</code>. Send a single "
                         "<code>-</code> to clear all buttons."))
            return
        if action == "mi":
            await _arm_prompt(
                client, container, q, state=_ST_MIMG, doc=parts[3],
                back_cb=cb(_BOT, _NS, "mp", parts[3]),
                caption=("<b>🖼 Replace image</b>\n\nUpload the new image as a "
                         "photo or an image file. The caption and buttons are kept."))
            return
        if action == "mf":
            idx = int(parts[4])
            if not 0 <= idx < len(_EDITABLE):
                await q.answer("Unknown field.", show_alert=True)
                return
            field = _EDITABLE[idx]
            await _arm_prompt(
                client, container, q, state=_ST_MFIELD, doc=parts[3], field=field,
                back_cb=cb(_BOT, _NS, "mt", parts[3]),
                caption=(f"<b>🎨 {_FIELD_LABELS[field]}</b>\n\nSend the new value. "
                         "The card re-renders and the live post updates."))
            return

        # ── Index channel ──
        if action == "il":
            await q.answer()
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            await _show_index_grid(client, container, q, page)
            return
        if action == "ip":
            await q.answer()
            await _show_index_menu(client, container, q, int(parts[3]))
            return
        if action == "it":
            await _arm_prompt(
                client, container, q, state=_ST_ITEXT, order=int(parts[3]),
                back_cb=cb(_BOT, _NS, "ip", parts[3]),
                caption=("<b>✏️ Section text</b>\n\nSend the new caption (HTML / "
                         "Telegram styling / Markdown). The letter buttons are kept."))
            return
        if action == "ii":
            await _arm_prompt(
                client, container, q, state=_ST_IIMG, order=int(parts[3]),
                back_cb=cb(_BOT, _NS, "ip", parts[3]),
                caption=("<b>🖼 Section image</b>\n\nUpload the new image as a photo "
                         "or an image file."))
            return
        if action == "ib":
            await _arm_prompt(
                client, container, q, state=_ST_IBTN, order=int(parts[3]),
                back_cb=cb(_BOT, _NS, "ip", parts[3]),
                caption=("<b>🔘 Section buttons</b>\n\nSend one per line as "
                         "<code>Label | https://url</code>. Send <code>-</code> to clear."))
            return

        await q.answer("Unknown action.", show_alert=True)

    # ── Text capture (group 14 — must dodge tasks.py::_fsm_text at group 0) ──
    @client.on_message(
        filters.text & filters.private & ~filters.command(["start", "editpost"]),
        group=14,
    )
    async def _consume_text(_: Client, message: Message) -> None:
        if (container.redis is None or not message.from_user
                or not is_owner(container, message)):
            return
        fsm = _fsm(container)
        state, data = await fsm.get(message.from_user.id)
        if state not in (_ST_MCAP, _ST_MBTN, _ST_MFIELD, _ST_ITEXT, _ST_IBTN):
            return
        await fsm.clear(message.from_user.id)

        if state == _ST_MCAP:
            html_cap = parse_user_markup(message)
            ok, text = await _apply_main_caption(container, str(data["doc"]), html_cap)
            await _result(client, container, message, data, ok, text,
                          back_cb=cb(_BOT, _NS, "mp", str(data["doc"])))
        elif state == _ST_MBTN:
            raw = (message.text or "").strip()
            lines = "" if raw == "-" else raw
            ok, text = await _apply_main_buttons(container, str(data["doc"]), lines)
            await _result(client, container, message, data, ok, text,
                          back_cb=cb(_BOT, _NS, "mp", str(data["doc"])))
        elif state == _ST_MFIELD:
            value = (message.text or "").strip()
            if not value:
                await message.reply_text("The value can't be empty. Reopen the field.")
                return
            ok, text = await _apply_main_field(
                container, str(data["doc"]), str(data["field"]), value)
            await _result(client, container, message, data, ok, text,
                          back_cb=cb(_BOT, _NS, "mt", str(data["doc"])))
        elif state == _ST_ITEXT:
            html_cap = parse_user_markup(message)
            order = int(data["order"])
            ok, text = await _apply_index_text(container, order, html_cap)
            await _result(client, container, message, data, ok, text,
                          back_cb=cb(_BOT, _NS, "ip", str(order)))
        elif state == _ST_IBTN:
            raw = (message.text or "").strip()
            lines = "" if raw == "-" else raw
            order = int(data["order"])
            ok, text = await _apply_index_buttons(container, order, lines)
            await _result(client, container, message, data, ok, text,
                          back_cb=cb(_BOT, _NS, "ip", str(order)))

    # ── Photo/document capture (group 16) ──
    @client.on_message((filters.photo | filters.document) & filters.private, group=16)
    async def _consume_image(_: Client, message: Message) -> None:
        if (container.redis is None or not message.from_user
                or not is_owner(container, message)):
            return
        fsm = _fsm(container)
        state, data = await fsm.get(message.from_user.id)
        if state not in (_ST_MIMG, _ST_IIMG):
            return
        # A document must actually be an image; ignore stray files.
        if message.document is not None and not (
                (message.document.mime_type or "").startswith("image/")):
            await message.reply_text("That file isn't an image. Send a photo or image file.")
            return
        await fsm.clear(message.from_user.id)

        if state == _ST_MIMG:
            # Main image swap goes through refresh_thumbnail, which needs a PATH.
            try:
                path = await message.download()
            except Exception as exc:  # noqa: BLE001
                log.warning("gojo.postedit.main_image_download_failed", error=str(exc))
                await message.reply_text("I couldn't read that image. Try again.")
                return
            ok, text = await _apply_main_image(container, str(data["doc"]), str(path))
            await _result(client, container, message, data, ok, text,
                          back_cb=cb(_BOT, _NS, "mp", str(data["doc"])))
        else:  # _ST_IIMG — the index primitive accepts a file_id directly.
            file_id = (message.photo.file_id if message.photo
                       else message.document.file_id)
            order = int(data["order"])
            ok, text = await _apply_index_image(container, order, file_id)
            await _result(client, container, message, data, ok, text,
                          back_cb=cb(_BOT, _NS, "ip", str(order)))
