"""Tests for the four production fixes:

  1. nvenc runtime probe → CPU fallback (``_transcode``).
  2. Series-wide watermark burn decision (``stages._fully_text_branded``).
  3. ``RequestService.reset_source`` — Change-Source total reset.
  4. Cancel routing (NotificationService → Lelouch) + Levi card finalized-latch.

Live ffmpeg / Telegram can't run here, so encoder + card flows are exercised
with fakes; the DB teardown is exercised against the in-memory schema.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.domain.enums import AudioType, JobStatus, RequestStatus
from nekofetch.infrastructure.database.postgres.models import (
    ChannelPost,
    DistributionBot,
    DownloadJob,
    MediaFile,
    PublishedPostBackup,
    StoragePack,
)
from kurosoden.tests.helpers import _create_request

aio = pytest.mark.asyncio


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeRedis:
    """Minimal async Redis: records every key ever set (set_log) + a live store."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.set_log: list[str] = []

    async def set(self, key, value=None, ex=None, **kw):
        self.store[key] = value if value is not None else "1"
        self.set_log.append(key)

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]


class _FakeClient:
    def __init__(self):
        self.sent: list[tuple] = []
        self.deleted: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, ids))


@pytest.fixture(autouse=True)
def _silence_log(monkeypatch):
    async def _noop(self, *a, **k):
        return None
    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService.event", _noop
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. nvenc runtime probe → CPU fallback
# ═══════════════════════════════════════════════════════════════════════════

@aio
async def test_hw_encoder_usable_false_when_probe_fails_and_is_cached(monkeypatch):
    import nekofetch.sources._transcode as tc
    tc._HW_PROBE_CACHE.clear()
    calls = {"n": 0}

    def _fake_run(cmd, **kw):
        calls["n"] += 1
        return SimpleNamespace(returncode=1, stdout="", stderr="no cuda")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run)

    venc = ["-c:v", "h264_nvenc", "-pix_fmt", "yuv420p", "-preset", "p4", "-cq", "20"]
    assert await tc._hw_encoder_usable("ffmpeg", "h264_nvenc", venc) is False
    # Second call for the same (ffmpeg, encoder) is served from cache — no re-probe.
    assert await tc._hw_encoder_usable("ffmpeg", "h264_nvenc", venc) is False
    assert calls["n"] == 1


