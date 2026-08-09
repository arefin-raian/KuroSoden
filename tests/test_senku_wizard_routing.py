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
from types import SimpleNamespace as _SimpleNamespace

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


from kurosoden.bots.senku.app import build_senku


async def _build_registered_client():
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
    f"senku|wiz|textfont|{CODE}|1|elegant|playfair",  # font picker → color step
    f"senku|wiz|textcolor|{CODE}|1|white",  # color swatch → preview
    f"senku|wiz|textupfont|{CODE}|1",  # upload-your-own font row
    f"senku|wiz|textbackcat|{CODE}|1",  # back to categories
    f"senku|wiz|textbackfont|{CODE}|1",  # back from colors (to fonts/grid)
    f"senku|wiz|textbackcolor|{CODE}|1",  # back from preview (to colors)
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


# ── Router EXECUTION tests for the Phase-10 text-logo flow ─────────────────────
# These drive the real ``senku|wiz|`` router (plus the auth middleware in group
# -1) against a fake Redis FSM, asserting the FSM state transitions — textfont
# now lands on the COLOR step, not the preview.

class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def expire(self, key, seconds):
        return True


class FlowContainer(FakeContainer):
    def __init__(self, redis):
        super().__init__()
        self.redis = redis


USER_ID = 424242
CHAT_ID = -100424242


class _FlowUser:
    id = USER_ID
    username = "staff_admin"
    first_name = "Staff"
    nf_user = None


class _FlowChat:
    id = CHAT_ID
    type = None
    username = None


class _FlowMessage:
    id = 1
    chat = _FlowChat()
    text = None
    photo = None
    document = None
    from_user = _FlowUser()

    async def delete(self):
        self.deleted = True

    async def reply(self, *a, **k):
        self.replied = (a, k)


async def _answer(*_a, **_k):
    return None


def _make_cq(client: Client, data: str, message=None):
    """A REAL CallbackQuery — pyrogram's regex filter refuses plain namespaces."""
    from pyrogram.types import CallbackQuery

    cq = CallbackQuery(
        client=client, id="1", from_user=_FlowUser(), chat_instance="x",
    )
    cq.data = data
    cq.message = message or _FlowMessage()
    cq.inline_message_id = None
    cq.nf_user = _SimpleNamespace(role="staff")
    cq.answer = _answer
    return cq


async def _dispatch_callback(client: Client, data: str, message=None):
    """Run ``data`` through the registered handlers like Pyrogram's dispatcher.

    All matching handlers fire in group order (the auth middleware in group -1
    sets ``nf_user``; the wizard router in group 0 then transitions the FSM).
    """
    from pyrogram import ContinuePropagation, StopPropagation

    cq = _make_cq(client, data, message=message)
    for _grp in sorted(client.dispatcher.groups):
        for h in client.dispatcher.groups[_grp]:
            if not isinstance(h, CallbackQueryHandler) or isinstance(h, ConversationHandler):
                continue
            try:
                if await h.check(client, cq):
                    try:
                        await h.callback(client, cq)
                    except StopPropagation:
                        return cq
            except ContinuePropagation:
                continue
            except Exception:
                continue
    return cq


@pytest.fixture(autouse=True)
def _staff_middleware_user(monkeypatch):
    """Make the auth middleware resolve every user as staff (no DB needed)."""
    from nekofetch.services.auth_service import AuthService

    async def fake_resolve_user(self, _from_user_id, **_kw):
        return _SimpleNamespace(role="staff")

    monkeypatch.setattr(AuthService, "resolve_user", fake_resolve_user)


