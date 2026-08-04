"""Routing coverage — the guard against dead taps (PLAN §2).

The historical bug: Lelouch screens emitted callbacks (``admin|home``,
``queue|view``, bare ``home``, ``mg|…``) that no registered handler matched, so
tapping the button did nothing.

This test builds the *real* Lelouch client (offline, fake container), lets its
handlers register, then asserts every callback string any Lelouch surface can
emit is matched by some registered ``CallbackQueryHandler`` filter — invoking
the actual Pyrogram filters against a synthetic ``CallbackQuery``, not scraping
source. If someone adds a button with a new namespace and forgets the handler,
this fails.
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


class _Config:
    security = _Sec()


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


async def _build_registered_client():
    """Build Lelouch and drain Pyrogram's deferred handler registration."""
    from kurosoden.bots.lelouch.app import build_lelouch
    client = build_lelouch(FakeContainer(), token="1:AAAA")
    # on_callback_query defers add_handler onto the loop; let those tasks run.
    for _ in range(20):
        await asyncio.sleep(0)
    return client


def _callback_handlers(client: Client):
    """All real CallbackQueryHandlers (excluding the ConversationHandler base
    Pyrogram installs, and middleware handlers that have no filter)."""
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
            # A filter that can't evaluate this update type simply doesn't match.
            continue
    return False


# Every callback a Lelouch surface can emit (from screens, voice buttons, the
# management control plane, and the welcome-screen bridges). Args use concrete
# sample values so the ``^ns\|verb\|`` prefixes are exercised.
EMITTED_CALLBACKS = [
    # menu backbone
    "lelouch|home", "lelouch|admin", "lelouch|settings", "lelouch|set|source",
    "lelouch|reqtoggle", "lelouch|queue", "lelouch|manage",
    "lelouch|avail", "lelouch|hours", "lelouch|pending",
    "lelouch|dbclear", "lelouch|dbclear|confirm", "lelouch|dbclear|cancel",
    # admin self-service profile
    "lelouch|profile", "lelouch|pr|home", "lelouch|pr|country",
    "lelouch|pr|hours", "lelouch|pr|slots|weekday", "lelouch|pr|slots|weekend",
    "lelouch|tz|home", "lelouch|tz|set|Asia/Dhaka", "lelouch|tz|type",
    # request flow
    "req|new", "req|mine|0",
    # reused NekoFetch review board (pending screen's "Open Review Board")
    "staff|requests|0",
    # batch flow
    "batch|new", "batch|fpage|0|1", "batch|fpick|0|2", "batch|commit", "batch|cancel",
    # management control plane
    "mg|roster", "mg|adm|500", "mg|addlist", "mg|addid|500", "mg|rm|500",
    "mg|bot|500|levi", "mg|wt|500|1", "mg|av|500", "mg|brk|500", "mg|endbrk|500",
    "mg|hrs|500", "mg|sethrs|500|9|17", "mg|clrhrs|500",
    "mg|mode|normal", "mg|mode|paused", "mg|reasgn|500",
    # welcome-screen bridges (the original dead taps)
    "home", "admin|home", "queue|view|0",
]


@pytest.mark.asyncio
class TestNoDeadTaps:
    async def test_every_emitted_callback_is_routed(self):
        client = await _build_registered_client()
        handlers = _callback_handlers(client)
        assert handlers, "no callback handlers registered — build/registration broke"

        unrouted = []
        for data in EMITTED_CALLBACKS:
            if not await _is_routed(client, handlers, data):
                unrouted.append(data)
        assert not unrouted, f"dead taps (no handler matches): {unrouted}"

    async def test_unknown_callback_still_caught_by_menu_or_ignored(self):
        """A stray ``lelouch|<garbage>`` is claimed by the menu dispatcher (which
        answers a themed toast) rather than falling through to nothing."""
        client = await _build_registered_client()
        handlers = _callback_handlers(client)
        assert await _is_routed(client, handlers, "lelouch|totally-unknown-verb")

    async def test_truly_foreign_callback_is_not_falsely_matched(self):
        """Sanity: a namespace no Lelouch surface owns must NOT match — proves the
        matcher isn't a rubber stamp."""
        client = await _build_registered_client()
        handlers = _callback_handlers(client)
        assert not await _is_routed(client, handlers, "zzz|nope|123")
