from __future__ import annotations

import asyncio
import html as _html
import json
import re
from urllib.parse import quote

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from nekofetch.bots.channel_reply import arm as _arm_reply
from nekofetch.bots.channel_reply import disarm as _disarm_reply
from nekofetch.bots.channel_reply import peek as _peek_reply
from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.exceptions import NekoFetchError
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import Permission
from nekofetch.localization.messages import M, t
from nekofetch.services.auth_service import AuthService
from nekofetch.services.franchise_flow import FranchiseFlowService
from nekofetch.ui.components import cb, keyboard, lock_buttons, paginate
from nekofetch.ui.franchise_screens import franchise_map_selection
from nekofetch.ui.screens import show
from nekofetch.ui.typography import user_label

log = get_logger(__name__)

PAGE_SIZE = 8
STATE_MANUAL_COMP = "staff:manual:comp"
STATE_MANUAL_AUDIO = "staff:manual:audio"
STATE_MANUAL_RES = "staff:manual:res"
STATE_MANUAL_CUSTOM_RES = "staff:manual:custom_res"
STATE_MANUAL_CONFIRM = "staff:manual:confirm"
STATE_MANUAL_INTAKE = "staff:manual:intake"
STATE_MANUAL = STATE_MANUAL_COMP  # legacy alias for the entry-point transition
STATE_TORRENT = "staff:torrent_pick"
STATE_TORRENT_PROVIDE = "staff:torrent_provide"
STATE_TORRENT_MAP = "staff:torrent_map"
STATE_TORRENT_MAP_FIX = "staff:torrent_map_fix"  # admin is reassigning file→season
STATE_DDL_PROVIDE = "staff:ddl_provide"
STATE_PROVIDE = "staff:await_provide"   # admin is sending a file for a stuck episode
STATE_MISSING_SOURCE = "staff:await_more_links"  # admin is sending more links to fill coverage gaps

# Franchise flow states
STATE_FRANCHISE_MAP = "staff:franchise:map"
STATE_FRANCHISE_CONFIRM = "staff:franchise:confirm"
STATE_ANIZONE_SLUGS = "staff:anizone:slugs"


def _comp_key(comp: dict) -> str:
    """Unique key for a component in the FSM data bag."""
    t = comp.get("type", "season")
    if t == "season":
        return f"season_{comp.get('number', 1)}"
    return f"{t}_{comp.get('title', '0')}"


def _comp_label(comp: dict) -> str:
    """Human-readable label for a component."""
    t = comp.get("type", "season")
    if t == "season":
        base = f"Season {comp.get('number', 1)}"
        title = (comp.get("title") or "").strip()
        if title and title != base and not title.startswith("Season "):
            base += f" — {title[:50]}"
        return base
    title = comp.get("title", "")
    return f"{t.title()}: {title}" if title else t.title()


def _esc(text: str) -> str:
    """HTML-escape user-facing text for safe rendering."""
    return _html.escape(text or "", quote=False)


def _torrent_map_kb(L, code: str, map_url: str, *, with_fix: bool = True) -> InlineKeyboardMarkup:
    """Mapping card keyboard — a single, STABLE button set.

    The old card had four overlapping buttons (Full Mapping / Details / Toggle /
    Fix Seasons) and a ``with_fix`` flag that different Back targets set
    inconsistently, so a round-trip produced a different keyboard than the
    original card (the owner's "Fix Seasons → Back drops the buttons" bug). This
    renders the SAME rows on every paint, keyed only by ``code``:

      • Full Mapping  — the structured read-only view (Telegraph page when we have
        a URL, else an inline callback so the button NEVER collapses into a bare
        caption hyperlink).
      • Toggle Entry  — enable/disable whole franchise entries.
      • Edit Mapping  — reassign seasons / mark junk (buttons + paste fallback).
      • Confirm / Back.

    ``with_fix`` is accepted for backward-compat but ignored (the keyboard no
    longer branches on it).
    """
    rows: list[list[InlineKeyboardButton]] = []
    if map_url:
        rows.append([InlineKeyboardButton(L(M.TORRENT_MAP_BTN_FULL), url=map_url)])
    else:
        # No Telegraph page → render the full mapping INLINE via a callback, so
        # "Full Mapping" is always a real button (never a vanishing URL button).
        rows.append([InlineKeyboardButton(
            L(M.TORRENT_MAP_BTN_FULL), callback_data=cb("staff", "rtmapfull", code, 0))])
    rows.append([
        InlineKeyboardButton(L(M.TORRENT_MAP_BTN_TOGGLE),
                             callback_data=cb("staff", "rtmaptgl", code, "list")),
        InlineKeyboardButton(L(M.TORRENT_MAP_BTN_EDIT),
                             callback_data=cb("staff", "rtmapedit", code)),
    ])
    rows.append([InlineKeyboardButton(L(M.TORRENT_MAP_BTN_CONFIRM),
                                      callback_data=cb("staff", "rtmapok", code))])
    rows.append([InlineKeyboardButton(L(M.BTN_BACK),
                                      callback_data=cb("staff", "rdetail", code))])
    return InlineKeyboardMarkup(rows)


async def _anime_backdrop(container, req) -> str | None:
    """Rotating artwork for the anime behind a request.

    Reuses the per-anime pool seeded at request time (persisted on
    ``franchise_data["backdrops"]``), so every downloader wizard card shows a
    DIFFERENT piece of that anime's own art. Falls back to a one-off TMDB fetch
    (and seeds the pool) when the request predates artwork persistence.
    """
    from nekofetch.ui.artwork import (
        ensure_anime_art,
        key_for_franchise,
        next_anime_art,
    )
    franchise = req.franchise_data or {}
    title = franchise.get("title") or req.anime_title
    key = key_for_franchise(franchise, title=title)
    await ensure_anime_art(key, tmdb=container.tmdb, title=title,
                           franchise=franchise)
    art = next_anime_art(key)
    # next_anime_art returns a local Path when the pool is empty; screens want a
    # URL here (they already fall back to local art themselves), so coerce.
    return art if isinstance(art, str) else None


# ── Manual-upload DM handoff helpers ──────────────────────────────────────────
# The manual upload wizard is configured in the control-center channel, but the
# actual file collection happens in a private chat with the bot (so large uploads
# and the per-batch back-and-forth never clutter the channel). These module-level
# helpers are shared between review.py's handlers and start.py's deep-link resume.

# Deep-link start payload that resumes an in-progress manual intake in the DM.
MANUAL_RESUME_PREFIX = "nfresume"


def _channel_deep_link(channel_id: int) -> str | None:
    """Build a ``t.me/c/<id>`` link back to a private channel, for members."""
    if not channel_id:
        return None
    s = str(channel_id)
    return f"https://t.me/c/{s[4:]}" if s.startswith("-100") else None


async def _dm_track(fsm, user_id: int, *msg_ids: int) -> None:
    """Record DM message ids so the whole intake exchange can be purged later.

    Tracks both the bot's prompts/acks and the admin's uploads, so once every
    file is in we can wipe the entire conversation and leave only a clean
    "all done" summary with a link back to the channel.
    """
    _, data = await fsm.get(user_id)
    ids = data.get("dm_msg_ids", [])
    ids.extend(int(m) for m in msg_ids if m)
    await fsm.update(user_id, dm_msg_ids=ids)


def _intake_prompt_lines(batch: tuple) -> list[str]:
    """The per-batch upload prompt ("📤 Uploading: Season 1 [dual] 480p" + how-to)."""
    _comp_key_, comp_label, audio_type, res = batch
    return [
        t(M.MANUAL_INTAKE_PROMPT, component=_esc(comp_label),
          audio=audio_type, res=res),
        "",
        t(M.MANUAL_INTAKE_INSTRUCTIONS),
    ]


async def resume_manual_intake_dm(client, container, chat_id: int, user_id: int) -> bool:
    """Resume an in-progress manual intake inside the private chat.

    Called by the ``/start <MANUAL_RESUME_PREFIX>…`` deep-link handler once the
    admin opens the bot. Returns ``True`` when an intake was actually resumed so
    the caller can skip the normal welcome screen.
    """
    fsm = FSM(container.redis, bot="admin")
    state, data = await fsm.get(user_id)
    build_order = data.get("build_order", [])
    if state != STATE_MANUAL_INTAKE or not build_order:
        return False
    batch_idx = data.get("current_batch", 0)
    if batch_idx >= len(build_order):
        return False
    # Remember which chat the DM exchange lives in (for the final purge).
    await fsm.update(user_id, dm_chat_id=chat_id)
    title = data.get("anime_title", "")
    intro = await client.send_message(
        chat_id, t(M.MANUAL_HANDOFF_DM_INTRO, title=_esc(title)),
        parse_mode=ParseMode.HTML,
    )
    prompt = await client.send_message(
        chat_id, "\n".join(_intake_prompt_lines(build_order[batch_idx])),
        parse_mode=ParseMode.HTML,
    )
    await _dm_track(fsm, user_id, intro.id, prompt.id)
    return True


def _extract_components(franchise: dict, anime_title: str) -> list[dict]:
    """Extract uploadable components from franchise_data.

    Returns a list of dicts with ``type`` and identifying fields (``number`` for
    seasons, ``title`` for others). If there are no non-season components and only
    1 season, returns a single-entry list."""
    components: list[dict] = []
    seasons = franchise.get("franchise_seasons", 1) or 1
    for n in range(1, seasons + 1):
        components.append({"type": "season", "number": n})
    # Non-season components from the relations list
    relations = franchise.get("relations", [])
    for rel in relations:
        fmt = (rel.get("format") or "").upper()
        title = rel.get("title") or rel.get("english") or ""
        if fmt == "OVA":
            components.append({"type": "ova", "title": title})
        elif fmt == "MOVIE":
            components.append({"type": "movie", "title": title})
        elif fmt == "ONA":
            components.append({"type": "ona", "title": title})
        elif fmt == "SPECIAL":
            components.append({"type": "special", "title": title})
    return components


def _format_anizone_slug_prompt(mapping_dict: dict) -> str:
    """Build the AniZone slug-mapping prompt showing numbered franchise entries.

    Returns a message asking the admin to reply with AniZone slugs (one per line)
    for each included franchise entry. Also warns about OVAs/specials that may
    be bundled with regular episodes on AniZone.
    """
    entries = mapping_dict.get("entries", [])
    included = [e for e in entries if e.get("included", True)]
    # Auto-include all entries when none are selected — the admin may have
    # excluded everything on the franchise mapping screen, or (for single
    # entries) never saw the toggle screen at all.
    if not included and entries:
        for e in entries:
            e["included"] = True
        included = list(entries)
    if not included:
        return "No entries selected for mapping."
    has_ovas = any(e.get("kind", "season") in ("ova", "special", "movie", "ona") for e in included)
    lines: list[str] = [
        "<b>📋 AniZone Slug Mapping</b>",
        "",
        "Provide the AniZone URL or slug for each entry below.",
        "You can paste full links — the slug will be extracted automatically.",
        "Reply with one per line, like:",
        "",
        "<pre>1. https://anizone.to/anime/bsagbos2",
        "2. /anime/xyz123",
        "3. yk8nyzlr</pre>",
        "",
        "💡 <b>Tip:</b> If an entry has both regular episodes AND OVAs under the",
        "same slug, map the same slug to multiple entries with a type prefix:",
        "<pre>1. reg /anime/yk8nyzlr",
        "2. spec /anime/yk8nyzlr</pre>",
    ]
    if has_ovas:
        lines += [
            "",
            "⚠️ <b>Note:</b> AniZone may bundle OVAs/specials alongside regular episodes.",
            "The episode counts shown below (from AniList) may differ from what AniZone reports.",
        ]
    lines += ["", "<b>Entries to map:</b>"]
    for i, e in enumerate(included, start=1):
        kind = e.get("kind", "season")
        s_num = e.get("season_number", i)
        s_part = e.get("season_part")
        title = _esc(e.get("title", "") or "")
        label = f"Season {s_num:02d}"
        if s_part:
            label += f" Part {s_part}"
        if title and title != label and not title.startswith("Season "):
            label += f" — {title[:50]}"
        ep = e.get("episodes")
        ep_str = f" ({ep} ep)" if ep else ""
        if kind != "season":
            fmt_label = kind.title()
            label = f"{fmt_label}: {title}" if title else fmt_label
        lines.append(f"{i}. <b>{_esc(label)}</b>{ep_str}")
    return "\n".join(lines)


async def _walk_franchise_for_mapping(
    container: Container, franchise: dict, anime_doc_id: str,
) -> dict | None:
    """Walk the AniList franchise graph for per-entry titles.

    Returns the result of ``AnilistClient.walk_franchise_full``, or ``None``
    when the walk fails (graceful degradation — the mapping builder then falls
    back to aggregated data).
    """
    from nekofetch.core.logging import get_logger
    _log = get_logger(__name__)
    try:
        anilist_id = franchise.get("anilist_id")
        if not anilist_id:
            return None
        root_id = int(anilist_id)
        entries = await container.anilist.walk_franchise_full(root_id)
        if entries:
            return entries
    except Exception as exc:
        _log.debug("franchise.walk.failed", anime=anime_doc_id, error=str(exc))
    return None


async def _anizone_confirm_cleanup_scheduled(
    client,
    container: Container,
    *,
    confirm_mid: int,
    chat_id: int,
    code: str,
    sleep_seconds: float = 5.0,
) -> None:
    """Deferred cleanup of the AniZone success path's transient messages.

    Once the admin's slug reply is consumed the prompt card is deleted
    IMMEDIATELY at the call site (instant cleans-after-consume, per the
    user's explicit ask). Only the brief ``\u2b07\ufe0f Downloading\u2026`` ack and the
    request divider sticker linger, and this task removes them on a short
    delay so the admin can see the assigned ``job_id`` before the channel
    goes quiet.

    The ``sleep_seconds`` parameter is configurable so unit tests can drive
    this directly with ``sleep_seconds=0`` instead of waiting 5 real seconds.

    Every delete is wrapped in ``try/except`` so a missing message or
    Telegram permission issue never bubbles up — the channel-cleanup is
    best-effort, never fatal.
    """
    # Lazily imported on purpose — path-based unit tests patch
    # ``nekofetch.services.log_channel_service.LogChannelService`` and rely on
    # this re-resolution per call. Matches the codebase's 27-lazy-import
    # convention for this class; do NOT hoist to top-of-file without first
    # updating those tests to patch the local binding.
    from nekofetch.core.logging import get_logger as _glog
    from nekofetch.services.log_channel_service import LogChannelService
    _log = _glog(__name__)
    try:
        await asyncio.sleep(sleep_seconds)
        try:
            await client.delete_messages(chat_id, confirm_mid)
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "anizone.confirmation.delete_failed",
                mid=confirm_mid, error=str(exc),
            )
        try:
            await LogChannelService(container).clear_request_markers(
                code, delete_divider=True, force=True,
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "anizone.divider.delete_failed", code=code, error=str(exc),
            )
    except Exception as exc:  # noqa: BLE001
        _log.debug("anizone.cleanup.failed", error=str(exc))