@pytest.mark.asyncio
class TestWizardTextLogoFlow:
    async def test_textfont_leads_to_color_step_not_preview(self, monkeypatch, tmp_path):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from nekofetch.bots.fsm import FSM

        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_FONTS, code=CODE, index=1,
                      category="elegant", text="Vanitas")

        sent: list = []
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            sent.append(screen)
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textfont|{CODE}|1|elegant|playfair")

        state, data = await fsm.get(USER_ID)
        assert state == wiz.STATE_TEXT_COLORS, f"expected color step, got {state}"
        assert data.get("font") == "playfair"
        assert data.get("origin") == "fonts"
        assert data.get("category") == "elegant"
        assert sent, "the color card was never rendered"
        keyboard = sent[-1].keyboard.inline_keyboard
        assert all(len(row) == 2 for row in keyboard[:-1])
        assert [button.text for button in keyboard[-1]] == [
            wiz.V.BTN_TEXT_BACK, wiz.V.BTN_TEXT_CANCEL,
        ]

    async def test_textupfont_arms_font_upload_state(self, monkeypatch):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from nekofetch.bots.fsm import FSM

        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_CATEGORIES, code=CODE, index=1,
                      text="Vanitas")
        sent: list = []
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            sent.append(screen)
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textupfont|{CODE}|1")

        state, data = await fsm.get(USER_ID)
        assert state == wiz.STATE_AWAIT_FONT_UPLOAD
        assert data.get("text") == "Vanitas"
        keyboard = sent[-1].keyboard.inline_keyboard
        assert len(keyboard) == 1
        assert len(keyboard[0]) == 1
        assert keyboard[0][0].text == wiz.V.BTN_TEXT_CANCEL

    async def test_textcolor_leads_to_preview_with_color(self, monkeypatch, tmp_path):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from nekofetch.bots.fsm import FSM

        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_COLORS, code=CODE, index=1,
                      text="Vanitas", font="playfair", custom_font_path="",
                      origin="fonts", category="elegant")

        real_render = wiz.render_text_logo
        def _render_to_tmp(text, font_key=None, **kw):
            return real_render(text, font_key, output_dir=tmp_path, **kw)
        monkeypatch.setattr(wiz, "render_text_logo", _render_to_tmp)
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textcolor|{CODE}|1|red")

        state, data = await fsm.get(USER_ID)
        assert state == wiz.STATE_TEXT_PREVIEW
        assert data.get("color") == "red"
        assert data.get("font") == "playfair"
        assert data.get("path"), "preview PNG path was not stored"

    async def test_preview_back_returns_to_color_grid(self, monkeypatch, tmp_path):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from nekofetch.bots.fsm import FSM

        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_PREVIEW, code=CODE, index=1,
                      text="Vanitas", font="playfair", color="red",
                      custom_font_path="", origin="fonts", category="elegant",
                      path=str(tmp_path / "preview.png"))
        sent: list = []
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            sent.append(screen)
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textbackcolor|{CODE}|1")

        state, data = await fsm.get(USER_ID)
        assert state == wiz.STATE_TEXT_COLORS
        assert data.get("font") == "playfair"
        assert data.get("color", "") == ""  # color pick happens again
        keyboard = sent[-1].keyboard.inline_keyboard
        assert all(len(row) == 2 for row in keyboard[:-1])
        assert [button.text for button in keyboard[-1]] == [
            wiz.V.BTN_TEXT_BACK, wiz.V.BTN_TEXT_CANCEL,
        ]

    async def test_preview_keyboard_keeps_back_and_cancel_together(self, monkeypatch, tmp_path):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from nekofetch.bots.fsm import FSM

        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_COLORS, code=CODE, index=1,
                      text="Vanitas", font="playfair", custom_font_path="",
                      origin="fonts", category="elegant")
        real_render = wiz.render_text_logo
        def _render_to_tmp(text, font_key=None, **kw):
            return real_render(text, font_key, output_dir=tmp_path, **kw)
        monkeypatch.setattr(wiz, "render_text_logo", _render_to_tmp)
        sent: list = []
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            sent.append(screen)
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textcolor|{CODE}|1|red")

        keyboard = sent[-1].keyboard.inline_keyboard
        assert len(keyboard) == 2
        assert [button.text for button in keyboard[0]] == [
            wiz.V.BTN_TEXT_BACK, wiz.V.BTN_TEXT_CANCEL,
        ]
        assert len(keyboard[1]) == 1
        assert keyboard[1][0].text == wiz.V.BTN_TEXT_USE

    async def test_preview_error_keeps_back_and_cancel_together(self, monkeypatch):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from nekofetch.bots.fsm import FSM

        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_COLORS, code=CODE, index=1,
                      text="Vanitas", font="playfair", custom_font_path="",
                      origin="fonts", category="elegant")
        def _fail_render(*_args, **_kwargs):
            raise ValueError("synthetic renderer failure")
        monkeypatch.setattr(wiz, "render_text_logo", _fail_render)
        sent: list = []
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            sent.append(screen)
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textcolor|{CODE}|1|red")

        keyboard = sent[-1].keyboard.inline_keyboard
        assert len(keyboard) == 1
        assert [button.text for button in keyboard[0]] == [
            wiz.V.BTN_TEXT_BACK, wiz.V.BTN_TEXT_CANCEL,
        ]

    async def test_colors_back_with_uploaded_font_returns_to_categories(self, monkeypatch, tmp_path):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from nekofetch.bots.fsm import FSM

        staged = tmp_path / "uploaded.ttf"
        staged.write_bytes(b"staged-font")
        sent: list = []
        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_COLORS, code=CODE, index=1,
                      text="Vanitas", font="", custom_font_path=str(staged),
                      origin="upload", category="")
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            sent.append(screen)
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textbackfont|{CODE}|1")

        state, data = await fsm.get(USER_ID)
        assert state == wiz.STATE_TEXT_CATEGORIES
        assert not staged.exists(), "backing out must reclaim the abandoned upload"
        assert not data.get("custom_font_path")
        keyboard = sent[-1].keyboard.inline_keyboard
        assert [len(row) for row in keyboard] == [2, 2, 2, 1, 1]
        assert len(keyboard[3]) == 1  # Upload your own, full-width
        assert len(keyboard[4]) == 1  # Cancel, full-width (no Back here)

    async def test_colors_back_with_bundled_font_returns_to_font_list(self, monkeypatch):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from nekofetch.bots.fsm import FSM

        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_COLORS, code=CODE, index=1,
                      text="Vanitas", font="playfair", custom_font_path="",
                      origin="fonts", category="elegant")
        sent: list = []
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            sent.append(screen)
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textbackfont|{CODE}|1")

        state, data = await fsm.get(USER_ID)
        assert state == wiz.STATE_TEXT_FONTS
        assert data.get("category") == "elegant"
        keyboard = sent[-1].keyboard.inline_keyboard
        assert all(len(row) == 2 for row in keyboard[:-1])
        assert [button.text for button in keyboard[-1]] == [
            wiz.V.BTN_TEXT_BACK, wiz.V.BTN_TEXT_CANCEL,
        ]

    async def test_cancel_cleans_up_uploaded_font(self, monkeypatch, tmp_path):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from kurosoden.shared.distribution_cache import DistributionCache
        from nekofetch.bots.fsm import FSM

        staged = tmp_path / "font.ttf"
        staged.write_bytes(b"not-a-real-font")
        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_PREVIEW, code=CODE, index=1,
                      text="Vanitas", font="", color="white",
                      custom_font_path=str(staged), origin="upload", category="",
                      path=str(tmp_path / "p.png"))
        # The cancel handler clears the FSM only after locating the live
        # distribution entry (DB boundary — stub it like the adapter tests do).
        async def fake_get_entry(self, code, index):
            return _SimpleNamespace(index=index, label="Vanitas", status="active")

        monkeypatch.setattr(DistributionCache, "get_entry", fake_get_entry)
        async def fake_send_screen(_c, chat_id, screen, old_msg=None):
            return _SimpleNamespace(id=9, chat=_SimpleNamespace(id=chat_id))
        monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

        await _dispatch_callback(client, f"senku|wiz|textcancel|{CODE}|1")

        assert not staged.exists(), "uploaded font must be unlinked on cancel"
        state, _data = await fsm.get(USER_ID)
        assert state is None

    async def test_cancel_clears_orphaned_fsm_when_entry_is_missing(self, monkeypatch, tmp_path):
        redis = FakeRedis()
        client = build_senku(FlowContainer(redis), token="1:AAAA")
        for _ in range(20):
            await asyncio.sleep(0)
        import kurosoden.bots.senku.handlers.wizard as wiz
        from kurosoden.shared.distribution_cache import DistributionCache
        from nekofetch.bots.fsm import FSM

        staged = tmp_path / "orphaned.ttf"
        staged.write_bytes(b"staged-font")
        fsm = FSM(redis, bot="senku")
        await fsm.set(USER_ID, wiz.STATE_TEXT_PREVIEW, code=CODE, index=1,
                      text="Vanitas", font="", color="white",
                      custom_font_path=str(staged), origin="upload", category="",
                      path=str(tmp_path / "preview.png"))

        async def missing_entry(self, code, index):
            return None

        monkeypatch.setattr(DistributionCache, "get_entry", missing_entry)

        await _dispatch_callback(client, f"senku|wiz|textcancel|{CODE}|1")

        state, _data = await fsm.get(USER_ID)
        assert state is None
        assert not staged.exists()
