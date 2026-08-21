"""Senku post editor — edit a published distribution post from its link.

Staff run ``/editpost`` (or the home-menu button), paste the LINK of the post
they want to change, and — once the bot confirms it administers that channel —
choose to rewrite the caption or replace the buttons. The editor:

  1. edits the LIVE Telegram message in the distribution channel,
  2. when the post is tracked, updates the matching ``BotContentPost`` row (so a
     ban-restore ships the new caption/buttons) and bumps ``content_revision``
     (returning /start users get the new text),
  3. refreshes the wipe-proof channel backup.

The flow is restart-safe via the same ``channel_reply`` arm/peek/disarm pattern
the other editors use; a chat-scoped lock prevents two staff from editing in the
same chat at once. Captions accept HTML / Markdown / Telegram entities and are
rendered properly (never raw) via ``parse_user_markup``.
"""

from __future__ import annotations

import asyncio
import html

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import CallbackQuery, Message
from sqlalchemy import select

from nekofetch.bots.channel_reply import arm as arm_reply
from nekofetch.bots.channel_reply import disarm as disarm_reply
from nekofetch.bots.channel_reply import peek as peek_reply
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    DistributionBot,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.ui.components import cb
from nekofetch.ui.screens import card, message_ref, send_screen
from nekofetch.services.bot_render import build_audio_keyboard
from kurosoden.shared import post_edit_ops
from kurosoden.shared import senku_voice as V
from kurosoden.shared.access_gate import is_staff

log = get_logger(__name__)

_BOT = "senku"

# FSM-ish channel_reply markers. AWAIT_LINK captures the pasted post link;
# AWAIT_EDIT captures the replacement caption / button lines once a post is
# locked in.
_STATE_LINK = "senku_postedit_link"
_STATE_EDIT = "senku_postedit_edit"
_LOCK_PREFIX = "nf:senku:post_edit_lock:"

# Telegram's hard message-text limit (captions are shared with the photo).
_TEXT_LIMIT = 4096


# ── Link parsing + channel resolution ────────────────────────────────────────

def _parse_post_link(raw: str) -> tuple[str | int, int] | None:
    """Parse a Telegram post link into ``(chat_ref, message_id)``.

    Accepts the two shapes an admin can copy from a channel post:

      * private channel — ``https://t.me/c/<internal>/<msg>`` → the chat id is
        ``int("-100" + internal)`` and ``chat_ref`` is that numeric id;
      * public channel  — ``https://t.me/<username>/<msg>`` → ``chat_ref`` is
        ``"@<username>"`` (resolved live).

    A ``/<thread>/<msg>`` (forum topic) link keeps the LAST path segment as the
    message id. Returns ``None`` when the text isn't a usable message link.
    """
    text = (raw or "").strip()
    if not text:
        return None
    # Tolerate a bare "t.me/…" without a scheme, and strip any query/fragment.
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("?", 1)[0].split("#", 1)[0]
    if text.startswith("t.me/"):
        text = text[len("t.me/"):]
    elif text.startswith("telegram.me/"):
        text = text[len("telegram.me/"):]
    else:
        return None
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        return None
    try:
        message_id = int(parts[-1])
    except ValueError:
        return None
    if message_id <= 0:
        return None
    if parts[0] == "c":
        # t.me/c/<internal>/<...>/<msg>
        if len(parts) < 3:
            return None
        internal = parts[1]
        if not internal.isdigit():
            return None
        return int(f"-100{internal}"), message_id
    # t.me/<username>/<...>/<msg>
    username = parts[0]
    if not username or username.lower() in {"c", "s", "joinchat", "+"}:
        return None
    return f"@{username}", message_id


