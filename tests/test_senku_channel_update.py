"""Coverage for the Phase-6 incremental channel-update path in SenkuPublisher.

When a new franchise entry finishes the pipeline, we update its already-published
distribution channel *in place* rather than reposting the whole thing:

  • ``_persist_channel`` — on publish, register a durable DistributionBot anchor
    (idempotent: the auto pipeline may already have one) and snapshot the posted
    message layout into ``channel_layout``.
  • ``update_distribution_channel`` — a no-op when the title has no channel row.
  • ``_append_and_refooter`` — delete the trailing footer + its leading divider,
    append the new card(s) behind a divider, re-post divider + footer, and rewrite
    ``channel_layout`` to the new tail (pinned info card / guide untouched).

These run against the SQLite fixture engine + a fake admin client, so no
Telegram / catbox / AniList calls happen.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.core.config import PostFormatConfig
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    ChannelLayout,
    DistributionBot,
)
from kurosoden.shared.senku_publisher import SenkuPublisher

pytestmark = pytest.mark.asyncio


class _FakeMsg:
    def __init__(self, mid):
        self.id = mid
        self.pinned_message = None


class _FakeClient:
    """Admin-client stub recording sends/deletes; hands out increasing ids."""

    def __init__(self, *, username: str | None = "aot_channel"):
        self.username = username
        self.photos: list[str] = []
        self.messages: list[str] = []
        self.stickers: list[str] = []
        self.deleted: list[int] = []
        self._next_id = 1000

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, username=self.username)

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

    async def delete_messages(self, chat_id, mid):
        self.deleted.append(mid)

    async def get_messages(self, chat_id, mid):
        return None

    async def pin_chat_message(self, chat_id, mid, disable_notification=None):
        return None


def _container(sessionmaker):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        admin_client=None,
        redis=None,
        config=SimpleNamespace(
            post_format=PostFormatConfig(),
            bot=SimpleNamespace(
                divider_sticker_id="DIV",
                footer_text="Join the channel!",
                footer_image_url=None,
            ),
        ),
    )


async def _make_channel(session, *, chat_id, anime_doc_id, layout):
    """Create a channel anchor + its ordered layout rows."""
    ch = DistributionBot(
        name="Ch", username="aot_channel", anime_doc_id=anime_doc_id,
        encrypted_token="fake", enabled=True, is_channel=True, chat_id=chat_id,
    )
    session.add(ch)
    await session.commit()
    for seq, (kind, mid, aid, pinned) in enumerate(layout):
        session.add(ChannelLayout(
            channel_bot_id=ch.id, seq=seq, kind=kind,
            tg_message_id=mid, anilist_id=aid, is_pinned=pinned,
        ))
    await session.commit()
    return ch


async def _layout_kinds(sessionmaker, bot_id):
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(ChannelLayout)
                .where(ChannelLayout.channel_bot_id == bot_id)
                .order_by(ChannelLayout.seq)
            )
        ).scalars().all()
    return rows


# ── update_distribution_channel: no-op when no channel row ────────────────────

async def test_update_no_channel_is_noop(sessionmaker, session):
    pub = SenkuPublisher(_container(sessionmaker))
    client = _FakeClient()
    out = await pub.update_distribution_channel(client, "anilist:999", [42])
    assert out == {"appended": 0, "chat_id": None}
    assert client.stickers == [] and client.photos == [] and client.messages == []


# ── _append_and_refooter: season auto-update rebuilds the guide tail ──────────

async def test_append_and_refooter_rebuilds_guide_tail_and_records_layout(sessionmaker, session):
    pub = SenkuPublisher(_container(sessionmaker))
    client = _FakeClient()

    # Existing channel: info (pinned) · div · S1 card · div · guide (pinned) · div · footer
    layout = [
        ("info_card", 1, None, True),
        ("divider", 2, None, False),
        ("season_card", 3, 101, False),
        ("divider", 4, None, False),
        ("watch_guide", 5, None, True),
        ("divider", 6, None, False),
        ("footer", 7, None, False),
    ]
    ch = await _make_channel(session, chat_id=-100123, anime_doc_id="anilist:1", layout=layout)

    layout_dicts = [
        {"kind": k, "tg_message_id": m, "anilist_id": a, "is_pinned": p}
        for k, m, a, p in layout
    ]
    new_cards = [{
        "post_type": "season_card", "caption": "Season 2",
        "image": None, "button_data": None, "pinned": False, "anilist_id": 102,
    }]

    appended = await pub._append_and_refooter(
        client, -100123, ch.id, layout_dicts, new_cards,
        "Watch guide (regenerated)",
    )

    assert appended == 1
    # The guide (5), its leading divider (4), the trailing divider (6) and the
    # footer (7) are all stripped — the whole guide tail is rebuilt.
    assert set(client.deleted) == {4, 5, 6, 7}

    rows = await _layout_kinds(sessionmaker, ch.id)
    kinds = [r.kind for r in rows]
    # Body kept through the last existing card, then: new card · div · guide · div · footer.
    assert kinds == [
        "info_card", "divider", "season_card",
        "divider", "season_card", "divider", "watch_guide", "divider", "footer",
    ]
    # The new season card carries its anilist id and precedes the fresh guide.
    assert any(r.kind == "season_card" and r.anilist_id == 102 for r in rows)
    # The regenerated guide is freshly posted (new id) and re-pinned.
    guide = next(r for r in rows if r.kind == "watch_guide")
    assert guide.tg_message_id not in {5} and guide.is_pinned is True
    assert rows[-1].kind == "footer"


async def test_append_falls_back_to_footer_only_without_guide(sessionmaker, session):
    """No regenerated guide (guide_caption=None): keep the classic footer swap."""
    pub = SenkuPublisher(_container(sessionmaker))
    client = _FakeClient()

    layout = [
        ("info_card", 1, None, True),
        ("divider", 2, None, False),
        ("season_card", 3, 101, False),
        ("divider", 4, None, False),
        ("watch_guide", 5, None, True),
        ("divider", 6, None, False),
        ("footer", 7, None, False),
    ]
    ch = await _make_channel(session, chat_id=-100124, anime_doc_id="anilist:11", layout=layout)
    layout_dicts = [
        {"kind": k, "tg_message_id": m, "anilist_id": a, "is_pinned": p}
        for k, m, a, p in layout
    ]
    new_cards = [{
        "post_type": "season_card", "caption": "Season 2",
        "image": None, "button_data": None, "pinned": False, "anilist_id": 102,
    }]

    appended = await pub._append_and_refooter(
        client, -100124, ch.id, layout_dicts, new_cards, None,
    )

    assert appended == 1
    # Guide preserved; only the old footer (7) is swapped.
    assert client.deleted == [7]
    rows = await _layout_kinds(sessionmaker, ch.id)
    guide = next(r for r in rows if r.kind == "watch_guide")
    assert guide.tg_message_id == 5 and guide.is_pinned is True


async def test_append_when_no_footer_tracked(sessionmaker, session):
    """A channel whose layout has no footer row: append after everything, add footer."""
    pub = SenkuPublisher(_container(sessionmaker))
    client = _FakeClient()

    layout = [
        ("info_card", 1, None, True),
        ("divider", 2, None, False),
        ("season_card", 3, 101, False),
    ]
    ch = await _make_channel(session, chat_id=-100200, anime_doc_id="anilist:2", layout=layout)
    layout_dicts = [
        {"kind": k, "tg_message_id": m, "anilist_id": a, "is_pinned": p}
        for k, m, a, p in layout
    ]
    new_cards = [{
        "post_type": "movie_card", "caption": "Movie",
        "image": None, "button_data": None, "pinned": False, "anilist_id": 201,
    }]

    appended = await pub._append_and_refooter(
        client, -100200, ch.id, layout_dicts, new_cards,
    )

    assert appended == 1
    assert client.deleted == []  # nothing to delete
    rows = await _layout_kinds(sessionmaker, ch.id)
    kinds = [r.kind for r in rows]
    assert kinds == ["info_card", "divider", "season_card",
                     "divider", "movie_card", "divider", "footer"]


async def test_append_multiple_entries_interleaves_dividers(sessionmaker, session):
    """Two newly-added entries: card · divider · card · divider · footer.

    Every card ends up separated by exactly one divider, and only the old
    footer is deleted (the pre-footer divider is kept and reused).
    """
    pub = SenkuPublisher(_container(sessionmaker))
    client = _FakeClient()

    layout = [
        ("info_card", 1, None, True),
        ("divider", 2, None, False),
        ("season_card", 3, 101, False),
        ("divider", 4, None, False),
        ("footer", 5, None, False),
    ]
    ch = await _make_channel(session, chat_id=-100300, anime_doc_id="anilist:3", layout=layout)
    layout_dicts = [
        {"kind": k, "tg_message_id": m, "anilist_id": a, "is_pinned": p}
        for k, m, a, p in layout
    ]
    new_cards = [
        {"post_type": "season_card", "caption": "S2", "image": None,
         "button_data": None, "pinned": False, "anilist_id": 102},
        {"post_type": "movie_card", "caption": "Movie", "image": None,
         "button_data": None, "pinned": False, "anilist_id": 103},
    ]

    appended = await pub._append_and_refooter(
        client, -100300, ch.id, layout_dicts, new_cards,
    )

    assert appended == 2
    assert client.deleted == [5]  # only the footer
    rows = await _layout_kinds(sessionmaker, ch.id)
    kinds = [r.kind for r in rows]
    # Kept: info · div · S1 · div(4).  Appended: S2 · div · Movie · div · footer.
    assert kinds == [
        "info_card", "divider", "season_card", "divider",
        "season_card", "divider", "movie_card", "divider", "footer",
    ]
    # The pre-footer divider (id 4) is reused, so the first new card follows it.
    assert rows[3].kind == "divider" and rows[3].tg_message_id == 4
    assert rows[4].kind == "season_card" and rows[4].anilist_id == 102
    assert rows[6].kind == "movie_card" and rows[6].anilist_id == 103
    assert rows[-1].kind == "footer"


# ── P0: _reconcile_content_posts keeps the ban-restore backup current ─────────

async def _content_rows(sessionmaker, bot_id):
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(BotContentPost)
                .where(BotContentPost.bot_id == bot_id)
                .order_by(BotContentPost.order)
            )
        ).scalars().all()
    return rows


async def test_update_reconciles_bot_content_posts_with_new_season(sessionmaker, session):
    """After a season auto-update, BotContentPost (the ban-restore backup source)
    must include the new season card + the regenerated guide — not the stale
    S1-only snapshot. This is the P0 data-loss fix."""
    pub = SenkuPublisher(_container(sessionmaker))
    client = _FakeClient()

    layout = [
        ("info_card", 1, None, True),
        ("divider", 2, None, False),
        ("season_card", 3, 101, False),
        ("divider", 4, None, False),
        ("watch_guide", 5, None, True),
        ("divider", 6, None, False),
        ("footer", 7, None, False),
    ]
    ch = await _make_channel(session, chat_id=-100900, anime_doc_id="anilist:9", layout=layout)

    # Seed the PRE-update BotContentPost snapshot: S1 card + guide + footer only.
    session.add_all([
        BotContentPost(bot_id=ch.id, post_type="season_card", order=0,
                       caption="Season 1", anilist_id=101, tg_message_id=3),
        BotContentPost(bot_id=ch.id, post_type="watch_guide", order=1,
                       caption="Old guide", is_pinned=True, tg_message_id=5),
        BotContentPost(bot_id=ch.id, post_type="footer", order=2,
                       caption="Join!", tg_message_id=7),
    ])
    await session.commit()

    layout_dicts = [
        {"kind": k, "tg_message_id": m, "anilist_id": a, "is_pinned": p}
        for k, m, a, p in layout
    ]
    new_cards = [{
        "post_type": "season_card", "caption": "Season 2",
        "image": None, "button_data": None, "pinned": False, "anilist_id": 102,
    }]

    await pub._append_and_refooter(
        client, -100900, ch.id, layout_dicts, new_cards,
        "Watch guide (regenerated with S2)",
    )

    rows = await _content_rows(sessionmaker, ch.id)
    by_type = {}
    for r in rows:
        by_type.setdefault(r.post_type, []).append(r)

    # Both seasons now present as content cards (backup source no longer stale).
    season_ids = {r.anilist_id for r in by_type.get("season_card", [])}
    assert season_ids == {101, 102}
    # Exactly one guide + one footer, and the guide carries the regenerated text.
    assert len(by_type.get("watch_guide", [])) == 1
    assert "regenerated with S2" in by_type["watch_guide"][0].caption
    assert len(by_type.get("footer", [])) == 1
    # Content revision bumped so returning /start users get the new set.
    async with sessionmaker() as s:
        bot = await s.get(DistributionBot, ch.id)
    assert (bot.content_revision or 0) >= 1


async def test_reconcile_content_posts_dedupes_on_rerun(sessionmaker, session):
    """A retried update (same new card twice) must not double-insert the card."""
    pub = SenkuPublisher(_container(sessionmaker))
    ch = DistributionBot(
        name="Ch", username="c", anime_doc_id="anilist:9b",
        encrypted_token="fake", enabled=True, is_channel=True, chat_id=-100901,
    )
    session.add(ch)
    await session.commit()
    session.add(BotContentPost(bot_id=ch.id, post_type="season_card", order=0,
                               caption="S1", anilist_id=101, tg_message_id=3))
    await session.commit()

    new_content = [{"post_type": "season_card", "caption": "S2",
                    "image": None, "button_data": None,
                    "anilist_id": 102, "tg_message_id": 30}]
    # Run twice — the second run should be a no-op for the S2 card.
    for _ in range(2):
        await pub._reconcile_content_posts(
            ch.id, new_content, guide_caption="G", guide_mid=40,
            footer_caption="F", footer_image=None, footer_mid=50,
        )

    rows = await _content_rows(sessionmaker, ch.id)
    season_102 = [r for r in rows if r.anilist_id == 102]
    assert len(season_102) == 1  # not doubled
    assert sum(1 for r in rows if r.post_type == "watch_guide") == 1
    assert sum(1 for r in rows if r.post_type == "footer") == 1


# ── _persist_channel: idempotent anchor + layout snapshot ─────────────────────

async def test_persist_channel_reuses_existing_anchor(sessionmaker, session):
    pub = SenkuPublisher(_container(sessionmaker))

    # Auto pipeline already made the anchor.
    ch = DistributionBot(
        name="Ch", username="aot_channel", anime_doc_id="anilist:5",
        encrypted_token="fake", enabled=True, is_channel=True, chat_id=-100555,
    )
    session.add(ch)
    await session.commit()
    bot_id = ch.id

    layout = [
        {"kind": "info_card", "tg_message_id": 10, "anilist_id": None, "is_pinned": True},
        {"kind": "divider", "tg_message_id": 11, "anilist_id": None, "is_pinned": False},
        {"kind": "season_card", "tg_message_id": 12, "anilist_id": 101, "is_pinned": False},
        {"kind": "footer", "tg_message_id": 13, "anilist_id": None, "is_pinned": False},
    ]
    await pub._persist_channel("anilist:5", -100555, "AOT", "aot_channel", layout)

    # No duplicate anchor created.
    async with sessionmaker() as s:
        bots = (
            await s.execute(select(DistributionBot).where(DistributionBot.chat_id == -100555))
        ).scalars().all()
    assert len(bots) == 1 and bots[0].id == bot_id

    rows = await _layout_kinds(sessionmaker, bot_id)
    assert [r.kind for r in rows] == ["info_card", "divider", "season_card", "footer"]


async def test_persist_channel_replaces_prior_layout(sessionmaker, session):
    pub = SenkuPublisher(_container(sessionmaker))
    ch = DistributionBot(
        name="Ch", username=None, anime_doc_id="anilist:6",
        encrypted_token="fake", enabled=True, is_channel=True, chat_id=-100666,
    )
    session.add(ch)
    await session.commit()
    session.add(ChannelLayout(channel_bot_id=ch.id, seq=0, kind="footer",
                              tg_message_id=99, anilist_id=None, is_pinned=False))
    await session.commit()

    await pub._persist_channel(
        "anilist:6", -100666, "T", None,
        [{"kind": "info_card", "tg_message_id": 1, "anilist_id": None, "is_pinned": True}],
    )
    rows = await _layout_kinds(sessionmaker, ch.id)
    # Old single footer row is gone, replaced by the fresh snapshot.
    assert [(r.kind, r.tg_message_id) for r in rows] == [("info_card", 1)]
