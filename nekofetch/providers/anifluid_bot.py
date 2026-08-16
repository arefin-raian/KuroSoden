"""@AniFluidbot userbot probe — info-card IMAGE fallback.

Used only when the whole metadata chain (AniList → datasets → Jikan → Kitsu)
and @acutebot have failed to supply an info-card image. AniFluid renders a card
image that can't be fetched from any API, so we drive it through the userbot and
download the picture.

Owner notes baked in:
* The command is a plain title search ("Shadows House") — AniFluid replies with a
  card. Its layout differs from @acutebot and gives NO romaji title, so this tier
  is treated as IMAGE-first: the downloaded picture is the payload; any caption
  fields (synopsis / score / episodes) are best-effort extras.
* The image needs a moment to render — the first snapshot often shows a
  placeholder ("image failed to load"). We therefore wait ``_RENDER_WAIT`` seconds
  and re-fetch the message before downloading, so we grab the finished picture.

Everything is best-effort and defensive: any hiccup returns ``None`` (never
raises), so the tier composes cleanly as the last image fallback and can't break
the request flow. NOTE: built without a live probe (the dev environment couldn't
reach Telegram); the send/tap/caption parsing is modelled on the @acutebot flow
and should be confirmed against @AniFluidbot's real replies on the server.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from nekofetch.core.logging import get_logger

log = get_logger(__name__)

_BOT_USERNAME = "AniFluidbot"

_WAIT_INITIAL = 2.0        # first poll delay
_POLL_INTERVAL = 0.7
_POLL_TIMEOUT_REPLY = 15.0  # window for AniFluid's first reply
_RENDER_WAIT = 6.0          # owner: wait ≥5 s for the image to render before download
_POLL_TIMEOUT_CARD = 45.0   # window for a tapped menu → card edit


async def fetch_image_from_anifluid(
    title_query: str,
    pool: object,  # UserbotPool
    photo_dir: str | None = None,
    *,
    on_step: Any | None = None,
) -> dict | None:
    """Fetch an info-card IMAGE (and best-effort caption fields) from AniFluid.

    Returns a flat dict (``title``, ``poster_url`` = local image path, and
    optional ``synopsis`` / ``score`` / ``episode_count`` / ``genres``) or
    ``None`` when AniFluid doesn't reply, has no image, or the tier is unusable.
    """
    try:
        from nekofetch.sources.telegram.userbot import UserbotPool, is_transport_error

        if not isinstance(pool, UserbotPool):
            return None
        return await pool.execute(
            lambda c: _do_fetch(c, title_query, photo_dir, on_step),
            retries=2,
            retry_on=is_transport_error,
            max_attempts=3,  # same courtesy cap as @acutebot
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("anifluid.fetch.failed", title=title_query, error=str(exc))
        return None


async def _do_fetch(
    client: "Any", title_query: str, photo_dir: str | None, on_step: Any | None,
) -> dict | None:
    _trace(on_step, f"send {title_query!r}")
    sent = await client.send_message(_BOT_USERNAME, title_query)
    await asyncio.sleep(_WAIT_INITIAL)
    boundary = sent.id

    reply = await _poll_for_new(client, boundary, _POLL_TIMEOUT_REPLY)
    if reply is None:
        return None

    # If AniFluid answered with a result menu (inline buttons, no photo yet), tap
    # the first option and wait for the card. A direct card skips this.
    card = reply
    if not getattr(reply, "photo", None) and _has_buttons(reply):
        card = await _tap_first_and_wait(client, reply, on_step) or reply

    # The picture needs a beat to render — wait, then re-fetch the freshest
    # snapshot so we don't grab the "image failed to load" placeholder.
    await asyncio.sleep(_RENDER_WAIT)
    try:
        card = await client.get_messages(_BOT_USERNAME, card.id) or card
    except Exception as exc:  # noqa: BLE001
        log.debug("anifluid.refetch.failed", error=str(exc))

    poster = await _maybe_download_photo(client, card, title_query, photo_dir)
    if poster is None:
        # No image is the whole point of this tier — nothing to offer.
        return None

    meta = _parse_caption(card, title_query)
    meta["poster_url"] = poster
    meta["_source"] = "anifluid"
    _trace(on_step, f"image saved {poster}")
    log.info("anifluid.hit", title=title_query)
    return meta


# ── helpers ───────────────────────────────────────────────────────────────────

def _trace(on_step: Any | None, msg: str) -> None:
    if on_step is not None:
        try:
            on_step(msg)
        except Exception:  # noqa: BLE001
            pass


def _is_from_anifluid(msg: Any) -> bool:
    frm = getattr(msg, "from_user", None)
    if frm is not None and (getattr(frm, "username", "") or "").lower() == \
            _BOT_USERNAME.lower():
        return True
    chat = getattr(msg, "chat", None)
    return bool(chat and (getattr(chat, "username", "") or "").lower() ==
                _BOT_USERNAME.lower())


def _has_buttons(msg: Any) -> bool:
    rm = getattr(msg, "reply_markup", None)
    return bool(rm and getattr(rm, "inline_keyboard", None))


async def _poll_for_new(client: Any, after_id: int, timeout_s: float) -> Any | None:
    """Return the first AniFluid message with id > ``after_id`` (with a photo or
    buttons), or ``None`` on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = after_id
    while True:
        try:
            async for msg in client.get_chat_history(_BOT_USERNAME, limit=20):
                if not _is_from_anifluid(msg) or msg.id <= last:
                    continue
                last = max(last, msg.id)
                if getattr(msg, "photo", None) or _has_buttons(msg) or \
                        (msg.caption or msg.text):
                    return msg
        except Exception as exc:  # noqa: BLE001
            log.debug("anifluid.poll.failed", error=str(exc))
        if asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(_POLL_INTERVAL)