async def _resolve_editable_channel(
    client: Client, container: Container, chat_ref: str | int,
) -> tuple[int, int | None, str] | None:
    """Confirm the bot can edit posts in ``chat_ref``.

    Returns ``(chat_id, bot_id, title)`` when the resolved chat is a channel the
    bot administers (with edit rights, when Telegram exposes the privilege), and
    ``bot_id`` is the matching :class:`DistributionBot` row id when we track it
    (``None`` for a channel we admin but don't have a bot row for). Returns
    ``None`` when the chat can't be resolved or the bot isn't an admin there.
    """
    try:
        chat = await client.get_chat(chat_ref)
    except Exception as exc:  # noqa: BLE001 — bad handle / not a member
        log.info("senku.postedit.resolve_failed", ref=str(chat_ref), error=str(exc))
        return None
    chat_id = int(chat.id)
    try:
        me = await client.get_chat_member(chat_id, "me")
    except Exception as exc:  # noqa: BLE001 — not a member ⇒ can't edit
        log.info("senku.postedit.membership_failed", chat=chat_id, error=str(exc))
        return None
    status = getattr(getattr(me, "status", None), "value",
                     str(getattr(me, "status", "")))
    if status not in ("administrator", "creator"):
        return None
    # When Telegram exposes granular privileges, require edit rights (creators
    # implicitly have them). A missing privileges object (older layers) is not
    # treated as a hard failure — the live edit will surface any real block.
    privileges = getattr(me, "privileges", None)
    if status == "administrator" and privileges is not None:
        if not getattr(privileges, "can_edit_messages", False):
            return None
    # Map the chat back to a tracked distribution channel (best-effort — a
    # channel we admin but don't track can still be live-edited).
    bot_id: int | None = None
    async with session_scope(container.pg_sessionmaker) as session:
        row = (await session.execute(
            select(DistributionBot).where(
                DistributionBot.is_channel.is_(True),
                DistributionBot.chat_id == chat_id,
            ).order_by(DistributionBot.id.desc())
        )).scalars().first()
        if row is not None:
            bot_id = row.id
    title = getattr(chat, "title", None) or (
        f"@{chat.username}" if getattr(chat, "username", None) else str(chat_id)
    )
    return chat_id, bot_id, title


# ── Button parsing ───────────────────────────────────────────────────────────

def _parse_button_lines(raw: str) -> dict | None:
    """Parse the operator's compact button format into durable button_data.

    One button per line: ``Label | https://example``.  An empty message or
    ``none`` intentionally removes all buttons.  We store the structured
    payload, rather than a Telegram markup object, so restore/re-render paths
    can use the same shared keyboard builder.
    """
    value = (raw or "").strip()
    if not value or value.casefold() in {"none", "clear", "remove"}:
        return {"type": "custom", "buttons": []}
    buttons: list[dict[str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            raise ValueError("Use one `Button label | https://link` per line.")
        label, url = (part.strip() for part in line.split("|", 1))
        if not label or not url.startswith(("http://", "https://", "tg://")):
            raise ValueError("Each button needs a label and an http(s) URL.")
        buttons.append({"text": label[:64], "url": url})
    if not buttons:
        return {"type": "custom", "buttons": []}
    return {"type": "custom", "buttons": buttons}


# ── Apply (live edit + durable sync) ─────────────────────────────────────────

async def _edit_caption(
    client: Client, container: Container, message: Message,
    *, chat_id: int, tg_message_id: int, new_caption: str,
    bot_id: int | None = None,
) -> tuple[bool, str]:
    """Apply a caption edit without claiming success when Telegram rejects it.

    Live-edits the message in ``chat_id`` first (Telegram is the source of
    truth). When ``bot_id`` is given, the matching ``BotContentPost`` rows and
    the channel backup are updated too; an untracked link-edit (``bot_id`` None)
    is live-only.
    """
    if len(new_caption) > _TEXT_LIMIT:
        return False, f"Caption is too long ({len(new_caption)} > {_TEXT_LIMIT} chars)."

    anime_doc_id: str | None = None
    if bot_id is not None:
        async with session_scope(container.pg_sessionmaker) as session:
            bot = await session.get(DistributionBot, bot_id)
            if bot is not None:
                anime_doc_id = bot.anime_doc_id

    # Telegram is the live source of truth. Persist the DB copy only after this
    # succeeds; otherwise a failed live edit would make restores silently ship a
    # caption that subscribers never saw. The shared primitive preserves the
    # post's inline keyboard (editMessageCaption/Text would otherwise drop it).
    ok, _msg = await post_edit_ops.live_edit_caption(
        client, chat_id, tg_message_id, new_caption,
        text_limit=_TEXT_LIMIT, log_event_prefix="senku.caption",
    )
    if not ok:
        return False, "Telegram rejected the live edit; the database was left unchanged."

    if bot_id is None:
        log.info("senku.caption.edited", chat=chat_id, mid=tg_message_id,
                 durable=False)
        return True, "Caption updated in the channel."

    try:
        async with session_scope(container.pg_sessionmaker) as session:
            rows = (await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot_id,
                    BotContentPost.tg_message_id == tg_message_id,
                )
            )).scalars().all()
            for row in rows:
                row.caption = new_caption
            if rows:
                bot = await session.get(DistributionBot, bot_id)
                if bot is not None:
                    bot.content_revision = (bot.content_revision or 0) + 1
    except Exception as exc:  # noqa: BLE001 — live edit succeeded, DB retry is needed
        log.error("senku.caption.database_update_failed", bot=bot_id,
                  mid=tg_message_id, error=str(exc))
        return False, "Telegram was updated, but the database update failed; retry the save."

    try:
        from nekofetch.services.backup_service import BackupService

        if anime_doc_id:
            await BackupService(container).record_distribution_channel(anime_doc_id)
    except Exception as exc:  # noqa: BLE001 — live + DB edits remain authoritative
        log.warning("senku.caption.backup_failed", bot=bot_id, error=str(exc))

    log.info("senku.caption.edited", bot=bot_id, chat=chat_id, mid=tg_message_id,
             durable=True)
    return True, "Caption updated in the channel and the database."


