"""v2 user-facing screens — artwork + HTML caption + keyboard per surface.

Pure builders (no Telegram I/O), unit-testable, handlers stay declarative. Every
visible string comes from the centralized catalog (``localization.messages``) —
no raw text here. HTML parse mode, bold-first emphasis, colon-separated fields,
no code styling, a 16:9 artwork (no back-to-back repeats) on every major surface.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from nekofetch.core.constants import BULLET, DOT_ACTIVE, DOT_DONE, DOT_PENDING
from nekofetch.localization.messages import PARSE_MODE, M, t
from nekofetch.ui.artwork import pick_artwork
from nekofetch.ui.components import cb

# ── status glyphs (lifecycle / lists) — shared design language ──
DONE, CURRENT, PENDING = DOT_DONE, DOT_ACTIVE, DOT_PENDING

# Lifecycle order; labels resolve from the catalog at render time.
_LIFECYCLE_KEYS = [
    M.LC_REQUESTED, M.LC_PENDING, M.LC_SOURCE_ASSIGNED, M.LC_DOWNLOADING,
    M.LC_PROCESSING_META, M.LC_EXTRACTING_SUBS, M.LC_WATERMARK,
    M.LC_UPLOADING, M.LC_PUBLISHED, M.LC_COMPLETED,
]


# Telegram hard limits. The photo-caption budget is kept a touch under 1024 so a
# trailing entity or stray character can never tip a send over the edge.
CAPTION_LIMIT = 1000
MESSAGE_LIMIT = 4096

# ── Artwork file_id cache ────────────────────────────────────────────────────
# Sending a card uploads the artwork bytes to Telegram over MTProto EVERY time —
# a big latency hit on a small pool of recurring images that get reused hundreds
# of times per run. After the first upload of a given local path / URL we cache
# the returned ``file_id`` and pass THAT on later sends: Telegram serves the
# already-hosted photo instantly, no re-upload. Keyed by the exact path/URL
# string; entries never go stale (a file_id for uploaded media is durable).
_FILE_ID_CACHE: dict[str, str] = {}


def _cached_photo_arg(photo_arg: str | None) -> str | None:
    """Return a cached ``file_id`` for this asset if we've uploaded it before."""
    if not photo_arg:
        return photo_arg
    return _FILE_ID_CACHE.get(photo_arg, photo_arg)


def _remember_file_id(photo_arg: str | None, msg) -> None:
    """Stash the ``file_id`` Telegram assigned so the next send skips the upload."""
    if not photo_arg or photo_arg in _FILE_ID_CACHE:
        return
    # Don't re-cache a value that already IS a file_id (idempotent).
    if photo_arg.startswith(("http://", "https://")) or "/" in photo_arg or "\\" in photo_arg:
        photo = getattr(msg, "photo", None)
        file_id = getattr(photo, "file_id", None)
        if file_id:
            _FILE_ID_CACHE[photo_arg] = file_id


