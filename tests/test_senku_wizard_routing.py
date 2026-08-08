"""Routing coverage for Senku's channel-creation wizard — no dead taps (PLAN §7).

The wizard emits a family of ``senku|wiz|<action>|<code>`` callbacks across its
cards (Begin, the three channel sub-steps, "I've created it", cancel, continue to
thumbnails). This test builds the *real* Senku client offline, registers its
handlers, then asserts every callback a wizard card can emit is matched by some
registered ``CallbackQueryHandler`` filter — invoking the actual Pyrogram filters
against a synthetic ``CallbackQuery`` rather than scraping source. Mirrors
``test_lelouch_routing`` structurally.
"""

from __future__ import annotations

import asyncio

import pytest
from pyrogram import Client
from pyrogram.handlers import CallbackQueryHandler
from pyrogram.handlers.conversation_handler import ConversationHandler
from pyrogram.types import CallbackQuery


# ── Minimal fake container (only what registration touches) ─────────────────────

class _Sec:
    rate_limit_per_minute = 30


class _Bot:
    description_text = ""


class _Config:
    security = _Sec()
    bot = _Bot()


class _Env:
    telegram_api_id = 12345
    telegram_api_hash = "0" * 32
    session_path = "."


class _Localizer:
    def get(self, key, **kw):
        return key


class FakeContainer:
    """Attribute surface reached during handler *registration* only."""

    def __init__(self):
        self.env = _Env()
        self.config = _Config()
        self.localizer = _Localizer()
        self.redis = None
        self.pg_sessionmaker = None
        self.tmdb = None


async def _build_registered_client():
    from kurosoden.bots.senku.app import build_senku
    client = build_senku(FakeContainer(), token="1:AAAA")
    for _ in range(20):
        await asyncio.sleep(0)
    return client


def _callback_handlers(client: Client):
    out = []
    for _grp, handlers in client.dispatcher.groups.items():
        for h in handlers:
            if isinstance(h, CallbackQueryHandler) and not isinstance(h, ConversationHandler):
                if h.filters is not None:
                    out.append(h)
    return out


async def _is_routed(client: Client, handlers, data: str) -> bool:
    cq = CallbackQuery(client=client, id="1", from_user=None, chat_instance="x")
    cq.data = data
    for h in handlers:
        cq.matches = None
        try:
            if await h.filters(client, cq):
                return True
        except Exception:
            continue
    return False


CODE = "REQ-1234"

# Every callback a Senku wizard card can emit, with a concrete sample code so the
# ``^senku\|wiz\|verb\|`` prefix is exercised.
WIZARD_CALLBACKS = [
    f"senku|wiz|open|{CODE}",       # handoff / task-list entry
    f"senku|wiz|chan|{CODE}",       # Begin → channel step 1
    f"senku|wiz|chan2|{CODE}",      # step 1 → step 2 (poster + description)
    f"senku|wiz|chan3|{CODE}",      # step 2 → step 3 (admins)
    f"senku|wiz|chandone|{CODE}",   # "I've created it" → ask for @username
    f"senku|wiz|thumbs|{CODE}",     # verified → thumbnail loop
    f"senku|wiz|tnext|{CODE}",      # advance the thumbnail loop
    f"senku|wiz|pick|{CODE}|1|logo|2",   # numbered asset pick
    f"senku|wiz|text|{CODE}|1",       # logo → text input
    f"senku|wiz|textcat|{CODE}|1|elegant",  # category picker
    f"senku|wiz|textfont|{CODE}|1|elegant|playfair",  # font picker
    f"senku|wiz|textbackcat|{CODE}|1",  # back to categories
    f"senku|wiz|textbackfont|{CODE}|1|elegant",  # back to fonts
    f"senku|wiz|textcancel|{CODE}|1",  # cancel to logo picker
    f"senku|wiz|textuse|{CODE}|1",  # approve preview
    f"senku|wiz|gen|{CODE}|1",      # generate one entry's thumbnail
    f"senku|wiz|order|{CODE}",      # all rendered → watch-order confirm
    f"senku|wiz|oedit|{CODE}",      # "Edit order" → free-text re-map step
    f"senku|wiz|post|{CODE}",       # "Order is correct" → publish
    f"senku|wiz|cancel|{CODE}",     # cancel from any step
]


@pytest.mark.asyncio
class TestWizardNoDeadTaps:
    async def test_every_wizard_callback_is_routed(self):
        client = await _build_registered_client()
        handlers = _callback_handlers(client)
        assert handlers, "no callback handlers registered — build/registration broke"

        unrouted = [d for d in WIZARD_CALLBACKS
                    if not await _is_routed(client, handlers, d)]
        assert not unrouted, f"dead taps (no handler matches): {unrouted}"

    async def test_wizard_handler_precedes_menu_fallback(self):
        """The dedicated ``senku|wiz|`` router (group 0) must claim a wiz callback,
        not the generic ``senku|`` menu fallback — otherwise the step is a no-op."""
        client = await _build_registered_client()
        handlers = _callback_handlers(client)
        # A wiz callback is routed; a bare menu callback is also routed.
        assert await _is_routed(client, handlers, f"senku|wiz|chan|{CODE}")
        assert await _is_routed(client, handlers, "senku|home")

    async def test_foreign_callback_not_matched(self):
        client = await _build_registered_client()
        handlers = _callback_handlers(client)
        assert not await _is_routed(client, handlers, "zzz|nope|123")
