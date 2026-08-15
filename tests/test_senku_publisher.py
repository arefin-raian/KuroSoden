"""Tests for kurosoden/shared/senku_publisher.py — the Phase 4 channel poster.

Covers the two pure transforms that make the publisher honour the admin's work
without touching Telegram, catbox, or AniList:

  • ``_reorder_franchise`` re-emits a fresh AniList walk in the *confirmed*
    cached order, splits TV vs extras, and drops entries the admin removed.
  • ``_build_buttons`` renders URL-only keyboards from ``button_data.links``
    (flat + separate-audio), and never emits a button without a link.
  • ``_send_posts`` resolves ``{BOT_QUAL:...}`` captions, drops dividers between
    sections, and pins the info card + watch guide — all against a fake client.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from nekofetch.core.config import PostFormatConfig
from nekofetch.services.bot_render import (
    build_audio_keyboard,
    resolution_label,
    resolve_premium_emoji,
)
from kurosoden.shared.distribution_cache import EntryData, Selection
from kurosoden.shared.senku_publisher import SenkuPublisher

FMT = PostFormatConfig()


@dataclass
class _FE:
    """Stand-in for AnilistClient.FranchiseEntry (only the fields we read)."""
    anilist_id: int
    format: str
    english_title: str = "X"


class _FakeContainer:
    redis = None


@pytest.fixture
def pub():
    return SenkuPublisher(_FakeContainer())


# ── _reorder_franchise ───────────────────────────────────────────────────────────

def _walk():
    return {
        "tv": [_FE(101, "TV"), _FE(102, "TV")],
        "extras": [_FE(201, "MOVIE"), _FE(202, "OVA")],
        "all": [_FE(101, "TV"), _FE(102, "TV"), _FE(201, "MOVIE"), _FE(202, "OVA")],
    }


def test_reorder_follows_confirmed_order(pub):
    # Admin confirmed: movie first, then S2, then S1 — a deliberate reshuffle.
    entries = [
        EntryData(index=1, label="Movie", kind="movie", anilist_id=201),
        EntryData(index=2, label="Season 2", kind="season", anilist_id=102),
        EntryData(index=3, label="Season 1", kind="season", anilist_id=101),
    ]
    out = pub._reorder_franchise(_walk(), entries)
    assert [e.anilist_id for e in out["all"]] == [201, 102, 101]
    # Split respects the reshuffle: TV entries keep confirmed order.
    assert [e.anilist_id for e in out["tv"]] == [102, 101]
    assert [e.anilist_id for e in out["extras"]] == [201]


def test_reorder_drops_removed_entries(pub):
    # Admin kept only one entry; the rest of the walk is discarded.
    entries = [EntryData(index=1, label="Season 1", kind="season", anilist_id=101)]
    out = pub._reorder_franchise(_walk(), entries)
    assert [e.anilist_id for e in out["all"]] == [101]
    assert out["extras"] == []


def test_reorder_falls_back_when_no_ids(pub):
    # Bare franchise (cached entries carry no anilist_id): keep the walk order.
    entries = [EntryData(index=1, label="Season 1", kind="season", anilist_id=None)]
    out = pub._reorder_franchise(_walk(), entries)
    assert [e.anilist_id for e in out["all"]] == [101, 102, 201, 202]


# ── _resolve_relink_client (redo button relink identity) ─────────────────────────
#
# The season cards are authored by Senku (or the userbot), so a relink driven by
# the caller's admin_client (NekoFetch/Gojo) silently fails to edit them. The
# resolver must prefer the authoring identity and only fall back to the passed
# client when no pipeline manager is reachable (e.g. a standalone script).


class _PM:
    def __init__(self, senku):
        self.senku = senku


class _CtnWithPM:
    redis = None

    def __init__(self, senku):
        self.pipeline_manager = _PM(senku)


@pytest.mark.asyncio
async def test_relink_client_prefers_senku_over_fallback():
    senku, fallback = object(), object()
    pub = SenkuPublisher(_CtnWithPM(senku))
    got = await pub._resolve_relink_client(fallback, creation_scope="bot",
                                           userbot_account=None)
    assert got is senku


@pytest.mark.asyncio
async def test_relink_client_falls_back_without_pipeline_manager():
    # A standalone relink script has no pipeline_manager: the passed (Senku-token)
    # client must be used verbatim rather than dropped.
    fallback = object()
    pub = SenkuPublisher(_FakeContainer())
    got = await pub._resolve_relink_client(fallback, creation_scope="bot",
                                           userbot_account=None)
    assert got is fallback


@pytest.mark.asyncio
async def test_relink_client_uses_userbot_for_userbot_scope(monkeypatch):
    ub, senku, fallback = object(), object(), object()
    pub = SenkuPublisher(_CtnWithPM(senku))

    async def _fake_acquire():
        return ub

    monkeypatch.setattr(pub, "_acquire_userbot", _fake_acquire)
    got = await pub._resolve_relink_client(fallback, creation_scope="userbot",
                                           userbot_account="acct")
    assert got is ub


@pytest.mark.asyncio
async def test_relink_client_userbot_scope_falls_back_to_senku_when_no_userbot(monkeypatch):
    senku, fallback = object(), object()
    pub = SenkuPublisher(_CtnWithPM(senku))

    async def _no_ub():
        return None

    monkeypatch.setattr(pub, "_acquire_userbot", _no_ub)
    got = await pub._resolve_relink_client(fallback, creation_scope="userbot",
                                           userbot_account="acct")
    assert got is senku


# ── build_audio_keyboard (shared render) ─────────────────────────────────────────

def test_flat_buttons_only_emit_linked_qualities():
    bd = {"type": "flat", "qualities": ["480p", "720p", "1080p"],
          "links": {"480p": "https://t.me/f?a", "1080p": "https://t.me/f?c"}}
    markup = build_audio_keyboard(bd, FMT)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    # 720p has no link, so it's dropped.
    assert labels == ["480p", "1080p"]
    assert all(b.url for row in markup.inline_keyboard for b in row)


def test_buttons_none_without_links():
    assert build_audio_keyboard({"type": "flat", "qualities": ["480p"], "links": {}}, FMT) is None
    assert build_audio_keyboard(None, FMT) is None


def test_flat_buttons_chunk_two_per_row():
    """Reference layout: 2 buttons → [2]; 3 → [2, 1]; 4 → [2, 2]."""
    def rows_for(quals):
        bd = {"type": "flat", "qualities": quals,
              "links": {q: f"https://t.me/f?{q}" for q in quals}}
        km = build_audio_keyboard(bd, FMT)
        return [len(r) for r in km.inline_keyboard]

    assert rows_for(["480p", "720p"]) == [2]
    assert rows_for(["480p", "720p", "1080p"]) == [2, 1]
    assert rows_for(["360p", "480p", "720p", "1080p"]) == [2, 2]


def test_separate_audio_japanese_first_and_chunked():
    bd = {
        "type": "separate_audio",
        "sections": [
            {"language": "english", "label": "English",
             "qualities": ["480p", "720p", "1080p"]},
            {"language": "japanese", "label": "Japanese",
             "qualities": ["480p", "720p", "1080p"]},
        ],
        "links": {
            "english_480p": "https://t.me/e?1", "english_720p": "https://t.me/e?2",
            "english_1080p": "https://t.me/e?3",
            "japanese_480p": "https://t.me/j?1", "japanese_720p": "https://t.me/j?2",
            "japanese_1080p": "https://t.me/j?3",
        },
    }
    km = build_audio_keyboard(bd, FMT)
    rows = km.inline_keyboard
    # Japanese section leads (label row) despite being listed second.
    assert rows[0][0].text == "Japanese"
    # Its 3 qualities wrap 2-per-row: [2, 1].
    assert [len(rows[1]), len(rows[2])] == [2, 1]
    # Then the English section header + wrapped qualities.
    assert rows[3][0].text == "English"
    assert [len(rows[4]), len(rows[5])] == [2, 1]


def test_separate_audio_english_first_when_disabled():
    """japanese_first=False keeps the button_data section order untouched."""
    fmt = PostFormatConfig(japanese_first=False)
    bd = {
        "type": "separate_audio",
        "sections": [
            {"language": "english", "label": "English", "qualities": ["720p"]},
            {"language": "japanese", "label": "Japanese", "qualities": ["720p"]},
        ],
        "links": {"english_720p": "https://t.me/e", "japanese_720p": "https://t.me/j"},
    }
    rows = build_audio_keyboard(bd, fmt).inline_keyboard
    assert rows[0][0].text == "English"
    assert rows[2][0].text == "Japanese"


def test_language_label_override():
    fmt = PostFormatConfig(
        language_label_japanese="🇯🇵 原語", language_label_english="🇬🇧 Dub")
    bd = {
        "type": "separate_audio",
        "sections": [
            {"language": "japanese", "label": "Japanese", "qualities": ["720p"]},
            {"language": "english", "label": "English", "qualities": ["720p"]},
        ],
        "links": {"japanese_720p": "https://t.me/j", "english_720p": "https://t.me/e"},
    }
    labels = [b.text for row in build_audio_keyboard(bd, fmt).inline_keyboard for b in row]
    assert "🇯🇵 原語" in labels
    assert "🇬🇧 Dub" in labels


# ── resolution_label + custom row width ───────────────────────────────────────────

def test_resolution_label_wraps_and_falls_back():
    fmt = PostFormatConfig(resolution_label="「 {res} 」")
    assert resolution_label("1080p", fmt) == "「 1080p 」"
    # A template missing {res} can't produce indistinguishable buttons — bare res.
    assert resolution_label("720p", PostFormatConfig(resolution_label="STATIC")) == "720p"


def test_buttons_per_row_single_column():
    fmt = PostFormatConfig(buttons_per_row=1)
    bd = {"type": "flat", "qualities": ["480p", "720p", "1080p"],
          "links": {q: f"https://t.me/f?{q}" for q in ["480p", "720p", "1080p"]}}
    rows = build_audio_keyboard(bd, fmt).inline_keyboard
    assert [len(r) for r in rows] == [1, 1, 1]


def test_buttons_per_row_zero_clamped_to_one():
    # A misconfigured 0 must not wipe every button (division/empty-row guard).
    fmt = PostFormatConfig(buttons_per_row=0)
    bd = {"type": "flat", "qualities": ["480p", "720p"],
          "links": {"480p": "https://t.me/a", "720p": "https://t.me/b"}}
    rows = build_audio_keyboard(bd, fmt).inline_keyboard
    assert all(len(r) == 1 for r in rows)
    assert sum(len(r) for r in rows) == 2


# ── premium emoji expansion ───────────────────────────────────────────────────────

def test_premium_emoji_expands_named_token():
    fmt = PostFormatConfig(premium_emoji={"movie": "5375464961822695008"})
    out = resolve_premium_emoji("A :movie: night", fmt)
    assert '<tg-emoji emoji-id="5375464961822695008">movie</tg-emoji>' in out


def test_premium_emoji_leaves_unmapped_tokens_untouched():
    fmt = PostFormatConfig(premium_emoji={"movie": "123"})
    assert resolve_premium_emoji("plain :sparkle: text", fmt) == "plain :sparkle: text"


def test_premium_emoji_noop_when_empty():
    # Empty map → plain unicode passes through unchanged (safe until premium wired).
    assert resolve_premium_emoji("🎬 :movie:", PostFormatConfig()) == "🎬 :movie:"


def test_premium_emoji_maps_raw_glyph():
    fmt = PostFormatConfig(premium_emoji={"🎬": "999"})
    out = resolve_premium_emoji("🎬 tonight", fmt)
    assert out == '<tg-emoji emoji-id="999">🎬</tg-emoji> tonight'


# ── _bridge_thumbnails ───────────────────────────────────────────────────────────

class _FakeCache:
    """Selections preloaded per entry index — no Redis needed."""

    def __init__(self, selections):
        self._selections = selections

    async def get_selection(self, code, index):
        return self._selections.get(index, Selection())


@pytest.mark.asyncio
async def test_bridge_passes_through_public_urls(pub, monkeypatch):
    """Renders mirrored at render time already carry a public URL — the bridge
    must NOT re-upload them, just index them by entry and anilist id."""
    entries = [
        EntryData(index=1, label="Season 1", anilist_id=101),
        EntryData(index=2, label="Season 1 Part 2", anilist_id=102),
    ]
    pub.cache = _FakeCache({
        1: Selection(thumbnail_url="https://files.catbox.moe/a.webp"),
        2: Selection(thumbnail_url="https://imgbb.com/b.webp"),
    })

    async def must_not_upload(*a, **k):
        raise AssertionError("public URLs must pass through, not re-upload")

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", must_not_upload)

    by_index, by_id = await pub._bridge_thumbnails("REQ-1", entries)
    assert by_index == {1: "https://files.catbox.moe/a.webp",
                        2: "https://imgbb.com/b.webp"}
    assert by_id == {101: "https://files.catbox.moe/a.webp",
                     102: "https://imgbb.com/b.webp"}


@pytest.mark.asyncio
async def test_bridge_still_uploads_legacy_file_renders(pub, monkeypatch, tmp_path):
    """Legacy ``file://`` selections (pre-render-time-hosting, or a render whose
    host upload failed) are still mirrored here — the old path is preserved."""
    webp = tmp_path / "thumb.webp"
    webp.write_bytes(b"WEBPRENDER")
    entries = [EntryData(index=1, label="Season 1", anilist_id=101)]
    pub.cache = _FakeCache({1: Selection(thumbnail_url=f"file://{webp}")})

    uploaded: dict = {}

    async def fake_backup(container, blob, *, mime="image/jpeg", source_url=""):
        from kurosoden.shared.image_backup import BackupImage
        uploaded["bytes"] = blob
        uploaded["mime"] = mime
        return BackupImage(source_url=source_url,
                           imgbb_url="https://imgbb.com/x.webp")

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", fake_backup)

    by_index, by_id = await pub._bridge_thumbnails("REQ-1", entries)
    assert by_index == {1: "https://imgbb.com/x.webp"}
    assert by_id == {101: "https://imgbb.com/x.webp"}
    assert uploaded["bytes"] == b"WEBPRENDER"
    assert uploaded["mime"] == "image/webp"


@pytest.mark.asyncio
async def test_bridge_skips_entries_without_thumbnail(pub, monkeypatch):
    entries = [EntryData(index=1, label="Season 1", anilist_id=101)]
    pub.cache = _FakeCache({1: Selection()})  # no thumbnail yet

    async def must_not_upload(*a, **k):
        raise AssertionError("no thumbnail → nothing to upload")

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", must_not_upload)
    by_index, by_id = await pub._bridge_thumbnails("REQ-1", entries)
    assert by_index == {}
    assert by_id == {}


# ── _send_posts (fake client) ────────────────────────────────────────────────────

class _FakeChat:
    username = "aot_channel"


class _FakeMsg:
    def __init__(self, mid):
        self.id = mid
        self.pinned_message = None


class _FakeClient:
    def __init__(self):
        self.photos = []
        self.messages = []
        self.stickers = []
        self.pinned = []
        self._next_id = 100

    async def get_chat(self, chat_id):
        return _FakeChat()

    async def send_sticker(self, chat_id, sticker):
        self._next_id += 1
        self.stickers.append(sticker)
        return _FakeMsg(self._next_id)

    async def send_photo(self, chat_id, image, caption=None, reply_markup=None, parse_mode=None):
        self._next_id += 1
        self.photos.append(caption)
        return _FakeMsg(self._next_id)

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None,
                           disable_web_page_preview=None):
        self._next_id += 1
        self.messages.append(text)
        return _FakeMsg(self._next_id)

    async def pin_chat_message(self, chat_id, message_id, disable_notification=True):
        self.pinned.append(message_id)

    async def get_messages(self, chat_id, mid):
        return _FakeMsg(mid)

    async def delete_messages(self, chat_id, mid):
        pass


@pytest.mark.asyncio
async def test_send_posts_dividers_pins_and_qual(pub):
    client = _FakeClient()

    class _Cfg:
        class bot:
            divider_sticker_id = "DIV"
        post_format = PostFormatConfig()
    pub._c.config = _Cfg()

    posts = [
        {"post_type": "info_card", "caption": "Info", "image": "u1",
         "button_data": None, "pinned": True},
        {"post_type": "season_card",
         "caption": "Watch on {BOT_QUAL:720p}", "image": "u2",
         "button_data": None, "pinned": False},
        {"post_type": "watch_guide", "caption": "Guide", "image": None,
         "button_data": None, "pinned": True},
    ]
    posted, pinned, layout = await pub._send_posts(client, -100123, posts)

    assert posted == 3
    # Layout captures every message in order: 3 cards + 2 dividers.
    assert [it["kind"] for it in layout] == [
        "info_card", "divider", "season_card", "divider", "watch_guide",
    ]
    assert all(it["tg_message_id"] is not None for it in layout)
    # Info + guide pinned (two ids), season card not.
    assert len(pinned) == 2 and len(client.pinned) == 2
    # Dividers: one between each of the 3 posts → 2 stickers.
    assert client.stickers == ["DIV", "DIV"]
    # {BOT_QUAL:720p} resolved to a link on the channel handle.
    assert any('href="https://t.me/aot_channel"' in c and ">720p<" in c
               for c in client.photos)


@pytest.mark.asyncio
async def test_send_posts_guide_qual_deeplinks_to_season_card(pub):
    """An anchored {BOT_QUAL#<id>:…} in the guide links to that season's card
    message (t.me/<handle>/<msg_id>) — not the bare channel root.

    The season card is posted BEFORE the guide, so its message id is known by the
    time the guide caption resolves."""
    client = _FakeClient()

    class _Cfg:
        class bot:
            divider_sticker_id = "DIV"
        post_format = PostFormatConfig()
    pub._c.config = _Cfg()

    posts = [
        {"post_type": "season_card", "caption": "S2 card", "image": "u1",
         "button_data": None, "pinned": False, "anilist_id": 555},
        {"post_type": "watch_guide",
         "caption": "S2: {BOT_QUAL#555:480p  720p}", "image": None,
         "button_data": None, "pinned": True},
    ]
    posted, pinned, layout = await pub._send_posts(client, -100123, posts)

    assert posted == 2
    # The season card's message id (from the layout) is what the guide links to.
    season_mid = next(it["tg_message_id"] for it in layout
                      if it["kind"] == "season_card")
    guide_text = client.messages[-1]
    assert f'href="https://t.me/aot_channel/{season_mid}"' in guide_text
    assert ">480p  720p<" in guide_text


@pytest.mark.asyncio
async def test_send_posts_survives_a_failed_card(pub):
    client = _FakeClient()

    async def _boom(*a, **k):
        raise RuntimeError("telegram down")
    client.send_photo = _boom

    class _Cfg:
        class bot:
            divider_sticker_id = None
        post_format = PostFormatConfig()
    pub._c.config = _Cfg()

    posts = [
        {"post_type": "info_card", "caption": "Info", "image": "u1",
         "button_data": None, "pinned": True},
        {"post_type": "footer", "caption": "Footer", "image": None,
         "button_data": None, "pinned": False},
    ]
    posted, pinned, layout = await pub._send_posts(client, -100123, posts)
    # The photo card failed; the text footer still posted.
    assert posted == 1
    assert client.messages == ["Footer"]
    # Only the successfully-sent footer is recorded in the layout.
    assert [it["kind"] for it in layout] == ["footer"]
