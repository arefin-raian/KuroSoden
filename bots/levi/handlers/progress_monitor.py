"""Levi live download progress card.

Kuro Sōden has no log channel, so the rich NekoFetch ``active_row`` panel never
renders here. Instead, when an admin queues a job through Levi we send *them* a
single message that self-refreshes from the Redis progress snapshot the download
worker writes, and — on a partial/complete finish — flips to a terminal card:

    QUEUED / RUNNING  →  live download card (bar, speed, ETA, elapsed, retries)
    COMPLETED         →  "done, handed to distribution"
    partial failures  →  recovery card (Retry / Switch source / Provide / Abandon)

Only ONE refresher task runs per job. It reads ``container.progress.get(job_id)``
on a slow cadence, edits the same message, tolerates MESSAGE_NOT_MODIFIED, and
self-terminates once the job leaves the active states. The ``(chat_id, msg_id)``
pair is parked in Redis so the mid-download Retry/Skip buttons and any external
caller can find the same message.
"""

from __future__ import annotations

import asyncio
import json
import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import JobStatus, Permission
from nekofetch.localization.messages import M, t
from nekofetch.services.auth_service import AuthService
from nekofetch.ui.components import cb
from nekofetch.ui.progress import download_card_html, human_elapsed

log = get_logger(__name__)

_ACTIVE = {JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.PAUSED.value}
# Only these definitively end the job. Anything else (an unknown/transient
# status, or a snapshot gap) is treated as "still working" so the card can never
# flip to the terminal "handed to distribution" frame while processing/encoding/
# uploading is genuinely still in flight.
_TERMINAL = {JobStatus.COMPLETED.value, JobStatus.FAILED.value,
             JobStatus.CANCELLED.value}
_REFRESH_CADENCE = 4.0          # seconds between edits; slow enough to dodge flood limits
_MAX_LIFETIME = 6 * 60 * 60     # hard stop so a wedged job can't leak a task forever


def _msg_key(job_id: int) -> str:
    return f"nf:job:{job_id}:progressmsg"


async def _store_msg_ref(container: Container, job_id: int, chat_id: int, msg_id: int) -> None:
    if container.redis:
        try:
            await container.redis.set(
                _msg_key(job_id), json.dumps({"chat": chat_id, "msg": msg_id}), ex=_MAX_LIFETIME
            )
        except Exception:  # noqa: BLE001 - the card is cosmetic, never fail the queue op
            log.debug("levi.progress.msgref_store_blip", job_id=job_id)


async def _load_msg_ref(container: Container, job_id: int) -> tuple[int, int] | None:
    if not container.redis:
        return None
    try:
        raw = await container.redis.get(_msg_key(job_id))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    d = json.loads(raw)
    return d["chat"], d["msg"]


async def _job_view(container: Container, job_id: int) -> dict | None:
    """One read of everything the card needs, merging the live Redis snapshot over
    the persisted job/request row. Returns None if the job vanished."""
    from nekofetch.infrastructure.database.postgres.models import DownloadJob, Request
    from nekofetch.infrastructure.database.postgres.session import session_scope

    async with session_scope(container.pg_sessionmaker) as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            return None
        req = await session.get(Request, job.request_id)
        status = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
        started = job.started_at
        rs = job.resume_state or {}
        view = {
            "status": status,
            "title": (req.anime_title if req else "—"),
            "code": (req.code if req else ""),
            "started_ts": started.timestamp() if started else None,
            "partial": bool(rs.get("partial_failures")),
            # A redo of a published title relinks the existing posts instead of
            # handing off to distribution — the terminal card is a merged
            # "Redo complete + relinked" card, not the generic hand-off one.
            "redo": bool((req.franchise_data or {}).get("redo_relink")) if req else False,
            # Coarse stage mirrored onto the row by _push_stage_progress. This is
            # the ONLY truthful stage source once the Redis snapshot's TTL lapses
            # (a single long encode/watermark file can outlive it). Without this
            # the card fell back to stage=None → "Downloading 0%" and froze.
            "stage": rs.get("stage"),
        }
    snap = await container.progress.get(job_id) if container.progress else None
    if snap is not None:
        # Live snapshot wins for the fast-moving fields; status too, since the
        # worker updates Redis before it commits the row.
        view.update({
            "status": snap.status or view["status"],
            "progress": snap.progress,
            "stage": snap.stage,
            "label": snap.label,
            "season": snap.season,
            "season_part": snap.season_part,
            "current_episode": snap.current_episode,
            "episode_index": snap.episode_index,
            "total_episodes": snap.total_episodes,
            "resolution": snap.resolution,
            "audio": snap.audio,
            "speed_bps": snap.speed_bps,
            "downloaded_bytes": snap.downloaded_bytes,
            "total_bytes": snap.total_bytes,
            "eta_seconds": snap.eta_seconds,
            "retry_attempt": snap.retry_attempt,
            "retry_max": snap.retry_max,
            "retry_reason": snap.retry_reason,
            "low_disk": snap.low_disk,
        })
    return view