async def _tap_first_and_wait(client: Any, menu: Any, on_step: Any | None) -> Any | None:
    """Tap the first inline button and wait for the menu to become a card."""
    try:
        button = menu.reply_markup.inline_keyboard[0][0]
        data = getattr(button, "callback_data", None)
        if data is None:
            return None
        await client.request_callback_answer(
            chat_id=menu.chat.id, message_id=menu.id, callback_data=data, timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("anifluid.tap.failed", error=str(exc))
        return None
    # AniFluid usually edits the menu in place into the card.
    deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT_CARD
    while True:
        try:
            current = await client.get_messages(_BOT_USERNAME, menu.id)
        except Exception:  # noqa: BLE001
            current = None
        if current is not None and getattr(current, "photo", None):
            return current
        if asyncio.get_event_loop().time() >= deadline:
            return current
        await asyncio.sleep(_POLL_INTERVAL)


async def _maybe_download_photo(
    client: Any, msg: Any, title_query: str, photo_dir: str | None,
) -> str | None:
    if not photo_dir or not getattr(msg, "photo", None):
        return None
    out = Path(photo_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in title_query if c.isalnum() or c in (" ", "-", "_")).strip()
    safe = safe.replace(" ", "_")[:64] or "anime"
    dest = out / f"anifluid_{safe}.jpg"
    try:
        # file_name= (not file_path=) per this project's pyrogram build.
        downloaded = await client.download_media(msg.photo.file_id, file_name=str(dest))
        if downloaded:
            return str(Path(downloaded))
    except Exception as exc:  # noqa: BLE001
        log.warning("anifluid.photo.download.failed", error=str(exc))
    return None


_EP_RE = re.compile(r"episodes?\s*[:\-]?\s*(\d+)", re.IGNORECASE)
_SCORE_RE = re.compile(r"(?:score|rating)\s*[:\-]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _parse_caption(msg: Any, title_query: str) -> dict:
    """Best-effort caption scrape — AniFluid's format differs and gives no romaji,
    so we only harvest what we can recognise. The image is the real payload."""
    text = (getattr(msg, "caption", None) or getattr(msg, "text", None) or "").strip()
    meta: dict[str, Any] = {"title": title_query}
    if not text:
        return meta
    # First non-empty line is usually the title.
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if first:
        meta["title"] = re.sub(r"[*_`>#]", "", first)[:200] or title_query
    ep = _EP_RE.search(text)
    if ep:
        meta["episode_count"] = int(ep.group(1))
    sc = _SCORE_RE.search(text)
    if sc:
        meta["score"] = sc.group(1)
    meta["synopsis"] = text  # keep the whole caption; caller may trim
    return meta