async def _edit_buttons(
    client: Client, container: Container,
    *, chat_id: int, tg_message_id: int, button_data: dict,
    bot_id: int | None = None,
) -> tuple[bool, str]:
    """Replace one post's buttons without claiming success on live-edit failure.

    Live-edits ``chat_id`` first; when ``bot_id`` is given the matching
    ``BotContentPost.button_data`` and the channel backup are synced too. An
    untracked link-edit (``bot_id`` None) is live-only.
    """
    anime_doc_id: str | None = None
    if bot_id is not None:
        async with session_scope(container.pg_sessionmaker) as session:
            bot = await session.get(DistributionBot, bot_id)
            if bot is not None:
                anime_doc_id = bot.anime_doc_id

    try:
        markup = build_audio_keyboard(button_data, container.config.post_format)
    except Exception as exc:  # noqa: BLE001 — malformed button_data must not crash
        log.warning("senku.buttons.build_failed", chat=chat_id,
                    mid=tg_message_id, error=str(exc))
        return False, "I couldn't build those buttons; the database was left unchanged."
    ok, _msg = await post_edit_ops.live_edit_buttons(
        client, chat_id, tg_message_id, markup,
        log_event_prefix="senku.buttons",
    )
    if not ok:
        return False, "Telegram rejected the live button edit; the database was left unchanged."

    if bot_id is None:
        log.info("senku.buttons.edited", chat=chat_id, mid=tg_message_id,
                 durable=False)
        return True, "Buttons updated in the channel."

    try:
        async with session_scope(container.pg_sessionmaker) as session:
            row = (await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot_id,
                    BotContentPost.tg_message_id == tg_message_id,
                )
            )).scalars().first()
            if row is not None:
                row.button_data = button_data
                bot = await session.get(DistributionBot, bot_id)
                if bot is not None:
                    bot.content_revision = (bot.content_revision or 0) + 1
    except Exception as exc:  # noqa: BLE001 — live edit succeeded, DB retry is needed
        log.error("senku.buttons.database_update_failed", bot=bot_id,
                  mid=tg_message_id, error=str(exc))
        return False, "Telegram was updated, but the database update failed; retry the save."

    try:
        from nekofetch.services.backup_service import BackupService
        if anime_doc_id:
            await BackupService(container).record_distribution_channel(anime_doc_id)
    except Exception as exc:  # noqa: BLE001 — live + DB edits remain authoritative
        log.warning("senku.buttons.backup_failed", bot=bot_id, error=str(exc))
    return True, "Buttons updated in the channel and the database."