def register(client: Client, container: Container) -> None:
    auth = AuthService(container)
    fsm = FSM(container.redis, bot="admin")
    L = container.localizer.get

    def _source_back_cb(code: str) -> str:
        """Callback for the "Back" button on the torrent/source sub-screens.

        On the standalone admin bot this returns to the review detail card
        (``staff|rdetail``). On Kuro Sōden's Levi, the review card is *not* the
        screen the admin came from — they arrived via Levi's route picker
        (Telegram / Website / Torrent), so Back must return there instead of
        dumping them onto the admin review card with its stray Reject button.
        A host sets ``container.source_back_cb`` to override the target."""
        hook = getattr(container, "source_back_cb", None)
        if hook is not None:
            try:
                return hook(code)
            except Exception:  # noqa: BLE001 — never break a keyboard build
                pass
        return cb("staff", "rdetail", code)

    async def _spawn_progress_card(job_id: int, chat_id: int | None) -> None:
        """Raise a live download card for a freshly-queued job, if the host wired
        one up (Kuro Sōden / Levi does; the standalone admin bot leaves it unset).
        Never let a card failure surface to the queueing path."""
        hook = getattr(container, "levi_progress_card", None)
        if hook is None or not chat_id:
            return
        try:
            await hook(job_id, chat_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("review.progress_card_spawn_failed", job_id=job_id, error=str(exc))

    async def _prompt_channel_reply(
        message: Message, state: str, hint: str, **data,
    ) -> None:
        """Arm a chat-scoped awaited-reply marker for a channel flow.

        Anonymous admins post AS the channel, so we can't key the flow to a user
        id — we arm it by chat id (see ``bots.channel_reply``) so the guard knows
        to hold off deleting the next message until the handler consumes it. The
        card itself already tells the admin what to reply with, so we do NOT send
        any extra "reply to this message" prompt (that just clutters the channel).

        ``hint`` is accepted for call-site readability but not posted.
        """
        del hint  # intentionally not sent — the card already carries instructions
        await _arm_reply(container.redis, message.chat.id, state, **data)

    async def _resolve_reply_flow(
        message: Message, *expected_states: str,
    ) -> tuple[str | None, dict, bool]:
        """Locate the awaited-reply flow behind an incoming text message.

        Handles both routes into a reply-expecting flow:

        * **DM / named reply** — ``from_user`` is set, so the per-user
          :class:`FSM` holds the state; the sender's permission is verified.
        * **Anonymous channel reply** — the admin posted AS the channel, so
          there's no ``from_user`` and no per-user state. The chat-scoped marker
          (armed when the card was shown) identifies the message as the awaited
          reply. Only Telegram channel admins can post as the channel, so the
          marker's existence is itself the authorisation.

        Accepts one or more ``expected_states``; the first that matches wins.
        Returns ``(state, data, via_channel)``. ``state`` is ``None`` when this
        message isn't the awaited reply (so the handler bails untouched).
        """
        # 1) Named sender → per-user FSM, permission-checked.
        if message.from_user:
            state, data = await fsm.get(message.from_user.id)
            if state in expected_states:
                user = getattr(message, "nf_user", None)
                if user and auth.has_permission(user, Permission.QUEUE_DOWNLOADS):
                    return state, data, False
                return None, {}, False  # wrong person — not their reply
        # 2) Anonymous / channel post → chat-scoped marker.
        state, data = await _peek_reply(container.redis, message.chat.id)
        if state in expected_states:
            return state, data, True
        return None, {}, False

    async def _finish_channel_reply(message: Message, data: dict) -> None:
        """Disarm the awaited-reply marker after a channel reply is consumed.

        The admin's own reply is deleted by the caller (delete-after-consume);
        here we just disarm the marker so the channel guard resumes normal
        cleanup. (``data`` is kept in the signature for call-site symmetry.)
        """
        del data
        await _disarm_reply(container.redis, message.chat.id)

    async def _claim_torrent_provide(code: str) -> bool:
        """Atomically claim the torrent-provide slot for ``code``.

        Returns ``True`` for the FIRST magnet/.torrent submitted for a request
        and ``False`` for any re-send while the claim is still held. Backed by a
        redis ``SET NX`` with a TTL long enough to cover the slowest magnet
        metadata resolve (~180s) plus mapping. When redis is unavailable the
        claim is granted (fail-open) so the single-submission happy path never
        breaks — the guard only exists to swallow accidental duplicates.
        """
        redis = getattr(container, "redis", None)
        if redis is None:
            return True
        key = f"kuro:torrent_provide:{code}"
        try:
            # SET key val NX EX=300 — only sets when absent; auto-expires.
            ok = await asyncio.wait_for(
                redis.set(key, "1", nx=True, ex=300), timeout=5.0,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open on any redis blip
            log.warning("torrent_provide.claim_failed", code=code, error=str(exc)[:160])
            return True
        return bool(ok)

    async def _release_torrent_provide(code: str) -> None:
        """Release the torrent-provide claim (e.g. after an enqueue failure) so
        the admin can immediately retry without waiting out the TTL."""
        redis = getattr(container, "redis", None)
        if redis is None:
            return
        try:
            await asyncio.wait_for(
                redis.delete(f"kuro:torrent_provide:{code}"), timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            pass

    def _allowed(q: CallbackQuery, permission: Permission) -> bool:
        user = getattr(q, "nf_user", None)
        return bool(user and auth.has_permission(user, permission))

    async def _check_shift(q: CallbackQuery) -> bool:
        """Verify the user is allowed to act on the Control Center (log channel).

        Returns True if allowed; shows a blocked-alert + Request Takeover button if not.
        The owner always bypasses.
        """
        from nekofetch.services.shift_service import ShiftService
        from nekofetch.ui.duty_board import blocked_alert
        shift = ShiftService(container)
        user_id = q.from_user.id
        can, reason = await shift.can_act("logcc", user_id)
        if can:
            return True
        # Blocked — show alert with takeover option
        kb = keyboard(
            [("🔵 Request Takeover", cb("shift", "takeover", "logcc"))],
        )
        await q.answer(blocked_alert(reason, "logcc"), show_alert=True)
        try:
            await q.message.reply(
                blocked_alert(reason, "logcc"),
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception:
            pass
        return False

    async def _guard(q: CallbackQuery, permission: Permission) -> bool:
        """Permission + shift gate for Control Center actions."""
        if not _allowed(q, permission):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return False
        if permission == Permission.QUEUE_DOWNLOADS:
            return await _check_shift(q)
        return True

    def _scope_label(req) -> str:
        if req.episodes:
            return L(M.SCOPE_SEASON_EPS, n=req.season or 1,
                     eps=", ".join(map(str, req.episodes)))
        if req.season:
            return L(M.SCOPE_SEASON, n=req.season)
        return req.scope.replace("_", " ").title()

    async def _render_list(q: CallbackQuery, page: int) -> None:
        from nekofetch.services.request_service import RequestService

        pending = await RequestService(container).list_pending()
        back = [(L(M.BTN_BACK), cb("admin", "home"))]
        if not pending:
            caption = f"{L(M.REVIEW_TITLE)}\n\n{L(M.REVIEW_EMPTY)}"
            await show(client, q.message, caption, keyboard(back))
            return
        items = [
            (L(M.REVIEW_ROW, code=r.code, title=r.anime_title[:28]),
             cb("staff", "rdetail", r.code))
            for r in pending
        ]
        kb = paginate(items, page=page, nav_action="staff|rpage", page_size=PAGE_SIZE)
        kb.inline_keyboard.append(keyboard(back).inline_keyboard[0])
        caption = f"{L(M.REVIEW_TITLE)}\n\n{L(M.REVIEW_COUNT, n=len(pending))}"
        await show(client, q.message, caption, kb)

    @client.on_callback_query(filters.regex(r"^staff\|requests"))
    async def _requests(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q, Permission.REVIEW_REQUESTS):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await q.answer()
        parts = q.data.split("|")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await _render_list(q, page)

    @client.on_callback_query(filters.regex(r"^staff\|rpage"))
    async def _rpage(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q, Permission.REVIEW_REQUESTS):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        await q.answer()
        await _render_list(q, int(q.data.split("|")[-1]))

    @client.on_callback_query(filters.regex(r"^staff\|rdetail"))
    async def _detail(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q, Permission.REVIEW_REQUESTS):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        from nekofetch.services.request_service import RequestService

        code = q.data.split("|", 2)[2]
        try:
            req = await RequestService(container).get(code)
        except NekoFetchError:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            await _render_list(q, 0)
            return
        await q.answer()
        caption = (
            f"{L(M.REVIEW_DETAIL_TITLE, code=req.code)}\n\n"
            + L(M.REVIEW_DETAIL_BODY, anime=req.anime_title, status=req.status,
                scope=_scope_label(req), source=req.source, by=user_label(req.user))
        )
        kb = keyboard(
            [(L(M.ADMIN_BTN_TELEGRAM), cb("staff", "rsource", code, "telegram")),
             (L(M.ADMIN_BTN_WEBSITE), cb("staff", "rsource", code, "website"))],
            [(L(M.ADMIN_BTN_TORRENT), cb("staff", "rsource", code, "torrent")),
             (L(M.ADMIN_BTN_DDL), cb("staff", "rsource", code, "ddl"))],
            [(L(M.ADMIN_BTN_REJECT), cb("staff", "rreject", code))],
            [(L(M.BTN_BACK), cb("staff", "requests", 0))],
        )
        await show(client, q.message, caption, kb)

    @client.on_callback_query(filters.regex(r"^staff\|rsource"))
    async def _source_select(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.log_channel_service import LogChannelService
        from nekofetch.services.request_service import RequestService

        parts = q.data.split("|", 3)
        code, chosen_source = parts[2], parts[3]

        # This request is being assigned: lock the buttons against a double-tap.
        # Delete the card AND its divider — once consumed, both go away.
        await lock_buttons(q)
        await LogChannelService(container).clear_request_markers(code, delete_divider=True)

        # Fetch the request to get franchise data for the mapping step.
        try:
            req = await RequestService(container).get(code)
        except NekoFetchError:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        franchise = req.franchise_data or {}
        title = franchise.get("title") or req.anime_title

        if chosen_source == "telegram":
            # Telegram is MANUAL-ONLY for the Kuro Sōden bots — there is no
            # automatic Telegram source. Skip the auto/manual chooser and go
            # straight to the manual intake flow (admin drops + names files).
            await q.answer()
            kb = keyboard(
                [(L(M.ADMIN_BTN_MANUAL), cb("staff", "rtgmode", code, "manual"))],
                [(L(M.BTN_BACK), cb("staff", "rdetail", code))],
            )
            await show(client, q.message, L(M.ADMIN_TG_CHOOSE), kb)
            return

        if chosen_source == "website":
            # Website sources: franchise-map-first then availability scan.
            # Step 1: Walk the AniList franchise graph for per-entry titles.
            await q.answer()
            ff = FranchiseFlowService(container)
            franchise_entries = await _walk_franchise_for_mapping(
                container, franchise, req.anime_doc_id or ""
            )
            mapping = ff.build_mapping(franchise, req.anime_doc_id or "",
                                       franchise_entries=franchise_entries)
            # Single entry (one season, no movies/OVAs/specials) — ask AniZone
            # question then go straight to the website report.
            if len(mapping.entries) <= 1:
                # Serialize whatever the mapping produced. If the franchise walk
                # came back empty (0 entries — e.g. a movie-only title or a walk
                # failure), synthesize ONE entry from the request itself so the
                # AniZone slug prompt still has something to map. AniZone always
                # needs a slug, so an empty list here would dead-end the flow.
                entry_dicts = [{
                    "anilist_id": e.anilist_id,
                    "kind": e.kind.value,
                    "season_number": e.season_number,
                    "season_part": e.season_part,
                    "title": e.title,
                    "episodes": e.episodes,
                    "included": e.included,
                } for e in mapping.entries]
                if not entry_dicts:
                    entry_dicts = [{
                        "anilist_id": None,
                        "kind": "season",
                        "season_number": 1,
                        "season_part": None,
                        "title": mapping.root_title or title,
                        "episodes": franchise.get("franchise_episodes"),
                        "included": True,
                    }]
                # Store a minimal mapping in FSM so the AniZone flow can use it.
                await fsm.set(q.from_user.id, STATE_FRANCHISE_MAP,
                              code=code, source="website",
                              mapping={
                                  "entries": entry_dicts,
                                  "root_title": mapping.root_title,
                                  "anime_doc_id": mapping.anime_doc_id,
                              })
                await q.answer()
                kb = keyboard(
                    [("✅ Yes, use AniZone", cb("anizone", "yes", code))],
                    [("❌ No, skip AniZone", cb("anizone", "no", code))],
                    [(L(M.BTN_BACK), cb("staff", "rdetail", code))],
                )
                await show(client, q.message,
                           "<b>🤔 AniZone Integration</b>\n\n"
                           "Would you like to include AniZone as a source?\n"
                           "AniZone uses different titles and may need manual slug mapping.",
                           kb)
                return
            # Rotating artwork from this anime's own gallery.
            backdrop_url = await _anime_backdrop(container, req)
            # Store mapping data in FSM
            await fsm.set(q.from_user.id, STATE_FRANCHISE_MAP,
                          code=code, source="website",
                          mapping={
                              "entries": [{
                                  "anilist_id": e.anilist_id,
                                  "kind": e.kind.value,
                                  "season_number": e.season_number,
                                  "season_part": e.season_part,
                                  "title": e.title,
                                  "episodes": e.episodes,
                                  "included": e.included,
                              } for e in mapping.entries],
                              "root_title": mapping.root_title,
                              "anime_doc_id": mapping.anime_doc_id,
                          },
                          backdrop_url=backdrop_url)
            screen = franchise_map_selection(mapping, backdrop_url=backdrop_url)
            from nekofetch.ui.screens import send_screen
            await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
            return

        if chosen_source == "ddl":
            # DDL is provide-only — there's nothing to search, so skip the
            # search/provide mode chooser and prompt for the archive link(s)
            # directly. Mirrors the torrent "provide" branch of _torrent_mode.
            await q.answer()
            kb = keyboard([(L(M.BTN_BACK), _source_back_cb(code))])
            card = await show(client, q.message,
                              L(M.DDL_PROVIDE_PROMPT, title=title), kb)
            # Re-entering the provide screen is an explicit "start over" — drop any
            # stale duplicate-guard claim so a fresh link is accepted immediately.
            await _release_torrent_provide(code)
            await fsm.set(q.from_user.id, STATE_DDL_PROVIDE,
                          code=code, title=title,
                          prompt_chat_id=card.chat.id, prompt_message_id=card.id)
            await _arm_reply(container.redis, card.chat.id,
                             STATE_DDL_PROVIDE, code=code, title=title,
                             prompt_chat_id=card.chat.id, prompt_message_id=card.id)
            return

        # Torrent: present a mode choice — search Nyaa or provide a magnet/.torrent.
        await q.answer()
        title = (req.franchise_data or {}).get("title") or req.anime_title
        kb = keyboard(
            [(L(M.TORRENT_BTN_SEARCH), cb("staff", "rtmode", code, "search")),
             (L(M.TORRENT_BTN_PROVIDE), cb("staff", "rtmode", code, "provide"))],
            [(L(M.BTN_BACK), _source_back_cb(code))],
        )
        await show(client, q.message, L(M.TORRENT_MODE_PROMPT, title=title), kb)

    _TPAGE = 6

    async def _render_torrent_page(msg, code: str, cands: list[dict],
                                   page: int, title: str) -> None:
        start = page * _TPAGE
        page_items = cands[start:start + _TPAGE]
        rows = [[(L(M.TORRENT_BTN_AUTO), cb("staff", "rtauto", code))]]
        for i, c in enumerate(page_items, start=start):
            rows.append([(c["label"][:48], cb("staff", "rtpick", code, i))])
        nav = []
        if page > 0:
            nav.append((L(M.BTN_PREV), cb("staff", "rtpage", code, page - 1)))
        if start + _TPAGE < len(cands):
            nav.append((L(M.BTN_NEXT), cb("staff", "rtpage", code, page + 1)))
        if nav:
            rows.append(nav)
        rows.append([(L(M.BTN_BACK), cb("staff", "rsource", code, "torrent"))])
        caption = (
            f"{L(M.TORRENT_TITLE, title=title)}\n\n"
            f"{L(M.TORRENT_INTRO, n=len(cands))}"
        )
        await show(client, msg, caption, keyboard(*rows))

    @client.on_callback_query(filters.regex(r"^staff\|rtmode"))
    async def _torrent_mode(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        parts = q.data.split("|")
        code, mode = parts[2], parts[3]
        from nekofetch.services.request_service import RequestService

        req = await RequestService(container).get(code)
        title = (req.franchise_data or {}).get("title") or req.anime_title
        await q.answer()

        if mode == "search":
            back = keyboard([(L(M.BTN_BACK), cb("staff", "rsource", code, "torrent"))])
            loading = await show(client, q.message,
                                 L(M.TORRENT_LOADING, title=title), back)
            try:
                stubs = (await container.sources.get("nyaa").search(title))[:24]
            except Exception:
                stubs = []
            if not stubs:
                await show(client, loading, L(M.TORRENT_EMPTY, title=title), back)
                return
            cands = [{"ref": s.source_ref, "label": s.title} for s in stubs]
            await fsm.set(q.from_user.id, STATE_TORRENT,
                          code=code, title=title, cands=cands)
            await _render_torrent_page(loading, code, cands, 0, title)
        else:
            kb = keyboard([(L(M.BTN_BACK), cb("staff", "rsource", code, "torrent"))])
            card = await show(client, q.message,
                              L(M.TORRENT_PROVIDE_PROMPT, title=title), kb)
            # Entering (or re-entering) the provide screen is an explicit
            # "start over" — clear any stale duplicate-guard claim so a fresh
            # magnet/.torrent is accepted immediately instead of waiting out the
            # previous claim's TTL.
            await _release_torrent_provide(code)
            # Remember the prompt card so the reply handlers can edit it in place
            # (rotating artwork + caption swap) instead of spawning a new message.
            await fsm.set(q.from_user.id, STATE_TORRENT_PROVIDE,
                          code=code, title=title,
                          prompt_chat_id=card.chat.id, prompt_message_id=card.id)
            await _arm_reply(container.redis, card.chat.id,
                             STATE_TORRENT_PROVIDE, code=code, title=title,
                             prompt_chat_id=card.chat.id, prompt_message_id=card.id)

    async def _torrent_queue(q: CallbackQuery, idx: int) -> None:
        _, data = await fsm.get(q.from_user.id)
        cands = data.get("cands", [])
        code = data.get("code")
        title = data.get("title", "")
        if not code or idx >= len(cands):
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        chosen = cands[idx]
        await q.answer()

        ref = chosen["ref"]
        ref_data = json.loads(ref)
        torrent_url = ref_data.get("torrent_url")

        if torrent_url:
            from nekofetch.sources._torrent import order_episodes, torrent_files

            try:
                import httpx as _httpx

                async with _httpx.AsyncClient(
                    timeout=30, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as hc:
                    resp = await hc.get(torrent_url)
                    resp.raise_for_status()
                _name, files = torrent_files(resp.content)
                ordered = order_episodes(files)
                await _show_torrent_mapping(
                    q.message, q.from_user.id, code, ref, ordered, title,
                )
                return
            except Exception:
                log.debug("torrent_queue.parse_failed", code=code)

        await _torrent_enqueue(q.message, q.from_user.id, code, ref, title)

    @client.on_callback_query(filters.regex(r"^staff\|rtpage"))
    async def _torrent_page(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        await q.answer()
        parts = q.data.split("|")
        code, page = parts[2], int(parts[3])
        _, data = await fsm.get(q.from_user.id)
        await _render_torrent_page(q.message, code, data.get("cands", []), page,
                                   data.get("title", ""))

    @client.on_callback_query(filters.regex(r"^staff\|rtpick"))
    async def _torrent_pick(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        await _torrent_queue(q, int(q.data.split("|")[3]))

    @client.on_callback_query(filters.regex(r"^staff\|rtauto"))
    async def _torrent_auto(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        await _torrent_queue(q, 0)  # candidates are already ranked best-first

    # ── torrent provide: magnet link handler ──────────────────────────────
    # group 12 so it doesn't shadow the channel guard in group 9.

    @client.on_message(filters.text & ~filters.command(["start"]), group=12)
    async def _torrent_magnet_reply(client: Client, message: Message) -> None:
        """Receive a magnet link in STATE_TORRENT_PROVIDE or STATE_MISSING_SOURCE."""
        text = (message.text or "").strip()
        if not text.startswith("magnet:"):
            return  # not a magnet link — let other handlers try

        state, data, via_channel = await _resolve_reply_flow(
            message, STATE_TORRENT_PROVIDE, STATE_MISSING_SOURCE,
        )
        if state not in (STATE_TORRENT_PROVIDE, STATE_MISSING_SOURCE):
            return
        # A magnet only tops up a torrent request; a ddl top-up wants http links.
        if state == STATE_MISSING_SOURCE and (data.get("source") or "") == "ddl":
            return

        code = data.get("code")
        title = data.get("title", "")
        if not code:
            return

        # ── Double-submission guard ────────────────────────────────────────
        # Magnet metadata resolution can take up to ~180s. If the ack card
        # doesn't visibly update (e.g. a connection blip), the admin naturally
        # re-sends the same magnet — which previously spawned a SECOND download
        # job. We take a short-lived redis lock keyed by request code: the FIRST
        # magnet claims it; any re-send while the first is still in-flight (or
        # for a few minutes after) finds the lock held, so we just delete the
        # duplicate message and drop it. The awaited-reply flow is consumed at
        # the very end, once the work has actually completed.
        if not await _claim_torrent_provide(code):
            try:
                await message.delete()
            except Exception:
                pass
            log.info("torrent_magnet.duplicate_dropped", code=code)
            return

        # Delete the admin's message (magnet links are ugly / sensitive).
        try:
            await message.delete()
        except Exception:
            pass

        import re as _re
        from urllib.parse import parse_qs, unquote, urlparse
        hash_m = _re.search(r"btih:([0-9a-fA-F]{40}|[0-9a-zA-Z]{32})", text)
        info_hash = hash_m.group(1) if hash_m else ""

        # Classify audio from the magnet's own display name (dn=…) — the release
        # name carries "Dual Audio"/"Multi" even when the request title doesn't.
        # Fall back to the request title if the magnet has no dn.
        try:
            _dn = (parse_qs(urlparse(text).query).get("dn") or [""])[0]
            _release_name = unquote(_dn).strip()
        except Exception:
            _release_name = ""
        from nekofetch.sources.nyaa import classify_audio as _classify_audio
        _audio_cls = _classify_audio(_release_name or title)
        magnet_ref = json.dumps({
            "magnet": text, "info_hash": info_hash, "title": title,
            "release_name": _release_name,
            "dual_audio": _audio_cls["dual_audio"],
            "audio": _audio_cls["audio"],
        })

        # Consume the awaited-reply flow now that the duplicate guard has let
        # this magnet through — clears the per-user FSM and disarms the
        # chat-scoped channel marker so nothing else treats a later message as
        # this reply.
        if via_channel:
            await _finish_channel_reply(message, data)
        elif message.from_user:
            await fsm.clear(message.from_user.id)

        user_id = message.from_user.id if message.from_user else 0

        # Retrieve the stored prompt card to edit it in place.
        prompt_chat_id = data.get("prompt_chat_id", message.chat.id)
        prompt_message_id = data.get("prompt_message_id")
        try:
            prompt_msg = await client.get_messages(prompt_chat_id, prompt_message_id) if prompt_message_id else None
        except Exception:
            prompt_msg = None

        _back_kb = keyboard(
            [(L(M.BTN_BACK), cb("staff", "rsource", code, "torrent"))],
        )

        async def _ack(text: str, kb: InlineKeyboardMarkup | None = None) -> Message:
            if prompt_msg:
                return await show(client, prompt_msg, text, kb)
            return await client.send_message(
                message.chat.id, text, reply_markup=kb, parse_mode=ParseMode.HTML,
            )

        # Update the card IMMEDIATELY — before the (potentially ~180s) metadata
        # resolve — so the admin sees "Resolving metadata…" the instant their
        # magnet is accepted, plus a Back button to bail out. We then edit this
        # same card in place once resolution finishes.
        ack = await _ack(L(M.TORRENT_MAGNET_ACK), _back_kb)

        # Try to resolve magnet metadata for mapping.
        from nekofetch.sources._torrent import order_episodes, torrent_files

        try:
            from nekofetch.sources.nyaa import NyaaSource

            ns = NyaaSource()
            raw = await ns._resolve_magnet(text)
            await ns.close()
            _name, files = torrent_files(raw)
            ordered = order_episodes(files)

            # The resolved torrent name + its file names are an ADDITIONAL audio
            # signal (they carry "Dual Audio" even when dn=/title don't). Combine
            # them with the dn= verdict and keep the STRONGEST — multi > dual >
            # single — so a "Dual Audio" dn is never downgraded by a BDRip whose
            # internal files only mention the codec, and vice-versa.
            _blob = " ".join(
                [_name or ""] + [str(f.get("path") or f.get("name") or "") for f in files]
            )
            _rc = _classify_audio(_blob or _release_name or title)
            _rank = {"single": 0, "dual": 1, "multi": 2}
            _best = max(_audio_cls["audio"], _rc["audio"],
                        key=lambda a: _rank.get(a, 0))
            magnet_ref = json.dumps({
                "magnet": text, "info_hash": info_hash, "title": title,
                "release_name": _release_name,
                "dual_audio": _best in ("dual", "multi"),
                "audio": _best, "audio_kind": _best,
            })

            await _show_torrent_mapping(
                ack, user_id, code, magnet_ref, ordered, title,
            )
            return
        except Exception:
            log.debug("torrent_magnet.metadata_resolve_failed", code=code)

        # Fallback: enqueue without mapping (reuse the ack card in place).
        await _torrent_enqueue(ack, user_id, code, magnet_ref, title)

    # ── torrent provide: .torrent file handler ────────────────────────────

    @client.on_message(filters.document, group=12)
    async def _torrent_file_reply(client: Client, message: Message) -> None:
        """Receive a .torrent file from the admin in STATE_TORRENT_PROVIDE."""
        doc = message.document
        if not doc:
            return
        fname = getattr(doc, "file_name", "") or ""
        mime = getattr(doc, "mime_type", "") or ""
        if not (fname.endswith(".torrent") or "bittorrent" in mime):
            return  # not a torrent file

        state, data, via_channel = await _resolve_reply_flow(
            message, STATE_TORRENT_PROVIDE, STATE_MISSING_SOURCE,
        )
        if state not in (STATE_TORRENT_PROVIDE, STATE_MISSING_SOURCE):
            return
        # A .torrent only tops up a torrent request, never a ddl one.
        if state == STATE_MISSING_SOURCE and (data.get("source") or "") == "ddl":
            return

        code = data.get("code")
        title = data.get("title", "")
        if not code:
            return

        # Double-submission guard (see _torrent_magnet_reply): the first
        # .torrent claims the slot; re-sends while it's held are dropped.
        if not await _claim_torrent_provide(code):
            log.info("torrent_file.duplicate_dropped", code=code)
            return

        from nekofetch.sources._torrent import torrent_files, order_episodes
        from nekofetch.ui.torrent_screens import format_torrent_summary

        raw = await message.download(in_memory=True)
        raw_bytes = bytes(raw.getbuffer()) if hasattr(raw, "getbuffer") else raw

        try:
            torrent_name, files = torrent_files(raw_bytes)
            ordered = order_episodes(files)
        except Exception:
            await _release_torrent_provide(code)  # parse failed — allow retry
            await message.reply("Failed to parse torrent file.",
                                parse_mode=ParseMode.HTML)
            return

        summary = format_torrent_summary(torrent_name, ordered)

        # Store the raw .torrent bytes in a temp file for later download use.
        from pathlib import Path
        work = Path(container.env.storage_path) / "work" / code
        work.mkdir(parents=True, exist_ok=True)
        # Content-address the stored .torrent so a coverage top-up (a SECOND
        # torrent for the same request) doesn't clobber the first one's file.
        import hashlib as _hashlib
        _digest = _hashlib.sha1(raw_bytes).hexdigest()[:12]
        torrent_path = work / f".provided-{_digest}.torrent"
        torrent_path.write_bytes(raw_bytes)

        # Build source_ref that points to the local .torrent file. Classify the
        # audio from the torrent's own name (the release name carries "Dual Audio"
        # etc. even when the request title doesn't).
        from nekofetch.sources.nyaa import classify_audio as _classify_audio
        _audio_cls = _classify_audio(torrent_name or title)
        ref = json.dumps({
            "torrent_path": str(torrent_path),
            "torrent_name": torrent_name,
            "title": title,
            "dual_audio": _audio_cls["dual_audio"],
            "audio": _audio_cls["audio"],
        })

        # Store ref + candidates in FSM for confirm step.
        await fsm.set(
            message.from_user.id if message.from_user else 0,
            STATE_TORRENT_PROVIDE,
            code=code, title=title, ref=ref,
            torrent_name=torrent_name,
        )

        # Delete the admin's file message.
        try:
            await message.delete()
        except Exception:
            pass

        # Retrieve the stored prompt card to edit it in place.
        prompt_chat_id = data.get("prompt_chat_id", message.chat.id)
        prompt_message_id = data.get("prompt_message_id")
        try:
            prompt_msg = await client.get_messages(prompt_chat_id, prompt_message_id) if prompt_message_id else None
        except Exception:
            prompt_msg = None

        kb = keyboard(
            [(L(M.TORRENT_BTN_CONFIRM), cb("staff", "rtfileok", code))],
            [(L(M.BTN_BACK), cb("staff", "rsource", code, "torrent"))],
        )
        text = L(M.TORRENT_FILE_ACK, name=torrent_name, summary=summary)
        if prompt_msg:
            await show(client, prompt_msg, text, kb)
        else:
            await client.send_message(
                message.chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
            )

    @client.on_callback_query(filters.regex(r"^staff\|rtfileok"))
    async def _torrent_file_confirm(_: Client, q: CallbackQuery) -> None:
        """Confirm a provided .torrent file — show mapping before enqueue."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        _, data = await fsm.get(q.from_user.id)
        code = data.get("code")
        ref = data.get("ref")
        if not code or not ref:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()

        from nekofetch.sources._torrent import order_episodes, torrent_files

        ref_data = json.loads(ref)
        torrent_path = ref_data.get("torrent_path")
        if not torrent_path:
            await _torrent_enqueue(
                q.message, q.from_user.id, code, ref, data.get("title", ""),
            )
            return

        from pathlib import Path

        try:
            raw = Path(torrent_path).read_bytes()
            _name, files = torrent_files(raw)
            ordered = order_episodes(files)
        except Exception:
            await _torrent_enqueue(
                q.message, q.from_user.id, code, ref, data.get("title", ""),
            )
            return

        await _show_torrent_mapping(
            q.message, q.from_user.id, code, ref, ordered,
            data.get("title", ""),
        )

    # ── DDL provide: direct-link handler ──────────────────────────────────
    # group 13 (not 12): the torrent magnet handler in group 12 also matches
    # `filters.text`, and Pyrogram runs only the first matching handler per
    # group. A separate group lets BOTH run — the magnet handler early-returns
    # for an http(s) link, and this one early-returns for a magnet — so the two
    # provide flows never shadow each other.

    @client.on_message(filters.text & ~filters.command(["start"]), group=13)
    async def _ddl_link_reply(client: Client, message: Message) -> None:
        """Receive direct download links in STATE_DDL_PROVIDE or STATE_MISSING_SOURCE."""
        text = (message.text or "").strip()
        # Accept one or more whitespace/newline-separated http(s) links; the DDL
        # ref carries an `archives` list so a single message can seed several.
        urls = [tok for tok in text.split()
                if tok.startswith(("http://", "https://"))]
        if not urls:
            return  # not a direct link — let other handlers try

        state, data, via_channel = await _resolve_reply_flow(
            message, STATE_DDL_PROVIDE, STATE_MISSING_SOURCE,
        )
        if state not in (STATE_DDL_PROVIDE, STATE_MISSING_SOURCE):
            return
        # http links only top up a DDL-type request. If the admin explicitly chose
        # the torrent path (source torrent/nyaa), let the magnet/.torrent handlers
        # take it instead — accept here only for the ddl choice (or an unset legacy
        # source, which defaulted to ddl-style http links).
        if state == STATE_MISSING_SOURCE and (data.get("source") or "ddl") not in ("ddl", ""):
            return

        code = data.get("code")
        title = data.get("title", "")
        if not code:
            return

        # Double-submission guard (shared per-code claim with the torrent flow —
        # only one provide flow is ever active for a request at a time).
        if not await _claim_torrent_provide(code):
            try:
                await message.delete()
            except Exception:
                pass
            log.info("ddl_link.duplicate_dropped", code=code)
            return

        # Delete the admin's message (direct links are long / noisy).
        try:
            await message.delete()
        except Exception:
            pass

        # Build or append to the DDL ref. For STATE_MISSING_SOURCE, merge with
        # what the request already has; for STATE_DDL_PROVIDE, start fresh.
        if state == STATE_MISSING_SOURCE:
            from nekofetch.services.request_service import RequestService
            req = await RequestService(container).get(code)
            existing_ref = json.loads(req.source_ref) if req.source_ref else {}
            existing_archives = existing_ref.get("archives", [])
            ddl_ref = json.dumps({
                "archives": existing_archives + [{"url": u, "season": None} for u in urls],
                "title": title or existing_ref.get("title", ""),
                "code": code,
            })
        else:
            ddl_ref = json.dumps({
                "archives": [{"url": u, "season": None} for u in urls],
                "title": title,
                "code": code,
            })

        # Consume the awaited-reply flow now the duplicate guard has let this
        # link through — clears the per-user FSM / disarms the channel marker.
        if via_channel:
            await _finish_channel_reply(message, data)
        elif message.from_user:
            await fsm.clear(message.from_user.id)

        user_id = message.from_user.id if message.from_user else 0

        # Retrieve the stored prompt card so we edit it in place.
        prompt_chat_id = data.get("prompt_chat_id", message.chat.id)
        prompt_message_id = data.get("prompt_message_id")
        try:
            prompt_msg = await client.get_messages(prompt_chat_id, prompt_message_id) if prompt_message_id else None
        except Exception:
            prompt_msg = None

        if prompt_msg:
            ack = await show(client, prompt_msg, L(M.DDL_LINK_ACK), None)
        else:
            ack = await client.send_message(
                message.chat.id, L(M.DDL_LINK_ACK), parse_mode=ParseMode.HTML,
            )

        # No mapping preview — the archive contents are unknown until it's fetched
        # and extracted (DdlSource.get_episodes handles that). Enqueue directly.
        await _torrent_enqueue(ack, user_id, code, ddl_ref, title,
                               source_name="ddl")

    # ── torrent mapping: build + show mapping before enqueue ──────────────

    async def _show_torrent_mapping(
        msg: Message,
        user_id: int,
        code: str,
        source_ref: str,
        ordered_files: list[dict],
        title: str,
    ) -> None:
        """Build a franchise->torrent mapping and present it for confirmation.

        Fetches episode titles from Jikan for gap detection. If the request
        has no franchise data (single season, no parts), skips the mapping
        and queues directly.
        """
        from nekofetch.services.franchise_flow import FranchiseFlowService
        from nekofetch.services.request_service import RequestService
        from nekofetch.services.torrent_mapping import (
            build_torrent_mapping,
            fetch_episode_titles_for_franchise,
        )
        from nekofetch.ui.torrent_screens import format_torrent_mapping

        req = await RequestService(container).get(code)
        franchise = req.franchise_data or {}

        ff = FranchiseFlowService(container)
        franchise_entries = await _walk_franchise_for_mapping(
            container, franchise, req.anime_doc_id or ""
        )
        mapping = ff.build_mapping(
            franchise, req.anime_doc_id or "",
            franchise_entries=franchise_entries,
        )

        if len(mapping.entries) <= 1 and not any(
            e.season_part for e in mapping.entries
        ):
            await _torrent_enqueue(msg, user_id, code, source_ref, title)
            return

        # Fetch episode titles from Jikan (best-effort — never blocks the flow).
        ep_titles: dict[int, list[dict]] = {}
        try:
            ep_titles = await fetch_episode_titles_for_franchise(mapping)
        except Exception:
            log.debug("torrent_mapping.jikan_titles_failed", code=code)

        tmapping = build_torrent_mapping(
            ordered_files, mapping, episode_titles=ep_titles,
        )
        summary = format_torrent_mapping(tmapping)

        # Full, uncramped mapping on Telegraph — the card caption can only show a
        # truncated summary (the "unusable" S01 24/12 ‧ S02P2 0/12 view); the
        # Telegraph page lists every entry with its numbered files + missing eps,
        # and its file numbers ARE the keys the "Fix Seasons" grammar uses.
        map_url = await _build_full_mapping_guide(title, tmapping, ordered_files)

        await fsm.set(
            user_id, STATE_TORRENT_MAP,
            code=code, ref=source_ref, title=title,
            torrent_mapping=tmapping.to_dict(),
            # Kept so a manual season override can re-run the cascade + regroup
            # from the raw files (Phase 5). Trimmed to the fields the resolver +
            # mapper read, to stay well under Redis value limits.
            ordered_files=[
                {k: f.get(k) for k in
                 ("index", "name", "path", "season", "episode",
                  "season_explicit", "kind", "seq")}
                for f in ordered_files
            ],
            ep_titles={str(k): v for k, v in ep_titles.items()},
            map_url=map_url or "",
        )

        text = L(M.TORRENT_MAP_CONFIRM, title=L(M.TORRENT_MAP_TITLE),
                 mapping=summary)
        kb = _torrent_map_kb(L, code, map_url or "")
        await show(client, msg, text, kb)

    async def _torrent_enqueue(
        msg: Message,
        user_id: int,
        code: str,
        source_ref: str,
        title: str,
        torrent_mapping_dict: dict | None = None,
        source_name: str = "nyaa",
    ) -> None:
        """Common enqueue path for all torrent/DDL flows.

        ``source_name`` is the plugin the request is bound to — ``"nyaa"`` for
        torrents (the default) or ``"ddl"`` for direct-link archives.
        """
        from nekofetch.services.queue_service import QueueService
        from nekofetch.services.request_service import RequestService

        try:
            await RequestService(container).update_source_ref(
                code, source_name, source_ref,
            )
            if torrent_mapping_dict:
                req = await RequestService(container).get(code)
                fr_dict = dict(req.franchise_data or {})
                fr_dict["_torrent_mapping"] = torrent_mapping_dict
                await RequestService(container).update_franchise_data(
                    code, fr_dict,
                )
            job_id = await QueueService(container).enqueue(code)
        except Exception:
            log.exception("torrent enqueue failed code=%s", code)
            await _release_torrent_provide(code)  # let the admin retry
            await msg.reply(L(M.ERR_GENERIC), parse_mode=ParseMode.HTML)
            return
        await fsm.clear(user_id)
        await _spawn_progress_card(job_id, user_id)
        try:
            await msg.delete()
        except Exception:
            pass

    @client.on_callback_query(filters.regex(r"^staff\|rtmapok"))
    async def _torrent_map_confirm(_: Client, q: CallbackQuery) -> None:
        """Confirm the torrent mapping and queue the download."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        _, data = await fsm.get(q.from_user.id)
        code = data.get("code")
        ref = data.get("ref")
        if not code or not ref:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()
        await _torrent_enqueue(
            q.message, q.from_user.id, code, ref,
            data.get("title", ""),
            torrent_mapping_dict=data.get("torrent_mapping"),
        )

    @client.on_callback_query(filters.regex(r"^staff\|rtmapdet"))
    async def _torrent_map_detail(_: Client, q: CallbackQuery) -> None:
        """Show detailed per-entry file assignments."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.torrent_mapping import TorrentMapping
        from nekofetch.ui.torrent_screens import format_mapping_detail

        parts = q.data.split("|")
        code, page = parts[2], int(parts[3])
        _, data = await fsm.get(q.from_user.id)
        td = data.get("torrent_mapping")
        if not td:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()
        tmapping = TorrentMapping.from_dict(td)
        detail = format_mapping_detail(tmapping, page=page)
        total_pages = len(tmapping.entries) or 1

        nav = []
        if page > 0:
            nav.append((L(M.BTN_PREV), cb("staff", "rtmapdet", code, page - 1)))
        if page + 1 < total_pages:
            nav.append((L(M.BTN_NEXT), cb("staff", "rtmapdet", code, page + 1)))
        rows = []
        if nav:
            rows.append(nav)
        rows.append([
            (L(M.BTN_BACK), cb("staff", "rtmapov", code)),
        ])
        await show(client, q.message, detail, keyboard(*rows))

    @client.on_callback_query(filters.regex(r"^staff\|rtmapov"))
    async def _torrent_map_overview(_: Client, q: CallbackQuery) -> None:
        """Return to the mapping overview from detail view."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.torrent_mapping import TorrentMapping
        from nekofetch.ui.torrent_screens import format_torrent_mapping

        parts = q.data.split("|")
        code = parts[2]
        _, data = await fsm.get(q.from_user.id)
        td = data.get("torrent_mapping")
        if not td:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()
        tmapping = TorrentMapping.from_dict(td)
        summary = format_torrent_mapping(tmapping)

        text = L(M.TORRENT_MAP_CONFIRM, title=L(M.TORRENT_MAP_TITLE),
                 mapping=summary)
        # Stable keyboard, recomputed from the SAME state every time — a Back
        # round-trip reproduces the original card (no dropped buttons / no
        # collapsed Full Mapping button).
        kb = _torrent_map_kb(L, code, data.get("map_url") or "")
        await show(client, q.message, text, kb)

    @client.on_callback_query(filters.regex(r"^staff\|rtmaptgl"))
    async def _torrent_map_toggle(_: Client, q: CallbackQuery) -> None:
        """Toggle inclusion of a franchise entry — or just RENDER the list.

        The action carries either the literal ``list`` (open the screen WITHOUT
        flipping anything — fixes the old bug where opening Toggle Entry via a
        fixed ``idx=0`` immediately de-selected Season 1) or a numeric entry index
        to flip. Selection lives on each entry's ``included`` flag in the FSM bag.
        """
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.torrent_mapping import TorrentMapping
        from nekofetch.services.franchise_flow import FranchiseFlowService

        parts = q.data.split("|")
        code, target = parts[2], parts[3]
        _, data = await fsm.get(q.from_user.id)
        td = data.get("torrent_mapping")
        if not td:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return

        tmapping = TorrentMapping.from_dict(td)
        # Flip only on an explicit numeric tap; ``list`` renders without mutating.
        if target != "list":
            try:
                idx = int(target)
            except ValueError:
                idx = -1
            if 0 <= idx < len(tmapping.entries):
                fe = tmapping.entries[idx].franchise_entry
                fe.included = not fe.included
                data["torrent_mapping"] = tmapping.to_dict()
                await fsm.set(q.from_user.id, STATE_TORRENT_MAP, **data)
        await q.answer()

        # Entry list: real titles (Season 01 — Title / Movie: Name / OVA: Name),
        # a clear "(no files yet)" marker for an included entry the source shipped
        # zero files for, and the ✅/❌ selection state.
        rows = []
        for i, me in enumerate(tmapping.entries):
            fe = me.franchise_entry
            icon = "✅" if fe.included else "❌"
            label = f"{icon} {FranchiseFlowService.entry_label(fe)}"
            if me.actual:
                label += f"  ·  {me.actual} file{'s' if me.actual != 1 else ''}"
            elif fe.included:
                label += "  ·  ⚠️ no files yet"
            rows.append([(label[:60], cb("staff", "rtmaptgl", code, i))])
        rows.append([(L(M.BTN_BACK), cb("staff", "rtmapov", code))])

        text = (f"<b>{L(M.TORRENT_MAP_TOGGLE_TITLE)}</b>\n\n"
                f"{L(M.TORRENT_MAP_TOGGLE_HELP)}")
        await show(client, q.message, text, keyboard(*rows))

    # ── manual season override (Phase 5 cascade fallback) ─────────────────────

    def _rebuild_mapping_from_fsm(data: dict, overrides: dict[int, int] | None,
                                  junk_indices: set[int] | None = None):
        """Re-run build_torrent_mapping from the FSM-cached raw files, applying
        any manual season overrides and admin-marked junk indices. Returns a
        TorrentMapping (or None)."""
        from nekofetch.services.franchise_flow import FranchiseMapping
        from nekofetch.services.torrent_mapping import build_torrent_mapping

        td = data.get("torrent_mapping")
        ordered = data.get("ordered_files")
        if not td or not ordered:
            return None
        # Reconstruct the franchise from the stored mapping entries (they carry
        # season_number/part/episodes — everything the cascade + mapper need).
        from nekofetch.services.torrent_mapping import TorrentMapping
        prev = TorrentMapping.from_dict(td)
        franchise = FranchiseMapping(
            anime_doc_id="", root_title="",
            entries=[e.franchise_entry for e in prev.entries],
        )
        ep_titles = {int(k): v for k, v in (data.get("ep_titles") or {}).items()}
        if junk_indices is None:
            junk_indices = set(data.get("junk_indices") or [])
        return build_torrent_mapping(
            ordered, franchise, episode_titles=ep_titles,
            season_overrides=overrides, junk_indices=junk_indices,
        )

    @client.on_callback_query(filters.regex(r"^staff\|rtmapfull"))
    async def _torrent_map_full(_: Client, q: CallbackQuery) -> None:
        """Render the structured, read-only Full Mapping inline (fallback when no
        Telegraph page exists, and always tappable so the button never collapses
        into a caption hyperlink)."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.torrent_mapping import TorrentMapping
        from nekofetch.ui.torrent_screens import format_full_mapping

        code = q.data.split("|")[2]
        _, data = await fsm.get(q.from_user.id)
        td = data.get("torrent_mapping")
        if not td:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()
        tmapping = TorrentMapping.from_dict(td)
        ep_titles = {int(k): v for k, v in (data.get("ep_titles") or {}).items()}
        text = format_full_mapping(
            tmapping, episode_titles=ep_titles, torrent_name=data.get("title", ""),
        )
        kb = keyboard([(L(M.BTN_BACK), cb("staff", "rtmapov", code))])
        await show(client, q.message, text, kb)

    @client.on_callback_query(filters.regex(r"^staff\|rtmapedit"))
    async def _torrent_map_edit(_: Client, q: CallbackQuery) -> None:
        """Edit-mapping hub: button-driven fixes (reassign seasons, mark a file as
        'not an episode') plus a paste-bulk-edit escape hatch."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|")[2]
        _, data = await fsm.get(q.from_user.id)
        if not data.get("torrent_mapping"):
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()
        rows = [
            [(L(M.TORRENT_MAP_BTN_FIX), cb("staff", "rtmapfix", code))],
            [(L(M.TORRENT_MAP_BTN_JUNK), cb("staff", "rtmapjunk", code, 0))],
            [(L(M.BTN_BACK), cb("staff", "rtmapov", code))],
        ]
        text = (f"<b>{L(M.TORRENT_MAP_EDIT_TITLE)}</b>\n\n"
                f"{L(M.TORRENT_MAP_EDIT_HELP)}")
        await show(client, q.message, text, keyboard(*rows))

    @client.on_callback_query(filters.regex(r"^staff\|rtmapjunk"))
    async def _torrent_map_junk(_: Client, q: CallbackQuery) -> None:
        """Mark a file as 'not an episode' (junk) — it moves to the unmatched
        'doesn't belong here' bucket and is never assigned an episode slot. This
        is the direct fix for a stray trailer/preview the auto-classifier missed.
        A ``list`` target renders the file list; a numeric target toggles that
        file's junk state and rebuilds the mapping."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.torrent_mapping import TorrentMapping

        parts = q.data.split("|")
        code, target = parts[2], parts[3]
        _, data = await fsm.get(q.from_user.id)
        td = data.get("torrent_mapping")
        ordered = data.get("ordered_files")
        if not td or not ordered:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return

        junk = set(data.get("junk_indices") or [])
        if target != "list":
            try:
                fi = int(target)
            except ValueError:
                fi = None
            if fi is not None:
                junk.symmetric_difference_update({fi})  # toggle
                data["junk_indices"] = sorted(junk)
                rebuilt = _rebuild_mapping_from_fsm(
                    data, data.get("overrides"), junk_indices=junk,
                )
                if rebuilt is not None:
                    data["torrent_mapping"] = rebuilt.to_dict()
                await fsm.set(q.from_user.id, STATE_TORRENT_MAP, **data)
        await q.answer()

        # List every file with a ✅/🚫 junk marker so the admin can flip any one.
        rows = []
        for f in ordered:
            fi = f.get("index")
            name = (f.get("name") or "")[:44]
            mark = "🚫" if fi in junk else "▫️"
            rows.append([(f"{mark} #{fi} {name}", cb("staff", "rtmapjunk", code, fi))])
        rows = rows[:40]  # keep the keyboard within Telegram limits
        rows.append([(L(M.BTN_BACK), cb("staff", "rtmapedit", code))])
        await show(client, q.message, f"<b>{L(M.TORRENT_MAP_BTN_JUNK)}</b>\n\n"
                   "Tap a file to flag it as <b>not an episode</b> (or un-flag). "
                   "Flagged files won't be downloaded as episodes.",
                   keyboard(*rows))

    @client.on_callback_query(filters.regex(r"^staff\|rtmapfix"))
    async def _torrent_map_fix(_: Client, q: CallbackQuery) -> None:
        """Enter manual season-mapping: prompt the admin to reassign file seasons.

        Shows a Telegraph guide of the detected file→season ranges (names can be
        long / numerous — a telegra.ph page reads far better than a chat message)
        and arms a reply flow that accepts ``<file#> S<season>`` lines.
        """
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        _, data = await fsm.get(q.from_user.id)
        td = data.get("torrent_mapping")
        ordered = data.get("ordered_files")
        if not td or not ordered:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()

        # Build a Telegraph guide listing each file, its current season, and the
        # index the admin references when overriding.
        guide_url = await _build_season_map_guide(data.get("title", ""), ordered)

        # Persist the override-collection state (overrides accumulate across replies).
        await fsm.set(
            q.from_user.id, STATE_TORRENT_MAP_FIX,
            **{**data, "overrides": {},
               "prompt_chat_id": q.message.chat.id,
               "prompt_message_id": q.message.id},
        )
        await _arm_reply(container.redis, q.message.chat.id,
                         STATE_TORRENT_MAP_FIX, code=code)

        prompt = L(M.TORRENT_MAP_FIX_PROMPT)
        if guide_url:
            prompt += f'\n\n<a href="{guide_url}">📋 File → season guide</a>'
        kb = keyboard(
            [(L(M.TORRENT_MAP_FIX_APPLY), cb("staff", "rtmapfixok", code))],
            [(L(M.BTN_BACK), cb("staff", "rtmapov", code))],
        )
        await show(client, q.message, prompt, kb)

    async def _build_full_mapping_guide(
        title: str, tmapping, ordered: list[dict],
    ) -> str | None:
        """Telegraph page: the COMPLETE franchise→torrent mapping — every entry with
        its numbered files, expected counts, and missing episodes.

        This is the "Telegraph display" that replaces the cramped confirm-card
        caption (which truncates for anything past a few seasons). The file numbers
        shown match the ``<file#> S<season>`` re-mapping grammar, so the page also
        serves as the edit-key reference. Returns the page URL, or None when no
        Telegraph token is configured / the call fails (caller falls back to the
        caption-only card)."""
        try:
            token = container.config.thumbnail_channel.telegraph_access_token
            if not token:
                return None
            from nekofetch.providers.metadata.telegraph_client import TelegraphClient
            from nekofetch.ui.torrent_screens import mapping_telegraph_nodes

            torrent_name = ""
            for f in ordered:
                p = f.get("path", "")
                if "/" in p:
                    torrent_name = p.split("/")[0]
                    break
            content = [
                {"tag": "p", "children": [
                    "Reply in chat with lines like  ",
                    {"tag": "code", "children": ["3 S2"]},
                    "  to move file #3 to Season 2. File numbers below are the keys."]},
            ]
            content += mapping_telegraph_nodes(tmapping, torrent_name)
            telegraph = TelegraphClient(token)
            result = await telegraph._call(
                "createPage", title=f"{title} — Franchise Mapping",
                author_name="NekoFetch", content=content,
            )
            await telegraph.close()
            return result.get("url") or (
                f"https://telegra.ph/{result.get('path', '')}"
                if result.get("path") else None
            )
        except Exception:  # noqa: BLE001
            log.debug("torrent_map.full_guide.failed", title=title)
            return None

    async def _build_season_map_guide(title: str, ordered: list[dict]) -> str | None:
        """Telegraph page: numbered file list with current-season + episode, so the
        admin can reference file numbers when reassigning seasons."""
        try:
            token = container.config.thumbnail_channel.telegraph_access_token
            if not token:
                return None
            from nekofetch.providers.metadata.telegraph_client import TelegraphClient
            telegraph = TelegraphClient(token)
            content = [
                {"tag": "h3", "children": [f"{title} — File → Season Map"]},
                {"tag": "p", "children": [
                    "Reply with lines like  3 S2  to move file #3 to Season 2."]},
            ]
            for i, f in enumerate(ordered, start=1):
                s = f.get("season", 1)
                ep = f.get("episode")
                ep_s = f"E{ep:02d}" if isinstance(ep, int) else "E??"
                content.append({"tag": "p", "children": [
                    f"#{i}  [S{s} {ep_s}]  {f.get('name', '')}"]})
            result = await telegraph._call(
                "createPage", title=f"{title} — Season Map",
                author_name="NekoFetch", content=content,
            )
            await telegraph.close()
            return result.get("url") or (
                f"https://telegra.ph/{result.get('path', '')}"
                if result.get("path") else None
            )
        except Exception:  # noqa: BLE001
            log.debug("season_map.telegraph.failed", title=title)
            return None

    @client.on_message(filters.text & ~filters.command(["start"]), group=14)
    async def _torrent_map_fix_reply(_: Client, message: Message) -> None:
        """Collect ``<file#> S<season>`` override lines while in STATE_TORRENT_MAP_FIX."""
        text = (message.text or "").strip()
        # Quick shape gate: at least one "<num> S<num>" token, else let others try.
        if not re.search(r"\b\d+\s+[sS]\s*\d+\b", text):
            return
        state, data, via_channel = await _resolve_reply_flow(
            message, STATE_TORRENT_MAP_FIX,
        )
        if state != STATE_TORRENT_MAP_FIX:
            return

        overrides = dict(data.get("overrides") or {})
        ordered = data.get("ordered_files") or []
        max_idx = len(ordered)
        applied = []
        for m in re.finditer(r"\b(\d+)\s+[sS]\s*(\d+)\b", text):
            file_no = int(m.group(1))
            season = int(m.group(2))
            if 1 <= file_no <= max_idx:
                # Map the 1-based display number to the file's real ``index``.
                real_index = ordered[file_no - 1].get("index")
                if real_index is not None:
                    overrides[real_index] = season
                    applied.append((file_no, season))

        try:
            await message.delete()
        except Exception:
            pass

        data["overrides"] = overrides
        if message.from_user:
            await fsm.set(message.from_user.id, STATE_TORRENT_MAP_FIX, **data)

        if not applied:
            return

        # Re-run the mapping with the accumulated overrides and show the preview.
        tmapping = _rebuild_mapping_from_fsm(data, {int(k): v for k, v in overrides.items()})
        prompt_chat_id = data.get("prompt_chat_id", message.chat.id)
        prompt_message_id = data.get("prompt_message_id")
        try:
            prompt_msg = await client.get_messages(prompt_chat_id, prompt_message_id) if prompt_message_id else None
        except Exception:
            prompt_msg = None

        from nekofetch.ui.torrent_screens import format_torrent_mapping
        applied_str = ", ".join(f"#{n}→S{s}" for n, s in applied)
        body = L(M.TORRENT_MAP_FIX_ACK, applied=applied_str)
        if tmapping:
            data["torrent_mapping"] = tmapping.to_dict()
            if message.from_user:
                await fsm.set(message.from_user.id, STATE_TORRENT_MAP_FIX, **data)
            body += "\n\n" + format_torrent_mapping(tmapping)

        code = data.get("code", "")
        kb = keyboard(
            [(L(M.TORRENT_MAP_FIX_APPLY), cb("staff", "rtmapfixok", code))],
            [(L(M.BTN_BACK), cb("staff", "rtmapov", code))],
        )
        target = prompt_msg or message
        await show(client, target, body, kb)

    @client.on_callback_query(filters.regex(r"^staff\|rtmapfixok"))
    async def _torrent_map_fix_apply(_: Client, q: CallbackQuery) -> None:
        """Commit the manual overrides: fold them into the mapping + return to overview."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        _, data = await fsm.get(q.from_user.id)
        overrides = {int(k): v for k, v in (data.get("overrides") or {}).items()}
        tmapping = _rebuild_mapping_from_fsm(data, overrides or None)
        await q.answer()
        if tmapping:
            data["torrent_mapping"] = tmapping.to_dict()
        # Drop back to STATE_TORRENT_MAP so Confirm/Detail/Toggle work again.
        await _disarm_reply(container.redis, q.message.chat.id)
        data.pop("overrides", None)

        from nekofetch.ui.torrent_screens import format_torrent_mapping
        from nekofetch.services.torrent_mapping import TorrentMapping
        summary = format_torrent_mapping(
            TorrentMapping.from_dict(data["torrent_mapping"])
        ) if data.get("torrent_mapping") else ""
        # Regenerate the full Telegraph mapping so its file→season assignments
        # reflect the overrides just applied (the old page is now stale).
        if tmapping:
            fresh_url = await _build_full_mapping_guide(
                data.get("title", ""), tmapping, data.get("ordered_files") or [])
            if fresh_url:
                data["map_url"] = fresh_url
        await fsm.set(q.from_user.id, STATE_TORRENT_MAP, **data)

        text = L(M.TORRENT_MAP_CONFIRM, title=L(M.TORRENT_MAP_TITLE),
                 mapping=summary)
        kb = _torrent_map_kb(L, code, data.get("map_url") or "")
        await show(client, q.message, text, kb)

    @client.on_callback_query(filters.regex(r"^staff\|rsiteprio"))
    async def _site_priority(_: Client, q: CallbackQuery) -> None:
        """Confirm website provider priority list and queue the request."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.queue_service import QueueService
        from nekofetch.services.request_service import RequestService

        parts = q.data.split("|")
        code = parts[2]
        priority = [p for p in parts[3:] if p]
        if not priority:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        primary = priority[0]
        # A single source stays single (only that site is queried); a multi-source
        # list becomes a ">"-joined priority chain (cross-source dual acquisition).
        priority_str = ">".join(priority)
        # Ack before the slow enqueue/progress-card work — see _torrent_queue for
        # why: answering after the TTL expires raises QueryIdInvalid.
        await q.answer()
        try:
            await RequestService(container).update_source(code, priority_str)
            job_id = await QueueService(container).enqueue(code)
        except NekoFetchError as exc:
            await q.message.reply(getattr(exc, "detail", None) or L(M.ERR_GENERIC))
            return
        await _spawn_progress_card(job_id, q.from_user.id)
        try:
            await q.message.delete()
        except Exception:
            pass

    async def _proceed_website_report(
        src: Message | CallbackQuery,
        code: str,
    ) -> None:
        """Build the website report and show source
        selection buttons. AniZone is intentionally skipped here — its title
        format is incompatible with AniList-based matching, so it has its own
        slug-mapping flow."""
        from nekofetch.core.redis_safe import safe_redis_get, safe_redis_set
        from nekofetch.services.request_service import RequestService
        from nekofetch.services.website_report import build_website_report
        from nekofetch.ui.website_report import render_report

        req = await RequestService(container).get(code)
        franchise = req.franchise_data or {}
        title = franchise.get("title") or req.anime_title

        msg = src.message if isinstance(src, CallbackQuery) else src

        back = keyboard([(L(M.BTN_BACK), cb("staff", "rdetail", code))])
        kb = keyboard(
            [(L(M.SITE_BTN_MIRURO_PRIMARY),
              cb("staff", "rsiteprio", code, "miruro", "anikoto", "kickassanime"))],
            [(L(M.SITE_BTN_ANIKOTO_PRIMARY),
              cb("staff", "rsiteprio", code, "anikoto", "miruro", "kickassanime"))],
            [(L(M.SITE_BTN_KICKASS_PRIMARY),
              cb("staff", "rsiteprio", code, "kickassanime", "miruro", "anikoto"))],
            [(L(M.BTN_BACK), cb("staff", "rdetail", code))],
        )
        cache_key = f"nf:website_report:html:{code}"
        cached = await safe_redis_get(
            container.redis, cache_key, label="website_report.cache_get"
        )
        if isinstance(cached, bytes):
            cached = cached.decode()
        if cached:
            await show(client, msg, cached, kb)
            return

        loading = await show(client, msg, L(M.WEB_REPORT_LOADING, title=title), back)
        try:
            # Always skip anizone in the regular website report — it has its own flow.
            report = await build_website_report(
                container, title=title, franchise=franchise,
                skip_anizone=True,
            )
            rendered = render_report(report)
            await safe_redis_set(
                container.redis,
                cache_key,
                rendered,
                label="website_report.cache_set",
                ex=6 * 60 * 60,
            )
            await show(client, loading, rendered, kb)
        except Exception as exc:
            from nekofetch.core.logging import get_logger
            get_logger(__name__).warning(
                "website_report.render.failed", code=code, error=str(exc)
            )
            await show(client, loading, L(M.WEB_REPORT_FAILED, title=title), back)

    @client.on_callback_query(filters.regex(r"^staff\|rtgmode"))
    async def _tg_mode(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.request_service import RequestService

        parts = q.data.split("|", 3)
        code, mode = parts[2], parts[3]

        if mode == "auto":
            # Show the franchise mapping first before queuing
            await q.answer()
            try:
                req = await RequestService(container).get(code)
            except NekoFetchError:
                await q.answer(L(M.ERR_GENERIC), show_alert=True)
                return
            fr = req.franchise_data or {}
            ff = FranchiseFlowService(container)
            franchise_entries = await _walk_franchise_for_mapping(
                container, fr, req.anime_doc_id or ""
            )
            mapping = ff.build_mapping(fr, req.anime_doc_id or "",
                                       franchise_entries=franchise_entries)
            backdrop_url = await _anime_backdrop(container, req)
            await fsm.set(q.from_user.id, STATE_FRANCHISE_MAP,
                          code=code, source="telegram",
                          mapping={
                              "entries": [{
                                  "anilist_id": e.anilist_id,
                                  "kind": e.kind.value,
                                  "season_number": e.season_number,
                                  "season_part": e.season_part,
                                  "title": e.title,
                                  "episodes": e.episodes,
                                  "included": e.included,
                              } for e in mapping.entries],
                              "root_title": mapping.root_title,
                              "anime_doc_id": mapping.anime_doc_id,
                          },
                          backdrop_url=backdrop_url)
            screen = franchise_map_selection(mapping, backdrop_url=backdrop_url)
            from nekofetch.ui.screens import send_screen
            await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        elif mode == "manual":
            await q.answer()
            try:
                req = await RequestService(container).get(code)
            except NekoFetchError:
                await q.answer(L(M.ERR_GENERIC), show_alert=True)
                return
            fr = req.franchise_data or {}
            # Rotating artwork from this anime's own gallery — every wizard
            # screen shows a different piece of the series' art.
            backdrop_url = await _anime_backdrop(container, req)
            components = _extract_components(fr, req.anime_title)
            if len(components) == 1 and components[0]["type"] == "season":
                # Single season, no extras — skip component picker but still seed
                # ``selected``/``queue`` so confirm + intake have a component to work
                # with (otherwise the confirm screen is empty and intake crashes).
                key0 = _comp_key(components[0])
                await fsm.set(q.from_user.id, STATE_MANUAL_AUDIO, code=code,
                              anime_title=req.anime_title, components=components,
                              selected={key0: True}, queue=[(components[0], "audio")],
                              audio={}, resolutions={},
                              current_index=0, backdrop_url=backdrop_url)
                await _render_audio_picker(q.message, q.from_user.id, components[0], 0)
            else:
                await fsm.set(q.from_user.id, STATE_MANUAL_COMP, code=code,
                              anime_title=req.anime_title, components=components,
                              selected={}, backdrop_url=backdrop_url)
                await _render_comp_picker(q.message, q.from_user.id)

    @client.on_callback_query(filters.regex(r"^staff\|jstop"))
    async def _job_stop(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        # Set the same Redis flag the download worker polls — it stops the CURRENT
        # episode, finishes the rest of the series, then retries this one at the end.
        try:
            job_id = int(q.data.split("|")[2])
        except (ValueError, IndexError):
            await q.answer()
            return
        if container.redis:
            await container.redis.set(f"nf:job:{job_id}:skip", "1", ex=300)
        await q.answer(L(M.TOAST_STOPPING), show_alert=True)

    @client.on_callback_query(filters.regex(r"^staff\|jcancel"))
    async def _job_cancel(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        try:
            job_id = int(q.data.split("|")[2])
        except (ValueError, IndexError):
            await q.answer()
            return
        # Terminate the whole job: marks it CANCELLED, signals a running worker to
        # abort, and clears live progress so it drops off ACTIVE TASKS.
        from nekofetch.services.queue_service import QueueService
        await QueueService(container).cancel(job_id)
        await q.answer(L(M.TOAST_CANCELLING), show_alert=True)

    # ── stuck-episode recovery: Retry / Switch source / Provide file ─────────────
    async def _load_stuck(code: str) -> dict | None:
        import json
        if not container.redis:
            return None
        raw = await container.redis.get(f"nf:stuck:{code}")
        return json.loads(raw) if raw else None

    async def _requeue(code: str, episodes: list, *, new_source: str | None = None,
                       chat_id: int | None = None) -> bool:
        from nekofetch.services.queue_service import QueueService
        from nekofetch.services.request_service import RequestService
        try:
            await RequestService(container).retry_episodes(code, episodes, new_source=new_source)
            job_id = await QueueService(container).enqueue(code)
            await _spawn_progress_card(job_id, chat_id)
            return True
        except NekoFetchError:
            return False

    @client.on_callback_query(filters.regex(r"^staff\|aretry"))
    async def _attn_retry(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        await lock_buttons(q)
        stuck = await _load_stuck(code)
        if not stuck or not await _requeue(code, stuck["episodes"], chat_id=q.from_user.id):
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer(L(M.TOAST_RETRY_QUEUED))
        try:
            await q.message.edit_text(
                L(M.ATTN_RETRYING, eps=", ".join(map(str, stuck["episodes"]))),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    @client.on_callback_query(filters.regex(r"^staff\|aswitch\|"))
    async def _attn_switch(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        stuck = await _load_stuck(code)
        alt = (stuck or {}).get("alt_source")
        if not alt:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()
        try:
            await q.message.edit_text(L(M.ATTN_CHECKING_ALT, alt=alt.title()),
                                      parse_mode=ParseMode.HTML)
        except Exception:
            pass
        # Probe what the alternate source ACTUALLY offers so we can explicitly say
        # whether the needed audio (e.g. dub) exists there, not silently fail.
        explain = await _audio_compat(code, alt, stuck.get("audio_kinds", []))
        kb = keyboard(
            [(L(M.CC_BTN_SWITCH_CONFIRM, alt=alt.title()), cb("staff", "aswitchgo", code))],
            [(L(M.CC_BTN_PROVIDE), cb("staff", "aprovide", code))],
        )
        try:
            await q.message.edit_text(explain, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass

    async def _audio_compat(code: str, alt: str, needed: list) -> str:
        from nekofetch.services.request_service import RequestService
        try:
            req = await RequestService(container).get(code)
        except NekoFetchError:
            return L(M.ATTN_SWITCH_UNAVAILABLE, alt=alt.title())
        fr = req.franchise_data or {}
        titles = [x for x in (fr.get("english") or req.anime_title, fr.get("romaji")) if x]
        try:
            cov = await container.sources.get(alt).coverage(*titles)
        except Exception:
            cov = None
        if cov is None or not getattr(cov, "available", False):
            return L(M.ATTN_SWITCH_UNAVAILABLE, alt=alt.title())
        lines = [L(M.ATTN_SWITCH_HEADER, alt=alt.title())]
        need_dub = "dubbed" in needed or "dual_audio" in needed
        need_sub = "subbed" in needed or "dual_audio" in needed or not needed
        if need_sub:
            key = M.ATTN_SWITCH_HAS if cov.sub_episodes > 0 else M.ATTN_SWITCH_LACKS
            lines.append(L(key, alt=alt.title(), kind="sub"))
        if need_dub:
            key = M.ATTN_SWITCH_HAS if cov.dub_episodes > 0 else M.ATTN_SWITCH_LACKS
            lines.append(L(key, alt=alt.title(), kind="dub"))
        return "\n".join(lines)

    @client.on_callback_query(filters.regex(r"^staff\|aswitchgo"))
    async def _attn_switch_go(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        await lock_buttons(q)
        stuck = await _load_stuck(code)
        alt = (stuck or {}).get("alt_source")
        if not (stuck and alt and await _requeue(code, stuck["episodes"], new_source=alt,
                                                  chat_id=q.from_user.id)):
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer(L(M.TOAST_RETRY_QUEUED))
        try:
            await q.message.edit_text(
                L(M.ATTN_RETRYING, eps=", ".join(map(str, stuck["episodes"]))),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    @client.on_callback_query(filters.regex(r"^staff\|aprovide"))
    async def _attn_provide(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        stuck = await _load_stuck(code)
        eps = (stuck or {}).get("episodes", [])
        await fsm.set(q.from_user.id, STATE_PROVIDE, code=code, episodes=eps)
        await q.answer()
        try:
            await q.message.edit_text(
                L(M.ATTN_PROVIDE_PROMPT, eps=", ".join(map(str, eps)) or "—"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # ── missing-source recovery: Provide more links / Publish what we have ────

    async def _load_missing(code: str) -> dict | None:
        import json
        if not container.redis:
            return None
        raw = await container.redis.get(f"nf:missing:{code}")
        return json.loads(raw) if raw else None

    @client.on_callback_query(filters.regex(r"^staff\|amore\|"))
    async def _missing_more(_: Client, q: CallbackQuery) -> None:
        """Admin chose 'Provide link(s)' — offer a DDL / Torrent choice.

        The owner can supply EITHER type regardless of the request's original
        source (e.g. fill a missing ONA with a torrent even on a DDL request), so
        we present both buttons and let the chosen handler arm the right prompt.
        """
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        missing = await _load_missing(code)
        if not missing:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return
        await q.answer()
        title = missing.get("title") or ""
        summary = _missing_summary(missing)
        text = (
            f"🧩 <b>{title}</b> — provide more links\n"
            f"<code>{code}</code>\n\n"
            f"Still missing: <b>{summary}</b>.\n\n"
            f"How do you want to supply it? You can use <b>either</b> — a torrent "
            f"even if this was a DDL request, or vice-versa."
        )
        kb = keyboard(
            [(L(M.ADMIN_BTN_DDL), cb("staff", "amoresrc", code, "ddl")),
             (L(M.ADMIN_BTN_TORRENT), cb("staff", "amoresrc", code, "torrent"))],
            [(L(M.CC_BTN_PUBLISH_ANYWAY), cb("staff", "amoredone", code))],
        )
        await show(client, q.message, text, kb)

    def _missing_summary(missing: dict) -> str:
        """Human 'S3 (all 12); S1 eps 10–12' summary from a stored missing-state."""
        from nekofetch.services.log_channel_service import _summarize_runs
        grouped = {int(s): eps for s, eps in missing.get("missing", {}).items()}
        empty = missing.get("empty_seasons", [])
        parts = []
        for season in sorted(grouped):
            eps = grouped[season]
            if season in empty:
                parts.append(f"S{season} (all {len(eps)})")
            else:
                parts.append(f"S{season} eps {_summarize_runs(eps)}")
        return "; ".join(parts) or "—"

    @client.on_callback_query(filters.regex(r"^staff\|amoresrc\|"))
    async def _missing_more_source(_: Client, q: CallbackQuery) -> None:
        """Admin picked DDL or Torrent to fill the gap — arm the matching prompt.

        Both funnel into STATE_MISSING_SOURCE, which the DDL/magnet/.torrent reply
        handlers all already accept; the only difference is the instruction text
        and which input is expected."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        parts = q.data.split("|")
        code, chosen = parts[2], parts[3]
        missing = await _load_missing(code)
        if not missing:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return

        from nekofetch.services.request_service import RequestService
        req = await RequestService(container).get(code)
        title = missing.get("title") or req.anime_title
        await q.answer()

        backdrop = await _anime_backdrop(container, req)
        telegraph_url = await _build_mapping_guide(req, missing)
        summary = _missing_summary(missing)

        prompt_text = (
            f"🧩 <b>{title}</b> — provide more links\n"
            f"<code>{code}</code>\n\n"
            f"Still missing: <b>{summary}</b>.\n\n"
        )
        if chosen == "ddl":
            prompt_text += (
                "Reply with http(s) direct-download links (one or more, separated by "
                "whitespace/newlines). Archives will be fetched + extracted and added "
                "to what we already have."
            )
        else:
            prompt_text += (
                "Reply with a magnet: link, http(s) torrent link, or upload a .torrent "
                "file. The new torrent's episodes will be added to what we already have."
            )
        if telegraph_url:
            prompt_text += f'\n\n<a href="{telegraph_url}">📋 Season mapping guide</a>'

        kb = keyboard(
            [(L(M.BTN_BACK), cb("staff", "amore", code))],
            [(L(M.CC_BTN_PUBLISH_ANYWAY), cb("staff", "amoredone", code))],
        )
        card = await show(client, q.message, prompt_text, kb, image=backdrop)

        # Arm the reply flow: DDL/magnet/file handlers detect STATE_MISSING_SOURCE.
        # ``source`` records the CHOSEN type so downstream enqueue uses it.
        await _release_torrent_provide(code)
        await fsm.set(q.from_user.id, STATE_MISSING_SOURCE,
                      code=code, title=title, source=chosen,
                      prompt_chat_id=card.chat.id, prompt_message_id=card.id)
        await _arm_reply(container.redis, card.chat.id,
                         STATE_MISSING_SOURCE, code=code, title=title, source=chosen,
                         prompt_chat_id=card.chat.id, prompt_message_id=card.id)

    async def _build_mapping_guide(req, missing: dict) -> str | None:
        """Build a Telegraph article showing the franchise's season structure so the
        admin sees which seasons exist and which are still missing. Returns the page
        URL, or None when no Telegraph token is configured / the call fails."""
        try:
            token = container.config.thumbnail_channel.telegraph_access_token
            if not token:
                return None

            franchise = req.franchise_data or {}
            title = franchise.get("title") or req.anime_title

            from nekofetch.providers.metadata.telegraph_client import TelegraphClient
            telegraph = TelegraphClient(token)

            from nekofetch.services.log_channel_service import _summarize_runs

            # Build simple DOM nodes: heading + season list.
            content = [
                {"tag": "h3", "children": [f"{title} — Season Structure"]},
                {"tag": "p", "children": ["Seasons still needing content:"]},
            ]

            grouped = {int(s): eps for s, eps in missing.get("missing", {}).items()}
            empty = missing.get("empty_seasons", [])
            for season in sorted(grouped):
                eps = grouped[season]
                label = f"Season {season}"
                if season in empty:
                    label += f" — all {len(eps)} episodes missing ❌"
                else:
                    label += f" — eps {_summarize_runs(eps)} still needed ⚠️"
                content.append({"tag": "p", "children": [label]})

            result = await telegraph._call(
                "createPage",
                title=f"{title} — Seasons",
                author_name="NekoFetch",
                content=content,
            )
            await telegraph.close()
            return result.get("url") or (
                f"https://telegra.ph/{result.get('path', '')}"
                if result.get("path") else None
            )
        except Exception:  # noqa: BLE001
            log.debug("missing.telegraph.failed", code=getattr(req, "code", "?"))
            return None

    @client.on_callback_query(filters.regex(r"^staff\|amoredone\|"))
    async def _missing_done(_: Client, q: CallbackQuery) -> None:
        """Admin chose 'Publish what we have' — set bypass flag + re-enqueue to finalize."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        await lock_buttons(q)

        # Set one-shot bypass flag so the gate lets this finalize despite gaps.
        if container.redis:
            await container.redis.set(f"nf:coverage:bypass:{code}", "1", ex=300)

        from nekofetch.services.queue_service import QueueService
        try:
            job_id = await QueueService(container).enqueue(code)
            await _spawn_progress_card(job_id, q.from_user.id)
        except Exception:
            await q.answer(L(M.ERR_GENERIC), show_alert=True)
            return

        await fsm.clear(q.from_user.id)
        await q.answer("Publishing with what we have…")
        try:
            await q.message.edit_text(
                "✅ Finalizing the release with downloaded content.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    @client.on_message((filters.document | filters.video) & filters.private, group=5)
    async def _provide_ingest(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, data = await fsm.get(message.from_user.id)
        if state != STATE_PROVIDE:
            return
        user = getattr(message, "nf_user", None)
        if not (user and auth.has_permission(user, Permission.QUEUE_DOWNLOADS)):
            return
        from pathlib import Path

        from nekofetch.services.download_service import DownloadWorker

        code = data.get("code")
        eps = data.get("episodes") or []
        episode = int(eps[0]) if eps else 1
        media = message.document or message.video
        orig = getattr(media, "file_name", "") or "provided.mkv"
        ext = Path(orig).suffix or ".mkv"
        target = Path(container.env.storage_path) / "work" / "_provided" / f"{code}_E{episode}{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            path = await message.download(file_name=str(target))
            await DownloadWorker(container).ingest_provided_file(code, episode, path)
        except Exception as exc:  # noqa: BLE001
            await message.reply(L(M.ATTN_PROVIDE_FAILED, reason=str(exc)[:160]),
                                parse_mode=ParseMode.HTML)
            return
        remaining = eps[1:]
        if remaining:
            await fsm.set(message.from_user.id, STATE_PROVIDE, code=code, episodes=remaining)
        else:
            await fsm.clear(message.from_user.id)
        await message.reply(L(M.ATTN_PROVIDE_DONE, name=orig, ep=episode),
                            parse_mode=ParseMode.HTML)

    # ── franchise edit reply state ──
    STATE_FRANCHISE_EDIT = "staff:franchise:edit"

    # ════════════════════════════════════════════════════════════════════════
    # Reply-based editing handler — parse admin replies to mapping messages
    # ════════════════════════════════════════════════════════════════════════

    # NOTE: group 12 — deliberately NOT group 9. The channel guard lives in
    # group 9, and Pyrogram runs only the first matching handler per group. This
    # handler now matches channel text too (to catch anonymous edits), so sharing
    # group 9 would shadow the guard and stop it cleaning up ordinary chatter.
    # A distinct group lets BOTH fire: the guard (group 9) sees the armed marker
    # and skips deletion, then this consumer (group 12) reads the edit.
    @client.on_message(filters.text & ~filters.command(["start"]), group=12)
    async def _franchise_edit_reply(_: Client, message: Message) -> None:
        """Handle admin replies to franchise mapping messages.

        The admin replies to a message with text like:
          "Season 3 Part 1 → 12 episodes"
          "Exclude Season 2"
          "Include Season 3 Part 2"
        The reply is parsed and applied to the stored mapping.

        Fires for a DM reply (per-user FSM) or a Control Center reply — including
        an ANONYMOUS admin's, matched via the chat-scoped marker.
        """
        state, data, via_channel = await _resolve_reply_flow(
            message, STATE_FRANCHISE_EDIT,
        )
        if state != STATE_FRANCHISE_EDIT:
            return

        code = data.get("code", "")
        text = (message.text or "").strip()
        parsed = FranchiseFlowService.parse_edit_reply(text)
        if parsed is None:
            await message.reply(
                "Could not parse your edit. Use format like:\n"
                "<code>Season 3 Part 1 → 12 episodes</code>\n"
                "<code>Exclude Season 2</code>\n"
                "<code>Include Season 3 Part 2</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        # Apply the edit to the stored mapping
        stored_mapping = data.get("mapping", {})
        entries = stored_mapping.get("entries", [])
        action = parsed.get("action")
        season = parsed.get("season")
        part = parsed.get("part")
        episodes = parsed.get("episodes")
        kind = parsed.get("kind")
        title_str = parsed.get("title")

        updated = False
        for e in entries:
            # Match by season number + part, or by kind + title
            if kind:
                if e.get("kind", "").upper() == kind.upper() and (
                    title_str and title_str.lower() in (e.get("title") or "").lower()
                ):
                    if action in ("exclude", "remove", "skip"):
                        e["included"] = False
                    elif action in ("include", "add"):
                        e["included"] = True
                    updated = True
                    break
            else:
                s_num = e.get("season_number")
                s_part = e.get("season_part")
                if s_num == season and s_part == part:
                    if action == "exclude":
                        e["included"] = False
                    elif action == "include" or action == "toggle":
                        e["included"] = not e.get("included", True) if action == "toggle" else True
                    elif action == "set_episodes" and episodes is not None:
                        e["episodes"] = episodes
                    updated = True
                    break

        if not updated:
            await message.reply("Could not find a matching entry in the mapping.",
                                parse_mode=ParseMode.HTML)
            return

        # Save the updated mapping. Franchise editing stays open for repeated
        # edits, so persist back to whichever store holds this flow and keep it
        # armed — the admin may send another edit line next.
        stored_mapping["entries"] = entries
        if via_channel:
            await _arm_reply(container.redis, message.chat.id, STATE_FRANCHISE_EDIT,
                             code=code, mapping=stored_mapping,
                             prompt_message_id=data.get("prompt_message_id"))
        elif message.from_user:
            await fsm.update(message.from_user.id, mapping=stored_mapping)

        # Rebuild marker state and acknowledge the applied edit.
        await message.reply(
            "✅ Edit applied! Refreshing the mapping view…",
            parse_mode=ParseMode.HTML,
        )
        # Delete-after-consume: the admin's edit reply has now been applied, so
        # remove it to keep the control-center channel clean. The channel guard
        # left it alone (marker armed) so it survived until this point. The
        # marker stays armed for the next edit; only Confirm/Cancel disarms it.
        try:
            await message.delete()
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════════════
    # Franchise Mapping Flow — callbacks
    # ════════════════════════════════════════════════════════════════════════

    @client.on_callback_query(filters.regex(r"^franchise\|toggle\|"))
    async def _franchise_toggle(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_FRANCHISE_MAP:
            await q.answer()
            return
        entry_key = q.data.split("|", 2)[2]
        entries = data.get("mapping", {}).get("entries", [])
        for e in entries:
            from nekofetch.services.franchise_flow import FranchiseFlowService
            ek = FranchiseFlowService._entry_key_from_dict(e)
            if ek == entry_key:
                e["included"] = not e.get("included", True)
                break
        await fsm.update(q.from_user.id, mapping={
            **data.get("mapping", {}),
            "entries": entries,
        })
        from nekofetch.services.franchise_flow import FranchiseFlowService
        mapping = FranchiseFlowService.dict_to_mapping(data.get("mapping", {}))
        screen = franchise_map_selection(mapping, backdrop_url=data.get("backdrop_url"))
        from nekofetch.ui.screens import send_screen
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^franchise\|all$"))
    async def _franchise_all(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_FRANCHISE_MAP:
            await q.answer()
            return
        entries = data.get("mapping", {}).get("entries", [])
        for e in entries:
            e["included"] = True
        await fsm.update(q.from_user.id, mapping={
            **data.get("mapping", {}),
            "entries": entries,
        })
        from nekofetch.services.franchise_flow import FranchiseFlowService
        mapping = FranchiseFlowService.dict_to_mapping(data.get("mapping", {}))
        screen = franchise_map_selection(mapping, backdrop_url=data.get("backdrop_url"))
        from nekofetch.ui.screens import send_screen
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^franchise\|confirm$"))
    async def _franchise_confirm(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        # Leaving the mapping screen — drop any armed franchise-edit marker so a
        # later stray channel message isn't parsed as an edit.
        await _disarm_reply(container.redis, q.message.chat.id)
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_FRANCHISE_MAP:
            await q.answer()
            return
        code = data.get("code", "")
        source = data.get("source", "")

        # Proceed based on source type
        if source == "website":
            # Ask whether to include AniZone before building the report.
            await q.answer()
            kb = keyboard(
                [("✅ Yes, use AniZone", cb("anizone", "yes", code))],
                [("❌ No, skip AniZone", cb("anizone", "no", code))],
                [(L(M.BTN_BACK), cb("franchise", "back"))],
            )
            await show(client, q.message,
                       "<b>🤔 AniZone Integration</b>\n\n"
                       "Would you like to include AniZone as a source?\n"
                       "AniZone uses different titles and may need manual slug mapping.",
                       kb)
        else:
            from nekofetch.services.queue_service import QueueService
            from nekofetch.services.request_service import RequestService
            await RequestService(container).update_source(code, source)
            job_id = await QueueService(container).enqueue(code)
            await _spawn_progress_card(job_id, q.from_user.id)
            await q.answer(L(M.TOAST_QUEUED, source=source, job=job_id), show_alert=True)
            await fsm.clear(q.from_user.id)
            try:
                await q.message.delete()
            except Exception:
                pass

    # ════════════════════════════════════════════════════════════════════════
    # AniZone slug mapping callbacks
    # ════════════════════════════════════════════════════════════════════════

    @client.on_callback_query(filters.regex(r"^anizone\|no\|"))
    async def _anizone_skip(_: Client, q: CallbackQuery) -> None:
        """User chose to skip AniZone — proceed to website report."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        await _proceed_website_report(q, code)

    @client.on_callback_query(filters.regex(r"^anizone\|yes\|"))
    async def _anizone_accept(_: Client, q: CallbackQuery) -> None:
        """User chose to use AniZone — show slug-mapping prompt and wait for reply."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_FRANCHISE_MAP:
            await q.answer()
            return
        mapping = data.get("mapping", {})
        entries = mapping.get("entries", [])
        included = [e for e in entries if e.get("included", True)]
        # Auto-include all entries when none are selected — the admin may have
        # excluded everything on the franchise mapping screen, or (for single
        # entries) never saw the toggle screen at all.
        if not included and entries:
            for e in entries:
                e["included"] = True
            included = list(entries)
        # Always show the slug-mapping prompt — AniZone REQUIRES a slug to
        # find episodes, even for single-entry requests. Skipping this step
        # would cause the download worker to fail with "no source could
        # provide episodes" because AniZone can't title-match like the other
        # website sources.
        prompt = _format_anizone_slug_prompt(mapping)
        await fsm.set(q.from_user.id, STATE_ANIZONE_SLUGS,
                      code=code, mapping=mapping,
                      backdrop_url=data.get("backdrop_url"))
        # Also arm a chat-scoped marker so an ANONYMOUS admin's reply (posted as
        # the channel, no ``from_user``) is still recognised as the awaited slug
        # list — the per-user FSM above can't match it.
        await _arm_reply(container.redis, q.message.chat.id, STATE_ANIZONE_SLUGS,
                         code=code, mapping=mapping,
                         backdrop_url=data.get("backdrop_url"))
        # Build keyboard with 🔍 search buttons — one per entry, linking
        # straight to AniZone's search page so admins don't have to open
        # a browser and search manually.
        root = mapping.get("root_title", "")
        kb_rows: list[list[InlineKeyboardButton]] = []
        for i, e in enumerate(included, start=1):
            e_title = e.get("title", "") or ""
            kind = e.get("kind", "season")
            s_num = e.get("season_number", i)
            s_part = e.get("season_part")
            # Label matches _format_anizone_slug_prompt entry lines.
            if kind != "season":
                label = f"{kind.title()}: {e_title}" if e_title else kind.title()
            else:
                label = f"Season {s_num:02d}"
                if s_part:
                    label += f" Part {s_part}"
                if e_title and e_title != label and not e_title.startswith("Season "):
                    label += f" — {e_title[:50]}"
            search_term = e_title if e_title else root
            search_url = f"https://anizone.to/anime?search={quote(search_term)}"
            kb_rows.append([InlineKeyboardButton(
                f"🔍 #{i} {_esc(label)[:30]}", url=search_url,
            )])
        kb_rows.append([InlineKeyboardButton(
            L(M.BTN_BACK), callback_data=cb("franchise", "back"),
        )])
        kb_rows.append([InlineKeyboardButton(
            L(M.BTN_CANCEL), callback_data=cb("anizone", "cancel", code),
        )])
        kb = InlineKeyboardMarkup(kb_rows)
        await q.answer()
        # Capture the prompt card's mid BEFORE the screen replaces the old
        # button card so we can later delete it once the slugs are consumed.
        # Without this, the slug-mapping card sticks around in the channel
        # until the next restart, cluttering the control center between the
        # admin's reply and the (eventual) "job failed" notice.
        prompt_msg = await show(client, q.message, prompt, kb,
                                image=data.get("backdrop_url"))
        prompt_mid = prompt_msg.id
        await fsm.update(q.from_user.id, prompt_message_id=prompt_mid)
        # Re-key the chat-scoped marker so an anonymous admin's slug reply
        # (read via `_peek_reply`) ALSO knows where the prompt lives. arm()
        # overwrites in place, so the previous arm with the same fields is
        # upgraded with `prompt_message_id` for this read path.
        await _arm_reply(container.redis, q.message.chat.id, STATE_ANIZONE_SLUGS,
                         code=code, mapping=mapping,
                         backdrop_url=data.get("backdrop_url"),
                         prompt_message_id=prompt_mid)

    @client.on_callback_query(filters.regex(r"^anizone\|cancel\|"))
    async def _anizone_cancel(_: Client, q: CallbackQuery) -> None:
        """Cancel AniZone slug mapping — fall back to skip."""
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2]
        await fsm.clear(q.from_user.id)
        await _disarm_reply(container.redis, q.message.chat.id)
        await q.answer("AniZone skipped.")
        await _proceed_website_report(q, code)

    @client.on_message(filters.text & ~filters.command(["start"]), group=10)
    async def _anizone_slug_reply(_: Client, message: Message) -> None:
        """Handle admin reply with AniZone slug mapping.

        Parses numbered lines like:
          1. /anime/bsagbos2
          2. /anime/xyz123
        Each line number corresponds to the entry index shown in the prompt.

        Fires both for a DM reply (per-user FSM) and for a reply typed straight
        into the Control Center channel — including an ANONYMOUS admin's, which
        carries no ``from_user`` and is matched via the chat-scoped marker.
        """
        state, data, via_channel = await _resolve_reply_flow(
            message, STATE_ANIZONE_SLUGS,
        )
        if state != STATE_ANIZONE_SLUGS:
            return

        text = (message.text or "").strip()
        code = data.get("code", "")
        mapping = data.get("mapping", {})
        entries = mapping.get("entries", [])
        included = [e for e in entries if e.get("included", True)]

        # Parse slugs from the reply (support full URLs, bare slugs, and type prefixes)
        slug_map: dict[int, dict] = {}
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Extract optional ep_type prefix: "reg", "regular", "spec", "special", "all"
            ep_type: str | None = None
            type_m = re.match(r"(reg(?:ular)?|spec(?:ial)?|all)\s+", line, re.IGNORECASE)
            if type_m:
                t = type_m.group(1).lower()
                ep_type = {"reg": "regular", "regular": "regular",
                            "spec": "special", "special": "special",
                            "all": "all"}.get(t, "all")
                line = line[type_m.end():].strip()
            # Extract slug from full URL or bare path
            m = re.match(r"(\d+)\.?\s+(?:https?://anizone\.to)?/?([^\s]+)", line, re.IGNORECASE)
            if m:
                idx = int(m.group(1)) - 1  # 0-indexed
                slug = m.group(2).strip("/")
                # Strip accidental /anime/ prefix (e.g. admin pastes the full path)
                slug = re.sub(r'^anime/', '', slug)
                if 0 <= idx < len(included):
                    slug_map[idx] = {"slug": slug, "ep_type": ep_type}

        if not slug_map:
            err = await message.reply(
                "Could not parse slugs. Reply with one slug per line, like:\n"
                "<pre>1. /anime/bsagbos2</pre>",
                parse_mode=ParseMode.HTML,
            )
            # Delete both the bot's error and the admin's bad reply to keep
            # the control-center channel clean for the retry.
            try:
                await message.delete()
                await err.delete()
            except Exception:
                pass
            return

        # Apply slugs to the mapping entries and get episode counts from AniZone
        from nekofetch.core.logging import get_logger
        _log = get_logger(__name__)
        for idx, sdata in slug_map.items():
            entry = included[idx]
            slug = sdata["slug"]
            ep_type = sdata.get("ep_type")
            entry["anizone_slug"] = slug
            if ep_type:
                entry["anizone_ep_type"] = ep_type
            # Fetch episode counts — if ep_type is specified, fetch only that type;
            # otherwise fetch both "all" and "regular" so the admin sees the diff.
            try:
                src = container.sources.get("anizone")
                if ep_type and ep_type != "all":
                    eps = await src.get_episodes(slug, ep_type=ep_type)
                    if eps:
                        entry["anizone_episodes"] = len(eps)
                        _log.info(
                            "anizone.slug.mapped",
                            slug=slug,
                            ep_type=ep_type,
                            episodes=len(eps),
                        )
                else:
                    all_eps = await src.get_episodes(slug)
                    reg_eps = await src.get_episodes(slug, ep_type="regular")
                    if all_eps:
                        entry["anizone_episodes"] = len(all_eps)
                        entry["anizone_regular_episodes"] = (
                            len(reg_eps) if reg_eps else len(all_eps)
                        )
                        _log.info(
                            "anizone.slug.mapped",
                            slug=slug,
                            episodes=len(all_eps),
                            regular=len(reg_eps) if reg_eps else 0,
                        )
            except Exception as exc:
                _log.warning("anizone.slug.failed", slug=slug, error=str(exc))

        # Deduplicate slug display for source selection
        unique_slugs: dict[str, str] = {}
        for idx, sdata in sorted(slug_map.items()):
            slug = sdata["slug"]
            ep = sdata.get("ep_type")
            label = f"#{idx+1}={slug}"
            if ep:
                label += f"({ep})"
            unique_slugs[slug + (ep or "")] = label

        # Save updated mapping
        mapping["entries"] = entries
        mapping["anizone_slug_map"] = {
            str(idx): sdata for idx, sdata in slug_map.items()
        }
        if message.from_user:
            await fsm.clear(message.from_user.id)

        # Store AniZone slugs on the request's franchise_data
        from nekofetch.services.request_service import RequestService as _RS
        req = await _RS(container).get(code)
        fr_dict = dict(req.franchise_data or {})
        fr_dict["_anizone_slugs"] = mapping["anizone_slug_map"]
        await _RS(container).update_franchise_data(code, fr_dict)

        # Queue directly with AniZone — no fallback, no priority selection.
        entry_str = ", ".join(
            f"#{idx+1}={sdata['slug']}" for idx, sdata in sorted(slug_map.items())
        )
        from nekofetch.services.queue_service import QueueService as _QS
        await _RS(container).update_source(code, "anizone")
        job_id = await _QS(container).enqueue(code)
        await _spawn_progress_card(job_id, message.chat.id)
        # Post a SEPARATE confirmation card so the slug prompt stays as an audit
        # trail (we deliberately do NOT edit the slug prompt card in place). After
        # 5 seconds, fire-and-forget cleanup removes BOTH this confirmation AND
        # the request's divider sticker so the Control Center channel stays
        # free of operational noise. The admin's slug reply is deleted below.
        title_for_confirm = req.anime_title or ""
        slugs_summary_fwd = entry_str  # avoid shadowing by binding now
        try:
            download_msg = await client.send_message(
                message.chat.id,
                f"⬇️ <b>Downloading</b> <code>{_esc(title_for_confirm)}</code> "
                f"from AniZone as job <code>#{job_id}</code>\n"
                f"<i>Slugs: {_esc(slugs_summary_fwd)[:160]} "
                f"\u2014 auto-cleans in <b>5s</b>.</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            from nekofetch.core.logging import get_logger as _gl
            _gl(__name__).debug(
                "anizone.confirmation.post_failed", error=str(exc),
            )
            download_msg = None
        # Delete-after-consume: IMMEDIATELY remove the admin's slug reply
        # AND the slug-mapping prompt card. The user explicitly asked for
        # instant cleans-after-consume ("Once you receive the slug, you
        # delete this, in a slug mapping shit. This message, you remove it
        # because you have the slug now.") — the prompt must vanish the
        # moment the slugs are accepted. Parse-error path above stays on
        # the prompt for retry and intentionally does NOT touch this.
        try:
            await message.delete()
        except Exception:
            pass
        prompt_message_id = data.get("prompt_message_id")
        if prompt_message_id:
            try:
                await client.delete_messages(message.chat.id, prompt_message_id)
            except Exception as exc:  # noqa: BLE001
                from nekofetch.core.logging import get_logger as _glog
                _glog(__name__).debug(
                    "anizone.prompt.delete_failed", mid=prompt_message_id,
                    error=str(exc),
                )
        # Disarm the marker so the channel guard resumes normal cleanup.
        await _disarm_reply(container.redis, message.chat.id)
        # Schedule the short deferred cleanup: the brief ``\u2b07\ufe0f Downloading\u2026`` ack
        # (so the admin still sees the assigned job_id) and the request
        # divider sticker. Module-level helper so the test suite can drive
        # it directly with sleep_seconds=0 instead of waiting 5 real seconds.
        if download_msg is not None:
            asyncio.create_task(_anizone_confirm_cleanup_scheduled(
                client, container,
                confirm_mid=download_msg.id,
                chat_id=message.chat.id,
                code=code,
            ))

    @client.on_callback_query(filters.regex(r"^franchise\|back$"))
    async def _franchise_back(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        state, data = await fsm.get(q.from_user.id)
        code = data.get("code", "")
        source_type = data.get("source", "")
        # Clear franchise FSM state (and any armed channel-reply marker) and go
        # back to the appropriate screen.
        await fsm.clear(q.from_user.id)
        await _disarm_reply(container.redis, q.message.chat.id)
        from nekofetch.ui.screens import show as _show
        if source_type == "telegram":
            await _show(client, q.message, L(M.ADMIN_TG_CHOOSE),
                        keyboard([
                            (L(M.ADMIN_BTN_AUTOMATIC), cb("staff", "rtgmode", code, "auto")),
                            (L(M.ADMIN_BTN_MANUAL), cb("staff", "rtgmode", code, "manual")),
                        ]))
        else:
            # Go back to request detail for website/torrent
            await _show(client, q.message, "Choose source for this request:",
                        keyboard([
                            (L(M.ADMIN_BTN_TELEGRAM), cb("staff", "rsource", code, "telegram")),
                            (L(M.ADMIN_BTN_WEBSITE), cb("staff", "rsource", code, "website")),
                        ],
                        [(L(M.ADMIN_BTN_TORRENT), cb("staff", "rsource", code, "torrent")),
                         (L(M.ADMIN_BTN_DDL), cb("staff", "rsource", code, "ddl"))],
                        [(L(M.ADMIN_BTN_REJECT), cb("staff", "rreject", code))],
                        ))
        await q.answer()

    @client.on_callback_query(filters.regex(r"^franchise\|source\|"))
    async def _franchise_source(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        await q.answer()
        await show(client, q.message,
                   "Source strategy selected. Proceeding to queue…")

    @client.on_callback_query(filters.regex(r"^franchise\|(confirm_pub|edit|cancel)"))
    async def _franchise_postproc(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        parts = q.data.split("|", 2)
        # parts = ["franchise", "confirm_pub", "CODE"] or ["franchise", "edit", "CODE"]
        inner_action = parts[1] if len(parts) > 1 else ""
        code = parts[2] if len(parts) > 2 else ""

        # Confirm/Cancel leave the mapping screen — drop any armed edit marker so
        # a later stray channel message isn't mistaken for an edit. (If the admin
        # armed Edit then changed their mind without replying, the marker would
        # otherwise linger until its TTL and spare the next unrelated message.)
        if inner_action in ("confirm_pub", "cancel"):
            await _disarm_reply(container.redis, q.message.chat.id)

        if inner_action == "confirm_pub":
            from nekofetch.services.publishing_service import PublishingService
            count = await PublishingService(container).publish(code)
            await q.answer(L(M.APPROVALS_TOAST_PUBLISHED, count=count), show_alert=True)
            await show(client, q.message, L(M.APPROVALS_PUBLISHED, code=code, count=count),
                       keyboard([(L(M.BTN_BACK), cb("staff", "requests", 0))]))
        elif inner_action == "cancel":
            from nekofetch.services.publishing_service import PublishingService
            await PublishingService(container).cancel(code)
            await q.answer(L(M.APPROVALS_TOAST_CANCELLED))
            await show(client, q.message, L(M.APPROVALS_CANCELLED, code=code),
                       keyboard([(L(M.BTN_BACK), cb("staff", "requests", 0))]))
        else:
            # Enter edit mode — rebuild mapping from franchise_data so the reply
            # handler always has fresh data regardless of how the admin got here.
            from nekofetch.services.request_service import RequestService
            try:
                req = await RequestService(container).get(code)
                fr = req.franchise_data or {}
                ff = FranchiseFlowService(container)
                mapping = await ff.build_mapping_resolved(fr, req.anime_doc_id or "")
                stored_mapping = {
                    "entries": [{
                        "anilist_id": e.anilist_id,
                        "kind": e.kind.value,
                        "season_number": e.season_number,
                        "season_part": e.season_part,
                        "title": e.title,
                        "episodes": e.episodes,
                        "included": e.included,
                    } for e in mapping.entries],
                    "root_title": mapping.root_title,
                    "anime_doc_id": mapping.anime_doc_id,
                }
                await fsm.set(q.from_user.id, STATE_FRANCHISE_EDIT,
                              code=code, mapping=stored_mapping)
                await q.answer("Reply to the mapping card to edit it.", show_alert=True)
                # Arm the chat marker so an ANONYMOUS admin (posting as the
                # channel, no ``from_user``) can still have their edit line
                # recognised. The card already explains the edit format.
                await _prompt_channel_reply(
                    q.message, STATE_FRANCHISE_EDIT,
                    "",
                    code=code, mapping=stored_mapping,
                )
            except Exception as exc:
                await q.answer(f"Could not load mapping: {exc}", show_alert=True)
            return
        await q.answer()

    @client.on_callback_query(filters.regex(r"^franchise\|refresh\|"))
    async def _franchise_refresh(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        code = q.data.split("|", 2)[2] if "|" in q.data else ""
        await q.answer()
        if code:
            from nekofetch.services.request_service import RequestService
            try:
                req = await RequestService(container).get(code)
                fr = req.franchise_data or {}
                ff = FranchiseFlowService(container)
                mapping = await ff.build_mapping_resolved(fr, req.anime_doc_id or "")
                backdrop_url = await _anime_backdrop(container, req)
                screen = franchise_map_selection(mapping, backdrop_url=backdrop_url)
                from nekofetch.ui.screens import send_screen
                await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
            except Exception:
                pass

    @client.on_callback_query(filters.regex(r"^staff\|rreject"))
    async def _reject(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        from nekofetch.services.log_channel_service import LogChannelService
        from nekofetch.services.request_service import RequestService

        code = q.data.split("|", 2)[2]
        await lock_buttons(q)
        try:
            await RequestService(container).reject(code)
        except NekoFetchError as exc:
            await q.answer(getattr(exc, "detail", None) or L(M.ERR_GENERIC), show_alert=True)
            return
        # Rejected → the request is gone entirely, so remove its card divider too.
        await LogChannelService(container).clear_request_markers(code)
        await q.answer(L(M.TOAST_REJECTED))
        await _render_list(q, 0)

    # ════════════════════════════════════════════════════════════════════════
    # Manual Upload Wizard — FSM-driven multi-step flow
    # ════════════════════════════════════════════════════════════════════════

    # ── renderers ──

    async def _render_comp_picker(msg, user_id: int) -> None:
        _, data = await fsm.get(user_id)
        components = data.get("components", [])
        selected = data.get("selected", {})
        code = data.get("code", "")
        backdrop_url = data.get("backdrop_url")
        lines = [L(M.MANUAL_WIZ_COMP_TITLE), "", L(M.MANUAL_WIZ_COMP_PROMPT), ""]
        kb_rows: list[list[tuple[str, str]]] = []
        for comp in components:
            key = _comp_key(comp)
            label = _comp_label(comp)
            prefix = "✓" if selected.get(key) else "☐"
            lines.append(f"{prefix}  {_esc(label)}")
            kb_rows.append([(f"{prefix} {_esc(label)[:42]}",
                             cb("staff", "manual", "comp", "toggle", key))])
        kb_rows.append([(L(M.MANUAL_WIZ_COMP_ENTIRE),
                         cb("staff", "manual", "comp", "entire"))])
        kb_rows.append([(L(M.MANUAL_WIZ_COMP_DONE),
                         cb("staff", "manual", "comp", "done")),
                        (L(M.BTN_BACK), cb("staff", "rdetail", code))])
        await show(client, msg, "\n".join(lines), keyboard(*kb_rows),
                   image=backdrop_url)

    async def _render_audio_picker(msg, user_id: int, component: dict, index: int) -> None:
        _, data = await fsm.get(user_id)
        backdrop_url = data.get("backdrop_url")
        label = _comp_label(component)
        lines = [L(M.MANUAL_WIZ_AUDIO_TITLE, component=_esc(label)), ""]
        audio_types = [
            (M.MANUAL_WIZ_AUDIO_SUBBED, "subbed"),
            (M.MANUAL_WIZ_AUDIO_DUBBED, "dubbed"),
            (M.MANUAL_WIZ_AUDIO_DUAL, "dual_audio"),
            (M.MANUAL_WIZ_AUDIO_MULTI, "multi"),
        ]
        kb_rows: list[list[tuple[str, str]]] = []
        for msg_key, audio_val in audio_types:
            lines.append(f"•  {L(msg_key)}")
            kb_rows.append([(L(msg_key), cb("staff", "manual", "audio", audio_val))])
        await show(client, msg, "\n".join(lines), keyboard(*kb_rows),
                   image=backdrop_url)

    async def _render_res_picker(msg, user_id: int, component: dict, index: int) -> None:
        _, data = await fsm.get(user_id)
        label = _comp_label(component)
        comp_key = _comp_key(component)
        audio = data.get("audio", {}).get(comp_key, "subbed")
        resolutions = data.get("resolutions", {}).get(comp_key, [])
        backdrop_url = data.get("backdrop_url")
        lines = [L(M.MANUAL_WIZ_RES_TITLE, component=_esc(label), audio=audio), ""]
        res_options = ["360p", "480p", "540p", "720p", "1080p"]
        kb_rows: list[list[tuple[str, str]]] = []
        row: list[tuple[str, str]] = []
        for r in res_options:
            prefix = "☑" if r in resolutions else "☐"
            lines.append(f"{prefix}  {r}")
            row.append((f"{prefix} {r}", cb("staff", "manual", "res", "toggle", r)))
            if len(row) == 2:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        kb_rows.append([(L(M.MANUAL_WIZ_RES_CUSTOM),
                         cb("staff", "manual", "res", "custom"))])
        kb_rows.append([(L(M.MANUAL_WIZ_RES_DONE),
                         cb("staff", "manual", "res", "done"))])
        await show(client, msg, "\n".join(lines), keyboard(*kb_rows),
                   image=backdrop_url)

    async def _render_confirm(msg, user_id: int) -> None:
        _, data = await fsm.get(user_id)
        code = data.get("code", "")
        audio = data.get("audio", {})
        resolutions = data.get("resolutions", {})
        selected_keys = list(data.get("selected", {}).keys())
        backdrop_url = data.get("backdrop_url")
        lines = [L(M.MANUAL_WIZ_CONFIRM_TITLE), ""]
        if not selected_keys:
            lines.append(L(M.MANUAL_WIZ_CONFIRM_EMPTY))
        else:
            for key in selected_keys:
                a = audio.get(key, "—")
                res_list = resolutions.get(key, [])
                res_str = ", ".join(res_list) if res_list else "—"
                lines.append(L(M.MANUAL_WIZ_CONFIRM_LINE, component=key.replace("_", " ").title(),
                               audio=a, resolutions=res_str))
        kb_rows: list[list[tuple[str, str]]] = [
            [(L(M.MANUAL_WIZ_CONFIRM_BTN), cb("staff", "manual", "confirm", "go"))],
            [(L(M.MANUAL_WIZ_CHANGE_BTN), cb("staff", "manual", "confirm", "back"))],
            [(L(M.BTN_BACK), cb("staff", "rdetail", code))],
        ]
        await show(client, msg, "\n".join(lines), keyboard(*kb_rows),
                   image=backdrop_url)

    # ── callback handlers for each wizard step ──

    @client.on_callback_query(filters.regex(r"^staff\|manual\|comp\|"))
    async def _manual_comp_cb(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        state, data = await fsm.get(q.from_user.id)
        if state not in (STATE_MANUAL_COMP, STATE_MANUAL_CONFIRM):
            await q.answer()
            return
        parts = q.data.split("|")
        action = parts[3]
        components = data.get("components", [])
        selected = data.get("selected", {})
        code = data.get("code", "")
        anime_title = data.get("anime_title", "")

        if action == "toggle":
            key = parts[4]
            if key in selected:
                del selected[key]
            else:
                selected[key] = True
            await fsm.update(q.from_user.id, selected=selected)
            await _render_comp_picker(q.message, q.from_user.id)
        elif action == "entire":
            selected = {_comp_key(c): True for c in components}
            await fsm.update(q.from_user.id, selected=selected)
            await _render_comp_picker(q.message, q.from_user.id)
        elif action == "done":
            if not selected:
                await q.answer(L(M.MANUAL_WIZ_CONFIRM_EMPTY), show_alert=True)
                return
            selected_components = [c for c in components if _comp_key(c) in selected]
            queue = [(c, "audio") for c in selected_components]
            await fsm.update(q.from_user.id, selected=selected, queue=queue,
                             current_index=0, audio={}, resolutions={})
            await _render_audio_picker(q.message, q.from_user.id,
                                       selected_components[0], 0)
            await fsm.set(q.from_user.id, STATE_MANUAL_AUDIO, code=code,
                          anime_title=anime_title, components=components,
                          selected=selected, queue=queue, current_index=0,
                          audio={}, resolutions={},
                          backdrop_url=data.get("backdrop_url"))
        await q.answer()

    @client.on_callback_query(filters.regex(r"^staff\|manual\|audio\|"))
    async def _manual_audio_cb(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_MANUAL_AUDIO:
            await q.answer()
            return
        audio_type = q.data.split("|")[3]
        comp_idx = data.get("current_index", 0)
        queue = data.get("queue", [])
        component = queue[comp_idx][0] if queue else {}
        comp_key = _comp_key(component)
        audio = data.get("audio", {})
        audio[comp_key] = audio_type
        await fsm.update(q.from_user.id, audio=audio)
        await fsm.set(q.from_user.id, STATE_MANUAL_RES,
                      code=data.get("code"), anime_title=data.get("anime_title"),
                      components=data.get("components"),
                      selected=data.get("selected"), queue=queue,
                      current_index=comp_idx, audio=audio,
                      resolutions=data.get("resolutions", {}),
                      backdrop_url=data.get("backdrop_url"))
        await _render_res_picker(q.message, q.from_user.id, component, comp_idx)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^staff\|manual\|res\|"))
    async def _manual_res_cb(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        state, data = await fsm.get(q.from_user.id)
        action = q.data.split("|")[3]
        # Any res button other than "custom" means the admin left the custom-res
        # text step — drop the armed marker so a stray channel message isn't
        # captured as a resolution. ("custom" re-arms it a few lines down.)
        if action != "custom":
            await _disarm_reply(container.redis, q.message.chat.id)
        comp_idx = data.get("current_index", 0)
        queue = data.get("queue", [])
        component = queue[comp_idx][0] if queue else {}
        if action == "back":
            # Return to the resolution picker from the custom-resolution prompt
            # (which runs in STATE_MANUAL_CUSTOM_RES, so it bypasses the gate below).
            await fsm.set(q.from_user.id, STATE_MANUAL_RES,
                          code=data.get("code"), anime_title=data.get("anime_title"),
                          components=data.get("components"),
                          selected=data.get("selected"), queue=queue,
                          current_index=comp_idx, audio=data.get("audio"),
                          resolutions=data.get("resolutions", {}),
                          backdrop_url=data.get("backdrop_url"))
            await _render_res_picker(q.message, q.from_user.id, component, comp_idx)
            await q.answer()
            return
        if state != STATE_MANUAL_RES:
            await q.answer()
            return
        comp_key = _comp_key(component)
        resolutions = data.get("resolutions", {})
        current = list(resolutions.get(comp_key, []))

        if action == "toggle":
            res = q.data.split("|")[4]
            if res in current:
                current.remove(res)
            else:
                current.append(res)
            resolutions[comp_key] = current
            await fsm.update(q.from_user.id, resolutions=resolutions)
            await _render_res_picker(q.message, q.from_user.id, component, comp_idx)
        elif action == "custom":
            await fsm.update(q.from_user.id, resolutions=resolutions)
            await fsm.set(q.from_user.id, STATE_MANUAL_CUSTOM_RES,
                          code=data.get("code"), anime_title=data.get("anime_title"),
                          components=data.get("components"),
                          selected=data.get("selected"), queue=queue,
                          current_index=comp_idx, audio=data.get("audio"),
                          resolutions=resolutions,
                          backdrop_url=data.get("backdrop_url"))
            kb = keyboard([(L(M.BTN_BACK), cb("staff", "manual", "res", "back"))])
            backdrop_url = data.get("backdrop_url")
            await show(client, q.message, L(M.MANUAL_WIZ_RES_CUSTOM_PROMPT), kb,
                       image=backdrop_url)
            # Arm a chat marker so an ANONYMOUS admin's typed resolution (posted
            # as the channel, no ``from_user``) is still recognised. The wizard
            # state lives in THIS clicker's FSM, so stash their id as owner_id —
            # the reply handler reads/writes that FSM. The card already prompts.
            await _prompt_channel_reply(
                q.message, STATE_MANUAL_CUSTOM_RES, "",
                owner_id=q.from_user.id,
            )
        elif action == "done":
            if not current:
                await q.answer(L(M.MANUAL_WIZ_CONFIRM_EMPTY), show_alert=True)
                return
            resolutions[comp_key] = current
            # Move to next component or confirm
            next_idx = comp_idx + 1
            if next_idx < len(queue):
                next_comp = queue[next_idx][0]
                await fsm.update(q.from_user.id, resolutions=resolutions)
                await fsm.set(q.from_user.id, STATE_MANUAL_AUDIO,
                              code=data.get("code"), anime_title=data.get("anime_title"),
                              components=data.get("components"),
                              selected=data.get("selected"), queue=queue,
                              current_index=next_idx, audio=data.get("audio"),
                              resolutions=resolutions,
                              backdrop_url=data.get("backdrop_url"))
                await _render_audio_picker(q.message, q.from_user.id, next_comp, next_idx)
            else:
                await fsm.update(q.from_user.id, resolutions=resolutions)
                await fsm.set(q.from_user.id, STATE_MANUAL_CONFIRM,
                              code=data.get("code"), anime_title=data.get("anime_title"),
                              components=data.get("components"),
                              selected=data.get("selected"), audio=data.get("audio"),
                              resolutions=resolutions,
                              backdrop_url=data.get("backdrop_url"))
                await _render_confirm(q.message, q.from_user.id)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^staff\|manual\|confirm\|"))
    async def _manual_confirm_cb(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        state, data = await fsm.get(q.from_user.id)
        if state != STATE_MANUAL_CONFIRM:
            await q.answer()
            return
        action = q.data.split("|")[3]
        if action == "go":
            await _start_intake(q.message, q.from_user.id, data)
        elif action == "back":
            components = data.get("components", [])
            selected = data.get("selected", {})
            await fsm.set(q.from_user.id, STATE_MANUAL_COMP,
                          code=data.get("code"), anime_title=data.get("anime_title"),
                          components=components, selected=selected,
                          backdrop_url=data.get("backdrop_url"))
            await _render_comp_picker(q.message, q.from_user.id)
        await q.answer()

    @client.on_callback_query(filters.regex(r"^staff\|manual\|cancel"))
    async def _manual_cancel_cb(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        state, data = await fsm.get(q.from_user.id)
        if state is None:
            await q.answer()
            return
        code = data.get("code", "")
        backdrop_url = data.get("backdrop_url")
        await fsm.clear(q.from_user.id)
        await q.answer()
        await show(client, q.message, L(M.MANUAL_CANCELLED),
                   keyboard([(L(M.BTN_BACK), cb("staff", "rdetail", code))]),
                   image=backdrop_url)

    async def _start_intake(msg, user_id: int, data: dict) -> None:
        """Build the upload queue and enter the file-intake loop."""
        selected_keys = list(data.get("selected", {}).keys())
        components = data.get("components", [])
        audio = data.get("audio", {})
        resolutions = data.get("resolutions", {})
        comp_map = {_comp_key(c): c for c in components}
        build_order: list[tuple[str, str, str, str]] = []
        for key in selected_keys:
            comp = comp_map.get(key, {"type": "season", "number": key})
            comp_label = _comp_label(comp)
            aud = audio.get(key, "subbed")
            for res in resolutions.get(key, []):
                build_order.append((key, comp_label, aud, res))
        if not build_order:
            # Nothing to collect (no component/resolution chosen) — bail cleanly
            # instead of indexing an empty queue.
            await show(client, msg, L(M.MANUAL_WIZ_CONFIRM_EMPTY),
                       keyboard([(L(M.BTN_BACK), cb("staff", "rdetail", data.get("code", "")))]),
                       image=data.get("backdrop_url"))
            return
        await fsm.set(user_id,
                      STATE_MANUAL_INTAKE,
                      code=data.get("code"), anime_title=data.get("anime_title"),
                      components=data.get("components"),
                      selected=data.get("selected"), audio=audio,
                      resolutions=resolutions, build_order=build_order,
                      current_batch=0, received={},
                      received_count=0, dm_msg_ids=[], dm_chat_id=None,
                      backdrop_url=data.get("backdrop_url"))
        # Hand off to the bot DM: file collection happens privately so the
        # channel stays clean. The channel screen becomes a deep-link button that
        # opens the bot and resumes this exact intake (state is keyed by user id,
        # so it carries over untouched). See ``resume_manual_intake_dm``.
        title = data.get("anime_title", "")
        bot_username = getattr(getattr(client, "me", None), "username", None)
        if not bot_username:
            try:
                bot_username = (await client.get_me()).username
            except Exception:
                bot_username = None
        deep_link = (
            f"https://t.me/{bot_username}?start={MANUAL_RESUME_PREFIX}_{data.get('code','')}"
            if bot_username else None
        )
        if deep_link:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t(M.MANUAL_HANDOFF_BTN), url=deep_link)],
                [InlineKeyboardButton(t(M.BTN_CANCEL),
                                      callback_data=cb("staff", "manual", "cancel"))],
            ])
        else:
            # No username (shouldn't happen for a live bot) — fall back to
            # prompting in-channel so the flow is never fully dead-ended.
            kb = keyboard([(t(M.BTN_CANCEL), cb("staff", "manual", "cancel"))])
        await show(client, msg,
                   t(M.MANUAL_HANDOFF_CHANNEL, title=_esc(title)), kb,
                   image=data.get("backdrop_url"))
        if not deep_link:
            # Degraded mode: keep the legacy in-channel prompt working.
            prompt = await client.send_message(
                msg.chat.id, "\n".join(_intake_prompt_lines(build_order[0])),
                parse_mode=ParseMode.HTML,
            )
            await _dm_track(fsm, user_id, prompt.id)

    async def _prompt_intake_dm(chat_id: int, user_id, batch) -> None:
        """Send the next batch prompt into the DM and track it for later purge."""
        prompt = await client.send_message(
            chat_id, "\n".join(_intake_prompt_lines(batch)),
            parse_mode=ParseMode.HTML,
        )
        await _dm_track(fsm, user_id, prompt.id)

    # ── intake message handlers ──

    @client.on_message((filters.document | filters.video) & filters.private, group=6)
    async def _manual_intake_files(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, data = await fsm.get(message.from_user.id)
        if state != STATE_MANUAL_INTAKE:
            return
        user = getattr(message, "nf_user", None)
        if not (user and auth.has_permission(user, Permission.QUEUE_DOWNLOADS)):
            return
        batch_idx = data.get("current_batch", 0)
        build_order = data.get("build_order", [])
        if batch_idx >= len(build_order):
            return
        media = message.document or message.video
        if not media:
            await message.reply(L(M.MANUAL_INVALID_FILE), parse_mode=ParseMode.HTML)
            return
        code = data.get("code", "")
        batch_key = f"{batch_idx}"
        received = data.get("received", {})
        paths = received.get(batch_key, [])
        # Save file to temp dir
        from pathlib import Path

        work_dir = (
            Path(container.env.storage_path) / "work" / "_manual" / code / f"batch_{batch_idx}"
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        orig_name = getattr(media, "file_name", "") or f"file_{len(paths) + 1}.mkv"
        dest = work_dir / orig_name
        try:
            saved = await message.download(file_name=str(dest))
            paths.append(str(saved))
        except Exception:
            await message.reply(
                L(M.MANUAL_INVALID_FILE),
                parse_mode=ParseMode.HTML,
            )
            return
        received[batch_key] = paths
        total = len(paths)
        await fsm.update(message.from_user.id, received=received,
                         received_count=data.get("received_count", 0) + 1)
        ack = await message.reply(
            L(M.MANUAL_INTAKE_RECEIVED, filename=_esc(orig_name[:60]), n=total),
            parse_mode=ParseMode.HTML,
        )
        # Track the admin's uploaded file AND our ack so both are purged once the
        # whole intake completes, leaving the DM clean.
        await _dm_track(fsm, message.from_user.id, message.id, ack.id)

    @client.on_message(filters.sticker & filters.private, group=7)
    async def _manual_intake_sticker(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, data = await fsm.get(message.from_user.id)
        if state != STATE_MANUAL_INTAKE:
            return
        batch_idx = data.get("current_batch", 0)
        build_order = data.get("build_order", [])
        if batch_idx >= len(build_order):
            return
        batch_key = f"{batch_idx}"
        received = data.get("received", {})
        paths = received.get(batch_key, [])
        if not paths:
            warn = await message.reply(L(M.MANUAL_NO_FILES), parse_mode=ParseMode.HTML)
            await _dm_track(fsm, message.from_user.id, message.id, warn.id)
            return
        batch = build_order[batch_idx]
        comp_key, comp_label, audio_type, res = batch
        done = await message.reply(
            L(M.MANUAL_INTAKE_BATCH_DONE, count=len(paths),
              component=_esc(comp_label), audio=audio_type, res=res),
            parse_mode=ParseMode.HTML,
        )
        # Track the admin's end-of-batch sticker and our confirmation.
        await _dm_track(fsm, message.from_user.id, message.id, done.id)
        next_idx = batch_idx + 1
        if next_idx < len(build_order):
            next_batch = build_order[next_idx]
            await fsm.update(message.from_user.id, current_batch=next_idx)
            await _prompt_intake_dm(message.chat.id, message.from_user.id, next_batch)
        else:
            # All batches collected. Ingest the files, then purge the whole DM
            # exchange (prompts, acks, uploads, stickers) and leave one clean
            # summary with a button back to the control-center channel.
            _, fresh = await fsm.get(message.from_user.id)
            # Include this final sticker in the purge set.
            dm_ids = list(fresh.get("dm_msg_ids", [])) + [message.id]
            title = fresh.get("anime_title", "")
            ok, detail = await _process_manual_upload(message.from_user.id, fresh)
            await fsm.clear(message.from_user.id)
            # Wipe the collection conversation now that files are safely ingested.
            try:
                await client.delete_messages(message.chat.id, dm_ids)
            except Exception:
                pass
            if ok:
                channel_link = _channel_deep_link(container.config.log_channel.channel_id)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                    t(M.MANUAL_BACK_TO_CHANNEL_BTN), url=channel_link)]]) if channel_link else None
                await client.send_message(
                    message.chat.id,
                    t(M.MANUAL_INTAKE_DONE_DM, title=_esc(title)),
                    parse_mode=ParseMode.HTML, reply_markup=kb,
                )
            else:
                await client.send_message(
                    message.chat.id, L(M.MANUAL_QUEUE_FAILED, reason=detail),
                    parse_mode=ParseMode.HTML,
                )

    # ── custom resolution text handler ──

    @client.on_message(filters.text & ~filters.command(["start"]), group=4)
    async def _manual_custom_res_input(_: Client, message: Message) -> None:
        # Resolve the wizard owner. A DM/named reply is the sender; an ANONYMOUS
        # channel reply (posted as the channel, no from_user) carries no id, so
        # we read owner_id off the chat marker armed when "custom" was tapped —
        # the wizard state still lives in THAT admin's FSM.
        owner_id: int | None = None
        via_channel = False
        marker_data: dict = {}
        if message.from_user:
            st, _d = await fsm.get(message.from_user.id)
            if st == STATE_MANUAL_CUSTOM_RES:
                user = getattr(message, "nf_user", None)
                if not (user and auth.has_permission(user, Permission.QUEUE_DOWNLOADS)):
                    return
                owner_id = message.from_user.id
        if owner_id is None:
            m_state, marker_data = await _peek_reply(container.redis, message.chat.id)
            if m_state == STATE_MANUAL_CUSTOM_RES:
                owner_id = marker_data.get("owner_id")
                via_channel = True
        if owner_id is None:
            return
        state, data = await fsm.get(owner_id)
        if state != STATE_MANUAL_CUSTOM_RES:
            return
        text = (message.text or "").strip()
        if not text:
            return
        comp_idx = data.get("current_index", 0)
        queue = data.get("queue", [])
        component = queue[comp_idx][0] if queue else {}
        comp_key = _comp_key(component)
        resolutions = data.get("resolutions", {})
        current = list(resolutions.get(comp_key, []))
        if text not in current:
            current.append(text)
        resolutions[comp_key] = current
        await fsm.set(owner_id, STATE_MANUAL_RES,
                      code=data.get("code"), anime_title=data.get("anime_title"),
                      components=data.get("components"),
                      selected=data.get("selected"), queue=queue,
                      current_index=comp_idx, audio=data.get("audio"),
                      resolutions=resolutions,
                      backdrop_url=data.get("backdrop_url"))
        # Re-render the picker (show() deletes `message`, so the typed reply is
        # cleaned up), then disarm the marker.
        await _render_res_picker(message, owner_id, component, comp_idx)
        if via_channel:
            await _finish_channel_reply(message, marker_data)

    async def _process_manual_upload(
        user_id: int, data: dict,
    ) -> tuple[bool, str]:
        """Ingest the collected files into the local library, then hand off to the
        standard pipeline by enqueuing the request against the ``local`` source.

        Returns ``(ok, detail)`` — ``detail`` is the job id on success or an
        error reason on failure — so the caller owns all user-facing messaging
        (this keeps the DM-purge + single "all done" summary in one place).

        Nothing here duplicates processing: the download worker copies each
        laid-down file and runs every normal stage (rename → subtitles →
        watermark → mux), uploads the finished packs to the storage channel, and
        publishes — exactly as for any other source."""
        import json
        import re
        import shutil
        from pathlib import Path

        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.infrastructure.repositories.request_repo import RequestRepository
        from nekofetch.services.queue_service import QueueService
        from nekofetch.sources.local import _slug

        code = data.get("code", "")
        title = data.get("anime_title", "") or code
        received = data.get("received", {})
        build_order = data.get("build_order", [])
        comp_map = {_comp_key(c): c for c in data.get("components", [])}

        # Audio value → a filename label LocalFileSource re-detects on ingest.
        audio_label_map = {
            "subbed": "Subbed", "dubbed": "Dubbed",
            "dual_audio": "Dual Audio", "multi": "Multi",
        }
        kind_word_map = {"movie": "Movie", "ova": "OVA", "ona": "ONA", "special": "Special"}
        safe_title = re.sub(r'[<>:"/\\|?*]', "", title).strip() or _slug(title)

        try:
            slug = _slug(title)
            lib_dir = Path(container.env.storage_path) / "library" / slug
            lib_dir.mkdir(parents=True, exist_ok=True)
            # Expose the real title (not the slug) to the library metadata reader.
            try:
                (lib_dir / "anime.json").write_text(
                    json.dumps({"title": title}), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001 - metadata override is best-effort
                pass

            # Non-season components (movies/OVAs/specials) each get a dedicated,
            # collision-free season slot (90+) so their episode numbers never clash
            # with real seasons or with one another.
            extra_slot = 90
            slot_for: dict[str, int] = {}
            total_files = 0

            for batch_idx, batch in enumerate(build_order):
                comp_key, comp_label, audio_type, res = batch
                paths = received.get(str(batch_idx), [])
                if not paths:
                    continue
                comp = comp_map.get(comp_key, {"type": "season", "number": 1})
                ctype = comp.get("type", "season")
                if ctype == "season":
                    season = int(comp.get("number", 1) or 1)
                    tag = ""
                else:
                    if comp_key not in slot_for:
                        extra_slot += 1
                        slot_for[comp_key] = extra_slot
                    season = slot_for[comp_key]
                    tag = f" - {kind_word_map.get(ctype, 'Special')}"
                audio_label = audio_label_map.get(audio_type, "Subbed")
                season_dir = lib_dir / f"Season {season:02d}"
                season_dir.mkdir(parents=True, exist_ok=True)
                # Files arrive in episode order (file #1 = E01, …).
                for i, src in enumerate(paths):
                    src_path = Path(src)
                    if not await asyncio.to_thread(src_path.exists):
                        continue
                    ext = src_path.suffix.lstrip(".") or "mkv"
                    fname = (f"{safe_title} - S{season:02d}E{i + 1:03d}{tag} "
                             f"[{res}] [{audio_label}].{ext}")
                    shutil.move(str(src_path), str(season_dir / fname))
                    total_files += 1

            if not total_files:
                raise NekoFetchError("no files were received to process")

            # Repoint the request at the local library and clear any season/episode
            # narrowing so the worker ingests everything just laid down.
            async with session_scope(container.pg_sessionmaker) as session:
                req = await RequestRepository(session).get_by_code(code)
                if req is None:
                    raise NekoFetchError(f"request {code} not found")
                req.source = "local"
                req.source_ref = slug
                req.season = None
                req.episodes = None

            job_id = await QueueService(container).enqueue(code)
            await _spawn_progress_card(job_id, user_id)
            return True, str(job_id)
        except NekoFetchError as exc:
            return False, getattr(exc, "detail", None) or L(M.ERR_GENERIC)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]
