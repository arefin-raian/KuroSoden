"""Isolated thumbnail regenerate — a self-contained asset picker for the editor.

When ``/edit_thumbnail`` opens an entry that was never saved (a card made before
thumbnails were stored, e.g. Takopi), the admin must regenerate it before its
values become editable. This drives a full logo → poster → backdrop pick and a
render, reusing the wizard's asset-picker SERVICES
(:class:`SenkuThumbnailAdapter`) — galleries, uploads, text-logo, render_entry —
but under this editor's OWN callback namespace (``senku|thumbedit|rg…``) and FSM
(``senku_editthumb``), seeded on a synthetic ``THUMBEDIT-<anime_doc_id>`` code.

It never touches the live wizard's closures, its ``senku|wiz|`` router, or the
publish/handoff path: when the render finishes we persist the ``ThumbnailSource``
(via ``render_entry``), push the image to the live surface, and hand back to the
editor's edit page. Nothing here builds or edits a channel.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.ui.components import cb
from nekofetch.ui.screens import Screen, card, send_screen
from kurosoden.shared import senku_voice as V
from kurosoden.shared.access_gate import is_staff
from kurosoden.shared.distribution_cache import DistributionCache, EntryData, Selection

log = get_logger(__name__)

_BOT = "senku"
_ASSETS = ("logo", "poster", "bg")  # pick order (matches the wizard)
_ASSET_WORD = {"logo": "logo", "poster": "poster", "bg": "backdrop"}
# Awaiting an uploaded image for the current asset (photo/document capture).
_ST_UPLOAD = "senku_editthumb_regen_upload"


def _fsm(container: Container) -> FSM:
    return FSM(container.redis, bot="senku_editthumb")


def _code_for(anime_doc_id: str, anilist_id: int) -> str:
    """Synthetic, per-entry cache namespace — never a real Request code.

    The anilist id is URL-ish-safe encoded (``m`` for the ``-1`` main sentinel)
    so ``_split_code`` can recover it even though ``anime_doc_id`` may itself
    contain hyphens.
    """
    tag = "m" if anilist_id < 0 else str(anilist_id)
    return f"THUMBEDIT-{anime_doc_id}#{tag}"


async def _adapter(container: Container):
    from kurosoden.shared.senku_thumbnail_adapter import SenkuThumbnailAdapter
    return SenkuThumbnailAdapter(container)


async def _seed(container: Container, anime_doc_id: str, anilist_id: int,
                title: str) -> tuple[str, EntryData]:
    """Seed a one-entry session (franchise blob + entry) under a synthetic code.

    The franchise blob MUST carry ``anime_doc_id`` or ``render_entry`` →
    ``persist_thumbnail_source`` silently writes nothing.
    """
    code = _code_for(anime_doc_id, anilist_id)
    cache = DistributionCache(container.redis)
    entry = EntryData(
        index=1, label=title, title=title,
        anilist_id=None if anilist_id < 0 else anilist_id,
    )
    await cache.set_franchise(code, {"anime_doc_id": anime_doc_id,
                                     "english": title, "title": title})
    await cache.set_entries(code, [entry])
    # The synthetic code is STABLE per (doc, entry), so a prior regenerate could
    # have left logo/poster/backdrop URLs behind. Clear the selection to a fresh
    # one — otherwise ``next_asset`` skips already-set assets and the picker
    # starts mid-way (or renders straight from stale picks).
    await cache.clear_selection(code, 1)
    return code, entry


async def begin_regenerate(
    client: Client, container: Container, message: Message, user_id: int,
    anime_doc_id: str, anilist_id: int,
) -> None:
    """Entry point from the editor's Regenerate button: seed + show first asset."""
    title = await _title_for(container, anime_doc_id)
    code, _entry = await _seed(container, anime_doc_id, anilist_id, title)
    await _fsm(container).set(
        user_id, "senku_editthumb_regen",
        doc=anime_doc_id, aid=anilist_id, code=code,
    )
    await _show_asset(client, container, message, code, anime_doc_id, anilist_id,
                      "logo", old_msg=message)


async def _title_for(container: Container, anime_doc_id: str) -> str:
    from sqlalchemy import select
    from nekofetch.infrastructure.database.postgres.models import StoragePack
    from nekofetch.infrastructure.database.postgres.session import session_scope

    async with session_scope(container.pg_sessionmaker) as session:
        row = (await session.execute(
            select(StoragePack.anime_title)
            .where(StoragePack.anime_doc_id == anime_doc_id).limit(1)
        )).first()
    return (row[0] if row else None) or anime_doc_id


