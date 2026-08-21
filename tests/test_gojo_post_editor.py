"""Tests for the Gojo post editor (main-channel + index-channel).

Covers the contracts that actually carry risk — the UI screen builders need a
live Telegram client, so they're exercised on the VPS, not here:

  * **Backup-sync** (the owner's core ask): a main caption/button edit updates
    the wipe-proof ``published_post_backups`` row, and an index edit re-snapshots
    the index backup — but only when the live edit SUCCEEDS (a rejected edit
    must leave the durable copy untouched, or a restore ships an edit subscribers
    never saw).
  * **Keyboard preservation**: every live edit re-passes ``reply_markup``.
  * **Callback-length safety**: with a worst-case 48-char ``anime_doc_id`` every
    callback the editor emits stays under Telegram's 64-byte cap (the reason the
    namespace is the short ``gojo|pe`` and the thumbnail field is an index).
  * **Button parsing** and the data helpers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.core.config import IndexChannelConfig
from nekofetch.infrastructure.database.postgres.models import (
    ChannelPost,
    IndexSection,
    PublishedPostBackup,
    StoragePack,
)
from nekofetch.ui.components import cb

import kurosoden.bots.gojo.handlers.post_editor as pe

aio = pytest.mark.asyncio

_DOC48 = "anilist:" + "9" * 40  # 48 chars — the String(48) worst case


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeMsg:
    def __init__(self, mid, *, caption=None, photo=True, reply_markup=None):
        self.id = mid
        self.caption = caption
        self.text = None
        self.photo = photo
        self.reply_markup = reply_markup


class _FakeClient:
    """Records live edits and re-serves the passed markup for assertions."""

    def __init__(self, *, live: _FakeMsg | None = None):
        self._live = live or _FakeMsg(55, caption="old", photo=True)
        self.caption_edits: list[dict] = []
        self.markup_edits: list[dict] = []
        self.media_edits: list[dict] = []

    async def get_messages(self, chat_id, mid):
        return self._live

    async def edit_message_caption(self, chat_id, mid, caption=None,
                                   parse_mode=None, reply_markup=None):
        self.caption_edits.append({"caption": caption, "reply_markup": reply_markup})
        return self._live

    async def edit_message_text(self, chat_id, mid, text=None,
                                parse_mode=None, reply_markup=None):
        self.caption_edits.append({"caption": text, "reply_markup": reply_markup})
        return self._live

    async def edit_message_reply_markup(self, chat_id, mid, reply_markup=None):
        self.markup_edits.append({"reply_markup": reply_markup})
        return self._live

    async def edit_message_media(self, chat_id, mid, media=None, reply_markup=None):
        self.media_edits.append({"reply_markup": reply_markup})
        return self._live


def _container(sessionmaker, client=None):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        admin_client=client,
        redis=None,
        config=SimpleNamespace(
            main_channel=SimpleNamespace(channel_id=-1009999, divider_sticker_id=None),
            index_channel=IndexChannelConfig(enabled=True, channel_id=-100500),
        ),
    )


async def _seed_backup(sessionmaker, doc=_DOC48, caption="original caption"):
    async with sessionmaker() as s:
        s.add(PublishedPostBackup(anime_doc_id=doc, caption=caption))
        await s.commit()


async def _seed_post(sessionmaker, doc=_DOC48, mid=55, chat=-1001234):
    async with sessionmaker() as s:
        s.add(ChannelPost(anime_doc_id=doc, main_channel_id=chat, main_message_id=mid))
        await s.commit()


# ── Pure helpers ────────────────────────────────────────────────────────────

def test_parse_button_lines_valid_and_skips_bad():
    raw = ("Watch | https://t.me/x\n"
           "  Join us  |  https://t.me/y  \n"
           "no pipe here\n"
           "Bad | not-a-url\n"
           "Deep | tg://resolve?domain=z\n")
    assert pe._parse_button_lines(raw) == [
        ("Watch", "https://t.me/x"),
        ("Join us", "https://t.me/y"),
        ("Deep", "tg://resolve?domain=z"),
    ]


def test_parse_button_lines_empty_clears():
    assert pe._parse_button_lines("") == []
    assert pe._parse_button_lines("   \n  \n") == []


def test_markup_from_lines_builds_url_buttons():
    markup = pe._markup_from_lines([("A", "https://a"), ("B", "https://b")])
    assert markup is not None
    assert [b.text for row in markup.inline_keyboard for b in row] == ["A", "B"]
    assert [b.url for row in markup.inline_keyboard for b in row] == ["https://a", "https://b"]


def test_markup_from_lines_none_when_empty():
    assert pe._markup_from_lines([]) is None


def test_slot_label_by_kind():
    assert pe._slot_label({"kind": "letter", "label": "A", "base_letter": "A", "order": 1}) == "A"
    assert pe._slot_label({"kind": "letter", "label": None, "base_letter": "B", "order": 2}) == "B"
    assert pe._slot_label({"kind": "reserved", "label": None, "order": 30}) == "·30"
    assert pe._slot_label({"kind": "repurposed", "label": None, "order": 31}) == "⟳31"


# ── Callback-length safety (the reason for the short namespace + field index) ──

def test_every_callback_under_64_bytes_worst_case():
    """A 48-char doc id must never push any editor callback past 64 bytes."""
    doc = _DOC48
    cbs = [
        cb(pe._BOT, pe._NS, "home"),
        cb(pe._BOT, pe._NS, "x"),
        cb(pe._BOT, pe._NS, "ml", "0"),
        cb(pe._BOT, pe._NS, "il", "0"),
        cb(pe._BOT, pe._NS, "mp", doc),
        cb(pe._BOT, pe._NS, "mc", doc),
        cb(pe._BOT, pe._NS, "mb", doc),
        cb(pe._BOT, pe._NS, "mi", doc),
        cb(pe._BOT, pe._NS, "mt", doc),
        cb(pe._BOT, pe._NS, "ip", "9999"),
        cb(pe._BOT, pe._NS, "it", "9999"),
        cb(pe._BOT, pe._NS, "ii", "9999"),
        cb(pe._BOT, pe._NS, "ib", "9999"),
    ]
    # Thumbnail field callbacks carry the field INDEX (0..len-1), not its name.
    for i in range(len(pe._EDITABLE)):
        cbs.append(cb(pe._BOT, pe._NS, "mf", doc, str(i)))
    # Pagination nav appends a page index onto the nav action.
    cbs.append(cb(cb(pe._BOT, pe._NS, "ml"), 999))
    cbs.append(cb(cb(pe._BOT, pe._NS, "il"), 999))
    over = [c for c in cbs if len(c.encode("utf-8")) > 64]
    assert not over, f"callbacks over 64 bytes: {[(c, len(c)) for c in over]}"


def test_field_index_maps_into_editable():
    # The grid encodes each field as its index; the dispatcher decodes it back.
    for i, fkey in enumerate(pe._EDITABLE):
        assert pe._EDITABLE[i] == fkey
        assert fkey in pe._FIELD_LABELS


# ── Data helpers ──────────────────────────────────────────────────────────────

@aio
async def test_main_posts_lists_only_live_and_sorts_by_title(sessionmaker, session):
    async with sessionmaker() as s:
        s.add(ChannelPost(anime_doc_id="d:z", main_channel_id=-100, main_message_id=5))
        s.add(ChannelPost(anime_doc_id="d:a", main_channel_id=-100, main_message_id=6))
        # No main_message_id → not editable, must be excluded.
        s.add(ChannelPost(anime_doc_id="d:none", main_channel_id=-100, main_message_id=None))
        s.add(StoragePack(anime_doc_id="d:z", anime_title="Zebra", season=1,
                          season_part=1, resolution="1080p", audio="jpn",
                          channel_id=1, start_message_id=1, end_message_id=2))
        s.add(StoragePack(anime_doc_id="d:a", anime_title="Apple", season=1,
                          season_part=1, resolution="1080p", audio="jpn",
                          channel_id=1, start_message_id=1, end_message_id=2))
        await s.commit()
    posts = await pe._main_posts(_container(sessionmaker))
    assert [d for d, _ in posts] == ["d:a", "d:z"]         # title-sorted
    assert [t for _, t in posts] == ["Apple", "Zebra"]
    assert "d:none" not in [d for d, _ in posts]


@aio
async def test_main_target_returns_chat_and_mid(sessionmaker, session):
    await _seed_post(sessionmaker, doc="d:t", mid=77, chat=-100777)
    assert await pe._main_target(_container(sessionmaker), "d:t") == (-100777, 77)
    assert await pe._main_target(_container(sessionmaker), "d:missing") is None


# ── Main-channel edits: backup-sync + keyboard preservation ───────────────────

@aio
async def test_apply_main_caption_persists_backup_and_keeps_keyboard(sessionmaker, session):
    await _seed_post(sessionmaker)
    await _seed_backup(sessionmaker, caption="original caption")
    kept = SimpleNamespace(inline_keyboard=[["btn"]])
    client = _FakeClient(live=_FakeMsg(55, caption="old", photo=True, reply_markup=kept))
    c = _container(sessionmaker, client)

    ok, msg = await pe._apply_main_caption(c, _DOC48, "<b>brand new</b>")
    assert ok, msg
    # Live edit re-passed the keyboard (never drop the Index/Download buttons).
    assert client.caption_edits and client.caption_edits[-1]["reply_markup"] is kept
    # Backup row carries the edit → a restore ships the NEW caption.
    async with sessionmaker() as s:
        row = (await s.execute(select(PublishedPostBackup).where(
            PublishedPostBackup.anime_doc_id == _DOC48))).scalar_one()
        assert row.caption == "<b>brand new</b>"


@aio
async def test_apply_main_buttons_persists_backup(sessionmaker, session):
    await _seed_post(sessionmaker)
    await _seed_backup(sessionmaker)
    client = _FakeClient()
    c = _container(sessionmaker, client)

    ok, msg = await pe._apply_main_buttons(
        c, _DOC48, "Index | https://t.me/idx\nWatch | https://t.me/dl")
    assert ok, msg
    assert client.markup_edits, "live button edit must fire"
    async with sessionmaker() as s:
        row = (await s.execute(select(PublishedPostBackup).where(
            PublishedPostBackup.anime_doc_id == _DOC48))).scalar_one()
        assert row.button_data, "backup must carry the new buttons"
        flat = str(row.button_data)
        assert "https://t.me/idx" in flat and "Watch" in flat


@aio
async def test_apply_main_buttons_clear_persists_empty(sessionmaker, session):
    await _seed_post(sessionmaker)
    await _seed_backup(sessionmaker)
    client = _FakeClient()
    c = _container(sessionmaker, client)

    ok, msg = await pe._apply_main_buttons(c, _DOC48, "")
    assert ok
    assert client.markup_edits[-1]["reply_markup"] is None  # cleared live
    async with sessionmaker() as s:
        row = (await s.execute(select(PublishedPostBackup).where(
            PublishedPostBackup.anime_doc_id == _DOC48))).scalar_one()
        assert not row.button_data


@aio
async def test_apply_main_caption_missing_post_leaves_backup_untouched(sessionmaker, session):
    await _seed_backup(sessionmaker, caption="original caption")
    c = _container(sessionmaker, _FakeClient())  # no ChannelPost seeded
    ok, msg = await pe._apply_main_caption(c, _DOC48, "should not persist")
    assert not ok
    async with sessionmaker() as s:
        row = (await s.execute(select(PublishedPostBackup).where(
            PublishedPostBackup.anime_doc_id == _DOC48))).scalar_one()
        assert row.caption == "original caption"  # unchanged


# ── Index edits: backup re-sync is the whole point ────────────────────────────

class _SpyBackup:
    """Stand-in that records whether ``record_index`` was invoked."""
    calls: list[str] = []

    def __init__(self, container):
        pass

    async def record_index(self):
        _SpyBackup.calls.append("record_index")
        return None


async def _seed_slot(sessionmaker, order=1, label="A", mid=10):
    async with sessionmaker() as s:
        s.add(IndexSection(sort_order=order, label=label, base_letter=label, message_id=mid))
        await s.commit()


@aio
async def test_apply_index_text_edits_and_resyncs_backup(sessionmaker, session, monkeypatch):
    await _seed_slot(sessionmaker, order=1, label="A", mid=10)
    _SpyBackup.calls = []
    monkeypatch.setattr(pe, "BackupService", _SpyBackup)
    client = _FakeClient(live=_FakeMsg(10, caption="A section", photo=True))
    c = _container(sessionmaker, client)

    ok, msg = await pe._apply_index_text(c, 1, "<b>New A</b>")
    assert ok, msg
    assert client.caption_edits, "live caption edit must fire"
    assert _SpyBackup.calls == ["record_index"], "index backup must be re-synced"


@aio
async def test_apply_index_text_missing_slot_skips_backup(sessionmaker, session, monkeypatch):
    # No slot seeded → edit_slot_caption returns False → backup NOT touched.
    _SpyBackup.calls = []
    monkeypatch.setattr(pe, "BackupService", _SpyBackup)
    c = _container(sessionmaker, _FakeClient())
    ok, msg = await pe._apply_index_text(c, 999, "x")
    assert not ok
    assert _SpyBackup.calls == [], "a failed edit must not re-sync the backup"


@aio
async def test_apply_index_buttons_edits_and_resyncs(sessionmaker, session, monkeypatch):
    await _seed_slot(sessionmaker, order=2, label="B", mid=12)
    _SpyBackup.calls = []
    monkeypatch.setattr(pe, "BackupService", _SpyBackup)
    client = _FakeClient(live=_FakeMsg(12, caption="B", photo=True))
    c = _container(sessionmaker, client)

    ok, msg = await pe._apply_index_buttons(c, 2, "Go | https://t.me/b")
    assert ok, msg
    assert client.markup_edits, "live button edit must fire"
    assert _SpyBackup.calls == ["record_index"]


# ── backup_one must snapshot the LIVE post, not re-derive from facts ──────────
# The owner's requirement: an edit must survive a full ``/backup`` so a later
# restore ships the edited copy — not the stale facts-derived one.

class _FakeFacts:
    title = "Facts Title"
    backdrop_url = "https://cdn/facts.jpg"
    poster_url = None


class _FakeMCS:
    """Stand-in MainChannelService whose facts-derived values are the STALE
    copy — a correct backup must prefer the live post over these."""
    def __init__(self, container):
        pass

    async def gather_facts(self, doc):
        return _FakeFacts()

    def _caption(self, facts):
        return "STALE facts caption"

    async def _buttons(self, facts):
        from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([[InlineKeyboardButton("Stale", url="https://stale")]])


@aio
async def test_backup_one_prefers_live_edited_post(sessionmaker, session, monkeypatch):
    from nekofetch.services.backup_service import BackupService
    import nekofetch.services.main_channel_service as mcs_mod
    import kurosoden.shared.image_backup as imgbak

    await _seed_post(sessionmaker, doc="d:live", mid=55, chat=-100555)
    monkeypatch.setattr(mcs_mod, "MainChannelService", _FakeMCS)

    async def _fake_backup_bytes(container, data, mime="image/jpeg"):
        return SimpleNamespace(catbox_url="https://cat/live.jpg",
                               telegraph_url=None, imgbb_url=None)
    monkeypatch.setattr(imgbak, "backup_bytes", _fake_backup_bytes)

    # Live post carries the EDITED caption/buttons/photo (what subscribers see).
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    edited_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Edited", url="https://edited")]])
    live = _FakeMsg(55, caption=SimpleNamespace(html="<b>EDITED live caption</b>"),
                    photo=True, reply_markup=edited_markup)

    class _DLClient(_FakeClient):
        async def download_media(self, msg, in_memory=True):
            import io
            return io.BytesIO(b"live-photo-bytes")

    c = _container(sessionmaker, _DLClient(live=live))
    row = await BackupService(c).backup_one("d:live")

    assert row is not None
    # The backup captured the LIVE edit, not the stale facts caption/buttons.
    assert row.caption == "<b>EDITED live caption</b>"
    assert row.button_data == [[{"text": "Edited", "url": "https://edited"}]]
    # Live photo bytes were mirrored (not the facts backdrop URL).
    assert row.image_catbox_url == "https://cat/live.jpg"


@aio
async def test_backup_one_falls_back_to_facts_without_client(sessionmaker, session, monkeypatch):
    from nekofetch.services.backup_service import BackupService
    import nekofetch.services.main_channel_service as mcs_mod
    import kurosoden.shared.image_backup as imgbak

    await _seed_post(sessionmaker, doc="d:nofc", mid=55, chat=-100555)
    monkeypatch.setattr(mcs_mod, "MainChannelService", _FakeMCS)

    async def _fake_backup_image(container, url):
        return SimpleNamespace(catbox_url="https://cat/facts.jpg",
                               telegraph_url=None, imgbb_url=None)
    monkeypatch.setattr(imgbak, "backup_image", _fake_backup_image)

    # No admin_client → cannot read live → must use the facts-derived values.
    c = _container(sessionmaker, client=None)
    row = await BackupService(c).backup_one("d:nofc")

    assert row is not None
    assert row.caption == "STALE facts caption"       # facts fallback
    assert row.image_source_url == "https://cdn/facts.jpg"
    assert row.image_catbox_url == "https://cat/facts.jpg"