@dataclass(slots=True)
class Screen:
    caption: str
    image: str | Path | None = None  # local path or HTTP(S) URL
    keyboard: InlineKeyboardMarkup | None = None
    parse_mode: ParseMode = PARSE_MODE


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def visible_len(html_text: str) -> int:
    """Approximate the length Telegram counts — tags become entities and don't
    count toward the caption/message limit, so measure the stripped text."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text or "")))


def _truncate_html(html_text: str, limit: int) -> str:
    """Shorten ``html_text`` so its *visible* length fits ``limit``.

    Drops whole lines from the end (keeping HTML tags balanced, since each line is
    self-contained) until it fits, then appends an ellipsis. A blunt last-resort
    safeguard — callers should budget content first; this only guarantees we never
    exceed the hard limit.
    """
    if visible_len(html_text) <= limit:
        return html_text
    lines = html_text.split("\n")
    while lines and visible_len("\n".join(lines)) > limit - 1:
        lines.pop()
    out = "\n".join(lines).rstrip()
    return (out + " …") if out else html_text[:limit]


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(lbl, callback_data=data) for lbl, data in row]
         for row in rows]
    )


def _field(key: str, value: str) -> str:
    """Clean ``Label : value`` row — bold label (from catalog), plain value."""
    return f"<b>{t(key)}</b> : {_esc(value)}"


def lifecycle_labels() -> list[str]:
    return [t(k) for k in _LIFECYCLE_KEYS]


# ── Generic card builder ─────────────────────────────────────────────────────
#
# One grammar for every character-bot surface: a caption (already-authored HTML,
# so callers keep their own voice), an image that is *never* omitted, and a
# keyboard. Kuro Sōden routes all of Lelouch's screens through this so forward →
# Back always lands on a full card — image + info + keyboard — not a bare image
# or a stripped text bubble.

def card(
    caption: str,
    *,
    image: "str | Path | None" = None,
    bot_name: str | None = None,
    buttons: list[list[tuple[str, str]]] | None = None,
    url_buttons: list[list[tuple[str, str]]] | None = None,
) -> Screen:
    """Build a :class:`Screen` in the house grammar.

    ``caption`` is finished HTML (the caller owns its voice/copy). ``image`` may
    be a URL string (e.g. a per-anime TMDB backdrop) or a local ``Path``; when
    ``None`` we fall back to the bot's recurring character art so the card is
    never imageless. ``buttons`` are ``(label, callback_data)`` rows; the rarer
    ``url_buttons`` carry ``(label, url)`` rows for external links (join,
    open-in-channel). Rows are rendered in order: url rows first, then callbacks.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for row in (url_buttons or []):
        rows.append([InlineKeyboardButton(lbl, url=target) for lbl, target in row])
    for row in (buttons or []):
        rows.append([InlineKeyboardButton(lbl, callback_data=data)
                     for lbl, data in row])
    keyboard = InlineKeyboardMarkup(rows) if rows else None
    return Screen(
        caption=_truncate_html(caption, CAPTION_LIMIT),
        image=image if image is not None else pick_artwork(bot_name),
        keyboard=keyboard,
    )


# ── User screens ───────────────────────────────────────────────────────────

def welcome(user_name: str, *, is_staff: bool = False, is_admin: bool = False,
           bot_name: str | None = None) -> Screen:
    name = _esc(user_name) or "there"
    caption = "\n\n".join([
        t(M.WELCOME_TITLE, name=name),
        t(M.WELCOME_BODY),
        t(M.WELCOME_LIBRARY),
    ])
    rows = [[(t(M.BTN_REQUEST_ANIME), cb("req", "new")),
             (t(M.BTN_MY_REQUESTS), cb("req", "mine", 0))]]
    if is_staff or is_admin:
        rows.append([(t(M.BTN_BATCH), cb("batch", "new")),
                     (t(M.BTN_REVIEW_REQUESTS), cb("staff", "requests", 0))])
        rows.append([(t(M.ADMIN_BTN_QUEUE), cb("queue", "view", 0))])
    if is_admin:
        rows.append([(t(M.ADMIN_BTN_PANEL), cb("admin", "home"))])
    return Screen(caption=caption, image=pick_artwork(bot_name), keyboard=_kb(rows))


def my_requests(user_name: str, requests: list[dict],
                *, bot_name: str | None = None) -> Screen:
    name = _esc(user_name) or "you"
    lines = [t(M.MYREQ_TITLE, name=name), ""]
    if not requests:
        lines.append(t(M.MYREQ_EMPTY))
    else:
        width = min(28, max((len(r["title"]) for r in requests), default=0))
        for r in requests:
            lines.append(t(M.MYREQ_ROW, title=_esc(r["title"])[:28].ljust(width),
                           status=_esc(r["status"])))
        ready = sum(1 for r in requests if "ready" in r["status"].lower())
        prog = sum(1 for r in requests if any(
            k in r["status"].lower() for k in ("process", "queue", "download", "upload")))
        wait = sum(1 for r in requests if "need" in r["status"].lower())
        lines += ["", t(M.MYREQ_SUMMARY, total=len(requests), ready=ready,
                        progress=prog, waiting=wait)]
    kb = _kb([[(t(M.BTN_REQUEST_ANIME), cb("req", "new"))],
              [(t(M.BTN_BACK), cb("home"))]])
    return Screen(caption="\n".join(lines), image=pick_artwork(bot_name), keyboard=kb)


