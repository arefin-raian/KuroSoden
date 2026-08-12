"""Uploaded-font UX for Senku's text logo — Phase 10 (PLAN §10.3/§10.9).

Drives the REAL handlers (the group=2 ``_upload_media`` message handler and the
``senku|wiz|`` router) against a fake Redis FSM:

    upload .ttf → color step (STATE_TEXT_COLORS, custom_font_path set,
                   admin message deleted) → pick color → preview renders
                   with the uploaded font → Use-this → ``store_text_logo``
                   persists the logo AND the temp font is unlinked.

Every network/DB boundary is stubbed the same way the adapter tests stub them
(``backup_bytes`` for the mirror hosts); the state machine is real.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.handlers.conversation_handler import ConversationHandler

from nekofetch.bots.fsm import FSM

import kurosoden.bots.senku.handlers.wizard as wiz

_CODE = "REQ-9876"
_USER_ID = 777001
_CHAT_ID = -100777001

FONT_FILE = Path("resources/fonts/text_logo/BebasNeue-Regular.ttf")


# ── fakes ─────────────────────────────────────────────────────────────────────

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


class FlowContainer:
    def __init__(self, redis):
        self.redis = redis
        self.env = _Env()
        self.config = _Config()
        self.localizer = _Localizer()
        self.pg_sessionmaker = None
        self.tmdb = None


async def _answer(*_a, **_k):
    return None


class _FlowUser:
    id = _USER_ID
    username = "staff_admin"
    first_name = "Staff"
    nf_user = None


class _FlowChat:
    id = _CHAT_ID
    type = ChatType.PRIVATE
    username = None


class _FlowMessage:
    def __init__(self, document=None):
        self.id = 5
        self.chat = _FlowChat()
        self.text = None
        self.caption = None
        self.command = None
        self.photo = None
        self.document = document
        self.from_user = _FlowUser()
        self.nf_user = SimpleNamespace(role="staff")
        self.deleted = False
        self.replied = None

    async def delete(self):
        self.deleted = True

    async def reply(self, *a, **k):
        self.replied = (a, k)


def _make_cq(client: Client, data: str, message=None):
    """A REAL CallbackQuery — pyrogram's regex filter refuses plain namespaces."""
    from pyrogram.types import CallbackQuery

    cq = CallbackQuery(
        client=client, id="1", from_user=_FlowUser(), chat_instance="x",
    )
    cq.data = data
    cq.message = message or _FlowMessage()
    cq.inline_message_id = None
    cq.nf_user = SimpleNamespace(role="staff")
    cq.answer = _answer
    return cq


async def _dispatch_callback(client: Client, data: str, message=None):
    from pyrogram import StopPropagation

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
            except Exception:
                continue
    return cq


async def _dispatch_message(client: Client, message):
    from pyrogram import StopPropagation

    for _grp in sorted(client.dispatcher.groups):
        for h in client.dispatcher.groups[_grp]:
            if not isinstance(h, MessageHandler) or isinstance(h, ConversationHandler):
                continue
            try:
                if await h.check(client, message):
                    try:
                        await h.callback(client, message)
                    except StopPropagation:
                        return message
            except Exception:
                continue
    return message


@pytest.fixture
async def flow_client(monkeypatch):
    """Real Senku client wired to a fake Redis; auth resolves everyone as staff."""
    from kurosoden.bots.senku.app import build_senku
    from nekofetch.services.auth_service import AuthService

    redis = FakeRedis()
    container = FlowContainer(redis)

    async def fake_resolve_user(self, _from_user_id, **_kw):
        return SimpleNamespace(role="staff")

    monkeypatch.setattr(AuthService, "resolve_user", fake_resolve_user)

    client = build_senku(container, token="1:AAAA")
    # The offline dispatcher evaluates ``~filters.command`` for text messages;
    # provide the same minimal ``me`` object a started Pyrogram client exposes.
    client.me = SimpleNamespace(username=None, usernames=[])
    # Pyrogram's add_handler schedules registration tasks on the loop — tick it
    # until every handler lands in the dispatcher (same pattern as the routing
    # test), otherwise the group lists are empty when we dispatch.
    for _ in range(20):
        await asyncio.sleep(0)
    client._test_redis = redis
    return client