async def _edit_media(
    client: Client, container: Container,
    *, chat_id: int, tg_message_id: int, image_bytes: bytes,
    bot_id: int | None = None,
) -> tuple[bool, str]:
    """Replace one post's image without claiming success on live-edit failure.

    Live-edits the message in ``chat_id`` first (Telegram is the source of
    truth), preserving the existing caption and inline keyboard — Telegram's
    ``editMessageMedia`` drops the markup unless it is re-supplied. When
    ``bot_id`` is given, the matching ``BotContentPost`` image URLs are
    re-pointed to a durably-mirrored copy of the new image (the model stores
    URLs, not file_ids) and the channel backup is refreshed. An untracked
    link-edit (``bot_id`` None) is live-only.
    """
    anime_doc_id: str | None = None
    if bot_id is not None:
        async with session_scope(container.pg_sessionmaker) as session:
            bot = await session.get(DistributionBot, bot_id)
            if bot is not None:
                anime_doc_id = bot.anime_doc_id

    # Preserve caption (via .html) + buttons across the swap — the shared
    # primitive reconstructs the styled caption and re-hands the keyboard that
    # editMessageMedia would otherwise drop.
    ok, msg = await post_edit_ops.live_edit_media(
        client, chat_id, tg_message_id, image_bytes,
        log_event_prefix="senku.image",
    )
    if not ok:
        return False, msg

    if bot_id is None:
        log.info("senku.image.edited", chat=chat_id, mid=tg_message_id, durable=False)
        return True, "Image replaced in the channel."

    # The row stores URLs (no file_id column): mirror the new bytes to a durable
    # host so ban-restore / re-render ship the new picture too.
    durable_url: str | None = None
    try:
        from kurosoden.shared.image_backup import backup_bytes

        mirrored = await backup_bytes(container, image_bytes, mime="image/jpeg")
        durable_url = mirrored.primary
    except Exception as exc:  # noqa: BLE001 — live edit already stands
        log.warning("senku.image.mirror_failed", bot=bot_id, error=str(exc))

    try:
        async with session_scope(container.pg_sessionmaker) as session:
            rows = (await session.execute(
                select(BotContentPost).where(
                    BotContentPost.bot_id == bot_id,
                    BotContentPost.tg_message_id == tg_message_id,
                )
            )).scalars().all()
            for row in rows:
                if durable_url:
                    row.image_url = durable_url
                    row.image_cached_url = durable_url
            if rows:
                bot = await session.get(DistributionBot, bot_id)
                if bot is not None:
                    bot.content_revision = (bot.content_revision or 0) + 1
    except Exception as exc:  # noqa: BLE001 — live edit succeeded, DB retry is needed
        log.error("senku.image.database_update_failed", bot=bot_id,
                  mid=tg_message_id, error=str(exc))
        return False, "Telegram was updated, but the database update failed; retry the save."

    try:
        from nekofetch.services.backup_service import BackupService

        if anime_doc_id:
            await BackupService(container).record_distribution_channel(anime_doc_id)
    except Exception as exc:  # noqa: BLE001 — live + DB edits remain authoritative
        log.warning("senku.image.backup_failed", bot=bot_id, error=str(exc))

    if not durable_url:
        # Live edit stands, but we couldn't persist a durable URL — be honest so
        # the operator knows a ban-restore might ship the old image.
        return True, ("Image replaced in the channel, but I couldn't mirror it to "
                      "durable storage — a ban-restore may show the old image.")
    log.info("senku.image.edited", bot=bot_id, chat=chat_id, mid=tg_message_id,
             durable=True)
    return True, "Image replaced in the channel and the database."


# ── Screens ──────────────────────────────────────────────────────────────────

def _cancel_row() -> list[list[tuple[str, str]]]:
    return [[(V.BTN_CANCEL, cb(_BOT, "postedit", "cancel"))]]


def _choose_buttons() -> list[list[tuple[str, str]]]:
    """The mode-picker keyboard shown after a post is resolved (and after a
    caption save reverts) — Edit caption / Replace buttons / Replace image."""
    return [
        [(V.BTN_POSTEDIT_CAPTION, cb(_BOT, "postedit", "caption")),
         (V.BTN_POSTEDIT_BUTTONS, cb(_BOT, "postedit", "buttons"))],
        [(V.BTN_POSTEDIT_IMAGE, cb(_BOT, "postedit", "image"))],
        [(V.BTN_CANCEL, cb(_BOT, "postedit", "cancel"))],
    ]


async def start_post_edit(
    client: Client, container: Container, target: Message,
) -> None:
    """Open the editor: arm the link capture and show the ask-for-link card."""
    redis = container.redis
    if redis is None:
        await target.reply_text("Post editing is temporarily unavailable.")
        return
    # Fresh start — clear any stale lock/marker for this chat.
    await disarm_reply(redis, target.chat.id)
    await redis.delete(f"{_LOCK_PREFIX}{target.chat.id}")
    await arm_reply(redis, target.chat.id, _STATE_LINK)
    await send_screen(
        client, target.chat.id,
        card(V.POSTEDIT_ASK_LINK, bot_name=_BOT, buttons=_cancel_row()),
        old_msg=target,
    )