def ask_title(*, bot_name: str | None = None) -> Screen:
    return Screen(caption=t(M.ASK_TITLE), image=pick_artwork(bot_name),
                  keyboard=_kb([[(t(M.BTN_BACK), cb("home"))]]))


def confirm_franchise(
    media_data: dict,
    backdrop_path: str | None = None,
    *,
    namespace: str = "series",
) -> Screen:
    """Rich franchise confirmation card — the centerpiece of Phase 1.

    ``media_data`` is a dict shaped like the expanded AnilistMedia fields:
    title, year, format, status, score, studio, genres, synopsis,
    franchise_episodes, franchise_seasons, franchise_movies, franchise_ovas,
    franchise_specials, relations (list of dicts each with relation, format,
    episodes, titles), anilist_url, cover_url, banner_url

    ``namespace`` prefixes the yes/no callbacks (``{namespace}_yes|…`` /
    ``{namespace}_no``) so a second flow on the same client (e.g. Lelouch's
    owner ``/redo``) can reuse this card without colliding with the request
    flow's ``series_yes|`` handler. Defaults to the original ``series``.
    """
    english = _esc(media_data.get("english") or media_data.get("title", "Unknown"))
    romaji = media_data.get("romaji")
    year = media_data.get("year")

    # ── header (inside a blockquote): 🎬 Title (year) ❘ romaji ──
    head_inner = f"🎬 <b>{english}"
    if year:
        head_inner += f" ({_esc(str(year))})"
    if romaji and romaji.casefold() != (media_data.get("english") or "").casefold():
        head_inner += f" ❘</b> <i>{_esc(romaji)}</i>"
    else:
        head_inner += "</b>"
    rows = [f"<blockquote>{head_inner}</blockquote>", ""]

    # ── metadata fields: "<b>Label :</b> value" ──
    def kv(label_key: str, value: str) -> str:
        return f"<b>{t(label_key)} :</b> {_esc(value)}"

    if media_data.get("format"):
        rows.append(kv(M.F_TYPE, media_data["format"]))
    if media_data.get("status"):
        rows.append(kv(M.F_STATUS, media_data["status"]))
    if media_data.get("score"):
        rows.append(f"<b>{t(M.F_RATING)} :</b> {media_data['score']}/10")
    if media_data.get("studio"):
        rows.append(kv(M.FIELD_STUDIO, media_data["studio"]))
    if media_data.get("genres"):
        rows.append(kv(M.F_GENRES, t(M.SEP_DOT).join(media_data["genres"][:5])))

    # ── synopsis inside an expandable blockquote (Read More button if clipped) ──
    synopsis = (media_data.get("synopsis") or "").strip()
    synopsis = html.unescape(re.sub(r"<[^>]+>", "", synopsis)).strip()
    read_more_url: str | None = None
    if synopsis:
        if len(synopsis) > 600:
            synopsis = _esc(synopsis[:600].rsplit(" ", 1)[0]) + "…"
            read_more_url = media_data.get("synopsis_url") or media_data.get("anilist_url")
        else:
            synopsis = _esc(synopsis)
        syn_label = t(M.FIELD_SYNOPSIS)
        rows += ["", f"<blockquote expandable><b>{syn_label} :</b> {synopsis}</blockquote>"]

    # ── franchise content (computed from the full relation graph) ──
    ep_total = media_data.get("franchise_episodes")
    units = (
        (media_data.get("franchise_seasons", 0), M.UNIT_SEASONS),
        (media_data.get("franchise_movies", 0), M.UNIT_MOVIES),
        (media_data.get("franchise_ovas", 0), M.UNIT_OVAS),
        (media_data.get("franchise_onas", 0), M.UNIT_ONAS),
        (media_data.get("franchise_specials", 0), M.UNIT_SPECIALS),
        (media_data.get("franchise_spinoffs", 0), M.UNIT_SPINOFFS),
    )
    breakdown_bits = []
    for n, key in units:
        if n and n > 0:
            word = t(key)
            if n == 1 and word.endswith("s"):   # 1 season, 1 OVA, 1 movie
                word = word[:-1]
            bit = f"{n} {word}"
            if key == M.UNIT_SEASONS and ep_total:
                bit += f" ({ep_total} {t(M.UNIT_EPS)})"
            breakdown_bits.append(bit)
    if breakdown_bits:
        rows += ["", t(M.FRANCHISE_CONTENT) + " " + f" {BULLET} ".join(breakdown_bits)]

    rows += ["", t(M.CONFIRM_QUESTION)]
    caption = _truncate_html("\n".join(rows), CAPTION_LIMIT)

    kb_rows: list[list[InlineKeyboardButton]] = []
    if read_more_url:
        kb_rows.append([InlineKeyboardButton(t(M.BTN_READ_MORE), url=read_more_url)])
    kb_rows.append([
        InlineKeyboardButton(t(M.BTN_SERIES_YES),
                             callback_data=cb(f"{namespace}_yes", str(media_data.get("anilist_id", "")))),
        InlineKeyboardButton(t(M.BTN_SERIES_NO), callback_data=cb(f"{namespace}_no")),
    ])
    kb = InlineKeyboardMarkup(kb_rows)

    # Image priority: TMDB backdrop → AniList banner → cover → random local art.
    image: str | Path | None = None
    if backdrop_path:
        image = backdrop_path  # URL string, sent directly to send_photo
    elif media_data.get("banner_url"):
        image = media_data["banner_url"]
    elif media_data.get("cover_url"):
        image = media_data["cover_url"]
    return Screen(caption=caption, image=image or pick_artwork(), keyboard=kb)


