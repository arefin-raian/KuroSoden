"""Owner-only Levi flow for editing persisted storage-pack headers.

The worker persists the exact HTML header caption when it uploads a pack. This
handler provides a small, restart-safe editor: select a pack, send the replacement
caption, and the storage service updates both Postgres and the live channel header.
"""

from __future__ import annotations

import html

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.bots.channel_reply import arm as arm_reply
from nekofetch.bots.channel_reply import disarm as disarm_reply
from nekofetch.bots.channel_reply import peek as peek_reply
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.services.storage_channel_service import StorageChannelService
from nekofetch.ui.components import cb, keyboard, lock_buttons
from nekofetch.ui.screens import Screen, send_screen
from kurosoden.shared.access_gate import is_owner

log = get_logger(__name__)
_STATE = "levi_pack_caption"
_LOCK_PREFIX = "nf:levi:pack_caption_lock:"


def _pack_label(pack) -> str:
    season = "Extra" if pack.season is None else f"S{pack.season:02d}"
    if pack.season_part:
        season += f"P{pack.season_part:02d}"
    audio = getattr(pack.audio, "value", pack.audio)
    return f"{pack.anime_title} · {season} · {pack.resolution} · {audio}"


def _pack_card(packs) -> tuple[str, list[list[tuple[str, str]]]]:
    lines = ["<b>🛠 Storage pack captions</b>", "",
             "Choose an entry; its resolution/audio packs will be updated together."]
    rows: list[list[tuple[str, str]]] = []
    seen: set[tuple] = set()
    for pack in packs:
        key = (pack.anime_doc_id, pack.season, pack.season_part, pack.entry_id)
        if key in seen:
            continue
        seen.add(key)
        label = _pack_label(pack)
        siblings = [p for p in packs if (
            p.anime_doc_id, p.season, p.season_part, p.entry_id
        ) == key]
        variants = len(siblings)
        current = "saved" if any(getattr(p, "caption", None) for p in siblings) else "legacy"
        suffix = f" · {variants} packs" if variants > 1 else ""
        lines.append(f"• <b>{html.escape(label)}</b>{suffix} · <i>{current}</i>")
        rows.append([(label[:48], cb("levi", "packedit", pack.id))])
    if not packs:
        lines.append("\n<i>No enabled storage packs were found.</i>")
    rows.append([("⇐ Back", cb("levi", "home"))])
    return "\n".join(lines), rows


def register(client: Client, container: Container) -> None:
    """Register owner-only pack caption list, selection, and text consumer."""

    async def _show_pack_list(message: Message) -> None:
        packs = await StorageChannelService(container).list_packs()
        caption, rows = _pack_card(packs)
        await send_screen(
            client, message.chat.id,
            Screen(caption=caption, keyboard=keyboard(*rows)),
            old_msg=message,
        )

    @client.on_message(filters.command("packcaptions"), group=4)
    async def _command(_: Client, message: Message) -> None:
        if not is_owner(container, message):
            return
        await _show_pack_list(message)

    @client.on_callback_query(filters.regex(r"^levi\|packcaptions$"))
    async def _list(_: Client, q: CallbackQuery) -> None:
        if q.message is None or not is_owner(container, q):
            await q.answer("Owner access required.", show_alert=True)
            return
        await lock_buttons(q)
        await q.answer()
        await _show_pack_list(q.message)

    @client.on_callback_query(filters.regex(r"^levi\|packedit\|"))
    async def _select(_: Client, q: CallbackQuery) -> None:
        if q.message is None or not is_owner(container, q):
            await q.answer("Owner access required.", show_alert=True)
            return
        if q.message.chat.type != ChatType.PRIVATE:
            await q.answer("Open Levi in a private chat to edit pack captions.", show_alert=True)
            return
        await lock_buttons(q)
        try:
            pack_id = int((q.data or "").split("|")[2])
        except (IndexError, TypeError, ValueError):
            await q.answer("Invalid pack.", show_alert=True)
            return
        lock_key = f"{_LOCK_PREFIX}{q.message.chat.id}"
        if container.redis is None:
            await q.answer("Caption editing is temporarily unavailable.", show_alert=True)
            return
        acquired = await container.redis.set(
            lock_key, str(pack_id), nx=True, ex=900,
        )
        if not acquired:
            await q.answer("Finish the current caption edit first.", show_alert=True)
            return
        await arm_reply(container.redis, q.message.chat.id, _STATE, pack_id=pack_id)
        await q.answer("Send the replacement caption.", show_alert=True)
        try:
            await q.message.reply_text(
                "<b>Send the new storage-pack header caption.</b>\n\n"
                "HTML is allowed; this replaces the header in the storage channel.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:  # noqa: BLE001 - prompt is best effort
            log.debug("levi.pack_caption.prompt_failed", error=str(exc))

    @client.on_message(filters.text & ~filters.command(["start"]), group=15)
    async def _consume(_: Client, message: Message) -> None:
        redis = container.redis
        if not message.from_user or not is_owner(container, message):
            return
        state, data = await peek_reply(redis, message.chat.id)
        if state != _STATE:
            return
        text = (message.text or "").strip()
        if not text:
            return
        lock_key = f"{_LOCK_PREFIX}{message.chat.id}"
        try:
            pack_id = int(data.get("pack_id"))
            locked_pack = await redis.get(lock_key)
            if isinstance(locked_pack, bytes):
                locked_pack = locked_pack.decode()
            if str(pack_id) != str(locked_pack):
                return
        except (TypeError, ValueError):
            await disarm_reply(redis, message.chat.id)
            await redis.delete(lock_key)
            return
        await disarm_reply(redis, message.chat.id)
        await redis.delete(lock_key)
        updated = await StorageChannelService(container).update_header_caption(pack_id, text)
        try:
            if updated is None:
                await message.reply_text("⚠️ Storage pack not found.", parse_mode=ParseMode.HTML)
            else:
                await message.reply_text("✅ Storage-pack caption updated.", parse_mode=ParseMode.HTML)
            await message.delete()
        except Exception:  # noqa: BLE001 - cosmetic cleanup
            pass
        log.info("levi.pack_caption.updated", pack=pack_id, admin=message.from_user.id)
