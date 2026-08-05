"""Redo feature — owner-triggered re-processing of an existing / published series.

Covers the three units the redo flow is built on:

* :meth:`RedoService.detect_state` — PUBLISHED vs IN_PROGRESS_PREPUBLISH vs ABSENT.
* :meth:`RequestService.purge_all_for_anime` — the ``keep_channel`` switch that
  makes a published redo keep its channel/posts while an in-progress redo wipes
  everything.
* :meth:`RedoService.submit` — the ``redo`` / ``redo_relink`` markers it stamps
  onto the re-queued work per state.
* :meth:`SenkuPublisher.relink_packs_in_place` — editing fresh pack buttons into
  the existing season cards in place.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.domain.enums import JobStatus, RequestStatus
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    ChannelLayout,
    ChannelPost,
    DistributionBot,
    DownloadJob,
    MediaFile,
    Request,
    StoragePack,
)
from kurosoden.tests.helpers import _create_request


# ── container + log silencing ────────────────────────────────────────────────


def _container(sessionmaker, tmp_path=None, *, admin_client=None,
               pipeline_manager=None):
    cfg = SimpleNamespace(
        log_channel=SimpleNamespace(),
        post_format=SimpleNamespace(max_quality_buttons=3, divider_sticker_id=None),
        bot=SimpleNamespace(filestore_bots=[], divider_sticker_id=None),
        storage_channel=SimpleNamespace(enabled=False),
    )
    env = SimpleNamespace(storage_path=tmp_path) if tmp_path is not None else None
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker, redis=None, progress=None,
        admin_client=admin_client, config=cfg, env=env,
        pipeline_manager=pipeline_manager,
    )


@pytest.fixture(autouse=True)
def _silence_log(monkeypatch):
    async def _noop(self, *a, **k):
        return None
    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService.event", _noop
    )


# ── seed helpers ─────────────────────────────────────────────────────────────


async def _mk_channel_bot(session, *, anime_doc_id, chat_id=-1001999888777):
    bot = DistributionBot(
        name="Chan", username=None, anime_doc_id=anime_doc_id,
        encrypted_token="x", enabled=True, is_channel=True, chat_id=chat_id,
    )
    session.add(bot)
    await session.commit()
    return bot


async def _mk_layout(session, *, bot_id, kind, seq, tg_message_id, anilist_id):
    row = ChannelLayout(
        channel_bot_id=bot_id, seq=seq, kind=kind,
        tg_message_id=tg_message_id, anilist_id=anilist_id, is_pinned=False,
    )
    session.add(row)
    await session.commit()
    return row


async def _mk_pack(session, *, doc, season=1, res="480p", audio="dual_audio"):
    from nekofetch.domain.enums import AudioType
    pack = StoragePack(
        anime_doc_id=doc, anime_title="Test Anime", season=season,
        resolution=res, audio=AudioType(audio), channel_id=-100123,
        start_message_id=10, end_message_id=20, file_count=6,
    )
    session.add(pack)
    await session.commit()
    return pack


# ── detect_state ─────────────────────────────────────────────────────────────


async def test_detect_state_published(session, sessionmaker):
    from kurosoden.shared.redo_service import RedoState, RedoService

    doc = "anilist:900"
    await _create_request(session, code="REQ-900", anime_doc_id=doc,
                          status="published")
    await _mk_channel_bot(session, anime_doc_id=doc)

    plan = await RedoService(_container(sessionmaker)).detect_state(doc)
    assert plan.state is RedoState.PUBLISHED
    assert plan.keep_channel is True
    assert "REQ-900" in plan.codes


async def test_detect_state_published_via_channel_post(session, sessionmaker):
    # No DistributionBot, but a main-channel ChannelPost with main_message_id
    # still means the title is live → keep the channel.
    from kurosoden.shared.redo_service import RedoState, RedoService

    doc = "anilist:901"
    await _create_request(session, code="REQ-901", anime_doc_id=doc,
                          status="published")
    cp = ChannelPost(anime_doc_id=doc, main_channel_id=-1001, main_message_id=42)
    session.add(cp)
    await session.commit()

    plan = await RedoService(_container(sessionmaker)).detect_state(doc)
    assert plan.state is RedoState.PUBLISHED


async def test_detect_state_in_progress(session, sessionmaker):
    from kurosoden.shared.redo_service import RedoState, RedoService

    doc = "anilist:902"
    req = await _create_request(session, code="REQ-902", anime_doc_id=doc,
                                status="queued")
    job = DownloadJob(request_id=req.id, status=JobStatus.RUNNING)
    session.add(job)
    await session.commit()

    plan = await RedoService(_container(sessionmaker)).detect_state(doc)
    assert plan.state is RedoState.IN_PROGRESS_PREPUBLISH
    assert plan.keep_channel is False
    assert job.id in plan.running_job_ids


async def test_detect_state_absent(session, sessionmaker):
    from kurosoden.shared.redo_service import RedoState, RedoService

    plan = await RedoService(_container(sessionmaker)).detect_state("anilist:404")
    assert plan.state is RedoState.ABSENT
    assert plan.keep_channel is False
    assert plan.codes == []


# ── purge_all_for_anime ──────────────────────────────────────────────────────


async def test_purge_keep_channel_keeps_channel_drops_packs(
    session, sessionmaker, tmp_path,
):
    from nekofetch.services.request_service import RequestService

    doc = "anilist:910"
    req = await _create_request(session, code="REQ-910", anime_doc_id=doc,
                                status="published")
    job = DownloadJob(request_id=req.id, status=JobStatus.COMPLETED)
    session.add(job)
    await session.commit()
    session.add(MediaFile(job_id=job.id, anime_doc_id=doc, season=1, episode=1,
                          resolution="480p"))
    await _mk_pack(session, doc=doc)
    bot = await _mk_channel_bot(session, anime_doc_id=doc)
    await _mk_layout(session, bot_id=bot.id, kind="season_card", seq=0,
                     tg_message_id=55, anilist_id=910)
    await session.commit()

    svc = RequestService(_container(sessionmaker, tmp_path))
    await svc.purge_all_for_anime(doc, keep_channel=True)

    async with sessionmaker() as s:
        assert (await s.execute(select(StoragePack).where(
            StoragePack.anime_doc_id == doc))).first() is None
        assert (await s.execute(select(DownloadJob).where(
            DownloadJob.request_id == req.id))).first() is None
        # Channel + posts + request row survive.
        assert (await s.execute(select(DistributionBot).where(
            DistributionBot.anime_doc_id == doc))).first() is not None
        assert (await s.execute(select(ChannelLayout).where(
            ChannelLayout.channel_bot_id == bot.id))).first() is not None
        assert (await s.execute(select(Request).where(
            Request.anime_doc_id == doc))).first() is not None


async def test_purge_full_wipe_clears_everything(session, sessionmaker, tmp_path):
    from nekofetch.services.request_service import RequestService

    doc = "anilist:911"
    req = await _create_request(session, code="REQ-911", anime_doc_id=doc,
                                status="queued")
    await _mk_pack(session, doc=doc)
    bot = await _mk_channel_bot(session, anime_doc_id=doc)
    await _mk_layout(session, bot_id=bot.id, kind="season_card", seq=0,
                     tg_message_id=55, anilist_id=911)
    cp = ChannelPost(anime_doc_id=doc, main_channel_id=-1001, main_message_id=7)
    session.add(cp)
    await session.commit()

    svc = RequestService(_container(sessionmaker, tmp_path))
    await svc.purge_all_for_anime(doc, keep_channel=False)

    async with sessionmaker() as s:
        for model in (StoragePack, DistributionBot, ChannelPost, Request):
            attr = model.anime_doc_id
            assert (await s.execute(select(model).where(attr == doc))).first() is None, model
        # ChannelLayout/BotContentPost are removed by the DistributionBot delete's
        # FK ondelete="CASCADE" in PostgreSQL. SQLite in-memory doesn't enforce
        # FKs by default, so we only assert the rows our code deletes explicitly
        # (above); the cascade is a schema guarantee verified against PostgreSQL.


# ── submit markers ───────────────────────────────────────────────────────────


async def test_submit_absent_marks_redo_only(session, sessionmaker, tmp_path,
                                              monkeypatch):
    from nekofetch.infrastructure.repositories import request_repo
    from kurosoden.shared import admin_assignment as aa
    from kurosoden.shared.redo_service import RedoService

    _seq = [1000]

    async def _fake_next_seq(self):
        _seq[0] += 1
        return _seq[0]

    async def _fake_assign(self, code, stage):
        return SimpleNamespace(admin_telegram_id=1, status="assigned")

    monkeypatch.setattr(request_repo.RequestRepository, "next_sequence",
                        _fake_next_seq)
    monkeypatch.setattr(aa.AdminAssignmentEngine, "assign", _fake_assign)

    svc = RedoService(_container(sessionmaker, tmp_path))
    plan = await svc.submit(42, "Fresh Title", "anilist:920", {"anilist_id": 920})

    assert plan.keep_channel is False
    async with sessionmaker() as s:
        req = (await s.execute(select(Request).where(
            Request.anime_doc_id == "anilist:920",
            Request.status == RequestStatus.QUEUED))).scalars().first()
        assert req is not None
        assert req.franchise_data.get("redo") is True
        assert "redo_relink" not in req.franchise_data


async def test_submit_published_marks_redo_relink(session, sessionmaker, tmp_path,
                                                  monkeypatch):
    from nekofetch.infrastructure.repositories import request_repo
    from kurosoden.shared import admin_assignment as aa
    from kurosoden.shared.redo_service import RedoService

    _seq = [2000]

    async def _fake_next_seq(self):
        _seq[0] += 1
        return _seq[0]

    async def _fake_assign(self, code, stage):
        return SimpleNamespace(admin_telegram_id=1, status="assigned")

    monkeypatch.setattr(request_repo.RequestRepository, "next_sequence",
                        _fake_next_seq)
    monkeypatch.setattr(aa.AdminAssignmentEngine, "assign", _fake_assign)

    doc = "anilist:921"
    await _create_request(session, code="REQ-921", anime_doc_id=doc,
                          status="published")
    await _mk_channel_bot(session, anime_doc_id=doc)

    svc = RedoService(_container(sessionmaker, tmp_path))
    plan = await svc.submit(42, "Published Title", doc, {"anilist_id": 921})

    assert plan.keep_channel is True
    async with sessionmaker() as s:
        req = (await s.execute(select(Request).where(
            Request.anime_doc_id == doc,
            Request.status == RequestStatus.QUEUED))).scalars().first()
        assert req is not None
        assert req.franchise_data.get("redo") is True
        assert req.franchise_data.get("redo_relink") is True
        assert req.franchise_data.get("redo_anime_doc_id") == doc


# ── relink_packs_in_place ────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self):
        self.edits = []

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.edits.append((chat_id, message_id, reply_markup))


async def test_relink_edits_each_card_with_fresh_buttons(
    session, sessionmaker, monkeypatch,
):
    from nekofetch.services import bot_content as bc
    from nekofetch.services import backup_service as bak
    from kurosoden.shared import senku_publisher as sp
    from kurosoden.shared.senku_publisher import SenkuPublisher

    doc = "anilist:930"
    bot = await _mk_channel_bot(session, anime_doc_id=doc, chat_id=-100777)
    await _mk_layout(session, bot_id=bot.id, kind="season_card", seq=0,
                     tg_message_id=101, anilist_id=930)
    # A BotContentPost that must have its button_data synced.
    session.add(BotContentPost(
        bot_id=bot.id, post_type="season_card", season=1, order=0,
        caption="cap", button_data={"links": {"480p_dual_audio": "OLD"}},
        anilist_id=930,
    ))
    await session.commit()

    entry = SimpleNamespace(anilist_id=930, format="TV")
    fake_pack = SimpleNamespace(season=1, entry_id=None)
    new_buttons = {"type": "flat", "qualities": ["480p"],
                   "links": {"480p_dual_audio": "NEW"}}

    async def _load_packs(self, _doc):
        return [fake_pack]

    async def _gather_metadata(self, _doc, *a, **k):
        return {}

    async def _walk_franchise(self, _doc, _meta):
        return {"tv": [entry], "all": [entry], "extras": []}

    async def _build_season_buttons(self, packs):
        return new_buttons if packs else None

    monkeypatch.setattr(bc.BotContentService, "_load_packs", _load_packs)
    monkeypatch.setattr(bc.BotContentService, "_gather_metadata", _gather_metadata)
    monkeypatch.setattr(bc.BotContentService, "_walk_franchise", _walk_franchise)
    monkeypatch.setattr(bc.BotContentService, "_build_season_buttons",
                        _build_season_buttons)
    monkeypatch.setattr(
        sp, "build_audio_keyboard",
        lambda button_data, fmt: f"KB:{button_data['links']['480p_dual_audio']}")

    async def _noop_backup(self, _doc):
        return None
    monkeypatch.setattr(bak.BackupService, "record_distribution_channel",
                        _noop_backup)

    client = _FakeClient()
    result = await SenkuPublisher(_container(sessionmaker)).relink_packs_in_place(
        client, doc)

    assert result["relinked"] == 1
    assert client.edits == [(-100777, 101, "KB:NEW")]
    # The stored button_data was synced to the fresh links.
    async with sessionmaker() as s:
        post = (await s.execute(select(BotContentPost).where(
            BotContentPost.bot_id == bot.id))).scalars().first()
        assert post.button_data["links"]["480p_dual_audio"] == "NEW"


async def test_relink_no_channel_is_noop(session, sessionmaker):
    from kurosoden.shared.senku_publisher import SenkuPublisher

    client = _FakeClient()
    result = await SenkuPublisher(_container(sessionmaker)).relink_packs_in_place(
        client, "anilist:404")
    assert result == {"relinked": 0, "chat_id": None}
    assert client.edits == []