def choose_version(query: str, versions: list[dict],
                  *, bot_name: str | None = None,
                  namespace: str = "ver", neither_ns: str = "series") -> Screen:
    rows = [t(M.VERSION_HEADER, query=_esc(query)), ""]
    width = min(24, max((len(v["title"]) for v in versions), default=0))
    for v in versions:
        meta = t(M.SEP_DOT).join(str(x) for x in (
            v.get("format"), v.get("year"),
            f"{v['episodes']} eps" if v.get("episodes") else None) if x)
        rows.append(f"{_esc(v['title'])[:24].ljust(width)} :  <i>{_esc(meta)}</i>")
    btns = [[(v["title"][:32], cb(f"{namespace}_pick", v.get("id", i)))]
            for i, v in enumerate(versions)]
    # 'Both' folds every adaptation into one combined request; 'Neither' restarts.
    btns.append([(t(M.BTN_VERSION_BOTH), cb(f"{namespace}_pick_both")),
                 (t(M.BTN_VERSION_NEITHER), cb(f"{neither_ns}_no"))])
    return Screen(caption="\n".join(rows), image=pick_artwork(bot_name), keyboard=_kb(btns))


def retry_title(*, bot_name: str | None = None) -> Screen:
    return Screen(caption=t(M.RETRY_TITLE), image=pick_artwork(bot_name),
                  keyboard=_kb([[(t(M.BTN_BACK), cb("home"))]]))


def _franchise_breakdown(franchise: dict | None) -> str | None:
    """A compact ``2 seasons · 1 movie · 3 OVAs``-style line, or ``None``.

    Only the parts that actually exist are shown — a single-cour TV request
    reads ``single entry`` rather than a wall of zeroes.
    """
    if not franchise:
        return None
    parts: list[tuple[int, str]] = [
        (int(franchise.get("franchise_seasons") or 0), "season"),
        (int(franchise.get("franchise_movies") or 0), "movie"),
        (int(franchise.get("franchise_ovas") or 0), "OVA"),
        (int(franchise.get("franchise_onas") or 0), "ONA"),
        (int(franchise.get("franchise_specials") or 0), "special"),
    ]
    bits = [f"{n} {word}{'s' if n != 1 else ''}" for n, word in parts if n]
    return "  ·  ".join(bits) if bits else None