def register(client: Client, container: Container) -> None:
    """Register /editpost — the link-based caption/button editor."""

    @client.on_message(filters.command("editpost") & filters.private)
    async def _command(_: Client, message: Message) -> None:
        if not is_staff(message):
            return
        await start_post_edit(client, container, message)

    @client.on_callback_query(filters.regex(r"^senku\|postedit\|"), group=2)
    async def _callback(_: Client, q: CallbackQuery) -> None:
        if q.message is None or not is_staff(q):
            await q.answer("Staff access required.", show_alert=True)
            return
        redis = container.redis
        parts = (q.data or "").split("|")
        action = parts[2] if len(parts) > 2 else ""

        if action == "cancel":
            if redis is not None:
                await disarm_reply(redis, q.message.chat.id)
                await redis.delete(f"{_LOCK_PREFIX}{q.message.chat.id}")
            await q.answer("Cancelled.")
            try:
                await q.message.delete()
            except Exception:  # noqa: BLE001 — cosmetic
                pass
            return

        if action in {"caption", "buttons", "image"}:
            if q.message.chat.type != ChatType.PRIVATE:
                await q.answer("Open Senku in a private chat to edit posts.",
                               show_alert=True)
                return
            if redis is None:
                await q.answer("Post editing is temporarily unavailable.",
                               show_alert=True)
                return
            # The resolved post lives in the AWAIT_EDIT marker armed by the link
            # step. Re-arm it with the chosen mode so the text consumer knows
            # whether the next message is a caption or button lines.
            state, data = await peek_reply(redis, q.message.chat.id)
            if state != _STATE_EDIT:
                await q.answer("Send the post link again to start over.",
                               show_alert=True)
                return
            await q.answer()

            # For caption mode, fetch the CURRENT caption in raw HTML so the
            # prompt can show the exact source to copy-edit (not a rendered
            # preview). Best-effort — a read failure just shows the plain prompt.
            current_html: str | None = None
            if action == "caption":
                try:
                    live = await client.get_messages(
                        int(data.get("target_chat_id")), int(data.get("tg_message_id")))
                    src = (getattr(live, "caption", None)
                           if getattr(live, "caption", None) is not None
                           else getattr(live, "text", None))
                    current_html = getattr(src, "html", None) or (
                        str(src) if src else None)
                except Exception as exc:  # noqa: BLE001 — preview is optional
                    log.debug("senku.postedit.caption_preview_failed", error=str(exc))

            # Render the prompt IN PLACE and stash its id, so the text consumer
            # updates this same card (working → done → back to the menu) instead
            # of spawning new messages.
            prompt = (V.POSTEDIT_ASK_BUTTONS if action == "buttons"
                      else V.POSTEDIT_ASK_IMAGE if action == "image"
                      else V.postedit_ask_caption(current_html))
            prompt_msg = await send_screen(
                client, q.message.chat.id,
                card(prompt, bot_name=_BOT, buttons=_cancel_row()),
                old_msg=q.message,
            )
            await arm_reply(
                redis, q.message.chat.id, _STATE_EDIT,
                target_chat_id=data.get("target_chat_id"), bot_id=data.get("bot_id"),
                tg_message_id=data.get("tg_message_id"), mode=action,
                prompt_msg_id=getattr(prompt_msg, "id", None),
            )
            return

        await q.answer("Unknown action.", show_alert=True)

    @client.on_message(
        filters.text & filters.private & ~filters.command(["start", "editpost"]),
        group=14,
    )
    async def _consume(_: Client, message: Message) -> None:
        redis = container.redis
        if redis is None or not message.from_user or not is_staff(message):
            return
        state, data = await peek_reply(redis, message.chat.id)
        if state == _STATE_LINK:
            await _consume_link(message, redis, data)
            return
        if state == _STATE_EDIT and data.get("mode"):
            if data.get("mode") == "image":
                # Image mode waits for a photo upload, not text — nudge, stay armed.
                await message.reply_text(
                    "🖼 Please upload the new image as a photo or image file."
                )
                return
            await _consume_edit(message, redis, data)
            return

    @client.on_message(
        (filters.photo | filters.document) & filters.private, group=16,
    )
    async def _consume_image(_: Client, message: Message) -> None:
        redis = container.redis
        if redis is None or not message.from_user or not is_staff(message):
            return
        state, data = await peek_reply(redis, message.chat.id)
        if state != _STATE_EDIT or data.get("mode") != "image":
            return
        # An image *document* must actually be an image; ignore stray files.
        if message.document is not None and not (
            (message.document.mime_type or "").startswith("image/")
        ):
            await message.reply_text(
                "🖼 That file isn't an image. Send a photo or an image file."
            )
            return
        await _consume_image_upload(message, redis, data)

    async def _finish_edit_ui(
        message: Message, redis, *, mode: str, prompt_msg_id, do_edit,
        chat_id: int, bot_id: int | None, tg_message_id: int,
    ) -> tuple[bool, str]:
        """Shared post-save UX for caption / image / buttons.

        Consume the operator's message (delete it), flip the prompt card in place
        to "working", run ``do_edit()`` (an async callable → ``(ok, result)``),
        show "done" on the SAME card, then after ~2s revert it to the choose menu
        — never spawning a new standalone message. Re-arms the edit marker so the
        reverted menu's buttons work again. The revert menu's preview is refreshed
        from the LIVE post caption so it's accurate whatever was edited. Falls back
        to a fresh card only when the prompt id was lost (older flow). Returns
        ``do_edit``'s ``(ok, result)``.
        """
        prompt_ref = message_ref(client, message.chat.id, prompt_msg_id) \
            if prompt_msg_id else None
        try:
            await message.delete()
        except Exception:  # noqa: BLE001 — cosmetic
            pass
        # Working state in place (the "rendering…" cue the owner asked for).
        if prompt_ref is not None:
            try:
                await send_screen(
                    client, message.chat.id,
                    card(V.postedit_working(mode), bot_name=_BOT),
                    old_msg=prompt_ref)
            except Exception:  # noqa: BLE001
                pass

        ok, result = await do_edit()

        if prompt_ref is not None:
            try:
                await send_screen(
                    client, message.chat.id,
                    card(V.postedit_done(mode, ok, result), bot_name=_BOT,
                         buttons=_cancel_row()),
                    old_msg=prompt_ref)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2)
            await arm_reply(
                redis, message.chat.id, _STATE_EDIT,
                target_chat_id=chat_id, bot_id=bot_id, tg_message_id=tg_message_id)
            # Refresh the menu preview from the live post (accurate for any mode).
            preview = ""
            try:
                live = await client.get_messages(chat_id, tg_message_id)
                src = (getattr(live, "caption", None)
                       if getattr(live, "caption", None) is not None
                       else getattr(live, "text", None))
                preview = str(src) if src else ""
            except Exception:  # noqa: BLE001 — preview is optional
                pass
            try:
                await send_screen(
                    client, message.chat.id,
                    card(V.postedit_choose(preview), bot_name=_BOT,
                         buttons=_choose_buttons()),
                    old_msg=prompt_ref)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                await send_screen(
                    client, message.chat.id,
                    card(V.postedit_done(mode, ok, result), bot_name=_BOT,
                         buttons=_cancel_row()))
            except Exception:  # noqa: BLE001
                pass
        return ok, result

    async def _consume_image_upload(message: Message, redis, data: dict) -> None:
        lock_key = f"{_LOCK_PREFIX}{message.chat.id}"
        try:
            chat_id = int(data.get("target_chat_id"))
            tg_message_id = int(data.get("tg_message_id"))
            bot_id = int(data["bot_id"]) if data.get("bot_id") is not None else None
        except (TypeError, ValueError):
            await disarm_reply(redis, message.chat.id)
            await redis.delete(lock_key)
            return
        prompt_msg_id = data.get("prompt_msg_id")
        # Read the uploaded bytes BEFORE clearing state, so a download failure
        # leaves the flow armed for a retry.
        try:
            raw = await message.download(in_memory=True)
            image_bytes = bytes(raw.getbuffer()) if hasattr(raw, "getbuffer") else bytes(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("senku.postedit.image_download_failed", error=str(exc))
            await message.reply_text("⚠️ I couldn't read that image. Try sending it again.")
            return
        await disarm_reply(redis, message.chat.id)
        await redis.delete(lock_key)

        # Same consume → working → done → back-to-menu UX as caption/buttons.
        async def _do():
            return await _edit_media(
                client, container, chat_id=chat_id, bot_id=bot_id,
                tg_message_id=tg_message_id, image_bytes=image_bytes)

        ok, _ = await _finish_edit_ui(
            message, redis, mode="image", prompt_msg_id=prompt_msg_id, do_edit=_do,
            chat_id=chat_id, bot_id=bot_id, tg_message_id=tg_message_id)
        log.info("senku.postedit.image_consumed", chat=chat_id, mid=tg_message_id,
                 admin=message.from_user.id, ok=ok)

    async def _consume_link(message: Message, redis, data: dict) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        parsed = _parse_post_link(text)
        if parsed is None:
            await send_screen(
                client, message.chat.id,
                card(V.postedit_bad_link(), bot_name=_BOT, buttons=_cancel_row()),
            )
            return
        chat_ref, tg_message_id = parsed
        resolved = await _resolve_editable_channel(client, container, chat_ref)
        if resolved is None:
            await send_screen(
                client, message.chat.id,
                card(V.postedit_not_mine(), bot_name=_BOT, buttons=_cancel_row()),
            )
            return
        chat_id, bot_id, title = resolved
        # Fetch the current caption for the preview (best-effort).
        preview = ""
        try:
            live = await client.get_messages(chat_id, tg_message_id)
            preview = (getattr(live, "caption", None)
                       or getattr(live, "text", None) or "")
            preview = str(preview)
        except Exception as exc:  # noqa: BLE001 — preview is optional
            log.debug("senku.postedit.preview_failed", error=str(exc))
        # Lock the chat and stash the resolved target for the mode step.
        await redis.set(
            f"{_LOCK_PREFIX}{message.chat.id}", str(tg_message_id), nx=True, ex=900,
        )
        await arm_reply(
            redis, message.chat.id, _STATE_EDIT,
            target_chat_id=chat_id, bot_id=bot_id, tg_message_id=tg_message_id,
        )
        screen = card(
            V.postedit_choose(preview),
            bot_name=_BOT,
            buttons=_choose_buttons(),
        )
        await send_screen(client, message.chat.id, screen)
        try:
            await message.delete()
        except Exception:  # noqa: BLE001 — cosmetic
            pass
        log.info("senku.postedit.resolved", chat=chat_id, mid=tg_message_id,
                 tracked=bot_id is not None, admin=message.from_user.id)

    async def _consume_edit(message: Message, redis, data: dict) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        lock_key = f"{_LOCK_PREFIX}{message.chat.id}"
        try:
            chat_id = int(data.get("target_chat_id"))
            tg_message_id = int(data.get("tg_message_id"))
            bot_id = int(data["bot_id"]) if data.get("bot_id") is not None else None
            mode = str(data.get("mode") or "caption")
        except (TypeError, ValueError):
            await disarm_reply(redis, message.chat.id)
            await redis.delete(lock_key)
            return
        prompt_msg_id = data.get("prompt_msg_id")

        if mode == "buttons":
            # Validate BEFORE consuming the flow, so a malformed line just nudges
            # and leaves the marker armed for another try (no working card).
            try:
                button_data = _parse_button_lines(text)
            except ValueError as exc:
                await message.reply_text(f"⚠️ {html.escape(str(exc))}")
                return
            await disarm_reply(redis, message.chat.id)
            await redis.delete(lock_key)

            async def _do_buttons():
                return await _edit_buttons(
                    client, container, chat_id=chat_id, bot_id=bot_id,
                    tg_message_id=tg_message_id, button_data=button_data)

            ok, _ = await _finish_edit_ui(
                message, redis, mode="buttons", prompt_msg_id=prompt_msg_id,
                do_edit=_do_buttons, chat_id=chat_id, bot_id=bot_id,
                tg_message_id=tg_message_id)
            log.info("senku.postedit.consumed", chat=chat_id, mid=tg_message_id,
                     mode=mode, admin=message.from_user.id, ok=ok)
            return

        # ── Caption mode ──
        from kurosoden.shared.settings_ui import parse_user_markup

        await disarm_reply(redis, message.chat.id)
        await redis.delete(lock_key)
        new_caption = parse_user_markup(message)

        async def _do_caption():
            return await _edit_caption(
                client, container, message, chat_id=chat_id, bot_id=bot_id,
                tg_message_id=tg_message_id, new_caption=new_caption)

        ok, _ = await _finish_edit_ui(
            message, redis, mode="caption", prompt_msg_id=prompt_msg_id,
            do_edit=_do_caption, chat_id=chat_id, bot_id=bot_id,
            tg_message_id=tg_message_id)
        log.info("senku.postedit.consumed", chat=chat_id, mid=tg_message_id,
                 mode=mode, admin=message.from_user.id, ok=ok)