def _live_keyboard(job_id: int, view: dict) -> InlineKeyboardMarkup:
    """Controls shown while Levi is working a source attempt."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Change source", callback_data=cb("levi", "dlcancel", job_id))
    ]])


async def _recovery_keyboard(container: Container, code: str) -> InlineKeyboardMarkup:
    """The terminal recovery card's controls. Reuses the existing staff|a* handlers
    (mounted on Levi via the shared review handler) and adds Abandon."""
    import json as _json

    alt_source = None
    if container.redis:
        try:
            raw = await container.redis.get(f"nf:stuck:{code}")
            if raw:
                alt_source = _json.loads(raw).get("alt_source")
        except Exception:  # noqa: BLE001
            pass

    rows = [[
        InlineKeyboardButton(t(M.CC_BTN_RETRY_EPS), callback_data=cb("staff", "aretry", code))
    ]]
    if alt_source:
        rows.append([InlineKeyboardButton(t(M.CC_BTN_SWITCH_SRC),
                                          callback_data=cb("staff", "aswitch", code))])
    rows.append([
        InlineKeyboardButton(t(M.CC_BTN_PROVIDE), callback_data=cb("staff", "aprovide", code))
    ])
    rows.append([
        InlineKeyboardButton(t(M.CC_BTN_ABANDON), callback_data=cb("levi", "dlabandon", code))
    ])
    return InlineKeyboardMarkup(rows)


def _render_live(job_id: int, view: dict) -> str:
    now = time.time()
    started = view.get("started_ts")
    elapsed = int(now - started) if started else None
    return download_card_html(
        title=view["title"], job_id=job_id, status=view["status"],
        progress=view.get("progress", 0.0), stage=view.get("stage"),
        season=view.get("season"), season_part=view.get("season_part"),
        current_episode=view.get("current_episode"),
        episode_index=view.get("episode_index"), total_episodes=view.get("total_episodes"),
        resolution=view.get("resolution"), audio=view.get("audio"),
        archive_name=view.get("label"),
        speed_bps=view.get("speed_bps", 0.0),
        downloaded_bytes=view.get("downloaded_bytes", 0),
        total_bytes=view.get("total_bytes", 0),
        eta_seconds=view.get("eta_seconds"), elapsed_seconds=elapsed,
        retry_attempt=view.get("retry_attempt", 0), retry_max=view.get("retry_max", 0),
        retry_reason=view.get("retry_reason"), low_disk=view.get("low_disk", False),
    )


async def start_monitor(client: Client, container: Container, job_id: int,
                        chat_id: int) -> None:
    """Send the initial card to ``chat_id`` and spawn the self-refreshing task.

    Safe to call at enqueue time — failures here never propagate to the caller,
    because a broken progress card must not break queueing."""
    try:
        view = await _job_view(container, job_id)
        if view is None:
            return
        msg = await client.send_message(
            chat_id, _render_live(job_id, view), parse_mode=ParseMode.HTML,
            reply_markup=_live_keyboard(job_id, view),
        )
        await _store_msg_ref(container, job_id, chat_id, msg.id)
        asyncio.create_task(_refresh_loop(client, container, job_id, chat_id, msg.id))
    except Exception as exc:  # noqa: BLE001
        log.warning("levi.progress.start_failed", job_id=job_id, error=str(exc))


async def _refresh_loop(client: Client, container: Container, job_id: int,
                        chat_id: int, msg_id: int) -> None:
    deadline = time.monotonic() + _MAX_LIFETIME
    last_text = ""
    held = False
    while time.monotonic() < deadline:
        await asyncio.sleep(_REFRESH_CADENCE)

        # While the naming/caption confirm cards own this message, stand down so
        # we never repaint the live card over a confirm prompt. When the hold
        # lifts, force one repaint (drop last_text) so the card returns to live.
        if container.redis is not None:
            try:
                hold = await container.redis.get(f"nf:job:{job_id}:confirm_hold")
            except Exception:  # noqa: BLE001
                hold = None
            if hold:
                held = True
                continue
            if held:
                held = False
                last_text = ""  # ensure the next edit fires even if text matches

        try:
            view = await _job_view(container, job_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("levi.progress.view_blip", job_id=job_id, error=str(exc))
            continue
        if view is None:
            return
        status = view["status"]

        if status in _TERMINAL:
            # Definitively done/failed/cancelled — paint the final card once, stop.
            await _paint_terminal(client, container, job_id, chat_id, msg_id, view)
            return

        # Active OR any transient/unknown status (snapshot gap between the
        # download loop and processing, etc.) → keep the live card refreshing.
        # This is what stops a premature "handed to distribution" while encoding
        # or uploading is still running.
        text = _render_live(job_id, view)
        if text != last_text:
            try:
                await client.edit_message_text(
                    chat_id, msg_id, text, parse_mode=ParseMode.HTML,
                    reply_markup=_live_keyboard(job_id, view),
                )
                last_text = text
            except MessageNotModified:
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("levi.progress.edit_blip", job_id=job_id, error=str(exc))
        continue
    log.info("levi.progress.monitor_expired", job_id=job_id)


async def _completion_art(container: Container, code: str) -> str | None:
    """Rotating backdrop for the finished anime, for the terminal completion card.

    Mirrors review.py's ``_anime_backdrop`` — reuse the per-anime art pool seeded
    at request time so the completion card shows this title's own backdrop (never
    a poster). Returns a URL, or ``None`` when no art is available (caller then
    falls back to a plain text card)."""
    if not code:
        return None
    try:
        from nekofetch.infrastructure.database.postgres.models import Request
        from nekofetch.infrastructure.database.postgres.session import session_scope
        from nekofetch.ui.artwork import (
            ensure_anime_art,
            key_for_franchise,
            next_anime_art,
        )

        async with session_scope(container.pg_sessionmaker) as session:
            from sqlalchemy import select
            req = (
                await session.execute(select(Request).where(Request.code == code))
            ).scalar_one_or_none()
            if req is None:
                return None
            franchise = req.franchise_data or {}
            title = franchise.get("title") or req.anime_title

        key = key_for_franchise(franchise, title=title)
        await ensure_anime_art(key, tmdb=container.tmdb, title=title,
                               franchise=franchise)
        art = next_anime_art(key)
        return art if isinstance(art, str) else None
    except Exception as exc:  # noqa: BLE001 - art is cosmetic, never fail the card
        log.debug("levi.progress.completion_art_blip", code=code, error=str(exc))
        return None


async def _paint_terminal(client: Client, container: Container, job_id: int,
                          chat_id: int, msg_id: int, view: dict) -> None:
    title, code, status = view["title"], view.get("code", ""), view["status"]
    if status == JobStatus.CANCELLED.value:
        return  # cancel path already told the admin; leave the last frame
    if view.get("partial"):
        text = t(M.DL_CARD_FAILED, title=title, job=job_id)
        kb = await _recovery_keyboard(container, code)
    elif status == JobStatus.FAILED.value:
        text = t(M.DL_CARD_FAILED, title=title, job=job_id)
        kb = await _recovery_keyboard(container, code)
    else:
        # Success — paint a proper completion card with a cover backdrop, not a
        # one-liner. Delete the live text message and send a photo card so the
        # admin sees the anime's own art on "handed to distribution".
        started = view.get("started_ts")
        elapsed = human_elapsed(int(time.time() - started)) if started else "—"
        if view.get("redo"):
            # Merged card: pipeline stats + the relink result, one message. The
            # separate redo DM in handoff is dropped so this is the only one.
            from kurosoden.shared import levi_voice as _LV
            text = _LV.redo_complete_card(title, elapsed, code)
        else:
            text = t(M.DL_CARD_DONE, title=title, job=job_id, elapsed=elapsed)
        art = await _completion_art(container, code)
        if art:
            try:
                await client.send_photo(
                    chat_id, art, caption=text, parse_mode=ParseMode.HTML,
                )
                try:
                    await client.delete_messages(chat_id, msg_id)
                except Exception:  # noqa: BLE001
                    pass
                return
            except Exception as exc:  # noqa: BLE001 - fall back to text edit below
                log.debug("levi.progress.done_photo_blip", job_id=job_id, error=str(exc))
        # No art (or photo send failed) → edit the existing card in place.
        try:
            await client.edit_message_text(chat_id, msg_id, text,
                                           parse_mode=ParseMode.HTML)
        except MessageNotModified:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("levi.progress.terminal_blip", job_id=job_id, error=str(exc))
        return
    try:
        await client.edit_message_text(chat_id, msg_id, text, parse_mode=ParseMode.HTML,
                                        reply_markup=kb)
    except MessageNotModified:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("levi.progress.terminal_blip", job_id=job_id, error=str(exc))


def register(client: Client, container: Container) -> None:
    """Mount the live-card action handlers (skip current ep / source abort / abandon).

    The Retry/Switch/Provide recovery buttons reuse the shared ``staff|a*``
    handlers already mounted by the review handler — we only add the Levi-owned
    controls here."""
    auth = AuthService(container)

    async def _guard(q: CallbackQuery, perm: Permission) -> bool:
        user = getattr(q, "nf_user", None)
        if not (user and auth.has_permission(user, perm)):
            await q.answer(t(M.ACCESS_DENIED), show_alert=True)
            return False
        return True

    @client.on_callback_query(filters.regex(r"^levi\|dlskip\|"))
    async def _dl_skip(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        parts = q.data.split("|")
        job_id, ep = int(parts[2]), (parts[3] if len(parts) > 3 else "?")
        from nekofetch.services.download_service import DownloadWorker
        await DownloadWorker(container).request_skip(job_id)
        await q.answer(t(M.DL_TOAST_SKIPPED, ep=ep), show_alert=True)

    @client.on_callback_query(filters.regex(r"^levi\|dlcancel\|"))
    async def _dl_cancel(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        job_id = int(q.data.split("|")[2])
        from nekofetch.services.download_service import DownloadWorker
        await DownloadWorker(container).request_source_abort(job_id)
        view = await _job_view(container, job_id) or {}
        code = view.get("code")
        if q.message is not None and code:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🌐 Website sources",
                    callback_data=cb("levi", "website", code),
                ),
                 InlineKeyboardButton(
                    "✈️ Telegram manual",
                    callback_data=cb("levi", "telegram", code),
                )],
                [InlineKeyboardButton(
                    "🧲 Torrent",
                    callback_data=cb("staff", "rsource", code, "torrent"),
                ),
                 InlineKeyboardButton(
                    "🔗 Direct Link (DDL)",
                    callback_data=cb("staff", "rsource", code, "ddl"),
                )],
            ])
            try:
                await q.message.edit_text(
                    (
                        "⚔️ <b>Source attempt stopped.</b>\n\n"
                        f"<b>{view.get('title', code)}</b>\n"
                        f"<code>{code}</code>\n\n"
                        "<i>Pick another route. The request stays alive.</i>"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            except Exception:  # noqa: BLE001
                pass
        await q.answer("Source stopped. Pick another route.", show_alert=True)

    @client.on_callback_query(filters.regex(r"^levi\|dlretry\|"))
    async def _dl_retry(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        parts = q.data.split("|")
        code, ep = parts[2], (parts[3] if len(parts) > 3 else "?")
        from nekofetch.services.queue_service import QueueService
        from nekofetch.services.request_service import RequestService
        try:
            await RequestService(container).retry_episodes(code, [int(ep)])
            await QueueService(container).enqueue(code)
            await q.answer(t(M.DL_TOAST_RETRYING, ep=ep), show_alert=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("levi.progress.retry_failed", code=code, error=str(exc))
            await q.answer(t(M.DL_TOAST_RETRYING, ep=ep))

    @client.on_callback_query(filters.regex(r"^levi\|dlabandon\|"))
    async def _dl_abandon(_: Client, q: CallbackQuery) -> None:
        if not await _guard(q, Permission.QUEUE_DOWNLOADS):
            return
        parts = q.data.split("|")
        code = parts[2]
        confirmed = len(parts) > 3 and parts[3] == "go"
        if not confirmed:
            from nekofetch.services.request_service import RequestService
            title = await RequestService(container).title_for(code)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t(M.CC_BTN_ABANDON_CONFIRM),
                                      callback_data=cb("levi", "dlabandon", code, "go"))],
            ])
            try:
                await q.message.edit_text(t(M.ATTN_ABANDON_CONFIRM, title=title),
                                          parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:  # noqa: BLE001
                pass
            await q.answer()
            return
        from nekofetch.services.request_service import RequestService
        result = await RequestService(container).abandon(code)
        try:
            await q.message.edit_text(
                t(M.ATTN_ABANDONED, title=result.get("title", code),
                  files=result.get("files", 0), packs=result.get("packs", 0)),
                parse_mode=ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001
            pass
        await q.answer()
