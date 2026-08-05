"""The log channel as an operational **control center**.

The channel is a fixed, ordered layout of persistent messages — a sticker
divider before each of: dashboard, pending, active, completed, activity stream,
catalog — every section edited in place rather than re-posted. Growth-prone
sections (pending / completed / catalog) trail a couple of reserved placeholder
messages for future overflow; static panels reserve none.

The layout is **self-healing**: on every startup we verify each section message
still exists. If the channel was wiped (or the target channel changed), the whole
layout is torn down and rebuilt in order, the pinned sections re-pinned, and
Telegram's "pinned message" service notices swept away so the channel stays clean.

Public surface kept stable for callers:
  * ``event(category, action, **fields)`` — feeds the rolling activity stream.
  * ``ensure_pins()`` / ``refresh()`` — startup self-heal + periodic refresh.
Plus control-center extras: ``post_request_card()`` and ``ask_clarification()``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import asyncio

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import safe_redis_delete, safe_redis_get, safe_redis_set
from nekofetch.localization.messages import M, t
from nekofetch.ui import log_sections as S
from nekofetch.ui.components import cb
from nekofetch.ui.screens import MESSAGE_LIMIT, _truncate_html
from nekofetch.ui.typography import user_label

log = get_logger(__name__)

# ── Redis keys ──
_K_CHANNEL = "nf:logcc:channel_id"
_K_INTRO = "nf:logcc:intro_id"          # intro message id — anchor for full-channel wipe
_K_NOTICES = "nf:logcc:notices"
_K_STICKERS = "nf:logcc:stickers"      # ordered list of layout message ids (cover/intro/dividers)
_K_REQ_MARKERS = "nf:logcc:reqmarkers"  # {code: {divider, card}} — per-request card+divider ids
_K_STUCK = "nf:stuck:{code}"           # per-request stuck-episode state for the attention card
_K_MISSING = "nf:missing:{code}"       # per-request missing-coverage state for the more-links card
_K_DUTY_BOARD = "nf:logcc:duty_board"      # duty board message id


def _sec_key(name: str) -> str:
    return f"nf:logcc:sec:{name}"


def _summarize_runs(nums: list[int]) -> str:
    """Compress a sorted episode list into human run notation.

    ``[1,2,3,7,10,11,12]`` → ``"1–3, 7, 10–12"``. Used by the missing-coverage
    card so a long gap reads as a range instead of a wall of numbers."""
    if not nums:
        return "—"
    nums = sorted(set(nums))
    runs: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append(str(start) if start == prev else f"{start}–{prev}")
        start = prev = n
    runs.append(str(start) if start == prev else f"{start}–{prev}")
    return ", ".join(runs)


def _reserved_key(name: str) -> str:
    return f"nf:logcc:reserved:{name}"


@dataclass(frozen=True)
class _Section:
    name: str
    title_key: str
    pinned: bool = False
    # Growth-prone sections (lists that can outgrow one message) reserve extra
    # slots; static panels (dashboard/active/notices) reserve none.
    growth: bool = False


# Canonical section order. A sticker divider is posted before each one.
_SECTIONS: tuple[_Section, ...] = (
    _Section("dashboard", M.CC_DASHBOARD_TITLE, pinned=True),
    _Section("pending", M.CC_PENDING_TITLE, growth=True),
    _Section("active", M.CC_ACTIVE_TITLE),
    _Section("completed", M.CC_COMPLETED_TITLE, growth=True),
    _Section("notices", M.CC_NOTICES_TITLE),
    _Section("catalog", M.CC_CATALOG_TITLE, pinned=True, growth=True),
)


class LogChannelService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self.cfg = container.config.log_channel
        self._refresh_lock = asyncio.Lock()

    # ── availability ──────────────────────────────────────────────────────────
    @property
    def _client(self):
        return getattr(self._c, "admin_client", None)

    def _active(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.channel_id != 0 and self._client is not None)

    def _sectioned(self) -> bool:
        return bool(self._active() and self.cfg.sections and self._c.redis)

    def _wants(self, category: str) -> bool:
        return "all" in self.cfg.events or category in self.cfg.events

    @staticmethod
    def _ts() -> str:
        from nekofetch.core.timefmt import now_label
        return now_label()

    # ── low-level message helpers ───────────────────────────────────────────────
    async def _send(self, text: str, **kw):
        for attempt in range(3):
            try:
                return await self._client.send_message(
                    self.cfg.channel_id, text, parse_mode=ParseMode.HTML, **kw
                )
            except FloodWait as fw:
                log.warning("logcc.send.flood_wait", wait=fw.value, attempt=attempt)
                await asyncio.sleep(fw.value + 1)
        return await self._client.send_message(
            self.cfg.channel_id, text, parse_mode=ParseMode.HTML, **kw
        )

    async def _edit(self, mid: int, text: str, reply_markup=None) -> None:
        for attempt in range(3):
            try:
                await self._client.edit_message_text(
                    self.cfg.channel_id, mid, _truncate_html(text, MESSAGE_LIMIT),
                    parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                )
                return
            except FloodWait as fw:
                log.warning("logcc.edit.flood_wait", wait=fw.value, attempt=attempt)
                await asyncio.sleep(fw.value + 1)

    async def _section_id(self, name: str) -> int | None:
        # HOT PATH: called every ``refresh`` / ``refresh_active`` tick. A bare
        # ``redis.get`` here would wedge the scheduler the same way the old
        # ``_drop_legacy_inbox`` traceback did, so route through ``safe_redis_get``.
        raw = await safe_redis_get(self._c.redis, _sec_key(name),
                                    label=f"logcc.section_id.{name}")
        return int(raw) if raw else None

    async def _exists(self, mid: int) -> bool:
        """True if message ``mid`` still exists in the channel (not deleted)."""
        try:
            msg = await self._client.get_messages(self.cfg.channel_id, mid)
        except Exception:
            return False
        return bool(msg) and not getattr(msg, "empty", False)

    async def _edit_or_resend(self, name: str, text: str, reply_markup=None) -> None:
        """Edit a section in place; if its message is gone, resend and re-store."""
        mid = await self._section_id(name)
        if mid is not None:
            try:
                await self._edit(mid, text, reply_markup=reply_markup)
                return
            except Exception as exc:
                if "MESSAGE_NOT_MODIFIED" in str(exc):
                    return
                log.debug("logcc.edit.failed", section=name, error=str(exc))
        msg = await self._send(text, reply_markup=reply_markup)
        await self._c.redis.set(_sec_key(name), msg.id)

    # ── startup / self-healing ──────────────────────────────────────────────────
    async def ensure_pins(self) -> None:
        """Backwards-compatible entry point used by the bot manager at startup."""
        await self.ensure_sections()

    async def ensure_sections(self) -> None:
        """Bring the channel into a known-good state on every startup.

        Always wipes and rebuilds the full layout — every restart gets a clean
        slate so no stale messages linger. All state is preserved in Redis.
        """
        if not self._sectioned():
            return
        try:
            await self._wipe_all()
            await self._build_layout()
            await self._reconcile_pins()
            await self.refresh()
        except Exception as exc:
            log.warning("logcc.ensure.failed", error=str(exc))

    async def _channel_changed(self) -> bool:
        stored = await safe_redis_get(self._c.redis, _K_CHANNEL,
                                       label="logcc.channel_changed.get")
        return stored is not None and int(stored) != self.cfg.channel_id

    async def _layout_intact(self) -> bool:
        """Every section message must still exist for the layout to be valid."""
        for sec in _SECTIONS:
            mid = await self._section_id(sec.name)
            if mid is None or not await self._exists(mid):
                return False
        return True

    async def _all_known_ids(self) -> list[int]:
        ids: list[int] = []
        for sec in _SECTIONS:
            mid = await self._section_id(sec.name)
            if mid:
                ids.append(mid)
            raw = await safe_redis_get(self._c.redis, _reserved_key(sec.name),
                                        label=f"logcc.reserved_key.{sec.name}")
            ids += json.loads(raw) if raw else []
        raw = await safe_redis_get(self._c.redis, _K_STICKERS,
                                    label="logcc.stickers.get")
        ids += json.loads(raw) if raw else []
        # Include request card IDs and their divider stickers.
        raw = await safe_redis_get(self._c.redis, _K_REQ_MARKERS,
                                    label="logcc.reqmarkers.get")
        markers = json.loads(raw) if raw else {}
        for entry in markers.values():
            if entry.get("divider"):
                ids.append(entry["divider"])
            if entry.get("card"):
                ids.append(entry["card"])
        return ids

    async def _wipe_all(self) -> None:
        """Delete every message we ever created and clear all stored ids.

        Default flow deletes tracked messages, then sweeps bot-self messages
        via ``from_user.is_self``. When ``cfg.wipe_all_on_rebuild`` is True,
        additionally invokes :func:`safe_full_channel_wipe` so admin-typed
        notes, attention-card echoes, and any other user's chatter get
        cleared too — the user explicitly asked for a true clean slate.
        Either way, ``INTRO`` is the floor: anything older than the intro
        is preserved.
        """
        for mid in await self._all_known_ids():
            try:
                await self._client.delete_messages(self.cfg.channel_id, mid)
            except Exception:
                pass
        # Sweep any remaining bot messages not tracked in Redis.
        for _pass in range(5):  # max 5 passes — safe guard against infinite loop
            deleted_any = False
            try:
                async for msg in self._client.get_chat_history(
                    self.cfg.channel_id, limit=100,
                ):
                    if msg and getattr(msg, "from_user", None) and getattr(msg.from_user, "is_self", False):
                        try:
                            await msg.delete()
                            deleted_any = True
                        except Exception:
                            pass
                        await asyncio.sleep(0.15)  # avoid flood-wait
            except Exception as exc:
                # If the admin_client is a Bot, Telegram forbids
                # ``messages.getHistory`` (returns [400 BOT_METHOD_INVALID]).
                # Retrying won't help — bail out of the 5-pass loop cleanly
                # with a distinct log entry so the operator can tell the
                # difference between "transient network" and "we literally
                # cannot enumerate history from this account".
                from nekofetch.core.channel_safety import is_bot_method_invalid
                if is_bot_method_invalid(exc):
                    log.warning(
                        "logcc.wipe_all.bot_client_skipped",
                        channel_id=self.cfg.channel_id,
                        hint=(
                            "admin_client is a Bot; Telegram forbids GetHistory. "
                            "Tracked message-IDs above were still deleted."
                        ),
                    )
                    break  # exit the retry loop — bots cannot iterate
                # Other transient errors: fall through to the ``deleted_any``
                # check below and let the next pass retry.
                if not deleted_any:
                    break
                continue
            if not deleted_any:
                break

        # Full-channel wipe: clears messages newer than the intro posted by
        # ANY user (admin typing, other bots, anonymous-admin sends via
        # sender_chat). Honours ``wipe_max_history`` and skips pinned ids.
        # Operator opt-out via ``wipe_all_on_rebuild = false`` in config.
        # ``getattr`` with defaults keeps the sweep opt-in for deployments
        # whose ``LogChannelConfig`` snapshot was built before the flag
        # existed (older AppConfig instances lack the field).
        if getattr(self.cfg, "wipe_all_on_rebuild", True):
            intro_raw = await safe_redis_get(self._c.redis, _K_INTRO,
                                             label="logcc.wipe_all.intro.get")
            intro_id = int(intro_raw) if intro_raw else None
            pinned_ids: list[int] = []
            for sec in _SECTIONS:
                if sec.pinned:
                    mid = await self._section_id(sec.name)
                    if mid:
                        pinned_ids.append(mid)
            # Acquire a userbot from the lazily-built pool. The bot-self
            # loop above used `self._client` (a Bot) because bots CAN call
            # `delete_messages`; the safe_full_channel_wipe history-iteration
            # path needs a USER account because Telegram forbids
            # `messages.getHistory` for bots. ``UserbotPool.from_env`` reads
            # TELEGRAM_USERBOT_SESSION from .env — if absent the pool
            # acquires via file session under STORAGE_PATH. Failure here
            # falls back to the (bot) client path inside the helper, which
            # surfaces a distinct ``channel_wipe.skipped_bot_client`` warning
            # so the operator knows the full sweep didn't fire.
            userbot_client = await self._acquire_userbot()
            from nekofetch.core.channel_safety import safe_full_channel_wipe
            await safe_full_channel_wipe(
                self._client, self.cfg.channel_id,
                intro_id=intro_id,
                max_history=getattr(self.cfg, "wipe_max_history", 200),
                preserve_pinned_ids=pinned_ids,
                userbot_client=userbot_client,
            )

    async def _acquire_userbot(self):
        """Acquire a working user account from the userbot pool.

        Lazily builds the pool on the container on first use so startup
        stays unaffected by external login constraints. Returns ``None``
        if no userbot is configured — callers fall back to the (bot)
        client and the safety helper emits a clean warning.
        """
        try:
            from nekofetch.sources.telegram.userbot import UserbotPool
            pool = getattr(self._c, "_userbot_pool", None)
            if pool is None:
                pool = UserbotPool.from_env(
                    self._c.env.telegram_api_id,
                    self._c.env.telegram_api_hash,
                    str(self._c.env.session_path),
                )
                self._c._userbot_pool = pool  # type: ignore[attr-defined]
            return await pool.acquire()
        except Exception as exc:
            log.debug("logcc.userbot.acquire.failed", error=str(exc))
            return None

        keys = [_K_INTRO, _K_STICKERS, _K_NOTICES, _K_DUTY_BOARD, _K_REQ_MARKERS]
        for sec in _SECTIONS:
            keys += [_sec_key(sec.name), _reserved_key(sec.name)]
        # Bulk delete through the safe wrapper so one blip doesn't wedge the
        # channel-rebuild loop. Best-effort — a failed delete just means the
        # next refresh re-edits the section instead of skipping it.
        for k in keys:
            await safe_redis_delete(self._c.redis, k,
                                    label=f"logcc.wipe_all.del.{k}")

    async def _build_layout(self) -> None:
        """Post the full layout, in order:

        1. a cover image (if configured),
        2. a formatted introduction explaining the channel,
        3. a divider, then each control section (reserved slots on growth-prone
           ones), every section preceded by a divider.

        The catalog (last section) and its reserved slots are the final layout
        messages — no trailing divider — so freshly posted request cards append
        into clean space. Every non-section message id is tracked so a later wipe
        removes it too.
        """
        extras: list[int] = []  # cover/intro, dividers — everything but sections

        # Cover + description as a single message: the intro rides as the photo's
        # caption when a cover image is configured, else it's plain text.
        intro_text = t(M.CC_INTRO)
        if self.cfg.cover_image:
            try:
                cover = await self._client.send_photo(
                    self.cfg.channel_id, self.cfg.cover_image,
                    caption=_truncate_html(intro_text, 1000), parse_mode=ParseMode.HTML,
                )
                extras.append(cover.id)
                # Track the intro message id as the FLOOR for the safe full-
                # channel wipe on rebuild — anything older is preserved.
                if self._c.redis:
                    await self._c.redis.set(_K_INTRO, str(cover.id))
            except Exception as exc:
                log.debug("logcc.cover.failed", error=str(exc))
                intro = await self._send(intro_text)
                extras.append(intro.id)
                if self._c.redis:
                    await self._c.redis.set(_K_INTRO, str(intro.id))
        else:
            intro = await self._send(intro_text)
            extras.append(intro.id)
            if self._c.redis:
                await self._c.redis.set(_K_INTRO, str(intro.id))

        # Duty board — shows who is on duty for this channel.
        db_div_id = await self._post_divider()
        if db_div_id:
            extras.append(db_div_id)
        try:
            db_id = await self._post_duty_board()
            if db_id:
                extras.append(db_id)
        except Exception as exc:
            log.debug("logcc.duty_board.failed", error=str(exc))

        for sec in _SECTIONS:
            sid = await self._post_divider()
            if sid:
                extras.append(sid)
            placeholder = f"{t(sec.title_key)}\n{t(M.CC_INITIALIZING)}"
            msg = await self._send(placeholder)
            await safe_redis_set(self._c.redis, _sec_key(sec.name), msg.id,
                                  label=f"logcc.layout.sec.{sec.name}.set")
            if sec.pinned:
                await self._pin_silently(msg.id)
            if sec.growth and self.cfg.reserved_slots > 0:
                reserved = []
                for i in range(self.cfg.reserved_slots):
                    r = await self._send(S.reserved_placeholder(i))
                    reserved.append(r.id)
                await safe_redis_set(self._c.redis, _reserved_key(sec.name),
                                      json.dumps(reserved),
                                      label=f"logcc.layout.reserved.{sec.name}.set")
            # Small delay between sections to avoid flood-wait during startup burst.
            await asyncio.sleep(1.5)

        await safe_redis_set(self._c.redis, _K_STICKERS, json.dumps(extras),
                              label="logcc.layout.stickers.set")
        await safe_redis_set(self._c.redis, _K_CHANNEL, self.cfg.channel_id,
                              label="logcc.layout.channel.set")

    async def _post_divider(self) -> int | None:
        if not self.cfg.divider_sticker_id:
            return None
        for attempt in range(3):
            try:
                msg = await self._client.send_sticker(
                    self.cfg.channel_id, self.cfg.divider_sticker_id
                )
                return msg.id
            except FloodWait as fw:
                log.warning("logcc.divider.flood_wait", wait=fw.value, attempt=attempt)
                await asyncio.sleep(fw.value + 1)
            except Exception as exc:
                log.debug("logcc.divider.failed", error=str(exc))
                return None
        return None

    async def _reconcile_pins(self) -> None:
        """Re-pin the pinned sections (idempotent) so a lost pin self-heals."""
        for sec in _SECTIONS:
            if not sec.pinned:
                continue
            mid = await self._section_id(sec.name)
            if mid:
                await self._pin_silently(mid)

    async def _pin_silently(self, message_id: int) -> None:
        """Pin a message and delete the "pinned this message" service notice
        Telegram auto-posts, so the channel stays clean."""
        for attempt in range(3):
            try:
                await self._client.pin_chat_message(
                    self.cfg.channel_id, message_id, disable_notification=True
                )
                break
            except FloodWait as fw:
                log.warning("logcc.pin.flood_wait", wait=fw.value, attempt=attempt)
                await asyncio.sleep(fw.value + 1)
            except Exception:
                return
        # Best-effort deletion of pinned-message service notices.
        for candidate in range(message_id + 1, message_id + 4):
            try:
                msg = await self._client.get_messages(self.cfg.channel_id, candidate)
                if msg and getattr(msg, "pinned_message", None) is not None:
                    await self._client.delete_messages(self.cfg.channel_id, candidate)
            except Exception:
                pass  # service-notice sweep is best-effort — never fatal

    # ── periodic rebuild ────────────────────────────────────────────────────────
    async def _post_duty_board(self) -> int | None:
        """Post or update the persistent duty board message. Returns message id."""
        if not (self._active() and self._c.redis):
            return None
        from nekofetch.services.shift_service import ShiftService
        from nekofetch.ui.duty_board import duty_board

        shift = ShiftService(self._c)
        state = await shift.get_state("logcc")
        # Resolve missing worker name (can_act auto-assigns with empty name)
        if state.worker_id and not state.worker_name:
            try:
                tg = await self._client.get_users(state.worker_id)
                state.worker_name = " ".join(p for p in (tg.first_name, tg.last_name) if p) or tg.username or ""
            except Exception:
                pass
        text = duty_board(state)

        # Build inline buttons
        kb_rows = []
        if state.worker_id is None:
            kb_rows.append([
                InlineKeyboardButton("🛡️ Take Shift", callback_data=cb("shift", "take", "logcc")),
            ])
        if state.worker_id is not None:
            kb_rows.append([
                InlineKeyboardButton("🟡 Need Relief", callback_data=cb("shift", "relief", "logcc")),
                InlineKeyboardButton("🔵 Request Takeover", callback_data=cb("shift", "takeover", "logcc")),
            ])

        markup = InlineKeyboardMarkup(kb_rows) if kb_rows else None

        mid_raw = await self._c.redis.get(_K_DUTY_BOARD)
        existing_id = int(mid_raw) if mid_raw else None

        if existing_id and await self._exists(existing_id):
            await self._edit(existing_id, text, reply_markup=markup)
            return existing_id

        msg = await self._send(text, reply_markup=markup)
        await self._c.redis.set(_K_DUTY_BOARD, str(msg.id))
        return msg.id

    async def update_duty_board(self) -> None:
        """Refresh the duty board message (called when shift state changes)."""
        if not self._active():
            return
        try:
            await self._post_duty_board()
        except Exception as exc:
            log.debug("logcc.duty_board.update.failed", error=str(exc))

    async def refresh(self) -> None:
        if not self._sectioned():
            return
        async with self._refresh_lock:
            await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> None:
        ts = self._ts()
        # NOTE: 'active' is intentionally absent — it's owned by refresh_active()
        # (the fast lane), which also attaches the per-job Stop keyboard. Editing it
        # here without that markup would strip the buttons every full refresh.
        for name, builder in (
            ("dashboard", self._build_dashboard),
            ("pending", self._build_pending),
            ("completed", self._build_completed),
            ("catalog", self._build_catalog),
        ):
            try:
                await self._edit_or_resend(name, await builder(ts))
            except Exception as exc:
                log.debug("logcc.refresh.section.failed", section=name, error=str(exc))
            await asyncio.sleep(0.6)  # small gap avoids Telegram edit flood-wait
        # Keep the active panel + its Stop controls fresh on the full tick too.
        await self._refresh_active_body()
        # One-time migration: delete the deprecated standalone "Request inbox" message
        # (removed feature — request cards are posted directly again).
        await self._drop_legacy_inbox()
    async def _drop_legacy_inbox(self) -> None:
        """Remove the leftover persistent request-inbox message, if one exists.

        Both Redis ops go through ``safe_redis_get`` / ``safe_redis_delete``
        (shared ``nekofetch.core.redis_safe`` helpers) so a single Upstash
        blip CAN'T wedge the 1-minute ``refresh`` apscheduler job (the
        original traceback). The legacy ``inbox`` key was removed in a prior
        migration; this scan-and-clean only runs so old deployments
        self-heal on first call. A blip on either op is harmless — the next
        tick retries.
        """
        if not self._c.redis:
            return
        old = await safe_redis_get(
            self._c.redis, _sec_key("inbox"), label="logcc.drop_legacy_inbox.get",
        )
        if not old:
            return
        try:
            await self._client.delete_messages(self.cfg.channel_id, int(old))
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass  # legacy inbox already gone or never existed
        # Symmetric timeout-bounded delete: a hung DELETE wedges the scheduler
        # exactly like a hung GET did; ``safe_redis_delete`` handles the same
        # transport-level blip cases. (Saves us one traceback.)
        await safe_redis_delete(
            self._c.redis, _sec_key("inbox"), label="logcc.drop_legacy_inbox.del",
        )

    async def refresh_active(self) -> None:
        """Fast-lane refresh of just the active-tasks panel.

        Runs under the same lock as refresh() so concurrent edits don't
        stack and trigger Telegram''s edit-rate flood-wait (which can reach
        11+ seconds).

        Re-renders only the live downloads/processing section so the progress bar
        tracks reality within seconds, without paying for a full rebuild of the
        dashboard/catalog/completed panels each tick. Identical content is a no-op
        (Telegram MESSAGE_NOT_MODIFIED is swallowed)."""
        if not self._sectioned():
            return
        async with self._refresh_lock:
            await self._refresh_active_body()

    async def _refresh_active_body(self) -> None:
        """Body of the active-panel refresh — does NOT acquire the lock.

        Called by both ``refresh_active()`` (acquires lock first) and
        ``_refresh_unlocked()`` (lock already held by ``refresh()``)."""
        try:
            from nekofetch.services.queue_service import QueueService

            qrows = await QueueService(self._c).dashboard(limit=8)
            text = S.active_section([self._active_row_dict(r) for r in qrows], self._ts())
            await self._edit_or_resend("active", text, reply_markup=self._active_keyboard(qrows))
        except Exception as exc:
            log.debug("logcc.refresh_active.failed", error=str(exc))

    @staticmethod
    def _active_row_dict(r) -> dict:
        return {
            "title": r.anime_title, "stage": r.stage or r.status, "progress": r.progress,
            "speed_bps": r.speed_bps, "eta_seconds": r.eta_seconds, "episode": r.current_episode,
            "season": r.season, "ep_index": r.episode_index, "ep_total": r.total_episodes,
            "done": r.downloaded_bytes, "total": r.total_bytes, "label": r.label,
            "resolution": r.resolution, "audio": r.audio,
        }

    def _active_keyboard(self, qrows):
        """Per in-flight job: a Stop button (skip just the current episode) and a
        Cancel button (terminate the whole series and remove it from the list)."""
        rows = []
        for r in qrows:
            running = str(getattr(r, "status", "")).lower() == "running" or (r.progress or 0) < 100
            if running:
                rows.append([
                    InlineKeyboardButton(t(M.CC_BTN_STOP_EP, ep=r.current_episode or "?"),
                                         callback_data=cb("staff", "jstop", r.job_id)),
                    InlineKeyboardButton(t(M.CC_BTN_CANCEL_JOB),
                                         callback_data=cb("staff", "jcancel", r.job_id)),
                ])
        return InlineKeyboardMarkup(rows) if rows else None

    async def _build_dashboard(self, ts: str) -> str:
        from nekofetch.services.analytics_service import AnalyticsService

        stats = await AnalyticsService(self._c).dashboard()
        return S.dashboard_section(stats, list(stats.most_requested), ts)

    async def _build_pending(self, ts: str) -> str:
        from nekofetch.services.request_service import RequestService

        reqs = await RequestService(self._c).list_pending(limit=10)
        rows = [{"code": r.code, "title": r.anime_title, "by": user_label(r.user)} for r in reqs]
        return S.pending_section(rows, ts)

    async def _build_active(self, ts: str) -> str:
        from nekofetch.services.queue_service import QueueService

        qrows = await QueueService(self._c).dashboard(limit=8)
        rows = [
            {
                "title": r.anime_title,
                "stage": r.stage or r.status,
                "progress": r.progress,
                "speed_bps": r.speed_bps,
                "eta_seconds": r.eta_seconds,
                "episode": r.current_episode,
                "season": r.season,
                "ep_index": r.episode_index,
                "ep_total": r.total_episodes,
                "done": r.downloaded_bytes,
                "total": r.total_bytes,
                "label": r.label,
                "resolution": r.resolution,
                "audio": r.audio,
            }
            for r in qrows
        ]
        return S.active_section(rows, ts)

    async def _published_items(self, limit: int) -> list[dict]:
        from nekofetch.services.distribution_service import DistributionService

        dist = DistributionService(self._c)
        titles = await dist.published_titles(limit=limit)
        items = []
        for doc_id, title in titles:
            seasons = await dist.seasons_for(doc_id)
            items.append({"title": title,
                          "seasons": ", ".join(f"S{s}" for s in seasons) or "—"})
        return items

    async def _build_completed(self, ts: str) -> str:
        return S.completed_section(await self._published_items(6), ts)

    async def _build_catalog(self, ts: str) -> str:
        items = await self._published_items(40)
        return S.catalog_section([(it["title"], it["seasons"]) for it in items], ts)

    # ── activity stream ─────────────────────────────────────────────────────────
    async def event(self, category: str, action: str, **fields) -> None:
        if not self._active() or not self._wants(category):
            return
        ts = self._ts()
        line = S.notice_line(category, action, ts, fields)
        try:
            if self._sectioned():
                await self._push_notice(line)
            else:  # no sections — fall back to a standalone message per event
                await self._send(line)
        except Exception as exc:
            log.warning("logchannel.event.failed", error=str(exc))

    async def _push_notice(self, line: str) -> None:
        raw = await self._c.redis.get(_K_NOTICES)
        lines: list[str] = json.loads(raw) if raw else []
        lines.append(line)
        lines = lines[-self.cfg.notices_lines:]
        await self._c.redis.set(_K_NOTICES, json.dumps(lines))
        # The whole stream lives inside one expandable blockquote.
        body = S.notices_section(lines, self._ts())
        await self._edit_or_resend("notices", body)

    # ── control-center cards (standalone messages with actions) ─────────────────
    async def post_request_card(self, *, code: str, title: str, by: str, scope: str) -> None:
        """Post a new request as a discrete, actionable card so staff can assign a
        source (Telegram / Website / Torrent) or reject it inline. Divider first,
        then the card: Border → Card → Border → Card."""
        if not self._active():
            return

        def _btn(key: str, *parts: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(t(key), callback_data=cb(*parts))

        kb = InlineKeyboardMarkup([
            [_btn(M.ADMIN_BTN_TELEGRAM, "staff", "rsource", code, "telegram"),
             _btn(M.ADMIN_BTN_WEBSITE, "staff", "rsource", code, "website")],
            [_btn(M.ADMIN_BTN_TORRENT, "staff", "rsource", code, "torrent"),
             _btn(M.ADMIN_BTN_DDL, "staff", "rsource", code, "ddl")],
            [_btn(M.ADMIN_BTN_REJECT, "staff", "rreject", code)],
        ])
        try:
            divider_id = await self._post_divider()
            card = await self._send(S.request_card(code, title, by, scope), reply_markup=kb)
            await self._remember_request_markers(code, divider_id, card.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("logcc.request_card.failed", error=str(exc))

    async def _remember_request_markers(
        self, code: str, divider_id: int | None, card_id: int
    ) -> None:
        if not self._c.redis:
            return
        raw = await self._c.redis.get(_K_REQ_MARKERS)
        markers = json.loads(raw) if raw else {}
        markers[code] = {"divider": divider_id, "card": card_id}
        await self._c.redis.set(_K_REQ_MARKERS, json.dumps(markers))

    async def clear_request_markers(self, code: str, *, delete_divider: bool = True,
                                     force: bool = False) -> None:
        """Stop tracking a request card once it's consumed.

        The card message itself is removed by the screen that replaces it, so we
        only ever need to deal with the divider here:

        * If other request cards remain below this one, the divider is safe to
          delete — the next card's divider provides the separator.
        * If this was the **last** card, the divider is kept so there's always a
          visual separator between the catalog and whatever appears below it.
        * ``force=True`` overrides the keep-last-divider rule: caller wants the
          channel pristine regardless (used by the AniZone 5-sec auto-cleanup,
          where the user explicitly told us "We do not need that, like, yeah.
          Always clean, that is always we want to do.").
        """
        if not (self._active() and self._c.redis):
            return
        raw = await self._c.redis.get(_K_REQ_MARKERS)
        markers = json.loads(raw) if raw else {}
        entry = markers.pop(code, None)
        if entry is None:
            return
        # The default rule keeps the divider of the LAST remaining card so the
        # catalog isn't naked against the next section. ``force=True`` breaks
        # that rule on caller demand — admin / automation explicitly wants the
        # channel empty at this point.
        should_delete = bool(
            delete_divider and entry.get("divider") and (markers or force)
        )
        if should_delete:
            try:
                await self._client.delete_messages(self.cfg.channel_id, entry["divider"])
            except Exception:
                pass  # divider already gone
        await self._c.redis.set(_K_REQ_MARKERS, json.dumps(markers))

    async def post_attention_card(
        self, *, code: str, title: str, failures: list, source: str,
        alt_source: str | None,
    ) -> None:
        """Post an actionable card for episodes that couldn't be downloaded, with
        Retry / Switch-source / Provide-file controls. ``failures`` is a list of
        ``{"ep": n, "audio": "subbed"|"dubbed"|...}`` so the card names exactly which
        version failed. The stuck-state is persisted for the action handlers."""
        # Persist the stuck-state FIRST, unconditionally — the recovery-action
        # handlers (staff|aretry / aswitch / aprovide / abandon) read it, and in
        # deployments with no log channel (Kuro Sōden) the Levi progress monitor
        # renders the recovery card from this same state. Without this, an
        # inactive channel would silently strip every recovery control.
        episodes = sorted({f["ep"] for f in failures})
        audio_kinds = sorted({f["audio"] for f in failures if f.get("audio")})
        if self._c.redis:
            await self._c.redis.set(_K_STUCK.format(code=code), json.dumps({
                "episodes": episodes, "title": title, "source": source,
                "audio_kinds": audio_kinds, "alt_source": alt_source,
            }), ex=86400)

        if not self._active():
            return

        def _btn(key: str, *parts: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(t(key), callback_data=cb(*parts))

        buttons = [[_btn(M.CC_BTN_RETRY_EPS, "staff", "aretry", code)]]
        if alt_source:
            buttons.append([_btn(M.CC_BTN_SWITCH_SRC, "staff", "aswitch", code)])
        buttons.append([_btn(M.CC_BTN_PROVIDE, "staff", "aprovide", code)])
        try:
            await self._send(S.attention_card(code, title, failures),
                             reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as exc:  # noqa: BLE001
            log.warning("logcc.attention_card.failed", error=str(exc))

    async def post_missing_source_card(
        self, *, code: str, title: str, report, source: str,
    ) -> None:
        """Persist a request's missing-coverage state and post a "provide more
        links" card, so an admin-driven multi-source request (ddl / torrent) can
        be topped up with the seasons/episodes it's still missing before it
        publishes.

        Like :meth:`post_attention_card`, the Redis state is written FIRST and
        unconditionally — the admin-side handlers (staff|amore / amoredone) and
        the backdrop-rich prompt render from it, so a request stays resumable
        even where the log channel is inactive (Kuro Sōden)."""
        grouped = report.grouped()
        missing_state = {
            "title": title,
            "source": source,
            "empty_seasons": report.empty_seasons,
            # {season: [ep, ...]} of everything still owed.
            "missing": {str(s): eps for s, eps in grouped.items()},
        }
        if self._c.redis:
            await self._c.redis.set(
                _K_MISSING.format(code=code), json.dumps(missing_state), ex=86400,
            )

        if not self._active():
            return

        # Human summary of the gaps: "S3 (all 12), S1 eps 10–12".
        parts: list[str] = []
        for season in sorted(grouped):
            eps = grouped[season]
            if season in report.empty_seasons:
                parts.append(f"S{season} (all {len(eps)})")
            else:
                parts.append(f"S{season} eps {_summarize_runs(eps)}")
        summary = "; ".join(parts) or "—"

        buttons = [
            [InlineKeyboardButton(t(M.CC_BTN_PROVIDE_LINKS)
                                  if hasattr(M, "CC_BTN_PROVIDE_LINKS")
                                  else "🔗 Provide link(s)",
                                  callback_data=cb("staff", "amore", code))],
            [InlineKeyboardButton(t(M.CC_BTN_PUBLISH_ANYWAY)
                                  if hasattr(M, "CC_BTN_PUBLISH_ANYWAY")
                                  else "✅ Publish what we have",
                                  callback_data=cb("staff", "amoredone", code))],
        ]
        text = (
            f"🧩 <b>{title}</b> — still missing content\n"
            f"<code>{code}</code>\n\n"
            f"Downloaded so far, but the franchise still needs: <b>{summary}</b>.\n"
            f"Provide more direct/torrent links to fill the gaps, or publish now "
            f"with what's downloaded."
        )
        try:
            await self._send(text, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as exc:  # noqa: BLE001
            log.warning("logcc.missing_card.failed", error=str(exc))

    async def post_failure_card(
        self, *, code: str, title: str, stage: str, error: str
    ) -> None:
        """Post a prominent, standalone failure card so a failed download/processing
        job is impossible to miss — distinct from the easy-to-overlook rolling
        activity line, which we still emit alongside it."""
        if not self._active():
            return
        try:
            await self._send(S.failure_card(code or "—", title or "—", stage, (error or "")[:300]))
        except Exception as exc:
            log.warning("logcc.failure_card.failed", error=str(exc))

    async def ask_clarification(self, *, code: str, title: str, question: str,
                                options: list[tuple[str, str]]) -> None:
        """Ask admins to resolve an ambiguity (e.g. 'Is this Season 1 or a Movie?').

        ``options`` are (label, callback_data) pairs handled by the admin bot.
        """
        if not self._active():
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=data)]
                                   for lbl, data in options])
        try:
            await self._send(S.ambiguity_card(code, title, question), reply_markup=kb)
        except Exception as exc:
            log.warning("logcc.clarify.failed", error=str(exc))
