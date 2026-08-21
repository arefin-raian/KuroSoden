"""Generic live-message edit primitives shared by the post editors.

Both Senku (distribution posts) and Gojo (main-channel + index posts) need the
exact same *live Telegram edit* behaviour, only the durable-persistence targets
differ (``BotContentPost`` vs ``ChannelPost``/``published_post_backups``). This
module owns the one correct implementation of the live edit; each bot's editor
layers its own DB/backup writes on top after a success.

Every primitive here defends the two invariants that have bitten us before
(memory ``telegram-edit-drops-keyboard-and-style``):

  * ``editMessageCaption``/``editMessageText``/``editMessageMedia`` DROP the
    inline keyboard unless it is re-supplied — so we always read the live
    ``reply_markup`` and hand it straight back.
  * ``Message.caption`` is PLAIN text; the styling lives in ``caption_entities``.
    Re-sending the plain text flattens bold/links/spoilers — so on a media swap
    we reconstruct the caption from the ``.html`` accessor.

Each function returns ``(ok, message)``: ``ok`` False means Telegram rejected the
live edit and the caller MUST leave its durable copy unchanged (otherwise a
restore would ship a caption subscribers never saw). ``message`` is a
user-facing default the caller may override once it has persisted.
"""

from __future__ import annotations

import io

from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaPhoto

from nekofetch.core.logging import get_logger

log = get_logger(__name__)

# Telegram's hard message-text limit (captions share it with the photo).
TEXT_LIMIT = 4096

_MEDIA_KINDS = ("photo", "video", "animation", "document")


def _has_media(live) -> bool:
    """True when the live message carries editable media (not a text post)."""
    return any(getattr(live, kind, None) for kind in _MEDIA_KINDS)


async def live_edit_caption(
    client,
    chat_id: int | str,
    tg_message_id: int,
    new_caption: str,
    *,
    text_limit: int = TEXT_LIMIT,
    log_event_prefix: str = "postedit.caption",
) -> tuple[bool, str]:
    """Edit a live post's caption/text, preserving its inline keyboard.

    Fetches the live message to learn whether it is a media post (edit the
    caption) or a text post (edit the text), and to recover the keyboard that
    Telegram would otherwise strip. HTML is the parse mode, so the caller passes
    already-rendered HTML.
    """
    if len(new_caption) > text_limit:
        return False, f"Caption is too long ({len(new_caption)} > {text_limit} chars)."
    try:
        live = await client.get_messages(chat_id, tg_message_id)
        keep_markup = getattr(live, "reply_markup", None)
        if _has_media(live):
            await client.edit_message_caption(
                chat_id, tg_message_id, new_caption, parse_mode=ParseMode.HTML,
                reply_markup=keep_markup,
            )
        else:
            await client.edit_message_text(
                chat_id, tg_message_id, new_caption, parse_mode=ParseMode.HTML,
                reply_markup=keep_markup,
            )
    except Exception as exc:  # noqa: BLE001 — keep the caller's durable data unchanged
        log.warning(f"{log_event_prefix}.live_edit_failed", chat=chat_id,
                    mid=tg_message_id, error=str(exc))
        return False, "Telegram rejected the live edit; the database was left unchanged."
    return True, "Caption updated in the channel."


async def live_edit_buttons(
    client,
    chat_id: int | str,
    tg_message_id: int,
    markup,
    *,
    log_event_prefix: str = "postedit.buttons",
) -> tuple[bool, str]:
    """Replace a live post's inline keyboard with ``markup`` (``None`` clears it).

    The caller builds the markup (distribution audio buttons, main-channel
    Index/Download row, index letter row, …); this only performs the live edit.
    """
    try:
        await client.edit_message_reply_markup(
            chat_id, tg_message_id, reply_markup=markup,
        )
    except Exception as exc:  # noqa: BLE001 — keep the caller's durable data unchanged
        log.warning(f"{log_event_prefix}.live_edit_failed", chat=chat_id,
                    mid=tg_message_id, error=str(exc))
        return False, "Telegram rejected the live button edit; the database was left unchanged."
    return True, "Buttons updated in the channel."


async def live_edit_media(
    client,
    chat_id: int | str,
    tg_message_id: int,
    image_bytes: bytes,
    *,
    filename: str = "image.jpg",
    log_event_prefix: str = "postedit.image",
) -> tuple[bool, str]:
    """Replace a live post's image, preserving its caption styling + keyboard.

    Rejects a text post (nothing to replace). Reconstructs the caption from the
    live ``.html`` so the swap keeps bold/links/spoilers, and re-supplies the
    live keyboard that ``editMessageMedia`` would otherwise drop.
    """
    try:
        live = await client.get_messages(chat_id, tg_message_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"{log_event_prefix}.fetch_failed", chat=chat_id,
                    mid=tg_message_id, error=str(exc))
        return False, "I couldn't read that post to replace its image."
    if not _has_media(live):
        return False, ("That post has no image to replace — edit its caption or "
                       "buttons instead.")
    # ``.html`` reconstructs the styled caption from caption_entities; plain
    # ``caption`` would flatten the formatting on re-send.
    cap_src = getattr(live, "caption", None)
    caption = getattr(cap_src, "html", None) or (str(cap_src) if cap_src else None)
    markup = getattr(live, "reply_markup", None)
    try:
        stream = io.BytesIO(image_bytes)
        stream.name = filename  # pyrogram needs a filename hint on a stream
        media = InputMediaPhoto(stream, caption=caption, parse_mode=ParseMode.HTML)
        await client.edit_message_media(
            chat_id, tg_message_id, media, reply_markup=markup,
        )
    except Exception as exc:  # noqa: BLE001 — keep the caller's durable data unchanged
        log.warning(f"{log_event_prefix}.live_edit_failed", chat=chat_id,
                    mid=tg_message_id, error=str(exc))
        return False, "Telegram rejected the live image edit; the database was left unchanged."
    return True, "Image replaced in the channel."