def request_received(user_name: str, title: str, queue_pos: int | None = None,
                     *, bot_name: str | None = None,
                     code: str | None = None, requester_id: int | None = None,
                     requested_at: str | None = None,
                     franchise: dict | None = None,
                     image: "str | Path | None" = None) -> Screen:
    """The requester's receipt card — richer when the extra fields are supplied.

    ``code`` / ``requester_id`` / ``requested_at`` / ``franchise`` are optional so
    the two existing callers keep working; when present the card shows the request
    code, who asked (name + id), when, a summarized franchise breakdown, episode
    count, and queue position.
    """
    header_lines = [f"♟️ <b>Request Accepted</b>\n"]
    header_lines.append(f"<b>{_esc(title)}</b>")
    if code:
        header_lines.append(f"<code>{_esc(code)}</code>")

    detail_parts: list[str] = []
    if requester_id is not None:
        detail_parts.append(f"<b>By</b> : {_esc(user_name) or 'user'} "
                            f"(<code>{requester_id}</code>)")
    if requested_at:
        detail_parts.append(f"<b>Requested</b> : {_esc(requested_at)}")
    breakdown = _franchise_breakdown(franchise)
    if breakdown:
        detail_parts.append(f"<b>Contents</b> : {_esc(breakdown)}")
    if franchise and franchise.get("franchise_episodes"):
        detail_parts.append(f"<b>Episodes</b> : {_esc(str(franchise['franchise_episodes']))}")

    if detail_parts:
        header_lines.append("")
        header_lines.extend(detail_parts)

    top_block = "<blockquote>" + "\n".join(header_lines) + "</blockquote>"

    status_text = t(M.VALUE_QUEUED)
    if queue_pos is not None:
        status_text += f" · Position #{queue_pos}"
    bottom_block = (
        f"<blockquote>{status_text}\n\n"
        f"{t(M.REQ_RECEIVED_BODY)}</blockquote>"
    )

    caption = f"{top_block}\n\n{bottom_block}"
    return Screen(caption=caption,
                  image=image if image is not None else pick_artwork(bot_name),
                  keyboard=_kb([[(t(M.BTN_MY_REQUESTS), cb("req", "mine", 0))]]))


# ── Log channel: one live card per request, edited as state advances ─────────

def log_card(req: dict, *, bot_name: str | None = None) -> Screen:
    """``req`` keys: id, title, requester, source, state, optional substate,
    optional failed/reason/detail, and completed-summary fields."""
    title = _esc(req.get("title", "Unknown"))
    state = req.get("state", t(M.LC_REQUESTED))
    labels = lifecycle_labels()

    if req.get("failed"):
        rows = [t(M.LOG_BLOCKED_TITLE, title=title), "",
                _field(M.F_STUCK_AT, state),
                _field(M.F_REASON, req.get("reason", "unknown")),
                _field(M.F_SOURCE, req.get("source", "—"))]
        if req.get("detail"):
            rows += ["", f"<blockquote expandable>{_esc(req['detail'])}</blockquote>"]
        kb = _kb([[(t(M.BTN_RETRY), cb("log_retry", req.get("id", ""))),
                   (t(M.BTN_REASSIGN), cb("log_reassign", req.get("id", ""))),
                   (t(M.BTN_DISMISS), cb("log_dismiss", req.get("id", "")))]])
        return Screen(caption="\n".join(rows), image=pick_artwork(bot_name), keyboard=kb)

    if state == t(M.LC_COMPLETED):
        rows = [t(M.LOG_COMPLETED_TITLE, title=title), ""]
        for key, field_key in ((("seasons"), M.F_SEASONS), ("qualities", M.F_QUALITIES),
                                ("episodes", M.F_EPISODES), ("source", M.F_SOURCE),
                                ("took", M.F_TOOK)):
            if req.get(key):
                rows.append(_field(field_key, str(req[key])))
        return Screen(caption="\n".join(rows), image=pick_artwork(bot_name))

    sub = f"  {t(M.SEP_DOT)}  {_esc(req['substate'])}" if req.get("substate") else ""
    rows = [t(M.LOG_PROGRESS_TITLE, title=title), "",
            _field(M.F_REQUEST, f"#{req.get('id', '—')}"),
            _field(M.F_BY, req.get("requester", "—")),
            _field(M.F_SOURCE, req.get("source", "—")),
            _field(M.F_NOW, f"{state}{sub}"), ""]
    cur_idx = labels.index(state) if state in labels else 0
    for i, step in enumerate(labels):
        glyph = DONE if i < cur_idx else (CURRENT if i == cur_idx else PENDING)
        rows.append(f"{glyph}  {'<b>' + step + '</b>' if i == cur_idx else step}")
    return Screen(caption="\n".join(rows), image=pick_artwork(bot_name))


