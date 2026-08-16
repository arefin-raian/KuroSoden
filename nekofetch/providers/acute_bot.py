"""AcuteBot metadata provider — multi-step userbot flow with AniList verification.

After sending ``/anime <title>`` to @acutebot, the bot replies with:

  Step 1 — a **search-result menu** of candidate titles (always, even for
            exact matches). Each row carries an inline-keyboard button whose
            ``callback_data`` picks that exact candidate.

  Step 2 — after a candidate is tapped, the **info card** itself: a photo
            + ``<Title> | <Alt Title>`` header + the ``‣ Genres / Type /
            Rating / Status / First aired / Last aired / Runtime / No of
            episodes / Synopsis`` fields. Underneath is a single inline
            button labelled "Information" whose URL is
            ``https://anilist.co/anime/<id>-<slug>`` — that ID is the
            canonical **cross-verification**.

We pick the best candidate (exact match → ``title_matches`` fuzzy → "first"),
tap into the info card, then parse the AniList ID out of the Information
button. That ID is our safeguard — we mark the row ``verified=True`` only
when we successfully extract it. The caller (``bot_content._gather_metadata``)
still receives the legacy dict shape (same keys it parsed before) plus three
additive fields: ``anilist_id``, ``verified``, ``_acutebot_selection``.

Falls back gracefully — every step has a bounded timeout and any exception
is logged + swallowed so a partial answer still bubbles up rather than the
caller hanging forever waiting on @acutebot.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from pyrogram.types import InlineKeyboardButton

from nekofetch.core.logging import get_logger
from nekofetch.sources.telegram.userbot import is_transport_error

if TYPE_CHECKING:
    from pyrogram import Client

log = get_logger(__name__)

_BOT_USERNAME = "acutebot"
_CMD_PREFIX = "/anime "

# Probe tunables — bounded overall latency regardless of what @acutebot does.
_WAIT_INITIAL = 2.5           # seconds to wait before first poll
_POLL_INTERVAL = 0.7          # gap between polls
_POLL_TIMEOUT_INITIAL = 12.0  # total poll window for the first menu response
_POLL_TIMEOUT_CARD = 60.0     # total poll window for the info card after a tap — acutebot's alert often says "Hold on..." while it builds the card, which can take ~30-45s under load.

# Field labels @acutebot uses on the info card — maps to our internal keys.
_FIELD_LABELS: dict[str, str] = {
    "genres": "genres",
    "type": "format",
    "average rating": "score",
    "status": "status",
    "first aired": "first_aired",
    "last aired": "last_aired",
    "runtime": "runtime",
    "no of episodes": "episode_count",
    "synopsis": "synopsis",
}

_LABEL_RE = re.compile(r"^‣\s*(.+?)\s*:\s*(.*)")

# Recognition helpers.
_MENU_HINT_RE = re.compile(
    r"\b(results\s+for|search\s+results|pick\s+a|choose\s+a|tap\s+to\s+select)\b",
    re.IGNORECASE,
)
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)[\.\)\-:\s]+(.+?)\s*$", re.MULTILINE)
_ANILIST_URL_RE = re.compile(r"anilist\.co/anime/(\d+)", re.IGNORECASE)
_INFO_BUTTON_TEXT_RE = re.compile(
    r"^\s*(?:info(?:rmation)?|details?|open\s+(?:on|in)\s+anilist|more\s+info)\s*$",
    re.IGNORECASE,
)


# ── public entry ─────────────────────────────────────────────────────────────


async def fetch_from_acutebot(
    title_query: str,
    pool: object,  # UserbotPool, typing imported lazily
    photo_dir: str | None = None,
    *,
    on_step: Any | None = None,  # callable(str) for tracing (probe script hook)
) -> dict | None:
    """Fetch anime metadata for ``title_query`` from @acutebot.

    Returns a dict matching ``BotContentService._gather_metadata``'s
    legacy shape **plus** ``anilist_id``, ``verified`` and
    ``_acutebot_selection`` fields. ``verified`` is True iff we extracted an
    AniList ID from the info card's Information button. Returns ``None``
    only when @acutebot didn't reply within the poll window.

    ``on_step(msg)`` is an optional hook the probe script uses to print a
    trace line at each state transition; production callers leave it None.
    """
    try:
        from nekofetch.sources.telegram.userbot import UserbotPool

        assert isinstance(pool, UserbotPool)
        # A transport can die after acquisition but before the first API call
        # (notably contacts.ResolveUsername while opening @acutebot). Give the
        # pool one bounded second attempt so a single configured account is
        # rebuilt instead of surfacing a closed-handler error to the caller.
        return await pool.execute(
            lambda c: _do_fetch(c, title_query, photo_dir, on_step),
            retries=2,
            retry_on=is_transport_error,
            max_attempts=3,  # owner spec: try @acutebot at most 3 times, then give up
        )
    except Exception as exc:
        log.warning("acutebot.fetch.failed", title=title_query, error=str(exc))
        return None


# ── state machine ─────────────────────────────────────────────────────────────


async def _do_fetch(
    client: "Client",
    title_query: str,
    photo_dir: str | None,
    on_step: Any | None,
) -> dict | None:
    """Acquire menu → pick candidate → read info card → verify AniList ID."""
    _trace(on_step, f"send  /anime {title_query!r}")
    sent = await client.send_message(_BOT_USERNAME, f"{_CMD_PREFIX}{title_query}")
    # One initial wait before polling; @acutebot typically answers within ~1.5 s
    # but a cold lookup can take longer.
    await asyncio.sleep(_WAIT_INITIAL)
    boundary_id = sent.id

    first = await _poll_for_new(
        client,
        boundary_id,
        _POLL_TIMEOUT_INITIAL,
        predicate=_looks_like_menu_or_card,
        on_step=on_step,
        step_label="first_response",
    )
    if first is None:
        log.warning("acutebot.no_response", title=title_query)
        _trace(on_step, "STOP  no_response")
        return None

    info_msg = first
    selection: tuple[Any, ...] = ("direct", None, None)
    selected_title = ""  # captured only on the menu branch — used by the verifier below.
    if _looks_like_menu(first) and not _looks_like_card(first):
        # A direct info card whose synopsis contains a "1. …" line trips the
        # numbered-line menu heuristic; the card signal ("‣ Genres") wins so we
        # don't tap a phantom menu candidate and lose the real card.
        _trace(on_step, "menu  received")
        candidates = _parse_menu(first)
        if not candidates:
            log.warning("acutebot.menu_unparseable", title=title_query)
            _trace(on_step, "STOP  menu_unparseable")
            return None
        idx, rationale = _select_candidate(candidates, title_query)
        _trace(
            on_step,
            f"menu  picked idx={idx} rationale={rationale!r} "
            f"titles={[t for _, t, _ in candidates]}",
        )
        if idx < 0:
            log.warning("acutebot.no_menu_match", title=title_query)
            _trace(on_step, "STOP  no_menu_match")
            return None
        info_msg = await _tap_into_card(client, first, candidates[idx], on_step)
        if info_msg is None:
            log.warning("acutebot.card_missing", title=title_query,
                        chosen=candidates[idx])
            _trace(on_step, "STOP  card_missing")
            return None
        selection = ("menu", idx, rationale)
        selected_title = candidates[idx][1]  # feed the verifier below.
    elif not _looks_like_card(first):
        preview = (first.text or first.caption or "")[:120]
        log.warning("acutebot.unknown_response", title=title_query, preview=preview)
        _trace(on_step, f"STOP  unknown_response preview={preview!r}")
        return None
    else:
        _trace(on_step, "card  received_direct")

    # Hoist text extraction; normalise a str ``info_msg`` to ``None`` for
    # the downstream helpers — they expect a real Telegram Message but
    # tolerate ``None`` via their ``getattr(..., None)`` patterns.
    if isinstance(info_msg, str):
        text = info_msg
        info_msg = None
    else:
        text = info_msg.text or info_msg.caption or ""
    photo_path = await _maybe_download_photo(client, info_msg, title_query, photo_dir)
    _trace(on_step, f"photo path={photo_path!r}")

    meta = _parse_card(text, info_msg, photo_path=photo_path)
    if info_msg is not None:
        anilist_id, fallback_btn = _find_information(info_msg)
        if anilist_id is None and fallback_btn is not None:
            anilist_id = await _tap_info_button(client, info_msg, fallback_btn, on_step)
    else:
        # Pattern-c: alert-text-only delivery has no inline button. We'll
        # try to recover the AniList ID straight from the alert body below.
        anilist_id, fallback_btn = None, None
    # Pattern-c fallback — @acutebot bakes ``https://anilist.co/anime/<id>-...``
    # into the alert body when there's no inline Information button. Pull
    # the ID from the already-extracted ``text`` so ``_verify_against_anilist``
    # still runs (otherwise it would silently soft-pass).
    if anilist_id is None and text:
        m_alert = _ANILIST_URL_RE.search(text)
        if m_alert:
            anilist_id = int(m_alert.group(1))
    _trace(on_step, f"verify anilist_id={anilist_id} via={'url' if fallback_btn is None else 'tap'}")
    meta["anilist_id"] = anilist_id

    # Real AniList cross-check: when we have an ID AND we picked a candidate
    # from a menu, do one tiny GraphQL query and verify Acutebot's URL really
    # points at the entry whose titles overlap what *we* selected. If acutebot
    # ever lied (the bot curated the URL, but the menu picked something else),
    # we'd flip ``verified=False`` and surface the mismatch in the row so the
    # bot_content caller can decide to retry or fall back to AniList alone.
    # ``selected_title`` was set in the menu branch above (or stays "" for the
    # direct-card path, where we don't know which title acutebot chose).
    verified, mismatch = await _verify_against_anilist(
        anilist_id, [selected_title] if selected_title else []
    )
    meta["verified"] = verified
    meta["_anilist_mismatch"] = mismatch
    meta["_acutebot_selection"] = selection

    log.info(
        "acutebot.fetch",
        title=title_query, selection=selection[0], picked=selection[1],
        rationale=selection[2], anilist_id=anilist_id,
        verified=verified, mismatch=mismatch,
    )
    return meta


# ── polling / detection helpers ──────────────────────────────────────────────


async def _poll_for_new(
    client: "Client",
    after_id: int,
    timeout_s: float,
    *,
    predicate,
    on_step: Any | None,
    step_label: str,
) -> Any | None:
    """Block until a new message from @acutebot after ``after_id`` matches
    ``predicate(text, msg)``, or ``timeout_s`` has elapsed.

    Iteratively fetches the chat history in 20-message chunks so a long
    burst of activity between polls doesn't push the message we want out
    of the window.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_max_id = after_id
    while True:
        async for msg in client.get_chat_history(_BOT_USERNAME, limit=20):
            if not _is_from_acutebot(msg):
                continue
            if msg.id <= last_max_id:
                continue
            last_max_id = max(last_max_id, msg.id)
            if predicate(msg):
                _trace(on_step, f"hit   {step_label} msg_id={msg.id}")
                return msg
        if asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(_POLL_INTERVAL)