@pytest.fixture(autouse=True)
def _clean_screens(monkeypatch, tmp_path):
    """Render into tmp_path and record screens instead of touching Telegram."""
    sent: list = []
    real_render = wiz.render_text_logo

    def _render_to_tmp(text, font_key=None, **kw):
        return real_render(text, font_key, output_dir=tmp_path, **kw)

    async def fake_send_screen(_c, chat_id, screen, old_msg=None):
        sent.append(screen)
        return SimpleNamespace(id=9, chat=SimpleNamespace(id=chat_id))

    monkeypatch.setattr(wiz, "render_text_logo", _render_to_tmp)
    monkeypatch.setattr(wiz, "send_screen", fake_send_screen)
    monkeypatch.setattr(wiz, "uploaded_font_dir", lambda: tmp_path / "uploaded")
    return sent


@pytest.fixture
def font_bytes() -> bytes:
    return FONT_FILE.read_bytes()


# ── the flow ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_text_submission_edits_existing_prompt_card(flow_client, monkeypatch):
    """Typed logo text replaces the prompt card instead of creating another one."""
    redis = flow_client._test_redis
    fsm = FSM(redis, bot="senku")
    await fsm.set(_USER_ID, wiz.STATE_AWAIT_TEXT, code=_CODE, index=1,
                  prompt_msg_id=42, prompt_chat_id=_CHAT_ID)

    edits: list[dict] = []

    async def fake_edit_message_caption(chat_id, message_id, **kwargs):
        edits.append({"chat_id": chat_id, "message_id": message_id, **kwargs})
        return SimpleNamespace(id=message_id, chat=SimpleNamespace(id=chat_id))

    monkeypatch.setattr(flow_client, "edit_message_caption", fake_edit_message_caption)
    message = _FlowMessage()
    message.text = "Vanitas"

    await _dispatch_message(flow_client, message)

    assert message.deleted
    assert edits and edits[0]["message_id"] == 42
    assert "Choose a lettering style" in edits[0]["caption"]
    # The successful edit path must not send a replacement screen.
    assert edits[0]["reply_markup"] is not None
    state, data = await fsm.get(_USER_ID)
    assert state == wiz.STATE_TEXT_CATEGORIES
    assert data.get("text") == "Vanitas"


@pytest.mark.asyncio
async def test_empty_text_replaces_prompt_and_updates_fsm_reference(flow_client):
    redis = flow_client._test_redis
    fsm = FSM(redis, bot="senku")
    await fsm.set(_USER_ID, wiz.STATE_AWAIT_TEXT, code=_CODE, index=1,
                  prompt_msg_id=42, prompt_chat_id=_CHAT_ID)

    message = _FlowMessage()
    # Telegram will not deliver a truly empty text message; whitespace still
    # reaches the text handler and becomes empty after the production trim.
    message.text = " "
    await _dispatch_message(flow_client, message)

    state, data = await fsm.get(_USER_ID)
    assert state == wiz.STATE_AWAIT_TEXT
    assert data.get("prompt_msg_id") == 9
    assert data.get("prompt_chat_id") == _CHAT_ID


@pytest.mark.asyncio
async def test_font_caption_does_not_repeat_button_labels():
    caption = wiz.V.thumb_text_fonts("Elegant serif", ["Playfair Display"])
    assert "Playfair Display" not in caption
    assert "Choose a font to preview your logo" in caption


@pytest.mark.asyncio
async def test_text_submission_replaces_prompt_when_caption_edit_fails(
    flow_client, monkeypatch,
):
    redis = flow_client._test_redis
    fsm = FSM(redis, bot="senku")
    await fsm.set(_USER_ID, wiz.STATE_AWAIT_TEXT, code=_CODE, index=1,
                  prompt_msg_id=42, prompt_chat_id=_CHAT_ID)

    async def fail_edit(*_args, **_kwargs):
        raise RuntimeError("synthetic edit failure")

    monkeypatch.setattr(flow_client, "edit_message_caption", fail_edit)
    message = _FlowMessage()
    message.text = "Vanitas"

    await _dispatch_message(flow_client, message)

    state, data = await fsm.get(_USER_ID)
    assert message.deleted
    assert state == wiz.STATE_TEXT_CATEGORIES
    assert data.get("prompt_msg_id") == 9
    assert data.get("prompt_chat_id") == _CHAT_ID


