"""Index slot management + repurposed-slot detection (feature #40).

Two things are pinned here:

  • **Repurpose detection** — the correctness-critical rule. A reserved slot is
    ours to consume only while its live caption still carries the RESERVED marker
    ("RESERVED FOR FUTURE" / "Slot N/N"). If an admin edits a reserved post into a
    normal one (losing the marker), ``_verify_reserved_slots`` flags it
    ``repurposed`` so auto-indexing never overwrites it and works with the
    remaining genuine slots.
  • **Slot management** — the Gojo UI edits (caption / image / buttons) drive the
    live message in place; ``list_slots`` classifies each slot.

The admin client is stubbed, so no Telegram calls happen.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.core.config import IndexChannelConfig
from nekofetch.infrastructure.database.postgres.models import IndexSection
from nekofetch.services.index_channel_service import (
    IndexChannelService,
    _RESERVED_CAP,
    _is_reserved_caption,
)

aio = pytest.mark.asyncio


class _FakeMsg:
    def __init__(self, mid, caption=None, *, empty=False):
        self.id = mid
        self.caption = caption
        self.text = None
        self.empty = empty


class _FakeClient:
    """Records edits; serves per-message captions for the verify pass."""

    def __init__(self, captions: dict[int, str] | None = None):
        self._captions = captions or {}
        self.edited_captions: dict[int, str] = {}
        self.edited_media: list[int] = []
        self.edited_markup: list[int] = []

    async def get_messages(self, chat_id, mid):
        if mid not in self._captions:
            return _FakeMsg(mid, empty=True)
        return _FakeMsg(mid, caption=self._captions[mid])

    async def edit_message_caption(self, chat_id, mid, caption=None, parse_mode=None, reply_markup=None):
        self.edited_captions[mid] = caption
        return _FakeMsg(mid, caption=caption)

    async def edit_message_media(self, chat_id, mid, media=None, reply_markup=None):
        self.edited_media.append(mid)
        return _FakeMsg(mid)

    async def edit_message_reply_markup(self, chat_id, mid, reply_markup=None):
        self.edited_markup.append(mid)
        return _FakeMsg(mid)


def _container(sessionmaker, client=None):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        admin_client=client,
        config=SimpleNamespace(
            index_channel=IndexChannelConfig(enabled=True, channel_id=-100500),
        ),
    )


async def _seed(sessionmaker, rows):
    async with sessionmaker() as s:
        for order, label, mid in rows:
            s.add(IndexSection(sort_order=order, label=label,
                               base_letter=label, message_id=mid))
        await s.commit()


# ── marker detection ──────────────────────────────────────────────────────────

def test_is_reserved_caption_matches_marker():
    assert _is_reserved_caption(f"{_RESERVED_CAP}\n\n<i>Slot 3/10</i>")
    assert _is_reserved_caption("RESERVED FOR FUTURE")
    assert _is_reserved_caption("something Slot 4/10 something")


def test_is_reserved_caption_rejects_repurposed():
    assert not _is_reserved_caption("🎉 Big announcement — join our new channel!")
    assert not _is_reserved_caption("")
    assert not _is_reserved_caption(None)


# ── repurpose detection ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_flags_repurposed_slot(sessionmaker, session):
    # Two reserved slots: 2 still reserved, 3 was edited into a promo post.
    await _seed(sessionmaker, [(1, "A", 10), (2, None, 11), (3, None, 12)])
    client = _FakeClient(captions={
        11: f"{_RESERVED_CAP}\n\n<i>Slot 1/10</i>",
        12: "🎬 Follow our backup channel!",
    })
    svc = IndexChannelService(_container(sessionmaker, client))

    flagged = await svc._verify_reserved_slots()
    assert flagged == 1

    async with sessionmaker() as s:
        rows = {r.sort_order: r for r in (
            await s.execute(select(IndexSection))
        ).scalars().all()}
    assert rows[2].repurposed is False   # still a genuine reserved slot
    assert rows[3].repurposed is True    # repurposed → hands off


@pytest.mark.asyncio
async def test_repurposed_excluded_from_reserved_count(sessionmaker, session):
    await _seed(sessionmaker, [(1, "A", 10), (2, None, 11), (3, None, 12)])
    async with sessionmaker() as s:
        row = (await s.execute(
            select(IndexSection).where(IndexSection.sort_order == 3)
        )).scalar_one()
        row.repurposed = True
        await s.commit()

    svc = IndexChannelService(_container(sessionmaker, _FakeClient()))
    async with sessionmaker() as s:
        n = await svc._count_reserved_in_session(s)
    assert n == 1   # only slot 2 counts; the repurposed slot 3 is excluded


@pytest.mark.asyncio
async def test_verify_leaves_unreadable_slot_alone(sessionmaker, session):
    # A reserved slot whose message can't be fetched (deleted/transient) must NOT
    # be flagged — a fetch miss shouldn't strand a genuine slot.
    await _seed(sessionmaker, [(1, "A", 10), (2, None, 11)])
    svc = IndexChannelService(_container(sessionmaker, _FakeClient(captions={})))
    flagged = await svc._verify_reserved_slots()
    assert flagged == 0
    async with sessionmaker() as s:
        row = (await s.execute(
            select(IndexSection).where(IndexSection.sort_order == 2)
        )).scalar_one()
    assert row.repurposed is False


# ── slot management ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_slots_classifies(sessionmaker, session):
    await _seed(sessionmaker, [(1, "A", 10), (2, None, 11), (3, None, 12)])
    async with sessionmaker() as s:
        row = (await s.execute(
            select(IndexSection).where(IndexSection.sort_order == 3)
        )).scalar_one()
        row.repurposed = True
        await s.commit()

    svc = IndexChannelService(_container(sessionmaker, _FakeClient()))
    slots = await svc.list_slots()
    kinds = {s["order"]: s["kind"] for s in slots}
    assert kinds == {1: "letter", 2: "reserved", 3: "repurposed"}


@pytest.mark.asyncio
async def test_edit_slot_caption_edits_live_message(sessionmaker, session):
    await _seed(sessionmaker, [(1, "A", 10)])
    client = _FakeClient()
    svc = IndexChannelService(_container(sessionmaker, client))
    ok = await svc.edit_slot_caption(1, "<b>New text</b>")
    assert ok is True
    assert client.edited_captions[10] == "<b>New text</b>"


@pytest.mark.asyncio
async def test_set_slot_buttons_edits_markup(sessionmaker, session):
    await _seed(sessionmaker, [(1, "A", 10)])
    client = _FakeClient()
    svc = IndexChannelService(_container(sessionmaker, client))
    ok = await svc.set_slot_buttons(1, [("Join", "https://t.me/x")])
    assert ok is True
    assert client.edited_markup == [10]


@pytest.mark.asyncio
async def test_edit_missing_slot_returns_false(sessionmaker, session):
    svc = IndexChannelService(_container(sessionmaker, _FakeClient()))
    assert await svc.edit_slot_caption(999, "x") is False
