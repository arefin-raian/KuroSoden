"""Levi side of the interactive filename / caption confirm gate.

The download **worker** (headless) posts a confirm card, arms a chat-scoped reply
marker, and BLOCK-POLLS a Redis flag (see ``nekofetch.services.naming_confirm``).
This module is the bot-side counterpart that consumes the admin's answer and
releases the worker:

* **✅ Use it**  (``levi|nmuse|{job}|{kind}``) — accept the computed default:
  write the ``__use__`` sentinel, clear the awaiting flag, disarm, tidy the card.
* **✏️ Edit**   (``levi|nmedit|{job}|{kind}``) — the marker is already armed, so we
  just nudge the admin to send their corrected text back.
* **text reply** (group=13) — when a marker with state ``levi_confirm_{kind}`` is
  live in this chat, the next text message IS the edit: write it to the value key,
  clear the awaiting flag, disarm, edit the card.

Everything is chat-scoped (via ``channel_reply``), so it works both in a DM with
Levi and from the anonymous Control Center channel. The text consumer sits in its
own handler group (13) so it never collides with the review flow's group=12
magnet/document consumers.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

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
    value_key,
)

log = get_logger(__name__)

_KINDS = {"name", "caption"}


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
        kind = parts[3]
        what = "file name" if kind == "name" else "caption"
        try:
            await q.answer(
                f"Copy the {what} above, edit it, and send it back to me.",
                show_alert=True)
        except Exception:  # noqa: BLE001
            pass

    @client.on_message(filters.text & ~filters.command(["start"]), group=13)
    async def _consume_edit(client: Client, message: Message) -> None:
        """Capture a text reply while a naming/caption marker is armed here."""
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

        text = (message.text or "").strip()
        if not text:
            return

        await _release(redis, job_id, kind, text)
        await _disarm_reply(redis, message.chat.id)

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