async def _show_asset(
    client: Client, container: Container, target: Message, code: str,
    anime_doc_id: str, anilist_id: int, asset: str, *, old_msg: Message | None,
) -> None:
    """Render one asset-pick card with numbered buttons in OUR namespace."""
    adapter = await _adapter(container)
    cache = DistributionCache(container.redis)
    entry = await cache.get_entry(code, 1)
    if entry is None:
        await send_screen(client, target.chat.id,
                          card("⚠️ This session expired. Start again from the list.",
                               bot_name=_BOT))
        return
    assets, gallery, _rows = await adapter.asset_step(code, entry, asset)
    word = _ASSET_WORD.get(asset, asset)
    if not assets:
        # No TMDB assets for this type → let the admin upload their own.
        caption = (f"{V.ICON} <b>No {word}s found</b>\n\n"
                   f"TMDB had no {word} for this title. Upload your own image, or "
                   "skip to keep the render without it.")
        rows = [
            [(f"⬆️ Upload {word}",
              cb(_BOT, "thumbregen", "up", code, asset))],
            [(V.BTN_CANCEL, cb(_BOT, "thumbedit", "regcancel",
                               anime_doc_id, str(anilist_id)))],
        ]
        await send_screen(client, target.chat.id,
                          card(caption, bot_name=_BOT, buttons=rows), old_msg=old_msg)
        return
    # Numbered pick buttons (2 per row) + Upload-your-own + Cancel, all ours.
    num_rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for i in range(1, len(assets) + 1):
        pair.append((str(i), cb(_BOT, "thumbregen", "pick", code, asset, str(i))))
        if len(pair) == 2:
            num_rows.append(pair)
            pair = []
    if pair:
        num_rows.append(pair)
    num_rows.append([(f"⬆️ Upload {word}", cb(_BOT, "thumbregen", "up", code, asset))])
    num_rows.append([(V.BTN_CANCEL,
                      cb(_BOT, "thumbedit", "regcancel", anime_doc_id, str(anilist_id)))])
    caption = (f"{V.ICON} <b>Pick a {word}</b>\n\n"
               f"Open the gallery, then tap the number you want.")
    url_rows = [[("🖼 Open gallery", gallery)]] if gallery else None
    screen = card(caption, bot_name=_BOT, buttons=num_rows, url_buttons=url_rows)
    await send_screen(client, target.chat.id, screen, old_msg=old_msg)


async def _advance(
    client: Client, container: Container, q: CallbackQuery, code: str,
    anime_doc_id: str, anilist_id: int, next_asset: str | None,
) -> None:
    """After a pick/upload: show the next asset, or render when all are chosen."""
    if next_asset is not None:
        await _show_asset(client, container, q.message, code, anime_doc_id,
                          anilist_id, next_asset, old_msg=q.message)
        return
    # All three chosen → render, persist, push live, return to the edit page.
    await send_screen(client, q.message.chat.id,
                      card(f"{V.ICON} <b>Rendering…</b>", bot_name=_BOT),
                      old_msg=q.message)
    adapter = await _adapter(container)
    cache = DistributionCache(container.redis)
    entry = await cache.get_entry(code, 1)
    path = await adapter.render_entry(code, entry) if entry else None
    if not path:
        why = getattr(adapter, "last_render_error", None)
        note = ("the headless browser isn't installed — run `playwright install`"
                if why == "browser" else "the render failed")
        await send_screen(
            client, q.message.chat.id,
            card(f"{V.ICON} <b>Couldn't render.</b>\n\nSorry — {note}. Try again.",
                 bot_name=_BOT,
                 buttons=[[(V.BTN_EDITTHUMB_REGEN,
                            cb(_BOT, "thumbedit", "regen", anime_doc_id, str(anilist_id)))]]),
        )
        return
    # render_entry persisted the ThumbnailSource; now refresh the live surface.
    from kurosoden.bots.senku.handlers.thumbnail_edit_senku import _refresh_live
    live = await _refresh_live(container, anime_doc_id, anilist_id, str(path))
    await _fsm(container).clear(q.from_user.id)
    tail = f" and refreshed the {live}" if live else ""
    await send_screen(
        client, q.message.chat.id,
        card(f"{V.ICON} <b>Done.</b>\n\nRegenerated the thumbnail{tail}. "
             "You can now edit its values.",
             bot_name=_BOT,
             buttons=[[("✏️ Edit values",
                        cb(_BOT, "thumbedit", "entry", anime_doc_id, str(anilist_id)))],
                      [(V.BTN_EDITTHUMB_BACK, cb(_BOT, "thumbedit", "fr", anime_doc_id))]]),
    )