async def _wait_for_edit(
    client: "Client",
    msg_id: int,
    timeout_s: float,
    *,
    predicate,
    on_step: Any | None,
    step_label: str,
) -> Any | None:
    """Block until a known @acutebot message id has been edited so it
    matches ``predicate``, or ``timeout_s`` has elapsed.

    Unlike ``_poll_for_new`` (which stream-scans chat history for ids
    *strictly greater* than a boundary, catching only NEW messages),
    this loops over ``client.get_messages(_BOT_USERNAME, msg_id)`` —
    handy when @acutebot edits the menu in place to become the info
    card. Pyrogram's ``get_messages`` always returns the most recent
    server-side snapshot, so each call picks up pending edits without
    needing raw update handlers.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        try:
            current = await client.get_messages(_BOT_USERNAME, msg_id)
        except Exception as exc:
            log.debug("acutebot.wait_for_edit.poll_failed", error=str(exc))
            current = None
        if current is not None and predicate(current):
            _trace(on_step, f"hit   {step_label} msg_id={msg_id}")
            return current
        if asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(_POLL_INTERVAL)


def _is_from_acutebot(msg: Any) -> bool:
    """True when the message originates from the @acutebot account itself,
    not our command echo or another bot. We compare usernames case-insensitive
    and also fall back to the chat being the @acutebot chat."""
    fuser = getattr(msg, "from_user", None)
    if fuser is not None:
        uname = getattr(fuser, "username", None)
        if uname:
            return uname.lower() == _BOT_USERNAME
    chat = getattr(msg, "chat", None)
    if chat is not None and getattr(chat, "username", None) == _BOT_USERNAME:
        # No author but the chat is acutebot — service messages from acutebot.
        return True
    return False


def _looks_like_menu(msg: Any) -> bool:
    """True when ``msg`` from @acutebot looks like a search-result menu rather
    than the final info card. Single-arg signature — text is extracted here
    so call sites can't accidentally drop it (this was the bug the live
    probe caught on the first run).

    Three keyboard layouts are recognised (see ``_parse_menu`` for the
    rationale). The header-text regex is the strongest signal — when present
    we don't bother inspecting the keyboard.
    """
    text = msg.text or msg.caption or ""
    if _MENU_HINT_RE.search(text):
        return True
    # Numbered-text-only menus (no header, no keyboard) are caught here so we
    # don't silently treat a "1. <title>\n2. <title>\n…" body as a direct
    # info card.
    if _NUMBERED_LINE_RE.search(text):
        return True
    kb = getattr(getattr(msg, "reply_markup", None), "inline_keyboard", None)
    # Layout 1 — title-buttons (current @acutebot): one non-empty callback
    # button per row, with no URL.
    if kb and any(
        (b.text or "").strip() and b.callback_data and not (b.url or "")
        for row in kb for b in row
    ):
        return True
    # Layout 2 — digit-only buttons (legacy).
    if kb and all(_is_digit_button(b) for row in kb for b in row):
        return True
    return False


def _looks_like_card(msg: Any) -> bool:
    """True when ``msg`` looks like the full info card (vs a menu).

    Accepts a pyrogram ``Message`` AND a plain ``str`` — the latter is
    needed because @acutebot sometimes answers a menu tap with a Telegram
    alert whose body IS the card, and Pyrogram's raw ``BotCallbackAnswer``
    exposes that body on ``.message`` as a ``str`` (not a wrapped Message).
    """
    if isinstance(msg, str):
        text = msg
    elif msg is None:
        return False
    else:
        text = (getattr(msg, "text", None)
                or getattr(msg, "caption", None)
                or "")
    return "‣ Genres" in text or "Genres :" in text


def _looks_like_menu_or_card(msg: Any) -> bool:
    return _looks_like_menu(msg) or _looks_like_card(msg)


# ── menu parsing + selection ─────────────────────────────────────────────────


def _is_digit_button(btn: InlineKeyboardButton) -> bool:
    return bool(btn.text and btn.text.strip().isdigit() and 1 <= int(btn.text.strip()) <= 99)


def _parse_menu(msg: Any) -> list[tuple[int, str, str]]:
    """Return ``[(row_index, title, callback_data), ...]`` for each candidate.

    Three @acutebot layouts are supported, in priority order:

      1. **Title-buttons** (current production format, confirmed via the
         live diagnostic at /c/Users/Admin/Documents/NekoFetch on 2026-07-03):
         each row holds one ``InlineKeyboardButton`` whose ``text`` IS the
         full English title and whose ``callback_data`` selects that row.
         ``button.url`` is empty for these.
      2. **Digit-buttons** (older format): button text is "1", "2", "30" …
         one per row, callback_data selects.
      3. **Numbered-text** (oldest, used when keyboards were disabled):
         one numbered line per candidate in the body — detected by the
         ``_NUMBERED_LINE_RE`` regex.

    ``row_index`` is the 1-based position among the candidates we *parsed*
    (not an acutebot-supplied label), so ``_select_candidate`` can pick by
    position even when acutebot doesn't prefix the row.
    """
    candidates: list[tuple[int, str, str]] = []
    kb = getattr(getattr(msg, "reply_markup", None), "inline_keyboard", None) or []
    # Layout 1 — title-buttons (current @acutebot): non-empty text + non-empty
    # callback_data + no URL.
    title_buttons = [
        b for row in kb for b in row
        if (b.text or "").strip() and b.callback_data and not (b.url or "")
    ]
    if title_buttons:
        for idx, b in enumerate(title_buttons, start=1):
            candidates.append((idx, (b.text or "").strip(), b.callback_data))
        return candidates
    # Layout 2 — digit-buttons (legacy): button text is "1", "2".
    digit_buttons = [
        b for row in kb for b in row
        if _is_digit_button(b) and b.callback_data
    ]
    if digit_buttons:
        for idx, b in enumerate(digit_buttons, start=1):
            title = (b.text or "").strip()
            candidates.append((idx, title, b.callback_data))
        return candidates
    # Layout 3 — numbered-text body (no keyboard).
    text = (msg.text or msg.caption or "").strip()
    for n_str, title in _NUMBERED_LINE_RE.findall(text):
        n = int(n_str)
        if not (1 <= n <= 99):
            continue
        candidates.append((n, title.strip(), ""))
    return candidates


def _select_candidate(
    candidates: list[tuple[int, str, str]], query: str
) -> tuple[int, str]:
    """Pick the best candidate for ``query``.

    Ranking, strongest first:
      1. exact case-insensitive equality of the candidate title against the
         normalized query (English / Romaji / native — covered by the
         normalized-words set),
      2. fuzzy ``title_matches`` score >= 0.85 (uses the existing pile —
         same matcher the rest of NekoFetch uses to avoid group/quality noise),
      3. the first candidate (the user's "first result is correct" default).

    Returns ``(index_in_candidates, rationale_string)``. ``index_in_candidates``
    is the position in the input list, NOT the candidate's own 1-based number.
    Returns ``(-1, "no_match")`` when nothing fits.
    """
    if not candidates:
        return -1, "no_match"
    norm_query = query.strip().lower()
    # tier 1 — exact.
    for i, (_, title, _) in enumerate(candidates):
        if title and title.strip().lower() == norm_query:
            return i, "exact"
    # tier 2 — fuzzy via the project's matcher.
    try:
        from nekofetch.sources.telegram.matching import best_match
    except Exception:  # noqa: BLE001 - import is local & safe-but-tolerated
        best_match = None
    if best_match is not None:
        titles = [t for _, t, _ in candidates]
        idx, score = best_match(query, titles, threshold=0.85)
        if idx >= 0:
            return idx, f"fuzzy({score:.2f})"
    # tier 3 — the first result (per the user's "first result is correct"
    # default for English titles where ambiguity is rare).
    return 0, "first"


async def _tap_into_card(
    client: "Client",
    menu_msg: Any,
    candidate: tuple[int, str, str],
    on_step: Any | None,
) -> Any | None:
    """Tap the candidate's callback button, then poll chat history for the
    resultant info card. Returns the info-card message or ``None``.

    Handles two common @acutebot layouts:
      • the menu message is EDITED in place to become the info card
        (callback returned without alerting → poll the menu chat_id again
         for an EDIT that looks like a card),
      • a brand-new info card message is sent (the menu stays around).

    For text-only numbered menus (``callback_data == ""``) we send the index
    back as a message since there's no button to tap.
    """
    index, _title, callback_data = candidate
    _trace(on_step, f"tap   candidate={_title!r} cb_data={callback_data!r}")
    # When @acutebot delivers the card body inside the callback alert
    # (pattern-c), the text is usable for _parse_card — but there is NO
    # attached photo on a BotCallbackAnswer.  We still need to poll for
    # the real Message (edited menu or brand-new) to get the photo.
    # Only fall back to the alert string when both polls time out.
    alert_fallback: str | None = None
    if callback_data:
        try:
            answer = await client.request_callback_answer(
                chat_id=menu_msg.chat.id,
                message_id=menu_msg.id,
                callback_data=callback_data,
                timeout=8,
            )
            ans_msg = (getattr(answer, "message", None)
                       or getattr(answer, "text", None) or "")
            if ans_msg and _looks_like_card(ans_msg):
                _trace(on_step, "card  alert_text (will poll for photo)")
                alert_fallback = ans_msg
            elif ans_msg:
                preview = ans_msg.replace("\n", " ")[:80]
                _trace(
                    on_step,
                    f"alert answer.text preview={preview!r}"
                    " (not card-shaped, will refresh/poll next)",
                )
        except Exception as exc:
            log.warning("acutebot.tap.failed", error=str(exc))
    else:
        # Text-only menu: send the 1-based index as a chat message.
        try:
            await client.send_message(_BOT_USERNAME, str(index))
        except Exception as exc:
            log.warning("acutebot.text_select.failed", error=str(exc))    # Pattern (a) — @acutebot commonly edits the menu IN PLACE to become
    # the info card, with a multi-second delay between the tap and the
    # edit (it sends "Hold on..." as a courtesy alert first). The edited
    # message keeps the SAME id, so we MUST poll by message-id rather
    # than scanning chat history for ids > menu_msg.id (which would miss
    # an in-place edit). 60 s has ample margin — the user observes the
    # edit landing within 5–30 s in practice.
    edited = await _wait_for_edit(
        client,
        menu_msg.id,
        _POLL_TIMEOUT_CARD,
        predicate=_looks_like_card,
        on_step=on_step,
        step_label="info_card_edit",
    )
    if edited is not None:
        _trace(on_step, "card  menu_edited_in_place")
        return edited
    
    # Fallback: a brand-new info card message (rare — only happens if
    # @acutebot deletes the menu and sends the card as a fresh message).
    new_msg = await _poll_for_new(
        client,
        menu_msg.id,
        _POLL_TIMEOUT_CARD,
        predicate=_looks_like_card,
        on_step=on_step,
        step_label="info_card_new",
    )
    if new_msg is not None:
        _trace(on_step, "card  new_message")
        return new_msg

    # Both polls timed out — fall back to the alert text (caption only,
    # no photo).  ``_maybe_download_photo`` will return None for a string
    # ``info_msg``, which is correct here: we genuinely don't have the photo.
    if alert_fallback is not None:
        _trace(on_step, "card  alert_fallback (no photo)")
        return alert_fallback
    return None


# ── info-card parsing (legacy) ────────────────────────────────────────────────


def _parse_card(text: str, msg: Any | None = None, *, photo_path: str | None = None) -> dict:
    """Parse @acutebot's info-card body into a structured dict.

    The ``msg`` argument is kept for backward-compatibility with callers
    that still pass a real pyrogram ``Message``; the function body never
    reads it. ``msg`` may also be ``None`` or a plain ``str`` (the
    pattern-(c) case where @acutebot delivers the entire card inside the
    callback answer's alert body, with no real Message attached).
    """

    meta: dict = {
        "title": None,
        "romaji": None,
        "format": None,
        "status": None,
        "score": None,
        "genres": [],
        "synopsis": None,
        "episode_count": None,
        "first_aired": None,
        "last_aired": None,
        "runtime": None,
        "poster_url": photo_path,
        "_source": "acutebot",
    }

    lines = text.split("\n")
    header = lines[0].strip() if lines else ""
    if "|" in header:
        parts = header.split("|", 1)
        meta["title"] = parts[0].strip()
        meta["romaji"] = parts[1].strip()
    else:
        meta["title"] = header
        meta["romaji"] = header

    current_synopsis: list[str] = []
    in_synopsis = False
    for line in lines:
        m = _LABEL_RE.match(line)
        if m:
            label = m.group(1).strip().lower()
            value = m.group(2).strip()
            key = _FIELD_LABELS.get(label)
            if key == "synopsis":
                in_synopsis = True
                current_synopsis.append(value)
            elif key == "genres":
                meta["genres"] = [g.strip() for g in value.split(",") if g.strip()]
            elif key == "score":
                try:
                    meta["score"] = str(round(float(value) / 10, 1))
                except (ValueError, TypeError):
                    meta["score"] = value
            elif key == "episode_count":
                try:
                    meta["episode_count"] = int(value)
                except (ValueError, TypeError):
                    meta["episode_count"] = value
            elif key == "runtime":
                rt_match = re.search(r"(\d+)", value)
                meta["runtime"] = f"{rt_match.group(1)} min/ep" if rt_match else value
            elif key:
                meta[key] = value
        elif in_synopsis:
            current_synopsis.append(line.strip())

    if current_synopsis:
        raw = " ".join(current_synopsis)
        raw = re.sub(r"\s*…?\s*read\s+more\s*$", "", raw, flags=re.IGNORECASE).strip()
        meta["synopsis"] = raw or None

    return meta


async def _maybe_download_photo(
    client: "Client", msg: Any, title_query: str, photo_dir: str | None,
) -> str | None:
    if not photo_dir or not getattr(msg, "photo", None):
        return None
    from pathlib import Path

    out = Path(photo_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in title_query if c.isalnum() or c in (" ", "-", "_")).strip()
    safe = safe.replace(" ", "_")[:64] or "anime"
    dest = out / f"{safe}.jpg"
    try:
        # ``download_media`` only accepts ``file_name=`` in this project's
        # pyrogram build (passing ``file_path=`` raised
        # ``unexpected keyword argument 'file_path'``); ``file_name`` with
        # an absolute path makes pyrogram save directly into ``dest``.
        downloaded = await client.download_media(msg.photo.file_id, file_name=str(dest))
        if downloaded:
            log.info("acutebot.photo.saved", path=str(Path(downloaded)))
            return str(Path(downloaded))
    except Exception as exc:
        log.warning("acutebot.photo.download.failed", error=str(exc))
    return None


# ── Information button + AniList verification ────────────────────────────────


def _find_information(msg: Any) -> tuple[int | None, InlineKeyboardButton | None]:
    """Locate the 'Information' button on the info card.

    Two layouts are supported:
      1. **URL button** — contains a link to ``https://anilist.co/anime/<id>``.
         We extract the ID directly without an extra round-trip.
      2. **Callback button** — has a ``callback_data`` but no URL. We return
         the button so the caller can tap it and parse the alert text.
    """
    kb = getattr(getattr(msg, "reply_markup", None), "inline_keyboard", None) or []
    for row in kb:
        for btn in row:
            url = getattr(btn, "url", None)
            if url:
                m = _ANILIST_URL_RE.search(url)
                if m:
                    return int(m.group(1)), None
            if btn.callback_data and _INFO_BUTTON_TEXT_RE.match(btn.text or ""):
                return None, btn
    return None, None


async def _tap_info_button(
    client: "Client", msg: Any, btn: InlineKeyboardButton, on_step: Any | None,
) -> int | None:
    """Tap a callback-style Information button and extract the AniList ID
    from the alert text (@acutebot shows the AniList link in the alert)."""
    _trace(on_step, f"tap   info_callback cb_data={btn.callback_data!r}")
    try:
        answer = await client.request_callback_answer(
            chat_id=msg.chat.id,
            message_id=msg.id,
            callback_data=btn.callback_data,
            timeout=8,
        )
    except Exception as exc:
        log.warning("acutebot.info_tap.failed", error=str(exc))
        return None
    alert_text = getattr(answer, "text", None) or ""
    m = _ANILIST_URL_RE.search(alert_text)
    if m:
        return int(m.group(1))
    return None


def _trace(on_step: Any | None, line: str) -> None:
    """Best-effort, exception-safe step tracer for the probe script."""
    if on_step is None:
        return
    try:
        on_step(line)
    except Exception:  # noqa: BLE001 - tracer must never crash the fetch
        pass


# ── AniList cross-verification ────────────────────────────────────────────────


_ANILIST_VERIFY_QUERY = """
query VerifyMedia($id: Int!) {
  Media(id: $id, type: ANIME) {
    title { romaji english native }
    synonyms
  }
}
"""


async def _verify_against_anilist(
    anilist_id: int | None,
    expected_titles: list[str],
) -> tuple[bool, bool]:
    """Cross-check `@acutebot`'s Information-button URL against AniList.

    Returns ``(verified, mismatch)``:
      * ``verified=True`` when AniList returned a media whose titles cover at
        least one of our ``expected_titles`` (via ``title_matches`` at the
        strict 1.0 threshold, mirroring the matcher the rest of NekoFetch
        uses for safe title comparisons),
      * ``verified=False, mismatch=True`` when AniList returned a media that
        is *different* from what we picked (rare — would mean acutebot lied
        in the menu reply),
      * ``verified=False, mismatch=False`` when there's nothing to verify
        (no ID, or AniList itself was unreachable) — caller treats this as a
        soft pass so a flaky upstream never breaks the bot_content pipeline.

    One httpx POST, 10 s timeout, runs after the userbot pool is freed so a
    AniList hiccup cannot block the rest of the distribution bot startup.
    """
    if not anilist_id or not expected_titles:
        return (anilist_id is not None), False
    try:
        import httpx
        from nekofetch.sources.telegram.matching import any_title_matches

        norm_titles: list[str] = []
        for t in expected_titles:
            if t:
                norm_titles.append(t)
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.post(
                "https://graphql.anilist.co",
                json={"query": _ANILIST_VERIFY_QUERY, "variables": {"id": anilist_id}},
            )
            r.raise_for_status()
            data = (r.json().get("data") or {}).get("Media") or {}
        titles = [str(t).strip() for t in (data.get("title") or {}).values() if t]
        synonym_set = list(data.get("synonyms") or [])
        haystack = titles + synonym_set
        if not haystack:
            return (True, False)  # can't compare; trust @acutebot's curated URL.
        if any_title_matches(norm_titles, " ".join(haystack), threshold=1.0):
            return True, False
        # Fall back to a case-insensitive contains check (catches acronyms).
        nh = " ".join(haystack).lower()
        if any((t.strip().lower() in nh) for t in norm_titles if t):
            return True, False
        return False, True
    except Exception as exc:  # noqa: BLE001 - never break the caller on verify failure
        log.warning("acutebot.verify.failed", id=anilist_id, error=str(exc))
        return (True, False)  # soft pass — AniList hiccup is not the bot's fault.