@aio
async def test_hw_encoder_usable_true_when_probe_succeeds(monkeypatch):
    import nekofetch.sources._transcode as tc
    tc._HW_PROBE_CACHE.clear()
    monkeypatch.setattr(tc.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    venc = ["-c:v", "h264_nvenc", "-pix_fmt", "yuv420p"]
    assert await tc._hw_encoder_usable("ffmpeg", "h264_nvenc", venc) is True


@aio
async def test_ladder_drops_nvenc_when_unusable(monkeypatch):
    """Compiled-in but unusable nvenc must NOT appear in the ladder — libx264 leads."""
    import nekofetch.sources._transcode as tc
    tc._HW_PROBE_CACHE.clear()
    # Everything reports "compiled in", but the runtime probe says the GPU is dead.
    monkeypatch.setattr(tc, "_encoder_available", lambda ffmpeg, enc: True)

    async def _unusable(ffmpeg, encoder, venc):
        return False
    monkeypatch.setattr(tc, "_hw_encoder_usable", _unusable)

    ladder = await tc.build_watermark_encode_args(
        "ffmpeg", Path("in.mkv"), Path("out.mkv"), "drawtext=text=x", [],
        crf=20, preset="fast", threads=4, fast=True, is_10bit=False,
    )
    assert [enc for enc, _ in ladder] == ["libx264"]


@aio
async def test_ladder_keeps_nvenc_when_usable(monkeypatch):
    import nekofetch.sources._transcode as tc
    tc._HW_PROBE_CACHE.clear()
    monkeypatch.setattr(tc, "_encoder_available",
                        lambda ffmpeg, enc: enc in ("h264_nvenc", "libx264"))

    async def _usable(ffmpeg, encoder, venc):
        return True
    monkeypatch.setattr(tc, "_hw_encoder_usable", _usable)

    ladder = await tc.build_watermark_encode_args(
        "ffmpeg", Path("in.mkv"), Path("out.mkv"), "drawtext=text=x", [],
        crf=20, preset="fast", threads=4, fast=True, is_10bit=False,
    )
    assert [enc for enc, _ in ladder] == ["h264_nvenc", "libx264"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. Series-wide burn decision
# ═══════════════════════════════════════════════════════════════════════════

def test_fully_text_branded_predicate():
    from nekofetch.services.processing.stages import _fully_text_branded
    assert _fully_text_branded([{"codec": "ass"}, {"codec": "subrip"}]) is True
    assert _fully_text_branded([{"codec": "ass"}, {"codec": "hdmv_pgs_subtitle"}]) is False
    assert _fully_text_branded([]) is False           # no subs → needs burn
    assert _fully_text_branded([{"codec": "dvd_subtitle"}]) is False


def test_series_needs_burn_if_any_file_unbranded():
    from nekofetch.services.processing.stages import _fully_text_branded
    # 3 fully-branded episodes + 1 with a PGS track → the whole series must burn.
    per_file = [
        [{"codec": "ass"}],
        [{"codec": "subrip"}],
        [{"codec": "ass"}, {"codec": "hdmv_pgs_subtitle"}],
        [{"codec": "ass"}],
    ]
    series_needs_burn = any(not _fully_text_branded(s) for s in per_file)
    assert series_needs_burn is True
    # All fully branded → no series burn (every file skipped, unchanged behavior).
    assert any(not _fully_text_branded(s)
               for s in [[{"codec": "ass"}], [{"codec": "srt"}]]) is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. reset_source — Change-Source total reset
# ═══════════════════════════════════════════════════════════════════════════

def _reset_container(sessionmaker, tmp_path, redis, admin):
    cfg = SimpleNamespace(
        log_channel=SimpleNamespace(),
        downloads=SimpleNamespace(concurrent_downloads=1),
        storage_channel=SimpleNamespace(enabled=False, channel_id=0),
    )
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker, redis=redis, progress=None,
        admin_client=admin, config=cfg,
        env=SimpleNamespace(storage_path=tmp_path),
        pipeline_manager=None,
    )


@aio
async def test_prune_work_dir_removes_both_output_and_ddl_cache(tmp_path):
    # DDL extracts archives + MKVs into work/<code>/.ddl (keyed by CODE), while
    # processed outputs live in work/<anime_doc_id>. A cleanup that swept only the
    # doc-id folder leaked the raw DDL files. _prune_work_dir must remove BOTH.
    from nekofetch.services.request_service import RequestService

    work = tmp_path / "work"
    out_dir = work / "132405"                       # outputs (anime_doc_id)
    ddl_mkv = work / "REQ-1094" / ".ddl" / "abc123" / "Episode 01.mkv"  # DDL cache (code)
    out_dir.mkdir(parents=True)
    (out_dir / "watermarked.mkv").write_bytes(b"x")
    ddl_mkv.parent.mkdir(parents=True)
    ddl_mkv.write_bytes(b"x" * 10)
    # metadata/ is a sibling that must NEVER be touched.
    meta = tmp_path / "metadata" / "132405"
    meta.mkdir(parents=True)
    (meta / "anilist.json").write_bytes(b"{}")

    container = _reset_container(None, tmp_path, _FakeRedis(), _FakeClient())
    await RequestService(container)._prune_work_dir("132405", "REQ-1094")

    assert not out_dir.exists(), "output folder should be removed"
    assert not (work / "REQ-1094").exists(), "DDL cache (work/<code>) should be removed"
    assert meta.exists() and (meta / "anilist.json").exists(), "metadata must survive"


def test_cleanup_local_files_targets_ddl_cache(tmp_path):
    # The post-upload sweep must also delete the code-keyed DDL cache.
    from nekofetch.services.publishing_service import PublishingService

    work = tmp_path / "work"
    ddl = work / "REQ-1094" / ".ddl" / "d1"
    ddl.mkdir(parents=True)
    (ddl / "Episode 01.mkv").write_bytes(b"x")
    out = work / "132405"
    out.mkdir(parents=True)
    (out / "ep1.mkv").write_bytes(b"x")

    container = SimpleNamespace(env=SimpleNamespace(storage_path=tmp_path))
    snapshot = [{"path": str(out / "ep1.mkv")}]
    PublishingService(container)._cleanup_local_files(
        snapshot, code="REQ-1094", title="My Dress-Up Darling")

    assert not (work / "REQ-1094").exists(), "DDL cache must be swept after upload"
    assert not out.exists(), "output folder must be swept after upload"


@aio
async def test_reset_source_full_teardown(session, sessionmaker, tmp_path, monkeypatch):
    from nekofetch.services.request_service import RequestService

    doc = "anilist:4242"
    # Seed a request mid-processing: a running job + a file + an uploaded pack +
    # (as if already published) channel rows.
    req = await _create_request(session, code="REQ-RS1", anime_doc_id=doc,
                                anime_title="Reset Me", source="ddl")
    # Give it a stale DDL ref + mapping so we can assert both are cleared.
    req.source_ref = "https://x/archive.zip"
    req.franchise_data = {"_torrent_mapping": {"a": 1}, "title": "Reset Me"}
    req.status = RequestStatus.APPROVED
    await session.commit()
    job = DownloadJob(request_id=req.id, status=JobStatus.RUNNING)
    session.add(job)
    await session.commit()
    session.add(MediaFile(job_id=job.id, anime_doc_id=doc, season=1, episode=1,
                          resolution="1080p", audio=AudioType.SUBBED))
    session.add(StoragePack(anime_doc_id=doc, anime_title="Reset Me", season=1,
                            resolution="1080p", audio=AudioType.SUBBED, channel_id=-100999,
                            start_message_id=10, end_message_id=16, file_count=6,
                            header_message_id=9, file_message_ids=[11, 12, 13]))
    session.add(ChannelPost(anime_doc_id=doc, main_channel_id=-100, main_message_id=5))
    session.add(PublishedPostBackup(anime_doc_id=doc, caption="c"))
    session.add(DistributionBot(anime_doc_id=doc, name="dist", encrypted_token="tok",
                                is_channel=True))
    await session.commit()
    job_id = job.id

    redis = _FakeRedis()
    admin = _FakeClient()
    container = _reset_container(sessionmaker, tmp_path, redis, admin)

    out = await RequestService(container).reset_source("REQ-RS1")
    assert out["packs"] == 1 and out["files"] == 1

    # Worker was signalled to STOP (source_abort + cancel) and the card latched.
    assert f"nf:job:{job_id}:source_abort" in redis.set_log
    assert f"nf:job:{job_id}:cancel" in redis.set_log
    assert f"nf:job:{job_id}:finalized" in redis.set_log
    # The uploaded pack's channel messages were deleted.
    assert admin.deleted, "pack channel messages must be deleted"

    async with sessionmaker() as s:
        # Everything downstream of the source pick is gone…
        assert (await s.execute(select(StoragePack).where(
            StoragePack.anime_doc_id == doc))).first() is None
        assert (await s.execute(select(MediaFile).where(
            MediaFile.job_id == job_id))).first() is None
        assert (await s.execute(select(DownloadJob).where(
            DownloadJob.id == job_id))).first() is None
        assert (await s.execute(select(ChannelPost).where(
            ChannelPost.anime_doc_id == doc))).first() is None
        assert (await s.execute(select(PublishedPostBackup).where(
            PublishedPostBackup.anime_doc_id == doc))).first() is None
        assert (await s.execute(select(DistributionBot).where(
            DistributionBot.anime_doc_id == doc))).first() is None
        # …but the request itself SURVIVES, reset for a fresh source pick.
        from nekofetch.infrastructure.repositories.request_repo import (
            RequestRepository,
        )
        kept = await RequestRepository(s).get_by_code("REQ-RS1")
        assert kept is not None
        assert kept.status == RequestStatus.PENDING
        assert kept.source_ref is None                       # "as if never picked"
        assert not (kept.franchise_data or {}).get("_torrent_mapping")


@aio
async def test_reset_source_no_jobs_is_safe(session, sessionmaker, tmp_path):
    """A request whose source was picked but never started must still reset cleanly."""
    from nekofetch.services.request_service import RequestService
    r = await _create_request(session, code="REQ-RS2", anime_doc_id="anilist:5",
                              anime_title="Idle", source="ddl")
    r.source_ref = "x"
    await session.commit()
    container = _reset_container(sessionmaker, tmp_path, _FakeRedis(), _FakeClient())
    out = await RequestService(container).reset_source("REQ-RS2")
    assert out["files"] == 0 and out["packs"] == 0
    async with sessionmaker() as s:
        from nekofetch.infrastructure.repositories.request_repo import (
            RequestRepository,
        )
        kept = await RequestRepository(s).get_by_code("REQ-RS2")
        assert kept.status == RequestStatus.PENDING and kept.source_ref is None


# ═══════════════════════════════════════════════════════════════════════════
# 4a. NotificationService routes through Lelouch, not the admin bot
# ═══════════════════════════════════════════════════════════════════════════

@aio
async def test_notification_prefers_lelouch():
    from nekofetch.services.notification_service import NotificationService
    lelouch, admin = _FakeClient(), _FakeClient()
    container = SimpleNamespace(
        pipeline_manager=SimpleNamespace(lelouch=lelouch),
        admin_client=admin,
    )
    await NotificationService(container).request_removed(123, "T", "REQ-9")
    assert lelouch.sent and lelouch.sent[0][0] == 123
    assert not admin.sent, "must NOT send from the NekoFetch admin bot"


@aio
async def test_notification_falls_back_to_admin_without_pipeline():
    from nekofetch.services.notification_service import NotificationService
    admin = _FakeClient()
    container = SimpleNamespace(pipeline_manager=None, admin_client=admin)
    await NotificationService(container).request_published(7, "T", "REQ-1")
    assert admin.sent and admin.sent[0][0] == 7


# ═══════════════════════════════════════════════════════════════════════════
# 4b. Levi card finalized-latch
# ═══════════════════════════════════════════════════════════════════════════

@aio
async def test_finalized_latch_roundtrip():
    from kurosoden.bots.levi.handlers import progress_monitor as pm
    container = SimpleNamespace(redis=_FakeRedis())
    assert await pm._is_finalized(container, 77) is False
    await pm._mark_finalized(container, 77)
    assert await pm._is_finalized(container, 77) is True


@aio
async def test_finalize_cancelled_card_sets_latch():
    from kurosoden.bots.levi.handlers import progress_monitor as pm
    redis = _FakeRedis()
    # No pipeline_manager → no live edit / no advance; we only assert the latch.
    container = SimpleNamespace(redis=redis, pipeline_manager=None, progress=None)
    await pm.finalize_cancelled_card(container, 88, title="T", code="REQ-1")
    assert await pm._is_finalized(container, 88) is True
