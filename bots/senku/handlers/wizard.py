"""Senku's channel-creation wizard — Phase 2 of the distribution flow.

An FSM-backed, button-driven flow keyed by **request code**. It replaces the old
``/create`` / ``/generate`` text stubs with a real stepper:

    open → franchise map → Begin → title → username → poster → description
         → add-admins → "I've created it" → send @username → verify → thumbnails

Every step is one voiced card with recurring artwork and clean buttons (the
cross-bot bar set by Lelouch/Levi). All copy comes from :mod:`senku_voice`; the
channel essentials (title / username / description) come from
:mod:`channel_essentials`, which reuses NekoFetch's exact auto-pipeline logic so
the manual output matches what the automated build would have produced. The
working set (franchise + entries + chosen channel) lives in
:class:`DistributionCache`, keyed by code.

Routing sits under a single ``^senku\\|wiz\\|`` dispatcher registered in group 0,
ahead of the ``^senku\\|`` home/settings fallback in ``app.py``. The thumbnail
loop (Phase 3) is entered via ``_enter_thumbnails`` — a stub here that hands off
to the Phase 3 handler once it lands.
"""

from __future__ import annotations

import asyncio
import secrets

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import Role
from nekofetch.ui.artwork import (
    ensure_anime_art, key_for_franchise, next_anime_art, pick_artwork,
)
from nekofetch.ui.components import cb, keyboard
from nekofetch.ui.screens import Screen, card, send_screen
from nekofetch.services.thumbnail_service import webp_to_jpeg

from kurosoden.shared import senku_voice as V
from kurosoden.shared.channel_essentials import build_channel_essentials
from kurosoden.shared.distribution_cache import DistributionCache
from kurosoden.shared import franchise_map
from kurosoden.shared.senku_thumbnail_adapter import SenkuThumbnailAdapter

log = get_logger(__name__)

BOT = "senku"

# FSM state: waiting for the admin to send the created channel's @username / id.
STATE_AWAIT_CHANNEL = "senku:wiz:await_channel"

# FSM state: waiting for the admin to paste a corrected watch order (Phase 4 edit).
STATE_AWAIT_ORDER = "senku:wiz:await_order"

# FSM state: waiting for the admin to send their own asset image (logo/poster/bg).
STATE_AWAIT_UPLOAD = "senku:wiz:await_upload"

# FSM state: after a userbot created the channel, waiting for the operator to JOIN
# it via the invite link — you can only promote an existing member, so they must
# join before we can grant them the admin rights needed to set the profile picture.
STATE_AWAIT_UBOT_JOIN = "senku:wiz:await_ubot_join"

# FSM state: operator promoted; waiting for them to add the PFP + clear the service
# message, then mark done (they don't send a handle — we already own the channel,
# so we advance straight to thumbnails).
STATE_AWAIT_UBOT_DONE = "senku:wiz:await_ubot_done"

# Commands that must never be swallowed by the free-text channel step.
_RESERVED = ["start", "tasks", "create", "generate", "settings", "help", "cancel"]


