"""K5 — update-during-redo is a CHOICE, not a notice (HANDOFF §9.4).

When the owner's ``/redo`` surfaces season(s) the channel doesn't have yet,
``RedoService`` detects them (``_detect_new_seasons``) and the handler asks
Yes/No. The Yes path — ``queue_update_for_new_seasons`` — queues each new
season as an ``update_entry`` request through the existing K3 update machinery
(entry download → card append via ``update_distribution_channel`` → main-post
reply). These tests drive those REAL methods; No simply keeps the redo as-is.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.domain.enums import RequestStatus
from nekofetch.infrastructure.database.postgres.models import (
    ChannelLayout,
    DistributionBot,
    Request,
)
from kurosoden.tests.helpers import _create_request


@pytest.fixture(autouse=True)
def _silence_log(monkeypatch):
    async def _noop(self, *a, **k):
        return None

    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService.event", _noop)
    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService.post_request_card",
        _noop)


def _container(sessionmaker):
    cfg = SimpleNamespace(
        features=SimpleNamespace(request_system=True),
        log_channel=SimpleNamespace(
            enabled=False, channel_id=0, sections=[], events=[],
        ),
    )
    return SimpleNamespace(pg_sessionmaker=sessionmaker, redis=None, config=cfg)


def _franchise(doc: str) -> dict:
    return {
        "anime_doc_id": doc,
        "title": "Test Anime",
        "franchise": {
            "1": {"anilist_id": 1, "format": "TV", "episodes": 12,
                  "score": 7.5, "season": 1},
            "2": {"anilist_id": 2, "format": "TV", "episodes": 12,
                  "score": 7.8, "season": 2},
        },
    }


async def _mk_published_channel(session, *, doc, anilist_id=1):
    """A published channel whose layout carries exactly one season id."""
    bot = DistributionBot(
        name="Chan", username=None, anime_doc_id=doc,
        encrypted_token="x", enabled=True, is_channel=True, chat_id=-1001,
    )
    session.add(bot)
    await session.flush()
    session.add(ChannelLayout(
        channel_bot_id=bot.id, seq=0, kind="season_card",
        tg_message_id=55, anilist_id=anilist_id, is_pinned=False,
    ))
    await session.commit()
    return bot


# ── Detection: new seasons surfaced by the redo ──────────────────────────────


async def test_detect_new_seasons_lists_unpublished_entries(session, sessionmaker):
    from kurosoden.shared.redo_service import RedoService

    doc = "anilist:50"
    await _create_request(session, code="REQ-50", anime_doc_id=doc,
                          status="published")
    await _mk_published_channel(session, doc=doc, anilist_id=1)

    svc = RedoService(_container(sessionmaker))
    plan = await svc.detect_state(doc)
    assert plan.published_season_ids == [1]

    new_ids = await svc._detect_new_seasons(plan, _franchise(doc))
    assert new_ids == [2]


async def test_detect_new_seasons_empty_when_no_new_entries(session, sessionmaker):
    from kurosoden.shared.redo_service import RedoService

    doc = "anilist:51"
    await _create_request(session, code="REQ-51", anime_doc_id=doc,
                          status="published")
    await _mk_published_channel(session, doc=doc, anilist_id=1)
    # A layout that already covers both seasons → nothing new.
    async with sessionmaker() as s:
        bot = (await s.execute(select(DistributionBot).where(
            DistributionBot.anime_doc_id == doc))).scalars().first()
        s.add(ChannelLayout(
            channel_bot_id=bot.id, seq=1, kind="season_card",
            tg_message_id=56, anilist_id=2, is_pinned=False,
        ))
        await s.commit()

    svc = RedoService(_container(sessionmaker))
    plan = await svc.detect_state(doc)
    new_ids = await svc._detect_new_seasons(plan, _franchise(doc))
    assert new_ids == []


# ── Yes path: queue each new season as an update_entry request ───────────────


async def test_yes_path_queues_update_entry_request(session, sessionmaker,
                                                     monkeypatch):
    from nekofetch.infrastructure.repositories import request_repo
    from kurosoden.shared.redo_service import RedoService

    _seq = [9000]

    async def _fake_next_seq(self):
        _seq[0] += 1
        return _seq[0]

    monkeypatch.setattr(request_repo.RequestRepository, "next_sequence",
                        _fake_next_seq)

    doc = "anilist:52"
    svc = RedoService(_container(sessionmaker))
    queued = await svc.queue_update_for_new_seasons(42, doc, _franchise(doc), [2])

    assert queued == 1
    async with sessionmaker() as s:
        rows = (await s.execute(select(Request).where(
            Request.anime_doc_id == doc))).scalars().all()
        assert len(rows) == 1
        fd = rows[0].franchise_data or {}
        assert fd.get("update_entry") is True
        assert fd.get("anilist_id") == 2
        assert fd.get("season") == 2
        assert rows[0].status is RequestStatus.PENDING


async def test_yes_path_skips_unknown_entries(session, sessionmaker):
    from kurosoden.shared.redo_service import RedoService

    doc = "anilist:53"
    svc = RedoService(_container(sessionmaker))
    # id 999 isn't in the franchise walk → nothing queued, no crash.
    queued = await svc.queue_update_for_new_seasons(42, doc, _franchise(doc), [999])

    assert queued == 0
    async with sessionmaker() as s:
        rows = (await s.execute(select(Request).where(
            Request.anime_doc_id == doc))).scalars().all()
        assert rows == []
