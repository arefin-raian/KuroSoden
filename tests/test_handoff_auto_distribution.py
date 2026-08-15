"""Download→distribution handoff is redo/update-aware.

A redo of an already-published title (``franchise_data["redo_relink"]``) and an
update entry (``franchise_data["update_entry"]``) must NOT get a Senku
"Ready for Distribution" task — the channel already exists and the publish
step relinks the quality buttons (redo) or appends the season card (update)
in place. These works are auto-published at the handoff so they complete
without a Senku task or a Gojo approval. Everything else keeps the normal
levi → senku handoff.

Regression for the ORB report: a redone anime that already had a distribution
channel + main-channel post was spawning a distribution task in Senku.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from kurosoden.shared.admin_assignment import AdminAssignment
from kurosoden.shared.work_service import STATUS_DONE, WorkItem, WorkService
from kurosoden.tests.helpers import (
    _create_admin_availability,
    _create_admin_assignment,
    _create_request,
)


# ── container + log silencing ────────────────────────────────────────────────


def _container(sessionmaker, *, pipeline_manager=None):
    cfg = SimpleNamespace(log_channel=SimpleNamespace())
    env = SimpleNamespace(storage_path=None)
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker, redis=None, progress=None,
        admin_client=None, config=cfg, env=env,
        pipeline_manager=pipeline_manager,
    )


class _FakeLevi:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id, photo, **kw):  # pragma: no cover
        self.sent.append((chat_id, kw.get("caption", "")))


@pytest.fixture(autouse=True)
def _silence_log(monkeypatch):
    async def _noop(self, *a, **k):
        return None

    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService.event", _noop
    )


@pytest.fixture(autouse=True)
def _no_art(monkeypatch):
    """Keep stage-assignment DMs text-only (no backdrop fetch)."""
    from kurosoden.shared import handoff as handoff_mod

    async def _no_art_fn(container, stage, code, title, franchise_json):
        return None

    monkeypatch.setattr(handoff_mod, "_stage_art", _no_art_fn)


# ── seed helpers ─────────────────────────────────────────────────────────────


async def _seed_handoff(
    session, sessionmaker, *, code="REQ-H1", doc="anilist:921",
    fd=None, levi_admin=500,
):
    """Request (+franchise data), linked work item, and an open Levi task."""
    req = await _create_request(session, code=code, anime_doc_id=doc,
                                status="processing")
    req.franchise_data = fd or {}
    await session.commit()

    svc = WorkService(sessionmaker)
    out = await svc.add_batch(levi_admin, [{"anime_title": "Handoff Anime"}])
    await svc.link(out[0].code, code)

    await _create_admin_availability(
        session, admin_telegram_id=levi_admin, admin_name="Downloader",
        assigned_bots=["levi", "senku", "gojo"],
    )
    await _create_admin_assignment(
        session, admin_telegram_id=levi_admin, request_code=code, stage="levi",
    )
    return req


async def _publish_completes_gojo(sessionmaker, raises=None):
    """Fake PublishingService.publish: run the real bookkeeping publish does.

    The real publish() ends with complete_task(code, "gojo"), which is what
    marks the linked work item done. The fake mirrors that so the test pins
    the whole chain, not just the call.
    """
    from nekofetch.services.publishing_service import PublishingService
    from kurosoden.shared.admin_assignment import AdminAssignmentEngine

    calls = []

    async def _publish(self, code, **kw):
        calls.append(code)
        if raises is not None:
            raise raises
        await AdminAssignmentEngine(sessionmaker).complete_task(code, "gojo")
        return 1

    return calls, _publish


# ── redo_relink: skip Senku, auto-publish, DM the downloader ─────────────────


async def test_redo_relink_skips_senku_and_auto_publishes(
    session, sessionmaker, monkeypatch,
):
    from nekofetch.services.publishing_service import PublishingService
    from kurosoden.shared.handoff import handoff_download_to_distribution

    code = "REQ-H1"
    await _seed_handoff(
        session, sessionmaker, code=code, fd={"redo": True, "redo_relink": True},
    )
    calls, fake = await _publish_completes_gojo(sessionmaker)
    monkeypatch.setattr(PublishingService, "publish", fake)

    levi = _FakeLevi()
    await handoff_download_to_distribution(
        _container(sessionmaker, pipeline_manager=SimpleNamespace(levi=levi)),
        code, "Handoff Anime",
    )

    # Auto-published in place — publish ran once.
    assert calls == [code]
    # No Senku distribution task was created.
    async with sessionmaker() as s:
        rows = (await s.execute(select(AdminAssignment).where(
            AdminAssignment.request_code == code,
            AdminAssignment.stage == "senku",
        ))).scalars().all()
        assert rows == []
        # Levi's task completed.
        levi_row = (await s.execute(select(AdminAssignment).where(
            AdminAssignment.request_code == code,
            AdminAssignment.stage == "levi",
        ))).scalars().first()
        assert levi_row.status == "completed"
        # The linked work finished (publish's complete_task(gojo) sync).
        w = (await s.execute(select(WorkItem).where(
            WorkItem.request_code == code))).scalars().first()
        assert w is not None
        assert w.status == STATUS_DONE
    # The downloader was told the relink happened.
    assert levi.sent and levi.sent[0][0] == 500
    assert "Redo complete" in levi.sent[0][1]


# ── update_entry: same skip-Senku auto-publish path ──────────────────────────


async def test_update_entry_skips_senku_and_auto_publishes(
    session, sessionmaker, monkeypatch,
):
    from nekofetch.services.publishing_service import PublishingService
    from kurosoden.shared.handoff import handoff_download_to_distribution

    code = "REQ-H2"
    await _seed_handoff(
        session, sessionmaker, code=code,
        fd={"update_entry": True, "anilist_id": 999},
    )
    calls, fake = await _publish_completes_gojo(sessionmaker)
    monkeypatch.setattr(PublishingService, "publish", fake)

    levi = _FakeLevi()
    await handoff_download_to_distribution(
        _container(sessionmaker, pipeline_manager=SimpleNamespace(levi=levi)),
        code, "Handoff Anime",
    )

    assert calls == [code]
    async with sessionmaker() as s:
        rows = (await s.execute(select(AdminAssignment).where(
            AdminAssignment.request_code == code,
            AdminAssignment.stage == "senku",
        ))).scalars().all()
        assert rows == []
        w = (await s.execute(select(WorkItem).where(
            WorkItem.request_code == code))).scalars().first()
        assert w.status == STATUS_DONE
    assert levi.sent and "Update landed" in levi.sent[0][1]


# ── normal work: unchanged levi → senku handoff ──────────────────────────────


async def test_normal_work_still_hands_to_senku(session, sessionmaker, monkeypatch):
    from nekofetch.services.publishing_service import PublishingService
    from kurosoden.shared.handoff import handoff_download_to_distribution

    code = "REQ-H3"
    # Fresh anime — no redo/update markers.
    await _seed_handoff(session, sessionmaker, code=code, fd={"anilist_id": 777})

    calls = []
    async def _publish(self, code, **kw):
        calls.append(code)
        return 1
    monkeypatch.setattr(PublishingService, "publish", _publish)

    await handoff_download_to_distribution(
        _container(sessionmaker, pipeline_manager=SimpleNamespace(levi=_FakeLevi())),
        code, "Handoff Anime",
    )

    # Senku got the distribution task; publish was NOT auto-run.
    assert calls == []
    async with sessionmaker() as s:
        rows = (await s.execute(select(AdminAssignment).where(
            AdminAssignment.request_code == code,
            AdminAssignment.stage == "senku",
        ))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status in ("assigned", "in_progress", "offered")


# ── auto-publish failure: recoverable via a Gojo task ────────────────────────


async def test_auto_publish_failure_falls_back_to_gojo(
    session, sessionmaker, monkeypatch,
):
    from nekofetch.services.publishing_service import PublishingService
    from kurosoden.shared.handoff import handoff_download_to_distribution

    code = "REQ-H4"
    await _seed_handoff(
        session, sessionmaker, code=code, fd={"redo": True, "redo_relink": True},
    )
    calls, fake = await _publish_completes_gojo(
        sessionmaker, raises=RuntimeError("telegram down"),
    )
    monkeypatch.setattr(PublishingService, "publish", fake)

    await handoff_download_to_distribution(
        _container(sessionmaker, pipeline_manager=SimpleNamespace(levi=_FakeLevi())),
        code, "Handoff Anime",
    )

    # The failed auto-publish surfaced as a Gojo publish task (manual retry).
    assert calls == [code]
    async with sessionmaker() as s:
        rows = (await s.execute(select(AdminAssignment).where(
            AdminAssignment.request_code == code,
            AdminAssignment.stage == "gojo",
        ))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status in ("assigned", "in_progress", "offered")
        # No Senku task either way.
        senku = (await s.execute(select(AdminAssignment).where(
            AdminAssignment.request_code == code,
            AdminAssignment.stage == "senku",
        ))).scalars().all()
        assert senku == []


# ── voice messages are HTML-safe with codes ──────────────────────────────────


def test_redo_relinked_voice_includes_title_and_code():
    from kurosoden.shared import levi_voice as V

    msg = V.redo_relinked("Orb <3", "REQ-1079")
    assert "Orb &lt;3" in msg
    assert "REQ-1079" in msg
    assert "Redo complete" in msg


def test_update_appended_voice_includes_title_and_code():
    from kurosoden.shared import levi_voice as V

    msg = V.update_appended("Takopi", "REQ-1080")
    assert "Takopi" in msg
    assert "REQ-1080" in msg
    assert "Update landed" in msg