async def show(
    client: Client,
    src_msg: Message,
    caption: str,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    image: str | Path | None = None,
) -> Message:
    """Render an admin screen in place of ``src_msg`` with rotating artwork.

    Works whether ``src_msg`` is a text or a photo message — it deletes the old
    one and sends a fresh photo, sidestepping Telegram's "can't edit a media
    message's text" limitation that plagues callback-driven panels.
    """
    screen = Screen(caption=caption, image=image or pick_artwork(), keyboard=keyboard)
    return await send_screen(client, src_msg.chat.id, screen, old_msg=src_msg)


def message_ref(client: Client, chat_id: int, message_id: int | str | None):
    """A lightweight ``old_msg`` stand-in for a prompt card held only by id.

    Text/media capture handlers usually have the *user's* message, not the prompt
    ``Message`` they want to update. Store ``prompt_msg_id`` + ``prompt_chat_id``
    in FSM at arm-time, then pass ``message_ref(client, chat_id, msg_id)`` as
    ``old_msg`` to :func:`send_screen`, which edits it in place by id (falling
    back to delete-and-resend via the ``delete`` shim only when editing fails).
    Returns ``None`` when the id is missing/invalid so callers can send fresh.
    """
    if not str(message_id or "").isdigit():
        return None

    async def _delete() -> None:
        try:
            await client.delete_messages(chat_id, int(message_id))
        except Exception:  # noqa: BLE001 — replacement cleanup is best-effort
            pass

    from types import SimpleNamespace
    return SimpleNamespace(
        id=int(message_id), chat=SimpleNamespace(id=chat_id), delete=_delete,
    )


