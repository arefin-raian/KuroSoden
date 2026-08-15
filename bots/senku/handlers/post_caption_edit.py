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

import html

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
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
from nekofetch.ui.screens import card, send_screen
from nekofetch.services.bot_render import build_audio_keyboard
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
    # caption that subscribers never saw.
    try:
        live = await client.get_messages(chat_id, tg_message_id)
        if any(getattr(live, kind, None)
               for kind in ("photo", "video", "animation", "document")):
            await client.edit_message_caption(
                chat_id, tg_message_id, new_caption, parse_mode=ParseMode.HTML,
            )
        else:
            await client.edit_message_text(
                chat_id, tg_message_id, new_caption, parse_mode=ParseMode.HTML,
            )
    except Exception as exc:  # noqa: BLE001 — keep durable data unchanged
        log.warning("senku.caption.live_edit_failed", chat=chat_id,
                    mid=tg_message_id, error=str(exc))
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
        await client.edit_message_reply_markup(
            chat_id, tg_message_id, reply_markup=markup,
        )
    except Exception as exc:  # noqa: BLE001 — keep durable data unchanged
        log.warning("senku.buttons.live_edit_failed", chat=chat_id,
                    mid=tg_message_id, error=str(exc))
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


# ── Screens ──────────────────────────────────────────────────────────────────

def _cancel_row() -> list[list[tuple[str, str]]]:
    return [[(V.BTN_CANCEL, cb(_BOT, "postedit", "cancel"))]]


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

        if action in {"caption", "buttons"}:
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
            await arm_reply(
                redis, q.message.chat.id, _STATE_EDIT,
                chat_id=data.get("chat_id"), bot_id=data.get("bot_id"),
                tg_message_id=data.get("tg_message_id"), mode=action,
            )
            await q.answer()
            prompt = (V.POSTEDIT_ASK_BUTTONS if action == "buttons"
                      else V.POSTEDIT_ASK_CAPTION)
            await send_screen(
                client, q.message.chat.id,
                card(prompt, bot_name=_BOT, buttons=_cancel_row()),
                old_msg=q.message,
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
            await _consume_edit(message, redis, data)
            return

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
            chat_id=chat_id, bot_id=bot_id, tg_message_id=tg_message_id,
        )
        screen = card(
            V.postedit_choose(preview),
            bot_name=_BOT,
            buttons=[
                [(V.BTN_POSTEDIT_CAPTION, cb(_BOT, "postedit", "caption")),
                 (V.BTN_POSTEDIT_BUTTONS, cb(_BOT, "postedit", "buttons"))],
                [(V.BTN_CANCEL, cb(_BOT, "postedit", "cancel"))],
            ],
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
            chat_id = int(data.get("chat_id"))
            tg_message_id = int(data.get("tg_message_id"))
            bot_id = int(data["bot_id"]) if data.get("bot_id") is not None else None
            mode = str(data.get("mode") or "caption")
        except (TypeError, ValueError):
            await disarm_reply(redis, message.chat.id)
            await redis.delete(lock_key)
            return
        await disarm_reply(redis, message.chat.id)
        await redis.delete(lock_key)

        if mode == "buttons":
            try:
                button_data = _parse_button_lines(text)
            except ValueError as exc:
                await message.reply_text(f"⚠️ {html.escape(str(exc))}")
                return
            ok, result = await _edit_buttons(
                client, container, chat_id=chat_id, bot_id=bot_id,
                tg_message_id=tg_message_id, button_data=button_data,
            )
        else:
            from kurosoden.shared.settings_ui import parse_user_markup
            new_caption = parse_user_markup(message)
            ok, result = await _edit_caption(
                client, container, message, chat_id=chat_id, bot_id=bot_id,
                tg_message_id=tg_message_id, new_caption=new_caption,
            )
        try:
            await message.reply_text(
                ("✅ " if ok else "⚠️ ") + result, parse_mode=ParseMode.HTML,
            )
            await message.delete()
        except Exception:  # noqa: BLE001 — cosmetic cleanup
            pass
        log.info("senku.postedit.consumed", chat=chat_id, mid=tg_message_id,
                 mode=mode, admin=message.from_user.id, ok=ok)
