"""Staff-scoped editor for already-rendered durable thumbnails.

The normal thumbnail workflow is intentionally restart-safe, but it used to stop
at generation: staff could not correct a saved title or artwork without
starting the whole asset-picking flow again. This small editor works from the
persisted ``ThumbnailSource.fields`` record, re-renders the card, and edits the
existing thumbnail-channel message when its message id is known.
"""

from __future__ import annotations

import html

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from nekofetch.bots.channel_reply import arm as arm_reply
from nekofetch.bots.channel_reply import disarm as disarm_reply
from nekofetch.bots.channel_reply import peek as peek_reply
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import ThumbnailSource
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.services.thumbnail_service import (
    ThumbnailRenderService,
    persist_thumbnail_source,
    render_fields,
)
from nekofetch.ui.components import cb
from kurosoden.shared.access_gate import is_staff

log = get_logger(__name__)
_STATE = "admin_thumbnail_edit"
_LOCK = "nf:admin:thumbnail_edit:"
_EDITABLE = (
    "title", "native_title", "romaji_title", "synopsis", "language",
    "studio", "meta_label", "logo_url", "poster_url", "bg_url",
)


def _label(row: ThumbnailSource) -> str:
    fields = row.fields or {}
    title = fields.get("title") or row.anime_doc_id
    entry = fields.get("entry_label") or f"entry {row.anilist_id}"
    return f"{title} · {entry}"


async def _sources(container: Container) -> list[ThumbnailSource]:
    async with session_scope(container.pg_sessionmaker) as session:
        rows = list((await session.execute(
            select(ThumbnailSource).order_by(ThumbnailSource.anime_doc_id, ThumbnailSource.id)
        )).scalars().all())
        for row in rows:
            session.expunge(row)
        return rows