async def send_screen(
    client: Client,
    chat_id: int,
    screen: Screen,
    old_msg: Message | None = None,
) -> Message:
    """Render a Screen as a photo card, editing ``old_msg`` IN PLACE when given.

    ``screen.image`` can be a local ``Path`` or an HTTP(S) URL (both work with
    Pyrogram's ``send_photo``/``edit_message_media``). With no image it falls back
    to a plain text message.

    When ``old_msg`` is supplied we EDIT that message in place — the card keeps
    its position in the chat, the image isn't re-uploaded, and there's no
    delete-then-resend flicker. This is the house behaviour for every flow: ask
    for a value / tap a button → the SAME card updates. We only fall back to
    send-a-new-card-then-delete-the-old when an in-place edit is impossible:
      * the message type must change (text card ↔ photo card), which Telegram
        can't edit across; or
      * Telegram rejects the edit (message too old / not ours / identical) —
        MessageNotModified is treated as success (the card is already right).
    """
    caption = screen.caption or ""
    photo = screen.image
    photo_arg = (str(photo) if isinstance(photo, Path) else photo) if photo else None
    fitted = (caption if visible_len(caption) <= CAPTION_LIMIT
              else _truncate_html(caption, CAPTION_LIMIT)) if photo else \
             _truncate_html(caption, MESSAGE_LIMIT)

    async def _send_photo(**kw):
        for _ in range(3):
            try:
                return await client.send_photo(chat_id, **kw)
            except FloodWait as fw:
                await asyncio.sleep(fw.value + 1)
        return await client.send_photo(chat_id, **kw)

    async def _send_text(text, **kw):
        for _ in range(3):
            try:
                return await client.send_message(chat_id, text, **kw)
            except FloodWait as fw:
                await asyncio.sleep(fw.value + 1)
        return await client.send_message(chat_id, text, **kw)

    # ── In-place edit path (preferred whenever we have a message to reuse) ──
    if old_msg is not None:
        edited = await _try_edit_in_place(client, old_msg, screen, photo_arg, fitted)
        if edited is not None:
            return edited
        # Edit impossible (type change / rejected) — fall through to send-new,
        # then delete the stale card so we never leave two on screen.

    if photo_arg:
        send_arg = _cached_photo_arg(photo_arg)
        msg = await _send_photo(
            photo=send_arg, caption=fitted,
            parse_mode=screen.parse_mode, reply_markup=screen.keyboard,
        )
        _remember_file_id(photo_arg, msg)
    else:
        msg = await _send_text(
            fitted, parse_mode=screen.parse_mode, reply_markup=screen.keyboard,
        )

    if old_msg is not None:
        try:
            await old_msg.delete()
        except Exception:  # noqa: BLE001
            pass
    return msg


async def _try_edit_in_place(client, old_msg, screen, photo_arg, fitted):
    """Edit ``old_msg`` to match ``screen``; return a truthy result or None.

    Uses ``client.edit_message_*`` keyed by (chat_id, message_id) so it works
    whether ``old_msg`` is a real Pyrogram ``Message`` (a live card / q.message)
    or a lightweight id-only reference (a prompt held across a text-capture hop).
    Returns the edited message (or the ref) on success — including
    MessageNotModified, meaning the card already shows this exact content — or
    ``None`` when an in-place edit is impossible (text↔photo type change, message
    too old, not ours) so the caller sends a fresh card instead.
    """
    from pyrogram.errors import MessageNotModified
    from pyrogram.types import InputMediaPhoto

    chat_id = getattr(getattr(old_msg, "chat", None), "id", None)
    message_id = getattr(old_msg, "id", None)
    if chat_id is None or message_id is None:
        return None

    # A card built by card() is always a photo message; a plain-text screen is
    # not. We can only edit within the same kind. For a real Message we can read
    # `.photo`; for an id-only ref we assume it matches the screen we're painting
    # (these refs are only ever created for photo prompt cards).
    is_ref = not hasattr(old_msg, "edit_media")
    old_has_photo = True if is_ref else bool(getattr(old_msg, "photo", None))
    want_photo = bool(photo_arg)
    if old_has_photo != want_photo:
        return None

    async def _do_edit():
        if want_photo:
            edited = await client.edit_message_media(
                chat_id, message_id,
                InputMediaPhoto(_cached_photo_arg(photo_arg), caption=fitted,
                                parse_mode=screen.parse_mode),
                reply_markup=screen.keyboard,
            )
            _remember_file_id(photo_arg, edited)
            return edited
        return await client.edit_message_text(
            chat_id, message_id, fitted,
            parse_mode=screen.parse_mode, reply_markup=screen.keyboard,
        )

    try:
        return await _do_edit()
    except MessageNotModified:
        return old_msg  # already correct — success
    except FloodWait as fw:
        await asyncio.sleep(fw.value + 1)
        try:
            return await _do_edit()
        except Exception:  # noqa: BLE001 — give up on in-place, caller sends fresh
            return None
    except Exception:  # noqa: BLE001 — too old / not ours / etc. → caller sends fresh
        return None