@pytest.mark.asyncio
async def test_font_upload_reaches_color_step(flow_client, monkeypatch,
                                              tmp_path, font_bytes):
    redis = flow_client._test_redis
    fsm = FSM(redis, bot="senku")
    await fsm.set(_USER_ID, wiz.STATE_AWAIT_FONT_UPLOAD, code=_CODE, index=1,
                  text="Vanitas", prompt_msg_id=42, prompt_chat_id=_CHAT_ID)

    staged_dir = tmp_path / "uploaded"
    staged_dir.mkdir()
    monkeypatch.setattr(wiz, "uploaded_font_dir", lambda: staged_dir)

    async def fake_download_media(_message, **kw):
        return io.BytesIO(font_bytes)

    monkeypatch.setattr(flow_client, "download_media", fake_download_media)

    message = _FlowMessage(document=SimpleNamespace(
        file_name="MyFont.ttf", mime_type="font/ttf",
    ))
    await _dispatch_message(flow_client, message)

    assert message.deleted, "the admin's uploaded message must be deleted"
    state, data = await fsm.get(_USER_ID)
    assert state == wiz.STATE_TEXT_COLORS, f"expected color step, got {state}"
    assert data.get("origin") == "upload"
    assert data.get("prompt_msg_id") == 9
    assert data.get("prompt_chat_id") == _CHAT_ID
    staged = Path(data.get("custom_font_path", ""))
    assert staged.is_file() and staged.parent == staged_dir
    assert staged.read_bytes() == font_bytes


@pytest.mark.asyncio
async def test_font_upload_rejects_non_font_and_stays_armed(flow_client,
                                                            monkeypatch,
                                                            tmp_path):
    redis = flow_client._test_redis
    fsm = FSM(redis, bot="senku")
    await fsm.set(_USER_ID, wiz.STATE_AWAIT_FONT_UPLOAD, code=_CODE, index=1,
                  text="Vanitas", prompt_msg_id=42, prompt_chat_id=_CHAT_ID)

    message = _FlowMessage(document=SimpleNamespace(
        file_name="notes.pdf", mime_type="application/pdf",
    ))
    await _dispatch_message(flow_client, message)

    # still armed, nothing staged (Path("") would resolve to "." which exists)
    state, data = await fsm.get(_USER_ID)
    assert state == wiz.STATE_AWAIT_FONT_UPLOAD
    assert not data.get("custom_font_path")
    assert data.get("prompt_msg_id") == 9
    assert data.get("prompt_chat_id") == _CHAT_ID


@pytest.mark.asyncio
async def test_use_this_persists_logo_and_unlinks_temp_font(flow_client,
                                                            monkeypatch,
                                                            tmp_path,
                                                            font_bytes):
    """Upload → color → preview → Use-this: store_text_logo + temp font unlinked."""
    redis = flow_client._test_redis
    fsm = FSM(redis, bot="senku")

    # 1) upload the font → color step
    staged_dir = tmp_path / "uploaded"
    staged_dir.mkdir()
    monkeypatch.setattr(wiz, "uploaded_font_dir", lambda: staged_dir)

    async def fake_download_media(_message, **kw):
        return io.BytesIO(font_bytes)

    monkeypatch.setattr(flow_client, "download_media", fake_download_media)
    await fsm.set(_USER_ID, wiz.STATE_AWAIT_FONT_UPLOAD, code=_CODE, index=1,
                  text="Vanitas", prompt_msg_id=42, prompt_chat_id=_CHAT_ID)
    message = _FlowMessage(document=SimpleNamespace(
        file_name="MyFont.ttf", mime_type="font/ttf",
    ))
    await _dispatch_message(flow_client, message)
    _state, data = await fsm.get(_USER_ID)
    staged = Path(data["custom_font_path"])
    assert staged.is_file()

    # 2) pick a color → preview renders with the uploaded font
    import kurosoden.shared.image_backup as _image_backup

    monkeypatch.setattr(_image_backup, "backup_bytes", _fake_backup)
    await _dispatch_callback(flow_client, f"senku|wiz|textcolor|{_CODE}|1|blue")
    state, data = await fsm.get(_USER_ID)
    assert state == wiz.STATE_TEXT_PREVIEW
    assert data.get("color") == "blue"
    assert data.get("prompt_msg_id") == 9
    assert data.get("prompt_chat_id") == _CHAT_ID
    preview = Path(data["path"])
    assert preview.is_file()

    # 3) Use-this → store_text_logo runs the real adapter path (mirror stubbed),
    #    the logo lands in the selection, and the temp font is reclaimed.
    await _dispatch_callback(flow_client, f"senku|wiz|textuse|{_CODE}|1")
    assert not staged.exists(), "temp uploaded font must be unlinked after use"
    state, _data = await fsm.get(_USER_ID)
    assert state is None, "FSM must be cleared after the logo is locked in"


async def _fake_backup(container, blob, *, mime="image/jpeg", source_url=""):
    from kurosoden.shared.image_backup import BackupImage

    return BackupImage(
        source_url=source_url,
        catbox_url="https://files.catbox.moe/textlogo-blue.png",
    )
