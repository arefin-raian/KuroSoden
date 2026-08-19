"""Phase 2 coverage: real probed audio languages are PERSISTED end-to-end.

Two links in the chain:

  1. VerifyStage._correct_audio_from_tracks records the file's real per-stream
     languages onto MediaFile.audio_langs — but ONLY when every track carries a
     real tag. Partial/"und" tagging is left null so labels fall back to the
     enum map (owner: assume Eng/Jpn/Hin when unknown).

  2. StorageChannelService._persist writes the pack-level audio_langs union to
     StoragePack, and a later untagged reprocess must not clobber a good value.

These are the writes; Phase 3 wires the reads.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.domain.enums import AudioType
from nekofetch.infrastructure.database.postgres.models import StoragePack
from nekofetch.services.processing import stages as stages_mod
from nekofetch.services.processing.stages import VerifyStage
from nekofetch.services.storage_channel_service import PackKey, StorageChannelService

pytestmark = pytest.mark.asyncio


# ── VerifyStage: probe → MediaFile.audio_langs ────────────────────────────────

def _verify_stage():
    # VerifyStage only touches self.c in code paths we don't exercise here; a
    # bare object with a container attribute is enough for _correct_audio_from_tracks.
    return VerifyStage(SimpleNamespace())


def _file(audio=AudioType.SUBBED):
    # A stand-in for the live ORM MediaFile row: the method only reads/writes
    # .audio and .audio_langs.
    return SimpleNamespace(audio=audio, audio_langs=None)


async def _run_correct(monkeypatch, tracks):
    """Drive _correct_audio_from_tracks with a faked ffprobe track-language list."""
    async def fake_track_langs(ffprobe, path, select):
        return tracks

    monkeypatch.setattr(stages_mod, "_track_langs", fake_track_langs)
    stage = _verify_stage()
    ctx = SimpleNamespace(notes=[])
    f = _file()
    from pathlib import Path
    await stage._correct_audio_from_tracks(
        ctx, f, "ffprobe", Path("Show S01E01.mkv"), AudioType,
    )
    return f


async def test_fully_tagged_multi_records_real_languages(monkeypatch):
    """Three distinct real tags → MULTI enum AND audio_langs=[eng, jpn, kor]."""
    f = await _run_correct(monkeypatch, ["eng", "jpn", "kor"])
    assert f.audio == AudioType.MULTI
    assert f.audio_langs == ["eng", "jpn", "kor"]  # sorted distinct


async def test_fully_tagged_dual_records_two_languages(monkeypatch):
    f = await _run_correct(monkeypatch, ["eng", "jpn"])
    assert f.audio == AudioType.DUAL_AUDIO
    assert f.audio_langs == ["eng", "jpn"]


async def test_partial_tagging_leaves_langs_null(monkeypatch):
    """One track tagged, one blank → can't trust it → audio_langs stays null.

    (The enum is still upgraded to dual on track COUNT — that logic is unchanged.)
    """
    f = await _run_correct(monkeypatch, ["eng", ""])
    assert f.audio == AudioType.DUAL_AUDIO  # 2 tracks → dual, as before
    assert f.audio_langs is None            # but languages untrustworthy → null


async def test_und_tag_leaves_langs_null(monkeypatch):
    """'und' (undetermined) is not a real language → langs stay null."""
    f = await _run_correct(monkeypatch, ["eng", "und"])
    assert f.audio_langs is None


async def test_single_track_untouched(monkeypatch):
    """One audio track → early return; neither audio nor langs change."""
    f = await _run_correct(monkeypatch, ["eng"])
    assert f.audio == AudioType.SUBBED  # unchanged
    assert f.audio_langs is None


# ── StorageChannelService._persist: pack-level audio_langs ────────────────────

def _storage_service(sessionmaker):
    container = SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        config=SimpleNamespace(storage_channel=SimpleNamespace(enabled=False, channel_id=0)),
    )
    return StorageChannelService(container)


async def test_persist_writes_pack_audio_langs(sessionmaker, session):
    svc = _storage_service(sessionmaker)
    key = PackKey("anilist:1", 1, "1080p", AudioType.MULTI)
    pack = await svc._persist(
        key, title="Show", channel_id=-100,
        header_message_id=1, start_message_id=2, end_message_id=9,
        file_message_ids=[2, 3, 4], ingest_method="uploaded",
        episode_from=1, episode_to=3, caption="cap",
        audio_langs=["en", "ja", "ko"],
    )
    assert pack.audio_langs == ["en", "ja", "ko"]

    # Reload from the DB to prove it actually persisted (not just the ORM object).
    from sqlalchemy import select
    row = (await session.execute(
        select(StoragePack).where(StoragePack.anime_doc_id == "anilist:1")
    )).scalar_one()
    assert row.audio_langs == ["en", "ja", "ko"]


async def test_persist_untagged_reprocess_does_not_clobber(sessionmaker, session):
    """A first upload records languages; a later untagged reprocess keeps them."""
    svc = _storage_service(sessionmaker)
    key = PackKey("anilist:2", 1, "1080p", AudioType.MULTI)

    await svc._persist(
        key, title="Show", channel_id=-100,
        header_message_id=1, start_message_id=2, end_message_id=4,
        file_message_ids=[2, 3], ingest_method="uploaded",
        episode_from=1, episode_to=2, caption="cap",
        audio_langs=["en", "ja", "ko"],
    )
    # Reprocess the SAME pack with no languages (e.g. an untagged re-encode).
    await svc._persist(
        key, title="Show", channel_id=-100,
        header_message_id=1, start_message_id=2, end_message_id=6,
        file_message_ids=[2, 3, 5], ingest_method="uploaded",
        episode_from=1, episode_to=3, caption="cap",
        audio_langs=None,
    )
    from sqlalchemy import select
    row = (await session.execute(
        select(StoragePack).where(StoragePack.anime_doc_id == "anilist:2")
    )).scalar_one()
    assert row.audio_langs == ["en", "ja", "ko"]  # preserved, not nulled


async def test_persist_without_langs_leaves_null(sessionmaker, session):
    """Indexed packs (no ffprobe pass) legitimately persist a null — enum fallback."""
    svc = _storage_service(sessionmaker)
    key = PackKey("anilist:3", 1, "720p", AudioType.DUAL_AUDIO)
    pack = await svc._persist(
        key, title="Show", channel_id=-100,
        header_message_id=1, start_message_id=2, end_message_id=3,
        file_message_ids=[2], ingest_method="indexed",
        episode_from=1, episode_to=1, caption="cap",
    )
    assert pack.audio_langs is None