def register(client: Client, container: Container) -> None:
    fsm = FSM(container.redis, bot="senku")
    cache = DistributionCache(container)
    thumbs = SenkuThumbnailAdapter(container)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _staff(obj) -> bool:
        user = getattr(obj, "nf_user", None)
        if user is None:
            return False
        try:
            return Role(user.role) in (Role.STAFF, Role.ADMIN)
        except Exception:  # noqa: BLE001 — unknown role string ⇒ not staff
            return False

    async def _art(franchise: dict | None, title: str):
        """This franchise's rotating backdrop, or Senku's character art.

        Mirrors the Lelouch/Levi rule (``requests.py``): a card about a specific
        title carries that title's art; otherwise it falls back to Senku's gallery
        — never bare. Returns a ``Path`` or URL string; ``card`` accepts both.
        """
        if franchise:
            try:
                key = key_for_franchise(franchise, title=title)
                await ensure_anime_art(key, tmdb=container.tmdb, title=title,
                                       franchise=franchise)
                return next_anime_art(key, fallback_bot=BOT)
            except Exception as exc:  # noqa: BLE001 — art is decorative
                log.debug("senku.wiz.art_failed", title=title, error=str(exc))
        return pick_artwork(BOT)

    async def _title_of(code: str, franchise: dict | None) -> str:
        if franchise:
            return (franchise.get("english") or franchise.get("title")
                    or franchise.get("anime_title") or code)
        return code

    async def _open(chat_id: int, code: str, *, old_msg: Message | None) -> None:
        """Seed the cache and render the franchise map + Begin."""
        franchise = await cache.ensure(code)
        if not franchise:
            await send_screen(
                client, chat_id,
                card(V.NO_TASK, image=pick_artwork(BOT), bot_name=BOT,
                     buttons=[[(V.BTN_HOME, cb(BOT, "home"))]]),
                old_msg=old_msg,
            )
            return
        title = await _title_of(code, franchise)
        entries = await cache.get_entries(code)
        tree = franchise_map.render_tree(entries, title)
        screen = card(
            f"{V.handoff_card(title, code, len(entries))}\n\n{tree}",
            image=await _art(franchise, title), bot_name=BOT,
            buttons=[
                [(V.BTN_BEGIN, cb(BOT, "wiz", "scope", code))],
                [(V.BTN_HOME, cb(BOT, "home"))],
            ],
        )
        await send_screen(client, chat_id, screen, old_msg=old_msg)

    async def _channel_ctx(code: str):
        """Resolve (franchise, title, essentials) for a channel step, or None.

        Shared by every channel sub-step so the essentials (title / username /
        description / poster link) are computed the same way each card.
        """
        franchise = await cache.get_franchise(code) or await cache.ensure(code)
        if not franchise:
            return None
        title = await _title_of(code, franchise)
        ess = await build_channel_essentials(
            container,
            anime_doc_id=franchise.get("anime_doc_id"),
            franchise=franchise,
        )
        return franchise, title, ess

    async def _no_task(chat_id: int, *, old_msg: Message | None) -> None:
        await send_screen(
            client, chat_id,
            card(V.NO_TASK, image=pick_artwork(BOT), bot_name=BOT,
                 buttons=[[(V.BTN_HOME, cb(BOT, "home"))]]),
            old_msg=old_msg,
        )

    async def _scope_step(chat_id: int, code: str, *, old_msg: Message | None) -> None:
        """Ask who creates the channel — the admin ("own") or a userbot.

        Both paths converge on the thumbnail loop. The userbot option carries a
        note: prefer creating your own until you've hit the ~10-channel cap.
        """
        ctx = await _channel_ctx(code)
        if ctx is None:
            await _no_task(chat_id, old_msg=old_msg)
            return
        franchise, title, _ess = ctx
        screen = card(
            V.channel_scope_prompt(title), image=await _art(franchise, title),
            bot_name=BOT,
            buttons=[
                [(V.BTN_SCOPE_OWN, cb(BOT, "wiz", "chan", code))],
                [(V.BTN_SCOPE_USERBOT, cb(BOT, "wiz", "ubot", code))],
                [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))],
            ],
        )
        await send_screen(client, chat_id, screen, old_msg=old_msg)

    async def _userbot_create(chat_id: int, user_id: int, code: str,
                              *, old_msg: Message | None) -> None:
        """Have a pooled userbot create + configure the channel, then hand off.

        On success we store the channel and drop into the "add the photo + clear
        the service message" wait; on no-free-account or failure we bounce the
        admin back to the manual (own) flow rather than dead-ending."""
        ctx = await _channel_ctx(code)
        if ctx is None:
            await _no_task(chat_id, old_msg=old_msg)
            return
        franchise, title, _ess = ctx
        # Immediate feedback — creation can take a few seconds.
        await send_screen(
            client, chat_id,
            card(V.userbot_creating(title), image=await _art(franchise, title),
                 bot_name=BOT),
            old_msg=old_msg,
        )

        anime_doc_id = franchise.get("anime_doc_id")
        info = None
        try:
            from nekofetch.services.bot_factory import BotFactory

            info = await BotFactory(container).create_channel_via_userbot(anime_doc_id)
        except Exception as exc:  # noqa: BLE001 — surface as a recoverable failure
            log.warning("senku.wiz.userbot_create_failed", code=code, error=str(exc))

        if info is None:
            # Either every account is full, or creation errored — go manual.
            await send_screen(
                client, chat_id,
                card(V.CHANNEL_SCOPE_NO_USERBOT, image=await _art(franchise, title),
                     bot_name=BOT,
                     buttons=[[(V.BTN_SCOPE_OWN, cb(BOT, "wiz", "chan", code))],
                              [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            )
            return

        handle = f"@{info.username}" if info.username else info.name
        invite = None
        try:
            from nekofetch.services.invite_link_service import InviteLinkService

            invite = await InviteLinkService(container).ensure_for_bot(info.id)
        except Exception as exc:  # noqa: BLE001 — link is best-effort
            log.warning("senku.wiz.invite_failed", code=code, error=str(exc))
        await cache.set_channel(code, handle=handle, chat_id=info.chat_id)
        # The operator must JOIN before we can promote them (you can only promote an
        # existing member), and they need admin rights to set the profile picture.
        # So: join → "I've joined" → promote → set-photo → done. Stash the chat_id
        # and invite so the join step can promote against the right channel.
        await fsm.set(user_id, STATE_AWAIT_UBOT_JOIN, code=code,
                      chat_id=info.chat_id)
        buttons = []
        if invite:
            buttons.append([(V.BTN_USERBOT_JOIN, invite)])
        await send_screen(
            client, chat_id,
            card(V.userbot_join(handle, invite), image=await _art(franchise, title),
                 bot_name=BOT,
                 url_buttons=buttons or None,
                 buttons=[[(V.BTN_USERBOT_JOINED, cb(BOT, "wiz", "ubotjoined", code))],
                          [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
        )

    async def _userbot_joined(chat_id: int, user_id: int, code: str,
                              *, old_msg: Message | None) -> None:
        """After the operator joins the invite link, promote them, then ask for the
        profile picture. Promotion grants change-info rights (needed to set the PFP);
        if it fails we say so plainly rather than sending them to a dead end."""
        _state, data = await fsm.get(user_id)
        channel_id = data.get("chat_id")
        ctx = await _channel_ctx(code)
        franchise, title = (ctx[0], ctx[1]) if ctx else (None, await _title_of(code, None))

        promoted = False
        if channel_id:
            try:
                from nekofetch.services.bot_factory import BotFactory

                promoted = await BotFactory(container).promote_operator(
                    int(channel_id), user_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("senku.wiz.promote_operator_failed", code=code, error=str(exc))

        if not promoted:
            # Couldn't promote — most often the operator hasn't actually joined yet.
            await send_screen(
                client, chat_id,
                card(V.userbot_promote_failed(), image=await _art(franchise, title),
                     bot_name=BOT,
                     buttons=[[(V.BTN_USERBOT_JOINED, cb(BOT, "wiz", "ubotjoined", code))],
                              [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
                old_msg=old_msg,
            )
            return

        await fsm.set(user_id, STATE_AWAIT_UBOT_DONE, code=code)
        await send_screen(
            client, chat_id,
            card(V.userbot_set_photo(), image=await _art(franchise, title),
                 bot_name=BOT,
                 buttons=[[(V.BTN_USERBOT_DONE, cb(BOT, "wiz", "ubotdone", code))],
                          [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            old_msg=old_msg,
        )

    async def _channel_step(chat_id: int, code: str, *, old_msg: Message | None) -> None:
        """Step 1 — CREATE: channel name + a menu of valid @username candidates.

        The admin creates a public channel with one of the suggested usernames.
        The bot sets the DECORATED title itself later (once it's an admin), so
        this card only needs a plain name + username options.
        """
        ctx = await _channel_ctx(code)
        if ctx is None:
            await _no_task(chat_id, old_msg=old_msg)
            return
        franchise, title, ess = ctx
        body = V.channel_create_card(ess.channel_name, ess.username_candidates)
        screen = card(
            body, image=await _art(franchise, title), bot_name=BOT,
            buttons=[
                [(V.BTN_NEXT, cb(BOT, "wiz", "chan2", code))],
                [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))],
            ],
        )
        await send_screen(client, chat_id, screen, old_msg=old_msg)

    async def _channel_step2(chat_id: int, code: str, *, old_msg: Message | None) -> None:
        """Step 2 — PFP: add the TMDB poster as the channel photo."""
        ctx = await _channel_ctx(code)
        if ctx is None:
            await _no_task(chat_id, old_msg=old_msg)
            return
        franchise, title, ess = ctx
        screen = card(
            V.channel_pfp_line(), image=await _art(franchise, title), bot_name=BOT,
            url_buttons=[[(V.BTN_TMDB_POSTER, ess.poster_search_url)]],
            buttons=[
                [(V.BTN_NEXT, cb(BOT, "wiz", "chan3", code))],
                [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))],
            ],
        )
        await send_screen(client, chat_id, screen, old_msg=old_msg)

    async def _channel_step3(chat_id: int, code: str, *, old_msg: Message | None) -> None:
        """Step 3 — ADMINS: add Senku + Gojo as admins, then ask for the link."""
        ctx = await _channel_ctx(code)
        if ctx is None:
            await _no_task(chat_id, old_msg=old_msg)
            return
        franchise, title, _ess = ctx
        screen = card(
            V.CHANNEL_ADMINS_LINE, image=await _art(franchise, title), bot_name=BOT,
            buttons=[
                [(V.BTN_SEND_LINK, cb(BOT, "wiz", "chandone", code))],
                [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))],
            ],
        )
        await send_screen(client, chat_id, screen, old_msg=old_msg)

    async def _ask_channel(chat_id: int, user_id: int, code: str,
                           *, old_msg: Message | None) -> None:
        """Step 4 — LINK: arm the reply flow to receive the channel link."""
        prompt = await send_screen(
            client, chat_id,
            card(V.CHANNEL_ASK_LINK, image=pick_artwork(BOT), bot_name=BOT,
                 buttons=[[(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            old_msg=old_msg,
        )
        # Stash the prompt card's message id so the finalise step can REPLACE it
        # (old_msg=) with the title/description progress card instead of sending a
        # separate message — the "name/description goes out as a new message with
        # no buttons" complaint.
        await fsm.set(user_id, STATE_AWAIT_CHANNEL, code=code,
                      prompt_msg_id=prompt.id, prompt_chat_id=prompt.chat.id)

    async def _verify_and_store(chat_id: int, user_id: int, code: str, raw: str) -> None:
        """Resolve the channel, confirm BOTH Senku and Gojo are admins, store, advance.

        Senku posts the info card / watch guide; Gojo runs the publishing side —
        so both bots must be admins before we proceed. We resolve the channel with
        Senku (which becomes the cached peer), then check Gojo's membership through
        Senku's peer by Gojo's user id, so Gojo needn't have seen the channel yet.
        """
        # The "please send the channel link" prompt card id (stashed at
        # _ask_channel) — the finalise step edits THIS card in place into the
        # title/description progress card instead of sending a separate message.
        _state, _data = await fsm.get(user_id)
        prompt_chat_id = _data.get("prompt_chat_id") or chat_id
        prompt_msg_id = _data.get("prompt_msg_id")

        handle = raw.strip()
        target: str | int = handle
        if not handle.startswith("@") and not handle.lstrip("-").isdigit():
            target = f"@{handle}"
        elif handle.lstrip("-").isdigit():
            target = int(handle)

        def _is_admin_status(member) -> bool:
            status = getattr(getattr(member, "status", None), "value",
                             str(getattr(member, "status", "")))
            return status in ("administrator", "creator")

        chat = None
        senku_ok = False
        missing: list[str] = []
        try:
            chat = await client.get_chat(target)
            me = await client.get_chat_member(chat.id, "me")
            senku_ok = _is_admin_status(me)
        except Exception as exc:  # noqa: BLE001 — bad handle / not a member / not admin
            log.info("senku.wiz.verify_failed", code=code, handle=handle, error=str(exc))

        # Verify Gojo (the publisher bot) is also an admin. Best-effort: in a
        # single-bot/test container with no pipeline manager we don't block.
        gojo = getattr(getattr(container, "pipeline_manager", None), "gojo", None)
        gojo_ok = True
        if chat is not None and gojo is not None:
            gojo_ok = False
            try:
                gojo_me = await gojo.get_me()
                gm = await client.get_chat_member(chat.id, gojo_me.id)
                gojo_ok = _is_admin_status(gm)
            except Exception as exc:  # noqa: BLE001 — not a member ⇒ not admin
                log.info("senku.wiz.gojo_verify_failed", code=code, error=str(exc))

        display = f"@{chat.username}" if chat and chat.username else (
            chat.title if chat else handle
        )
        if not senku_ok:
            missing.append("Senku (me)")
        if not gojo_ok:
            missing.append("Gojo")
        if chat is None or missing:
            # Replace the prompt card with the failure card (keep the flow single
            # -message). Crucially we KEEP the link step armed: the admin can just
            # send the @username / link AGAIN (no button tap needed) and we retry —
            # a bad or not-yet-admin handle must never dead-end the wizard. The
            # "I've added them" button is a convenience re-check for the same input.
            if prompt_msg_id:
                try:
                    await client.delete_messages(prompt_chat_id, prompt_msg_id)
                except Exception:  # noqa: BLE001
                    pass
            fail = await send_screen(
                client, chat_id,
                card(V.channel_verify_failed(display, missing or None),
                     image=pick_artwork(BOT), bot_name=BOT,
                     buttons=[
                         [(V.BTN_CHANNEL_DONE, cb(BOT, "wiz", "chandone", code))],
                         [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))],
                     ]),
            )
            # Re-arm the free-text step so a re-typed handle is picked up, and point
            # the prompt id at THIS failure card so the retry edits in place.
            await fsm.set(user_id, STATE_AWAIT_CHANNEL, code=code,
                          prompt_msg_id=fail.id, prompt_chat_id=fail.chat.id)
            return

        await fsm.clear(user_id)
        await cache.set_channel(code, handle=display, chat_id=chat.id)

        async def _sweep_channel_service_notices() -> None:
            """Delete the "changed the channel name/photo/description" service
            messages Telegram auto-posts when the bot edits the channel.

            Telegram posts these service messages ASYNCHRONOUSLY — they can land
            a beat AFTER set_chat_title/description returns, so a single immediate
            scan often misses them (the notice isn't in history yet). We therefore
            run several passes with a short settle delay between them, over a wider
            history window, and match the service flags AND the new-title text.

            IMPORTANT: a Telegram BOT client cannot reliably read channel history
            via ``get_chat_history`` (it usually comes back empty), so the bot-only
            sweep silently found nothing and the notice stayed. We prefer a USERBOT
            (a real account that CAN read history and delete the service message)
            and only fall back to the bot client if no userbot is available.
            Best-effort — a leftover notice is cosmetic, never fatal."""

            # Prefer a userbot: bots can't page channel history, real accounts can.
            sweep_client = client
            try:
                from nekofetch.sources.telegram.userbot import UserbotPool

                pool = getattr(container, "_userbot_pool", None)
                if pool is None:
                    pool = UserbotPool.from_env(
                        container.env.telegram_api_id,
                        container.env.telegram_api_hash,
                        str(container.env.session_path),
                    )
                    container._userbot_pool = pool  # type: ignore[attr-defined]
                ub = await pool.acquire()
                if ub is not None:
                    sweep_client = ub
            except Exception as exc:  # noqa: BLE001 — no userbot → use bot client
                log.debug("senku.wiz.sweep_no_userbot", code=code, error=str(exc))

            def _is_service_notice(m) -> bool:
                # Pyrogram exposes the service kind as `m.service` (a
                # MessageServiceType enum) plus convenience attrs. Any of these
                # present ⇒ it's an auto-posted service message about the channel.
                return (
                    getattr(m, "new_chat_title", None) is not None
                    or getattr(m, "new_chat_photo", None) is not None
                    or getattr(m, "delete_chat_photo", None)
                    or getattr(m, "service", None) is not None
                )

            async def _one_pass() -> int:
                removed = 0
                try:
                    async for m in sweep_client.get_chat_history(chat.id, limit=30):
                        if _is_service_notice(m):
                            try:
                                await sweep_client.delete_messages(chat.id, m.id)
                                removed += 1
                            except Exception:  # noqa: BLE001
                                pass
                except Exception as exc:  # noqa: BLE001 — sweep is best-effort
                    log.debug("senku.wiz.service_sweep_blip", code=code, error=str(exc))
                return removed

            # A few spaced passes catch notices that arrive slightly late.
            total = 0
            for delay in (0.0, 1.5, 2.5, 3.0):
                if delay:
                    await asyncio.sleep(delay)
                total += await _one_pass()
            if total:
                log.info("senku.wiz.service_swept", code=code, removed=total)

        # ── Bot-driven finalisation ─────────────────────────────────────────
        # Both bots are admins now, so Senku sets the decorated title + the
        # description ITSELF (the admin shouldn't type these). We EDIT the "send
        # the channel link" prompt card in place into a progress card (mirrors the
        # download bar's stage list) — never a separate message — and keep a
        # Cancel button on it throughout.
        steps = [("Set channel title", "active"),
                 ("Set channel description", "todo")]
        cancel_kb = keyboard([(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))])

        prog = None
        if prompt_msg_id:
            # Edit the existing prompt card (caption + keep the Cancel button).
            try:
                await client.edit_message_caption(
                    prompt_chat_id, prompt_msg_id,
                    caption=V.channel_setup_progress(steps),
                    parse_mode=ParseMode.HTML, reply_markup=cancel_kb,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to a fresh card
                log.debug("senku.wiz.progress_edit_failed", code=code, error=str(exc))
                prompt_msg_id = None
        if not prompt_msg_id:
            prog = await send_screen(
                client, chat_id,
                card(V.channel_setup_progress(steps), image=pick_artwork(BOT),
                     bot_name=BOT,
                     buttons=[[(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            )
            prompt_chat_id, prompt_msg_id = prog.chat.id, prog.id

        ctx = await _channel_ctx(code)
        ess = ctx[2] if ctx else None
        final_title = ess.title if ess else (chat.title or display)

        async def _edit_progress() -> None:
            try:
                await client.edit_message_caption(
                    prompt_chat_id, prompt_msg_id,
                    caption=V.channel_setup_progress(steps),
                    parse_mode=ParseMode.HTML, reply_markup=cancel_kb,
                )
            except Exception:  # noqa: BLE001 — progress is cosmetic
                pass

        # 1) Title
        try:
            await client.set_chat_title(chat.id, final_title[:128])
            steps[0] = ("Set channel title", "done")
        except Exception as exc:  # noqa: BLE001
            log.warning("senku.wiz.set_title_failed", code=code, error=str(exc))
            steps[0] = ("Set channel title (skipped — check my rights)", "done")
        steps[1] = ("Set channel description", "active")
        await _edit_progress()

        # 2) Description
        if ess and ess.description:
            try:
                await client.set_chat_description(chat.id, ess.description[:255])
                steps[1] = ("Set channel description", "done")
            except Exception as exc:  # noqa: BLE001
                log.warning("senku.wiz.set_desc_failed", code=code, error=str(exc))
                steps[1] = ("Set channel description (skipped — check my rights)",
                            "done")
        else:
            steps[1] = ("Set channel description", "done")
        await _edit_progress()

        # 3) Sweep the service notices Telegram posted for the title/photo change.
        await _sweep_channel_service_notices()

        # Replace the progress card with the "setup done → continue" card. We
        # delete the progress message by id (it may have been an edited prompt
        # card, so there's no Message object to hand to old_msg).
        try:
            await client.delete_messages(prompt_chat_id, prompt_msg_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        await send_screen(
            client, chat_id,
            card(V.channel_setup_done(display, final_title),
                 image=pick_artwork(BOT), bot_name=BOT,
                 buttons=[[(V.BTN_CONTINUE, cb(BOT, "wiz", "thumbs", code))]]),
        )

    async def _enter_thumbnails(chat_id: int, code: str, *, old_msg: Message | None) -> None:
        """Enter the per-entry thumbnail loop (Phase 3): intro → first pending entry."""
        entries = await cache.get_entries(code)
        franchise = await cache.get_franchise(code)
        title = await _title_of(code, franchise)
        await send_screen(
            client, chat_id,
            card(V.thumb_intro(title, len(entries)), image=await _art(franchise, title),
                 bot_name=BOT,
                 buttons=[[(V.BTN_CONTINUE, cb(BOT, "wiz", "tnext", code))],
                          [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            old_msg=old_msg,
        )

    async def _thumb_next(chat_id: int, code: str, *, old_msg: Message | None) -> None:
        """Advance the loop: render the next asset card, or finish → watch order."""
        entry = await thumbs.next_pending(code)
        franchise = await cache.get_franchise(code)
        title = await _title_of(code, franchise)
        if entry is None:
            # Every entry rendered — go STRAIGHT to Phase 4 (watch-order confirm).
            # No separate "all rendered → tap Order is correct" hop: that card's
            # only job was to navigate here, so fold it into this one message.
            await _enter_watch_order(chat_id, code, old_msg=old_msg, rendered=True)
            return
        sel = await cache.get_selection(code, entry.index)
        asset = thumbs.next_asset(sel)
        if asset is None:
            # All assets picked but not yet rendered — offer Generate.
            await _thumb_generate_card(chat_id, code, entry, old_msg=old_msg)
            return
        await _thumb_asset_card(chat_id, code, entry, asset, old_msg=old_msg)

    async def _thumb_asset_card(chat_id: int, code: str, entry, asset: str,
                                *, old_msg: Message | None) -> None:
        """One asset-pick card: header + gallery link + numbered buttons."""
        entries = await cache.get_entries(code)
        franchise = await cache.get_franchise(code)
        title = await _title_of(code, franchise)
        assets, gallery, rows = await thumbs.asset_step(code, entry, asset)
        # Manual override: the admin can upload their own image instead of picking
        # a numbered TMDB asset (senku|wiz|upl|<code>|<index>|<asset>).
        upload_row = [(V.BTN_UPLOAD_OWN,
                       cb(BOT, "wiz", "upl", code, str(entry.index), asset))]
        if not assets:
            # TMDB had nothing for this type — still let the admin upload their own
            # rather than dead-ending. The loop runs in the admin's private DM, so
            # chat_id IS their telegram user id (what the FSM keys on).
            await _ask_upload(chat_id, chat_id, code, entry.index, asset,
                              old_msg=old_msg)
            return
        body = "\n\n".join([
            V.thumb_entry_header(entry.label, entry.index, len(entries)),
            V.thumb_pick_prompt(asset),
        ])
        url_buttons = [[(V.BTN_SHOW_LOGOS if asset == "logo" else
                         V.BTN_SHOW_POSTERS if asset == "poster" else
                         V.BTN_SHOW_BACKDROPS, gallery)]] if gallery else None
        await send_screen(
            client, chat_id,
            card(body, image=await _art(franchise, title), bot_name=BOT,
                 url_buttons=url_buttons,
                 buttons=rows + [upload_row,
                                 [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            old_msg=old_msg,
        )

    async def _ask_upload(chat_id: int, user_id: int, code: str, index: int,
                          asset: str, *, old_msg: Message | None) -> None:
        """Arm the manual-upload step: prompt the admin to send their own image."""
        await fsm.set(user_id, STATE_AWAIT_UPLOAD, code=code, index=index, asset=asset)
        franchise = await cache.get_franchise(code)
        title = await _title_of(code, franchise)
        await send_screen(
            client, chat_id,
            card(V.thumb_upload_prompt(asset), image=await _art(franchise, title),
                 bot_name=BOT,
                 buttons=[[(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            old_msg=old_msg,
        )

    async def _thumb_generate_card(chat_id: int, code: str, entry,
                                   *, old_msg: Message | None) -> None:
        """All three assets picked — offer the Generate button for this entry."""
        entries = await cache.get_entries(code)
        franchise = await cache.get_franchise(code)
        title = await _title_of(code, franchise)
        await send_screen(
            client, chat_id,
            card(V.thumb_generate_header(entry.label, entry.index, len(entries)),
                 image=await _art(franchise, title), bot_name=BOT,
                 buttons=[[(V.BTN_GENERATE, cb(BOT, "wiz", "gen", code, str(entry.index)))],
                          [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            old_msg=old_msg,
        )

    async def _thumb_pick(q: CallbackQuery, code: str, index: int, asset: str,
                          number: int) -> None:
        """Store a numbered pick, then advance to the next asset or Generate."""
        sel, nxt = await thumbs.store_pick(code, index, asset, number)
        await q.answer(V.thumb_selected(asset, number))
        await _thumb_next(q.message.chat.id, code, old_msg=q.message)

    async def _thumb_generate(q: CallbackQuery, code: str, index: int) -> None:
        """Render one entry's thumbnail, then wait for explicit approval."""
        entry = await cache.get_entry(code, index)
        if entry is None:
            await q.answer("Entry not found.", show_alert=True)
            return
        await q.answer("Rendering…")
        path = await thumbs.render_entry(code, entry)
        if path is None:
            # Accurate failure copy: a missing headless browser is not a network
            # blip, so don't tell the operator to "tap again" — tell them to
            # install playwright.
            fail_msg = (V.THUMB_RENDER_FAIL
                        if getattr(thumbs, "last_render_error", None) == "browser"
                        else V.THUMB_GALLERY_FAIL)
            await send_screen(
                client, q.message.chat.id,
                card(fail_msg, image=pick_artwork(BOT), bot_name=BOT,
                     buttons=[[(V.BTN_GENERATE, cb(BOT, "wiz", "gen", code, str(index)))],
                              [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
                old_msg=q.message,
            )
            return
        # Upload the rendered card so the admin sees the result inline, but do not
        # advance until the operator explicitly approves this entry.
        #
        # Telegram's photo endpoint is unreliable with the rendered .webp (the
        # sticker format) — sends can hang or be rejected, which is exactly the
        # "Gallery didn't load" preview bug: the render succeeded and the image
        # hosts accepted it, only the DM send failed. Convert to a JPEG for the
        # send; the stored artifact stays WebP.
        preview = webp_to_jpeg(path) or path
        try:
            await client.send_photo(
                q.message.chat.id, str(preview),
                reply_markup=keyboard([
                    [(V.BTN_THUMB_APPROVE,
                      cb(BOT, "wiz", "thumbok", code, str(index))),
                     (V.BTN_THUMB_REDO,
                      cb(BOT, "wiz", "thumbredo", code, str(index)))],
                ]),
            )
        except Exception as exc:  # noqa: BLE001 — keep the Generate card usable
            log.debug("senku.wiz.thumb_preview_failed", code=code, error=str(exc))
            await send_screen(
                client, q.message.chat.id,
                card(V.THUMB_GALLERY_FAIL, image=pick_artwork(BOT), bot_name=BOT,
                     buttons=[[(V.BTN_GENERATE,
                                cb(BOT, "wiz", "gen", code, str(index)))],
                              [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
                old_msg=q.message,
            )
            return
        await q.message.delete()

    async def _thumb_callback_lock(code: str, index: int) -> tuple[str, str] | None:
        """Atomically claim one thumbnail callback until its state transition ends."""
        redis = container.redis
        if redis is None:
            return None
        key = f"nf:senku:thumb_callback:{code}:{index}"
        token = secrets.token_urlsafe(18)
        try:
            acquired = await redis.set(key, token, nx=True, ex=180)
        except Exception as exc:  # noqa: BLE001 — fail closed on Redis blips
            log.warning("senku.wiz.thumb_lock_failed", code=code, error=str(exc))
            return None
        return (key, token) if acquired else ("", "")

    async def _release_thumb_callback_lock(lock: tuple[str, str]) -> None:
        """Release only our lock; never delete a later owner's lock."""
        key, token = lock
        redis = container.redis
        if redis is None:
            return
        # Redis deployments in this project expose eval; use a compare/delete
        # script so an expired-and-reacquired lock cannot be removed by us.
        try:
            await redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1, key, token,
            )
        except Exception as exc:  # noqa: BLE001 — TTL still bounds the lock
            log.debug("senku.wiz.thumb_unlock_failed", key=key, error=str(exc))

    async def _thumb_approve(q: CallbackQuery, code: str, index: int) -> None:
        """Approve exactly one rendered entry and advance to the next pending one."""
        lock = await _thumb_callback_lock(code, index)
        if lock is None:
            await q.answer("Thumbnail actions are temporarily unavailable.", show_alert=True)
            return
        if lock == ("", ""):
            await q.answer("This thumbnail action is already processing.", show_alert=True)
            return
        try:
            await q.answer("Checking…")
            try:
                await q.message.edit_reply_markup(None)
            except Exception:  # noqa: BLE001 — callback locking is cosmetic
                pass
            sel = await cache.get_selection(code, index)
            if sel.done or not sel.thumbnail_url:
                return
            await cache.set_selection(code, index, done=True)
            await _thumb_next(q.message.chat.id, code, old_msg=q.message)
        finally:
            await _release_thumb_callback_lock(lock)

    async def _thumb_redo(q: CallbackQuery, code: str, index: int) -> None:
        """Reset only this entry's picks and reopen its logo step."""
        lock = await _thumb_callback_lock(code, index)
        if lock is None:
            await q.answer("Thumbnail actions are temporarily unavailable.", show_alert=True)
            return
        if lock == ("", ""):
            await q.answer("This thumbnail action is already processing.", show_alert=True)
            return
        try:
            await q.answer("Redoing this entry.")
            try:
                await q.message.edit_reply_markup(None)
            except Exception:  # noqa: BLE001
                pass
            await cache.clear_selection(code, index)
            entry = await cache.get_entry(code, index)
            if entry is None:
                await q.answer("Entry not found.", show_alert=True)
                return
            await _thumb_asset_card(q.message.chat.id, code, entry, "logo", old_msg=q.message)
        finally:
            await _release_thumb_callback_lock(lock)

    async def _enter_watch_order(chat_id: int, code: str, *, old_msg: Message | None,
                                 rendered: bool = False) -> None:
        """Enter the watch-order confirm step (Phase 4) — the last gate before publish.

        Renders the numbered order with Confirm/Edit buttons. Confirm publishes;
        Edit drops into a free-text step (``STATE_AWAIT_ORDER``) that re-maps the
        pasted order and returns here for a second look.

        ``rendered=True`` when we arrive straight from the thumbnail loop, so the
        card header folds in the old "All thumbnails rendered" beat (one message
        instead of a separate confirm-then-navigate hop).
        """
        entries = await cache.get_entries(code)
        franchise = await cache.get_franchise(code)
        title = await _title_of(code, franchise)
        order_html = franchise_map.render_watch_order(entries)
        await send_screen(
            client, chat_id,
            card(V.watch_order_card(title, order_html, rendered=rendered),
                 image=await _art(franchise, title), bot_name=BOT,
                 buttons=[
                     [(V.BTN_ORDER_CORRECT, cb(BOT, "wiz", "post", code))],
                     [(V.BTN_ORDER_EDIT, cb(BOT, "wiz", "oedit", code))],
                     [(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))],
                 ]),
            old_msg=old_msg,
        )

    async def _ask_order_edit(chat_id: int, user_id: int, code: str,
                              *, old_msg: Message | None) -> None:
        """Prompt for a corrected watch order and arm the free-text step."""
        await fsm.set(user_id, STATE_AWAIT_ORDER, code=code)
        entries = await cache.get_entries(code)
        franchise = await cache.get_franchise(code)
        title = await _title_of(code, franchise)
        copy_block = franchise_map.render_copy_block(entries)
        body = f"{V.WATCH_ORDER_EDIT_PROMPT}\n\n<pre>{copy_block}</pre>"
        await send_screen(
            client, chat_id,
            card(body, image=await _art(franchise, title), bot_name=BOT,
                 buttons=[[(V.BTN_CANCEL, cb(BOT, "wiz", "cancel", code))]]),
            old_msg=old_msg,
        )

    async def _publish(chat_id: int, user_id: int, code: str,
                       *, old_msg: Message | None) -> None:
        """Post the content pack into the channel, then hand off to Gojo."""
        await fsm.clear(user_id)
        franchise = await cache.get_franchise(code)
        title = await _title_of(code, franchise)

        # ── Filestore guard: never publish dead quality buttons ──
        # The quality links come from file-store bots; with none configured the
        # channel would ship links that lead nowhere. Block with a clear card and
        # a Continue button on the SAME message so a fix-then-retry is one tap.
        filestore = list(
            getattr(getattr(container.config, "bot", None), "filestore_bots", None)
            or []
        )
        if not filestore:
            # Owner detection by telegram id (owner is defined by id, not DB role).
            try:
                from nekofetch.services.auth_service import AuthService

                owner = user_id in AuthService(container).owner_ids()
            except Exception:  # noqa: BLE001
                owner = False
            await send_screen(
                client, chat_id,
                card(V.filestore_missing(owner), image=pick_artwork(BOT), bot_name=BOT,
                     buttons=[[(V.BTN_CONTINUE, cb(BOT, "wiz", "post", code))],
                              [(V.BTN_HOME, cb(BOT, "home"))]]),
                old_msg=old_msg,
            )
            return

        # "Working" card — publishing walks the whole pack + catbox uploads.
        # Capture it so the terminal card (done/fail) EDITS this same message in
        # place (delete-then-send) instead of stacking a second card below it —
        # the flow should read as one evolving message.
        work_msg = await send_screen(
            client, chat_id,
            card(V.publishing(title), image=await _art(franchise, title), bot_name=BOT),
            old_msg=old_msg,
        )
        try:
            from kurosoden.shared.senku_publisher import SenkuPublisher

            await SenkuPublisher(container).publish(client, code)
        except Exception as exc:  # noqa: BLE001 — surface a clean failure card
            log.warning("senku.wiz.publish_failed", code=code, error=str(exc))
            await send_screen(
                client, chat_id,
                card(V.PUBLISH_FAIL, image=pick_artwork(BOT), bot_name=BOT,
                     buttons=[[(V.BTN_PUBLISH, cb(BOT, "wiz", "post", code))],
                              [(V.BTN_HOME, cb(BOT, "home"))]]),
                old_msg=work_msg,
            )
            return
        # Hand the request to Gojo (publish stage) and clear the working cache.
        try:
            from kurosoden.shared.handoff import handoff_distribution_to_publish

            await handoff_distribution_to_publish(container, code, title)
        except Exception as exc:  # noqa: BLE001 — handoff is best-effort
            log.warning("senku.wiz.handoff_failed", code=code, error=str(exc))
        await cache.clear(code)
        await send_screen(
            client, chat_id,
            card(V.published_done(title), image=await _art(franchise, title), bot_name=BOT,
                 buttons=[[(V.BTN_TASKS, cb(BOT, "tasks"))],
                          [(V.BTN_HOME, cb(BOT, "home"))]]),
            old_msg=work_msg,
        )

    # ── /create — open the wizard for the admin's most recent task ─────────────
    @client.on_message(filters.command("create") & filters.private)
    async def _create_cmd(_: Client, message: Message) -> None:
        if not _staff(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else await _latest_task(container, message)
        if not code:
            await message.reply(V.TASKS_EMPTY)
            return
        await _open(message.chat.id, code, old_msg=None)

    # ── Handoff / task entry: senku|wiz|open|<code> ────────────────────────────
    @client.on_callback_query(filters.regex(r"^senku\|wiz\|"), group=0)
    async def _wiz_router(_: Client, q: CallbackQuery) -> None:
        if q.message is None:
            await q.answer()
            return
        if not _staff(q):
            await q.answer("Not for you.", show_alert=True)
            return
        parts = q.data.split("|")
        action = parts[2] if len(parts) > 2 else ""
        code = parts[3] if len(parts) > 3 else ""
        chat_id = q.message.chat.id

        if action == "open":
            # One click from the handoff: land directly on "who creates the
            # channel?" instead of an intermediate franchise-map + Begin card
            # (Levi already showed the map at download time). _scope_step seeds
            # the cache via _channel_ctx, so nothing is skipped.
            await q.answer()
            await _scope_step(chat_id, code, old_msg=q.message)
        elif action == "scope":
            await q.answer()
            await _scope_step(chat_id, code, old_msg=q.message)
        elif action == "ubot":
            await q.answer()
            await _userbot_create(chat_id, q.from_user.id, code, old_msg=q.message)
        elif action == "ubotjoined":
            # Operator joined the invite link; promote them, then ask for the photo.
            await q.answer("Checking…")
            await _userbot_joined(chat_id, q.from_user.id, code, old_msg=q.message)
        elif action == "ubotdone":
            # Userbot made the channel; admin added the photo. Straight to thumbs.
            await q.answer()
            await fsm.clear(q.from_user.id)
            await _enter_thumbnails(chat_id, code, old_msg=q.message)
        elif action == "chan":
            await q.answer()
            await _channel_step(chat_id, code, old_msg=q.message)
        elif action == "chan2":
            await q.answer()
            await _channel_step2(chat_id, code, old_msg=q.message)
        elif action == "chan3":
            await q.answer()
            await _channel_step3(chat_id, code, old_msg=q.message)
        elif action == "chandone":
            await q.answer()
            await _ask_channel(chat_id, q.from_user.id, code, old_msg=q.message)
        elif action == "thumbs":
            await q.answer()
            await _enter_thumbnails(chat_id, code, old_msg=q.message)
        elif action == "tnext":
            await q.answer()
            await _thumb_next(chat_id, code, old_msg=q.message)
        elif action == "pick":
            # senku|wiz|pick|<code>|<index>|<asset>|<number>
            try:
                index, asset, number = int(parts[4]), parts[5], int(parts[6])
            except (IndexError, ValueError):
                await q.answer("Bad selection.", show_alert=True)
                return
            await _thumb_pick(q, code, index, asset, number)
        elif action == "upl":
            # senku|wiz|upl|<code>|<index>|<asset> — arm the manual-upload step.
            try:
                index, asset = int(parts[4]), parts[5]
            except (IndexError, ValueError):
                await q.answer("Bad asset.", show_alert=True)
                return
            await _ask_upload(chat_id, q.from_user.id, code, index, asset,
                              old_msg=q.message)
            await q.answer()
        elif action == "gen":
            # senku|wiz|gen|<code>|<index>
            try:
                index = int(parts[4])
            except (IndexError, ValueError):
                await q.answer("Bad entry.", show_alert=True)
                return
            await _thumb_generate(q, code, index)
        elif action in ("thumbok", "thumbredo"):
            try:
                index = int(parts[4])
            except (IndexError, ValueError):
                await q.answer("Bad entry.", show_alert=True)
                return
            if action == "thumbok":
                await _thumb_approve(q, code, index)
            else:
                await _thumb_redo(q, code, index)
        elif action == "order":
            # Watch-order confirm card (Phase 4).
            await q.answer()
            await _enter_watch_order(chat_id, code, old_msg=q.message)
        elif action == "oedit":
            # "Edit order" — arm the free-text re-map step.
            await q.answer()
            await _ask_order_edit(chat_id, q.from_user.id, code, old_msg=q.message)
        elif action == "post":
            # "Order is correct" — publish the pack into the channel.
            await q.answer()
            await _publish(chat_id, q.from_user.id, code, old_msg=q.message)
        elif action == "cancel":
            # Abort-but-keep: cancel parks the in-progress pipeline (clears the
            # distribution cache + the FSM so nothing half-built lingers) but the
            # task ASSIGNMENT stays — the admin still sees it in /tasks and can
            # re-open it any time. The card tells them exactly that and gives a
            # single "Open Tasks" button back into the list.
            await fsm.clear(q.from_user.id)
            franchise = await cache.get_franchise(code)
            title = await _title_of(code, franchise)
            await cache.clear(code)
            await q.answer("Cancelled.")
            await send_screen(
                client, chat_id,
                card(V.task_aborted(title), image=await _art(franchise, title),
                     bot_name=BOT,
                     buttons=[[(V.BTN_OPEN_TASKS, cb(BOT, "tasks"))]]),
                old_msg=q.message,
            )
        else:
            await q.answer("Unknown step.", show_alert=True)

    # ── Free-text channel step (group=2, only while awaiting the channel) ──────
    @client.on_message(
        filters.text & filters.private & ~filters.command(_RESERVED),
        group=2,
    )
    async def _channel_text(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, data = await fsm.get(message.from_user.id)
        if state not in (STATE_AWAIT_CHANNEL, STATE_AWAIT_ORDER):
            return  # not our turn
        if not _staff(message):
            return
        code = data.get("code", "")
        raw = (message.text or "").strip()
        # Consume the admin's typed input: delete it so the wizard stays a single
        # evolving card instead of leaving the pasted link/order lingering above.
        try:
            await message.delete()
        except Exception:  # noqa: BLE001 — best-effort (needs delete rights)
            pass

        if state == STATE_AWAIT_ORDER:
            if not raw:
                await message.reply(V.watch_order_edit_failed())
                return
            entries = await cache.apply_order_correction(code, raw)
            if not entries:
                await message.reply(V.watch_order_edit_failed())
                return
            await fsm.clear(message.from_user.id)
            # Re-render the confirm card with the corrected order for a second look.
            await _enter_watch_order(message.chat.id, code, old_msg=None)
            return

        if not raw:
            await message.reply(V.channel_missing("the channel @username or ID"))
            return
        await _verify_and_store(message.chat.id, message.from_user.id, code, raw)

    # ── Manual asset upload (group=2, only while awaiting an uploaded image) ────
    @client.on_message(
        (filters.photo | filters.document) & filters.private,
        group=2,
    )
    async def _upload_media(_: Client, message: Message) -> None:
        if not message.from_user:
            return
        state, data = await fsm.get(message.from_user.id)
        if state != STATE_AWAIT_UPLOAD:
            return  # not our turn
        if not _staff(message):
            return
        code = data.get("code", "")
        index = int(data.get("index", 0))
        asset = data.get("asset", "")

        # A document must actually be an image — reject PDFs, archives, etc.
        if message.document and not (message.document.mime_type or "").startswith("image/"):
            await message.reply(V.THUMB_UPLOAD_BAD)
            return

        try:
            buf = await client.download_media(message, in_memory=True)
            file_bytes = buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            log.warning("senku.wiz.upload_download_failed", code=code, error=str(exc))
            await message.reply(V.THUMB_UPLOAD_FAILED)
            return

        try:
            await thumbs.store_upload(code, index, asset, file_bytes)
        except Exception as exc:  # noqa: BLE001 — catbox host hiccup
            log.warning("senku.wiz.upload_store_failed", code=code, error=str(exc))
            await message.reply(V.THUMB_UPLOAD_FAILED)
            return

        await fsm.clear(message.from_user.id)
        await message.reply(V.thumb_uploaded(asset))
        # Advance to the next asset (or the Generate card) just like a numbered pick.
        await _thumb_next(message.chat.id, code, old_msg=None)


async def _latest_task(container: Container, message: Message) -> str | None:
    """The admin's newest active distribution task code, if any."""
    try:
        from kurosoden.shared.admin_assignment import AdminAssignmentEngine

        engine = AdminAssignmentEngine(container.pg_sessionmaker)
        active = await engine.get_active_tasks(message.from_user.id, stage="senku")
        return active[0].request_code if active else None
    except Exception as exc:  # noqa: BLE001
        log.warning("senku.wiz.latest_task_failed", error=str(exc))
        return None
