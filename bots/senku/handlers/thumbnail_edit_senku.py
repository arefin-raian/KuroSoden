"""Senku-native thumbnail editor — ``/edit_thumbnail`` in Senku's own voice.

Replaces the plain-text admin editor (``nekofetch/bots/admin/handlers/
thumbnail_edit.py``, still used by the admin bot) for the Senku surface. It:

  * lists EVERY stored franchise by name (from ``StoragePack``, so anime
    uploaded before thumbnails were saved still appear), 10 per page in the
    recurring artwork card;
  * routes a single-entry franchise straight to its edit page, and a
    multi-entry one to a main-channel-post vs distribution-entries choice
    (distribution offers a "provide link" jump);
  * edits every text value on the card — including rating and genres — and
    re-renders + refreshes the live surface;
  * regenerates a card that was never saved, reusing the wizard's asset-picker
    SERVICES (galleries / upload / text-logo / render) under this editor's own
    callback namespace + FSM states, so it never touches the live wizard's
    publish/handoff path.

Everything is keyed by ``anime_doc_id`` (the franchise) + ``anilist_id`` (an
entry; ``-1`` = the main/root card), matching ``ThumbnailSource``.
"""

from __future__ import annotations

import html

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import CallbackQuery, Message
from sqlalchemy import distinct, select

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import (
    ChannelLayout,
    DistributionBot,
    StoragePack,
    ThumbnailSource,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.services.thumbnail_service import (
    ThumbnailRenderService,
    _truncate,
    _SYNOPSIS_MAX_CHARS,
    gather_thumbnail_fields,
    persist_thumbnail_source,
    render_fields,
)
from nekofetch.ui.components import cb, paginate
from nekofetch.ui.screens import Screen, card, send_screen
from kurosoden.shared import senku_voice as V
from kurosoden.shared.access_gate import is_staff

log = get_logger(__name__)

_BOT = "senku"
_PAGE_SIZE = 10          # 2 columns × 5 rows
_MAIN_ANILIST = -1       # ThumbnailSource sentinel for the main/root card

# FSM states (per-user, isolated from the wizard's STATE_* namespace).
_ST_FIELD = "senku_editthumb_field"     # awaiting a new field value
_ST_LINK = "senku_editthumb_link"       # awaiting a distribution post link
_ST_REGEN = "senku_editthumb_regen"     # regenerate: awaiting logo-text / upload

# Human labels for the editable fields (also the edit-page button captions).
_FIELD_LABELS: dict[str, str] = {
    "title": "Title",
    "native_title": "Native title",
    "romaji_title": "Romaji title",
    "synopsis": "Synopsis",
    "meta_label": "Meta line",
    "language": "Language",
    "studio": "Studio",
    "tmdb_rating": "TMDB rating",
    "anilist_score": "AniList score",
    "genres": "Genres",
    "logo_url": "Logo URL",
    "poster_url": "Poster URL",
    "bg_url": "Backdrop URL",
}
_EDITABLE = tuple(_FIELD_LABELS)


def _fsm(container: Container) -> FSM:
    # A DISTINCT bot namespace from the wizard's ``FSM(redis, "senku")`` so an
    # edit/regenerate capture can never clobber (or be clobbered by) a live
    # distribution wizard the same admin has open.
    return FSM(container.redis, bot="senku_editthumb")


# ── Data helpers ─────────────────────────────────────────────────────────────

async def _franchises(container: Container) -> list[tuple[str, str]]:
    """Every stored franchise as ``(anime_doc_id, anime_title)``, title-sorted.

    Sourced from ``StoragePack`` so anime uploaded before thumbnails were saved
    still appear (they have packs even without a ``ThumbnailSource`` row).
    """
    async with session_scope(container.pg_sessionmaker) as session:
        rows = (await session.execute(
            select(distinct(StoragePack.anime_doc_id), StoragePack.anime_title)
            .order_by(StoragePack.anime_title)
        )).all()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for doc_id, title in rows:
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        out.append((doc_id, title or doc_id))
    return out


async def _saved_entries(container: Container, anime_doc_id: str) -> list[ThumbnailSource]:
    """Saved ``ThumbnailSource`` rows for a franchise (may be empty)."""
    async with session_scope(container.pg_sessionmaker) as session:
        rows = list((await session.execute(
            select(ThumbnailSource)
            .where(ThumbnailSource.anime_doc_id == anime_doc_id)
            .order_by(ThumbnailSource.anilist_id)
        )).scalars().all())
        for row in rows:
            session.expunge(row)
        return rows


async def _distribution_entries(
    container: Container, anime_doc_id: str,
) -> list[tuple[int, str]]:
    """Distribution entry cards as ``(anilist_id, label)`` from the channel layout.

    Uses the live ``ChannelLayout`` season/movie cards so the list matches what's
    actually published, independent of whether a ThumbnailSource exists yet.
    """
    async with session_scope(container.pg_sessionmaker) as session:
        bot = (await session.execute(
            select(DistributionBot).where(
                DistributionBot.anime_doc_id == anime_doc_id,
                DistributionBot.is_channel.is_(True),
            ).order_by(DistributionBot.id.desc())
        )).scalars().first()
        if bot is None:
            return []
        cards = (await session.execute(
            select(ChannelLayout).where(
                ChannelLayout.channel_bot_id == bot.id,
                ChannelLayout.kind.in_(("season_card", "movie_card")),
                ChannelLayout.anilist_id.is_not(None),
            ).order_by(ChannelLayout.seq)
        )).scalars().all()
    out: list[tuple[int, str]] = []
    for c in cards:
        if c.anilist_id is None:
            continue
        out.append((int(c.anilist_id), _kind_label(c.kind, c.seq)))
    return out


def _kind_label(kind: str, seq: int) -> str:
    base = {"season_card": "Season", "movie_card": "Movie"}.get(kind, "Entry")
    return f"{base} · card {seq}"


async def _entry_fields(
    container: Container, anime_doc_id: str, anilist_id: int,
) -> dict | None:
    """The saved ``fields`` dict for one entry, or ``None`` when not saved."""
    async with session_scope(container.pg_sessionmaker) as session:
        row = (await session.execute(
            select(ThumbnailSource).where(
                ThumbnailSource.anime_doc_id == anime_doc_id,
                ThumbnailSource.anilist_id == anilist_id,
            )
        )).scalars().first()
        return dict(row.fields or {}) if row is not None else None


# ── Screens ──────────────────────────────────────────────────────────────────

def _paged_card(caption: str, kb):
    """A recurring-artwork card whose keyboard is a prebuilt (paginated) markup."""
    base = card(caption, bot_name=_BOT)
    return Screen(caption=base.caption, image=base.image, keyboard=kb)


async def _show_franchises(
    client: Client, container: Container, target: Message | CallbackQuery,
    page: int = 0, *, old_msg: Message | None = None,
) -> None:
    chat_id = (target.message.chat.id if isinstance(target, CallbackQuery)
               else target.chat.id)
    franchises = await _franchises(container)
    if not franchises:
        await send_screen(
            client, chat_id,
            card(V.editthumb_empty(), bot_name=_BOT), old_msg=old_msg,
        )
        return
    items = [
        (title[:40], cb(_BOT, "thumbedit", "fr", doc_id))
        for doc_id, title in franchises
    ]
    kb = paginate(items, page=page, nav_action=cb(_BOT, "thumbedit", "page"),
                  page_size=_PAGE_SIZE, columns=2)
    screen = _paged_card(V.editthumb_intro(), kb)
    await send_screen(client, chat_id, screen, old_msg=old_msg)


async def _open_franchise(
    client: Client, container: Container, q: CallbackQuery, anime_doc_id: str,
) -> None:
    """Route a franchise: single-entry → edit page; multi → surface choice."""
    dist = await _distribution_entries(container, anime_doc_id)
    saved = await _saved_entries(container, anime_doc_id)
    title = await _title_for(container, anime_doc_id)

    # The season/extra count is what decides single-vs-multi (per the spec:
    # Takopi = one season → straight to edit; AoT = many entries → choose).
    # More than one distribution entry ⇒ show the main-vs-distribution choice.
    if len(dist) > 1:
        rows = [
            [(V.BTN_EDITTHUMB_MAIN, cb(_BOT, "thumbedit", "main", anime_doc_id))],
            [(V.BTN_EDITTHUMB_DIST, cb(_BOT, "thumbedit", "dist", anime_doc_id, "0"))],
            [(V.BTN_EDITTHUMB_BACK, cb(_BOT, "thumbedit", "page", "0"))],
        ]
        await send_screen(
            client, q.message.chat.id,
            card(V.editthumb_choose_surface(title), bot_name=_BOT, buttons=rows),
            old_msg=q.message,
        )
        return

    # Single entry → straight to its edit page. Prefer a saved main/root card
    # (-1) — every published anime has one main-channel post, and for a lone
    # season it carries the franchise-level synopsis/rating the admin edits;
    # else the single distribution entry; else the main sentinel (unsaved →
    # the edit page shows the regenerate gate).
    saved_ids = {int(s.anilist_id) for s in saved}
    if _MAIN_ANILIST in saved_ids or not dist:
        anilist_id = _MAIN_ANILIST
    else:
        anilist_id = int(dist[0][0])
    await _open_edit_page(client, container, q, anime_doc_id, anilist_id, title)


async def _show_distribution_entries(
    client: Client, container: Container, q: CallbackQuery,
    anime_doc_id: str, page: int,
) -> None:
    entries = await _distribution_entries(container, anime_doc_id)
    title = await _title_for(container, anime_doc_id)
    items = [
        (label[:40], cb(_BOT, "thumbedit", "entry", anime_doc_id, str(aid)))
        for aid, label in entries
    ]
    # Provide-link + Back live below the (optional) pagination nav row.
    kb = paginate(items, page=page,
                  nav_action=cb(_BOT, "thumbedit", "dist", anime_doc_id),
                  page_size=_PAGE_SIZE, columns=2)
    from pyrogram.types import InlineKeyboardButton
    kb.inline_keyboard.append([InlineKeyboardButton(
        V.BTN_EDITTHUMB_PROVIDE_LINK,
        callback_data=cb(_BOT, "thumbedit", "link", anime_doc_id))])
    kb.inline_keyboard.append([InlineKeyboardButton(
        V.BTN_EDITTHUMB_BACK,
        callback_data=cb(_BOT, "thumbedit", "fr", anime_doc_id))])
    await send_screen(
        client, q.message.chat.id,
        _paged_card(V.editthumb_entry_list(title), kb),
        old_msg=q.message,
    )


async def _open_edit_page(
    client: Client, container: Container, q: CallbackQuery,
    anime_doc_id: str, anilist_id: int, title: str,
) -> None:
    """Show the field-edit menu, or the 'regenerate first' gate when unsaved."""
    fields = await _entry_fields(container, anime_doc_id, anilist_id)
    label = title if anilist_id == _MAIN_ANILIST else f"{title} · entry {anilist_id}"
    if fields is None:
        # Not saved — must regenerate before values are editable.
        rows = [
            [(V.BTN_EDITTHUMB_REGEN,
              cb(_BOT, "thumbedit", "regen", anime_doc_id, str(anilist_id)))],
            [(V.BTN_EDITTHUMB_BACK, cb(_BOT, "thumbedit", "fr", anime_doc_id))],
        ]
        await send_screen(
            client, q.message.chat.id,
            card(V.editthumb_not_saved(label), bot_name=_BOT, buttons=rows),
            old_msg=q.message,
        )
        return
    rows = []
    pair: list[tuple[str, str]] = []
    for fkey in _EDITABLE:
        pair.append((_FIELD_LABELS[fkey],
                     cb(_BOT, "thumbedit", "field", anime_doc_id, str(anilist_id), fkey)))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([(V.BTN_EDITTHUMB_BACK, cb(_BOT, "thumbedit", "fr", anime_doc_id))])
    await send_screen(
        client, q.message.chat.id,
        card(V.editthumb_edit_menu(label, fields), bot_name=_BOT, buttons=rows),
        old_msg=q.message,
    )


async def _title_for(container: Container, anime_doc_id: str) -> str:
    async with session_scope(container.pg_sessionmaker) as session:
        row = (await session.execute(
            select(StoragePack.anime_title)
            .where(StoragePack.anime_doc_id == anime_doc_id).limit(1)
        )).first()
    return (row[0] if row else None) or anime_doc_id


# ── Apply an edit (render + persist + refresh live) ──────────────────────────

async def _apply_field(
    container: Container, anime_doc_id: str, anilist_id: int,
    field: str, value: str,
) -> tuple[bool, str, str | None]:
    """Update one field, re-render, persist, refresh live. Returns (ok, msg, trimmed)."""
    fields = await _entry_fields(container, anime_doc_id, anilist_id)
    if fields is None:
        return False, "That thumbnail is no longer saved; regenerate it first.", None

    trimmed_note: str | None = None
    if field == "genres":
        fields["genres"] = [g.strip() for g in value.split(",") if g.strip()]
    elif field == "synopsis":
        fitted = _truncate(value, _SYNOPSIS_MAX_CHARS)
        fields["synopsis"] = value  # store full; render truncates too
        if fitted != value:
            trimmed_note = fitted
    else:
        fields[field] = value

    renderer = ThumbnailRenderService()
    try:
        image_path = await renderer.render_thumbnail(**render_fields(fields))
    except Exception as exc:  # noqa: BLE001
        log.warning("editthumb.render_failed", doc=anime_doc_id, aid=anilist_id,
                    error=str(exc))
        return False, "I couldn't render that change; the saved card is unchanged.", None
    finally:
        await renderer.close()
    if not image_path:
        return False, "The renderer returned no image; nothing was changed.", None

    await persist_thumbnail_source(
        container, anime_doc_id,
        None if anilist_id == _MAIN_ANILIST else anilist_id,
        fields, image_path=image_path,
    )
    live = await _refresh_live(container, anime_doc_id, anilist_id, str(image_path))
    msg = f"Updated the {live}." if live else (
        "Saved the new value, but no live post was found to refresh.")
    return True, msg, trimmed_note


async def _refresh_live(
    container: Container, anime_doc_id: str, anilist_id: int, image_path: str,
) -> str | None:
    """Push the new image to the live surface. Returns a human label or None."""
    if anilist_id == _MAIN_ANILIST:
        from nekofetch.services.main_channel_service import MainChannelService
        if await MainChannelService(container).refresh_thumbnail(anime_doc_id, image_path):
            return "main-channel post"
        return None
    from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService
    if await ThumbnailChannelService(container).refresh_published_thumbnail(
        anime_doc_id, int(anilist_id), image_path,
    ):
        return "distribution card"
    return None


def register(client: Client, container: Container) -> None:
    """Register Senku's ``/edit_thumbnail`` command + the editor callbacks."""

    @client.on_message(filters.command("edit_thumbnail") & filters.private, group=4)
    async def _command(_: Client, message: Message) -> None:
        if not is_staff(message):
            return
        await _show_franchises(client, container, message)

    @client.on_callback_query(filters.regex(r"^senku\|thumbedit\|"), group=3)
    async def _callback(_: Client, q: CallbackQuery) -> None:
        if q.message is None or not is_staff(q):
            await q.answer("Staff access required.", show_alert=True)
            return
        try:
            await _dispatch(q)
        except (IndexError, ValueError):
            # A truncated / stale / hand-crafted callback (missing or non-numeric
            # args) must never surface as an unhandled handler error — answer
            # with a soft "expired" toast so the button just no-ops cleanly.
            await q.answer("That button has expired — reopen /edit_thumbnail.",
                           show_alert=True)

    async def _dispatch(q: CallbackQuery) -> None:
        parts = (q.data or "").split("|")
        action = parts[2] if len(parts) > 2 else ""

        if action == "page":
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            await q.answer()
            await _show_franchises(client, container, q, page, old_msg=q.message)
            return

        if action == "fr":
            await q.answer()
            await _open_franchise(client, container, q, parts[3])
            return

        if action == "main":
            await q.answer()
            title = await _title_for(container, parts[3])
            await _open_edit_page(client, container, q, parts[3], _MAIN_ANILIST, title)
            return

        if action == "dist":
            page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            await q.answer()
            await _show_distribution_entries(client, container, q, parts[3], page)
            return

        if action == "entry":
            await q.answer()
            title = await _title_for(container, parts[3])
            await _open_edit_page(client, container, q, parts[3], int(parts[4]), title)
            return

        if action == "field":
            await _prompt_field(client, container, q, parts[3], int(parts[4]), parts[5])
            return

        if action == "link":
            await _prompt_link(client, container, q, parts[3])
            return

        if action == "regen":
            await _start_regen(client, container, q, parts[3], int(parts[4]))
            return

        if action == "regcancel":
            await _fsm(container).clear(q.from_user.id)
            await q.answer("Regenerate cancelled.")
            title = await _title_for(container, parts[3])
            await _open_edit_page(client, container, q, parts[3], int(parts[4]), title)
            return

        await q.answer("Unknown action.", show_alert=True)

    async def _prompt_field(
        client_: Client, container_: Container, q: CallbackQuery,
        anime_doc_id: str, anilist_id: int, field: str,
    ) -> None:
        if field not in _EDITABLE:
            await q.answer("That field can't be edited.", show_alert=True)
            return
        if q.message.chat.type != ChatType.PRIVATE:
            await q.answer("Open Senku in a private chat to edit thumbnails.",
                           show_alert=True)
            return
        await _fsm(container_).set(
            q.from_user.id, _ST_FIELD,
            doc=anime_doc_id, aid=anilist_id, field=field,
        )
        await q.answer()
        await send_screen(
            client_, q.message.chat.id,
            card(V.editthumb_ask_value(_FIELD_LABELS[field]), bot_name=_BOT,
                 buttons=[[(V.BTN_EDITTHUMB_BACK,
                            cb(_BOT, "thumbedit", "entry", anime_doc_id, str(anilist_id)))]]),
            old_msg=q.message,
        )

    async def _prompt_link(
        client_: Client, container_: Container, q: CallbackQuery, anime_doc_id: str,
    ) -> None:
        if q.message.chat.type != ChatType.PRIVATE:
            await q.answer("Open Senku in a private chat.", show_alert=True)
            return
        await _fsm(container_).set(q.from_user.id, _ST_LINK, doc=anime_doc_id)
        await q.answer()
        title = await _title_for(container_, anime_doc_id)
        await send_screen(
            client_, q.message.chat.id,
            card(V.editthumb_ask_link(title), bot_name=_BOT,
                 buttons=[[(V.BTN_EDITTHUMB_BACK,
                            cb(_BOT, "thumbedit", "dist", anime_doc_id, "0"))]]),
            old_msg=q.message,
        )

    async def _start_regen(
        client_: Client, container_: Container, q: CallbackQuery,
        anime_doc_id: str, anilist_id: int,
    ) -> None:
        await q.answer("Starting regenerate…")
        from kurosoden.bots.senku.handlers.thumbnail_regen import begin_regenerate

        await begin_regenerate(client_, container_, q.message, q.from_user.id,
                               anime_doc_id, anilist_id)

    # ── text capture (field value OR post link) ──────────────────────────────
    @client.on_message(
        filters.text & filters.private & ~filters.command(["start", "edit_thumbnail"]),
        group=15,
    )
    async def _consume(_: Client, message: Message) -> None:
        if not message.from_user or not is_staff(message) or container.redis is None:
            return
        fsm = _fsm(container)
        state, data = await fsm.get(message.from_user.id)
        if state == _ST_FIELD:
            await _consume_field(message, fsm, data)
        elif state == _ST_LINK:
            await _consume_link(message, fsm, data)

    async def _consume_field(message: Message, fsm: FSM, data: dict) -> None:
        value = (message.text or "").strip()
        if not value:
            await message.reply_text("The new value can't be empty.")
            return
        try:
            anime_doc_id = str(data["doc"])
            anilist_id = int(data["aid"])
            field = str(data["field"])
        except (KeyError, TypeError, ValueError):
            await fsm.clear(message.from_user.id)
            return
        await fsm.clear(message.from_user.id)
        ok, result, trimmed = await _apply_field(
            container, anime_doc_id, anilist_id, field, value,
        )
        if trimmed:
            await message.reply_text(V.editthumb_synopsis_trimmed(trimmed),
                                     parse_mode=ParseMode.HTML)
        await message.reply_text(("✅ " if ok else "⚠️ ") + result)

    async def _consume_link(message: Message, fsm: FSM, data: dict) -> None:
        from kurosoden.bots.senku.handlers.post_caption_edit import (
            _parse_post_link, _resolve_editable_channel,
        )
        anime_doc_id = str(data.get("doc") or "")
        parsed = _parse_post_link(message.text or "")
        if parsed is None:
            await message.reply_text("⚠️ That's not a post link I can read.")
            return
        chat_ref, tg_message_id = parsed
        resolved = await _resolve_editable_channel(client, container, chat_ref)
        if resolved is None:
            await message.reply_text("⚠️ I can't edit posts in that channel.")
            return
        chat_id, _bot_id, _title = resolved
        # Confirm the link points at THIS franchise's channel, then map the
        # message id back to the entry's anilist id via the layout.
        async with session_scope(container.pg_sessionmaker) as session:
            bot = (await session.execute(
                select(DistributionBot).where(
                    DistributionBot.anime_doc_id == anime_doc_id,
                    DistributionBot.is_channel.is_(True),
                ).order_by(DistributionBot.id.desc())
            )).scalars().first()
            if bot is None or int(bot.chat_id or 0) != int(chat_id):
                await message.reply_text(
                    "⚠️ That post isn't in this franchise's distribution channel.")
                return
            layout = (await session.execute(
                select(ChannelLayout).where(
                    ChannelLayout.channel_bot_id == bot.id,
                    ChannelLayout.tg_message_id == tg_message_id,
                    ChannelLayout.anilist_id.is_not(None),
                ).limit(1)
            )).scalars().first()
        if layout is None or layout.anilist_id is None:
            await message.reply_text(
                "⚠️ That message isn't a tracked entry card in this channel.")
            return
        await fsm.clear(message.from_user.id)
        title = await _title_for(container, anime_doc_id)
        fields = await _entry_fields(container, anime_doc_id, int(layout.anilist_id))
        label = f"{title} · entry {layout.anilist_id}"
        # Re-open the edit page as a fresh card (no callback query to edit).
        if fields is None:
            await message.reply_text(
                f"⚠️ {label} has no saved thumbnail — open it from the list to regenerate.")
            return
        rows = []
        pair: list[tuple[str, str]] = []
        for fkey in _EDITABLE:
            pair.append((_FIELD_LABELS[fkey],
                         cb(_BOT, "thumbedit", "field", anime_doc_id,
                            str(int(layout.anilist_id)), fkey)))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([(V.BTN_EDITTHUMB_BACK, cb(_BOT, "thumbedit", "fr", anime_doc_id))])
        await send_screen(
            client, message.chat.id,
            card(V.editthumb_edit_menu(label, fields), bot_name=_BOT, buttons=rows),
        )