def _source_rows(rows: list[ThumbnailSource]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        buttons.append([
            InlineKeyboardButton(
                _label(row)[:50], callback_data=cb("thumbedit", "pick", str(row.id)),
            )
        ])
    buttons.append([InlineKeyboardButton("Close", callback_data=cb("thumbedit", "close"))])
    return InlineKeyboardMarkup(buttons)


async def show_editor(client: Client, container: Container, target: Message) -> None:
    """Open the durable thumbnail editor from a command or inline menu."""
    rows = await _sources(container)
    if not rows:
        await target.reply_text("No durable thumbnails have been generated yet.")
        return
    await target.reply_text(
        "<b>Edit a saved thumbnail</b>\n\nChoose the card you want to change.",
        parse_mode=ParseMode.HTML,
        reply_markup=_source_rows(rows),
    )


def register(client: Client, container: Container) -> None:
    @client.on_message(filters.command("edit_thumbnail"), group=4)
    async def _command(_: Client, message: Message) -> None:
        if not is_staff(message):
            return
        await show_editor(client, container, message)

    @client.on_callback_query(filters.regex(r"^thumbedit\|(?!field\|)"), group=4)
    async def _callback(_: Client, query: CallbackQuery) -> None:
        if query.message is None or not is_staff(query):
            await query.answer("Staff access required.", show_alert=True)
            return
        parts = (query.data or "").split("|")
        action = parts[1] if len(parts) > 1 else ""
        if action == "close":
            await query.message.edit_reply_markup(reply_markup=None)
            await query.answer()
            return
        if action != "pick" or len(parts) < 3:
            await query.answer("Unknown thumbnail editor action.", show_alert=True)
            return
        try:
            source_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid thumbnail.", show_alert=True)
            return
        async with session_scope(container.pg_sessionmaker) as session:
            row = await session.get(ThumbnailSource, source_id)
            if row is None:
                await query.answer("Thumbnail not found.", show_alert=True)
                return
            fields = dict(row.fields or {})
        rows = [
            [InlineKeyboardButton(
                f"{field}: {(fields.get(field) or 'blank')[:24]}",
                callback_data=cb("thumbedit", "field", str(source_id), field),
            )]
            for field in _EDITABLE
        ]
        rows.append([InlineKeyboardButton("Close", callback_data=cb("thumbedit", "close"))])
        await query.message.edit_text(
            f"<b>Edit thumbnail</b>\n{_label(row)}\n\nChoose one field and send its new value.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        await query.answer()

    @client.on_callback_query(filters.regex(r"^thumbedit\|field\|"), group=4)
    async def _field(_: Client, query: CallbackQuery) -> None:
        if query.message is None or not is_staff(query):
            await query.answer("Staff access required.", show_alert=True)
            return
        parts = (query.data or "").split("|")
        if len(parts) < 4 or parts[3] not in _EDITABLE:
            await query.answer("Invalid field.", show_alert=True)
            return
        source_id, field = parts[2], parts[3]
        if container.redis is None:
            await query.answer("Editor storage is unavailable.", show_alert=True)
            return
        chat_id = query.message.chat.id
        acquired = await container.redis.set(
            f"{_LOCK}:{chat_id}", source_id, nx=True, ex=900,
        )
        if not acquired:
            await query.answer("Finish the current thumbnail edit first.", show_alert=True)
            return
        await arm_reply(container.redis, chat_id, _STATE, source_id=source_id, field=field)
        await query.answer("Send the new value.", show_alert=True)
        await query.message.reply_text(
            f"Send the new <b>{field}</b> value.\n\n"
            "The card will be rendered again immediately.",
            parse_mode=ParseMode.HTML,
        )

    @client.on_message(filters.text & ~filters.command(["start", "edit_thumbnail"]), group=16)
    async def _consume(_: Client, message: Message) -> None:
        if not message.from_user or not is_staff(message) or container.redis is None:
            return
        state, data = await peek_reply(container.redis, message.chat.id)
        if state != _STATE:
            return
        try:
            source_id = int(data["source_id"])
            field = str(data["field"])
        except (KeyError, TypeError, ValueError):
            return
        locked = await container.redis.get(f"{_LOCK}:{message.chat.id}")
        if isinstance(locked, bytes):
            locked = locked.decode()
        if str(source_id) != str(locked) or field not in _EDITABLE:
            return
        await disarm_reply(container.redis, message.chat.id)
        await container.redis.delete(f"{_LOCK}:{message.chat.id}")
        value = (message.text or "").strip()
        if not value:
            await message.reply_text("The new value cannot be empty.")
            return
        try:
            async with session_scope(container.pg_sessionmaker) as session:
                row = await session.get(ThumbnailSource, source_id)
                if row is None:
                    await message.reply_text("Thumbnail not found.")
                    return
                fields = dict(row.fields or {})
                fields[field] = value
                renderer = ThumbnailRenderService()
                try:
                    image_path = await renderer.render_thumbnail(**render_fields(fields))
                finally:
                    await renderer.close()
                if not image_path:
                    raise RuntimeError("renderer returned no image")
                row.fields = fields
                row.image_path = str(image_path)
                channel_id = fields.get("thumbnail_chat_id")
                message_id = fields.get("thumbnail_message_id")
                await session.flush()
                session.expunge(row)
            await persist_thumbnail_source(
                container, row.anime_doc_id, None if row.anilist_id == -1 else row.anilist_id,
                fields, image_path=image_path,
            )
            # Keep the Redis workflow in sync through its public service API.
            try:
                from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService
                await ThumbnailChannelService(container).mark_entry_rendered(
                    row.anime_doc_id,
                    None if row.anilist_id == -1 else row.anilist_id,
                    str(image_path),
                )
            except Exception as exc:  # noqa: BLE001 - durable DB edit still stands
                log.warning("thumbnail.edit.workflow_sync_failed", error=str(exc))

            client_obj = getattr(container, "admin_client", None)
            title = html.escape(str(fields.get("title") or row.anime_doc_id), quote=False)
            entry_label = html.escape(str(fields.get("entry_label") or ""), quote=False)
            staging_caption = f"<b>{title}</b> — <i>{entry_label}</i>"
            staging_edited = False
            if client_obj and channel_id and message_id:
                from pyrogram.types import InputMediaPhoto
                try:
                    live = None
                    if hasattr(client_obj, "get_messages"):
                        live = await client_obj.get_messages(int(channel_id), int(message_id))
                    existing_caption = (
                        getattr(live, "caption", None)
                        or getattr(live, "text", None)
                        or staging_caption
                    )
                    await client_obj.edit_message_media(
                        int(channel_id), int(message_id),
                        InputMediaPhoto(str(image_path), caption=existing_caption,
                                        parse_mode=ParseMode.HTML),
                    )
                    staging_edited = True
                except Exception as exc:  # noqa: BLE001 - live surfaces remain authoritative
                    log.warning("thumbnail.edit.staging_failed", error=str(exc))

            live_surface = None
            if row.anilist_id == -1:
                from nekofetch.services.main_channel_service import MainChannelService
                if await MainChannelService(container).refresh_thumbnail(
                    row.anime_doc_id, str(image_path),
                ):
                    live_surface = "main-channel post"
            else:
                from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService
                if await ThumbnailChannelService(container).refresh_published_thumbnail(
                    row.anime_doc_id, int(row.anilist_id), str(image_path),
                ):
                    live_surface = f"distribution card ({entry_label or 'entry'})"

            if live_surface:
                suffix = "; staging preview refreshed" if staging_edited else ""
                await message.reply_text(f"✅ Updated the {live_surface}{suffix}.")
            elif staging_edited:
                await message.reply_text(
                    "⚠️ Updated the staging preview, but the live published surface was not found."
                )
            else:
                await message.reply_text(
                    "⚠️ Saved the new inputs, but no published or staging thumbnail was found."
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("thumbnail.edit.failed", source=source_id, error=str(exc))
            await message.reply_text("⚠️ I couldn't render that update. The saved card was left unchanged.")