def register(client: Client, container: Container) -> None:
    """Register the regenerate picker callbacks + its upload capture."""

    @client.on_callback_query(
        filters.regex(r"^senku\|thumbregen\|"), group=3,
    )
    async def _regen_cb(_: Client, q: CallbackQuery) -> None:
        if q.message is None or not is_staff(q):
            await q.answer("Staff access required.", show_alert=True)
            return
        try:
            parts = (q.data or "").split("|")
            action = parts[2]
            code = parts[3]
            doc, aid = _split_code(code)

            if action == "pick":
                asset, number = parts[4], int(parts[5])
                await q.answer()
                adapter = await _adapter(container)
                _sel, nxt = await adapter.store_pick(code, 1, asset, number)
                await _advance(client, container, q, code, doc, aid, nxt)
                return

            if action == "up":
                asset = parts[4]
                await _fsm(container).set(
                    q.from_user.id, _ST_UPLOAD, code=code, doc=doc, aid=aid, asset=asset,
                )
                await q.answer("Send the image now.", show_alert=True)
                await send_screen(
                    client, q.message.chat.id,
                    card(f"{V.ICON} <b>Send your {_ASSET_WORD.get(asset, asset)}</b>\n\n"
                         "Upload it as a photo or an image file.", bot_name=_BOT,
                         buttons=[[(V.BTN_CANCEL,
                                    cb(_BOT, "thumbedit", "regcancel", doc, str(aid)))]]),
                    old_msg=q.message,
                )
                return
        except (IndexError, ValueError):
            await q.answer("That button has expired — reopen /edit_thumbnail.",
                           show_alert=True)

    @client.on_message(
        (filters.photo | filters.document) & filters.private, group=15,
    )
    async def _regen_upload(_: Client, message: Message) -> None:
        if not message.from_user or not is_staff(message) or container.redis is None:
            return
        fsm = _fsm(container)
        state, data = await fsm.get(message.from_user.id)
        if state != _ST_UPLOAD:
            return
        code = str(data.get("code") or "")
        doc, aid = str(data.get("doc") or ""), int(data.get("aid", -1))
        asset = str(data.get("asset") or "logo")
        try:
            raw = await message.download(in_memory=True)
            file_bytes = bytes(raw.getbuffer()) if hasattr(raw, "getbuffer") else bytes(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("editthumb.regen.download_failed", error=str(exc))
            await message.reply_text("⚠️ I couldn't read that image. Try again.")
            return
        adapter = await _adapter(container)
        try:
            _sel, nxt = await adapter.store_upload(code, 1, asset, file_bytes)
        except Exception as exc:  # noqa: BLE001
            log.warning("editthumb.regen.upload_failed", error=str(exc))
            await message.reply_text("⚠️ Every image host rejected that upload. Try again.")
            return
        # Re-enter the regen state (upload state consumed) and advance.
        await fsm.set(message.from_user.id, "senku_editthumb_regen",
                      doc=doc, aid=aid, code=code)

        class _Shim:  # minimal stand-in so _advance can edit in place
            def __init__(self, msg):
                self.message = msg
                self.from_user = msg.from_user

            async def answer(self, *a, **k):
                return None

        await _advance(client, container, _Shim(message), code, doc, aid, nxt)


def _split_code(code: str) -> tuple[str, int]:
    """Recover ``(anime_doc_id, anilist_id)`` from ``THUMBEDIT-<doc>#<tag>``.

    ``<tag>`` is ``m`` for the main/root sentinel (-1) or a positive int. Splits
    on the ``#`` delimiter so a hyphen inside ``anime_doc_id`` is preserved.
    """
    body = code[len("THUMBEDIT-"):] if code.startswith("THUMBEDIT-") else code
    doc, _, tag = body.rpartition("#")
    if not doc:  # no delimiter (legacy / malformed) — treat whole as doc
        return body, -1
    if tag == "m":
        return doc, -1
    try:
        return doc, int(tag)
    except ValueError:
        return doc, -1
