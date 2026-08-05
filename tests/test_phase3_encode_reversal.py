"""Phase-3 tests: reversed encode logic (download all qualities, encode only missing).

Covers the coordinated changes wired up in Phase 3:

* :func:`nekofetch.sources._torrent.group_variants` collapses a multi-resolution
  release into one entry per real (season, episode), each exposing a sibling
  ``files`` list so nyaa/ddl can emit one VideoVariant per tier.
* :class:`nekofetch.sources.nyaa.NyaaSource.get_variants` returns one variant per
  resolution the torrent actually ships (no 1080p-only collapse), so the download
  worker fetches each tier and EncodeStage fills only genuinely-missing ones.
* :class:`nekofetch.services.processing.stages.EncodeStage` skips any
  ``encode_heights`` tier already present on disk for a given (season, episode,
  audio) unit, deriving only the genuinely-missing tiers from the highest present.
  Net: 1080+720 present → encode 480 only; 720 only → encode 480; all three → none.

The torrent-naming / variant tests are pure (no network / Postgres / Container),
but EncodeStage requires a minimal Container stub + fake MediaFile rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nekofetch.domain.enums import AudioType
from nekofetch.sources._torrent import group_variants, order_episodes


def test_group_variants_collapses_per_episode():
    # A multi-quality release: ep1 in 1080p+720p, ep2 in 1080p only. The old
    # order_episodes(prefer_resolution=1080) collapsed them down to 2 files total
    # (one file per episode). The new model groups per real (season, episode),
    # keeping EVERY quality as a sibling file so get_variants can enumerate them.
    files = [
        {"name": "Show S01E01 1080p.mkv", "index": 1, "length": 1_000_000_000,
         "path": "/torrent/Show S01E01 1080p.mkv"},
        {"name": "Show S01E01 720p.mkv", "index": 2, "length": 500_000_000,
         "path": "/torrent/Show S01E01 720p.mkv"},
        {"name": "Show S01E02 1080p.mkv", "index": 3, "length": 1_000_000_000,
         "path": "/torrent/Show S01E02 1080p.mkv"},
    ]
    ordered = order_episodes(files)
    groups = group_variants(ordered)

    # Two episodes (not three); each is keyed by its real episode number.
    assert len(groups) == 2
    assert {g["number"] for g in groups} == {1, 2}
    assert all(g["season"] == 1 for g in groups)

    by_number = {g["number"]: g for g in groups}
    ep1 = by_number[1]
    ep2 = by_number[2]

    # Ep1 keeps BOTH resolution files; ep2 keeps its one.
    assert len(ep1["files"]) == 2
    assert sorted(f["resolution"] for f in ep1["files"]) == ["1080p", "720p"]
    assert len(ep2["files"]) == 1
    assert ep2["files"][0]["resolution"] == "1080p"

    # Files are sorted highest-resolution first (so files[0] is the natural source).
    assert ep1["files"][0]["resolution"] == "1080p"


async def test_nyaa_get_variants_emits_all_resolutions():
    from nekofetch.sources.nyaa import NyaaSource

    # A ref built by the new grouped get_episodes: one episode, two sibling files.
    ref = json.dumps({
        "torrent_url": "https://nyaa.example/dl/123.torrent",
        "audio_kind": "dual",
        "files": [
            {"index": 1, "path": "/t/Ep01 1080p.mkv", "name": "Ep01 1080p.mkv",
             "length": 1_000_000_000, "resolution": "1080p"},
            {"index": 2, "path": "/t/Ep01 720p.mkv", "name": "Ep01 720p.mkv",
             "length": 500_000_000, "resolution": "720p"},
        ],
        # Back-compat single-file fields (primary = highest res).
        "file_index": 1,
        "path": "/t/Ep01 1080p.mkv",
        "name": "Ep01 1080p.mkv",
        "length": 1_000_000_000,
        "resolution": "1080p",
        "kind": "episode",
        "season": 1,
        "episode": 1,
    })

    src = NyaaSource()
    variants = await src.get_variants(ref)

    # Two variants (one per resolution); both share the same audio (dual).
    assert len(variants) == 2
    assert sorted(v.resolution for v in variants) == ["1080p", "720p"]
    assert all(v.audio == AudioType.DUAL_AUDIO for v in variants)

    # Each variant's ref points at its OWN file (distinct file_index/path).
    paths = {json.loads(v.source_ref)["path"] for v in variants}
    assert paths == {"/t/Ep01 1080p.mkv", "/t/Ep01 720p.mkv"}


@pytest.mark.asyncio
async def test_encode_stage_skip_tiers_already_on_disk(tmp_path: Path):
    """EncodeStage skips any encode_heights tier already present for a given
    (season, episode, audio) unit, deriving only genuinely-missing tiers.
    Case: 1080+720 on disk → encode 480 only."""
    from nekofetch.infrastructure.database.postgres.models import MediaFile
    from nekofetch.services.processing.base import StageContext
    from nekofetch.services.processing.stages import EncodeStage

    # Two files on disk: ep1 in both 1080p and 720p (already downloaded).
    f1080 = tmp_path / "ep1_1080p.mkv"
    f720 = tmp_path / "ep1_720p.mkv"
    f1080.write_bytes(b"x" * 1000)
    f720.write_bytes(b"x" * 500)

    # Fake MediaFile rows (no DB session; EncodeStage reads them from ctx.files).
    mf1080 = MediaFile(
        job_id=1, anime_doc_id="anilist:999", season=1, episode=1,
        resolution="1080p", audio=AudioType.DUAL_AUDIO,
        local_path=str(f1080), final_name=f1080.name, size_bytes=1000,
    )
    mf720 = MediaFile(
        job_id=1, anime_doc_id="anilist:999", season=1, episode=1,
        resolution="720p", audio=AudioType.DUAL_AUDIO,
        local_path=str(f720), final_name=f720.name, size_bytes=500,
    )

    # Minimal Container stub (EncodeStage needs config.processing.encode* +
    # config.downloads.concurrent_downloads for thread calc + redis/progress).
    cfg = SimpleNamespace(
        processing=SimpleNamespace(
            encode=True, encode_heights=[720, 480], encode_preset="faster",
        ),
        downloads=SimpleNamespace(concurrent_downloads=5),
    )
    container = SimpleNamespace(config=cfg, redis=None, progress=None)
    stage = EncodeStage(container)

    # Fake Request (source must be in _TORRENT_SOURCES so EncodeStage runs).
    req = SimpleNamespace(source="nyaa", code="REQ-TEST", anime_title="Test")
    ctx = StageContext(job_id=1, request=req, files=[mf1080, mf720])

    # Mock _push_stage_progress and the encode helper so we don't actually transcode.
    async def _noop(*a, **k):
        pass

    import nekofetch.services.processing.stages as stages_mod
    stages_mod._push_stage_progress = AsyncMock(side_effect=_noop)

    # Mock _encode to skip the real ffmpeg call; just touch the output file.
    async def _fake_encode(src, out, height, crf, **kw):
        out.write_bytes(b"x" * 100)

    import nekofetch.sources._transcode as transcode_mod
    transcode_mod._encode = AsyncMock(side_effect=_fake_encode)

    # Mock find_ffmpeg/find_ffprobe.
    import nekofetch.sources._hls as hls_mod
    hls_mod.find_ffmpeg = lambda: "ffmpeg"
    hls_mod.find_ffprobe = lambda: "ffprobe"

    # Mock _ffprobe_ok to always pass.
    async def _ok_probe(probe, path):
        return True, None
    stages_mod._ffprobe_ok = AsyncMock(side_effect=_ok_probe)

    await stage.process(ctx)

    # 720p was already on disk → skipped (note in ctx.notes).
    # 480p was missing → derived (new MediaFile row added to ctx.files).
    assert any("already downloaded" in note and "720p" in note for note in ctx.notes)
    new_480 = [f for f in ctx.files if f.resolution == "480p"]
    assert len(new_480) == 1
    assert new_480[0].season == 1 and new_480[0].episode == 1
    # No 720p rendition row should have been added (it was skipped).
    assert sum(1 for f in ctx.files if f.resolution == "720p") == 1  # the original


@pytest.mark.asyncio
async def test_encode_stage_all_tiers_present_encode_nothing(tmp_path: Path):
    """When all encode_heights tiers are already on disk, EncodeStage does nothing
    (the reversal: don't re-derive what we downloaded)."""
    from nekofetch.infrastructure.database.postgres.models import MediaFile
    from nekofetch.services.processing.base import StageContext
    from nekofetch.services.processing.stages import EncodeStage

    # Three files on disk: 1080p, 720p, 480p (all tiers present).
    f1080 = tmp_path / "ep1_1080p.mkv"
    f720 = tmp_path / "ep1_720p.mkv"
    f480 = tmp_path / "ep1_480p.mkv"
    f1080.write_bytes(b"x" * 1000)
    f720.write_bytes(b"x" * 500)
    f480.write_bytes(b"x" * 200)

    mf1080 = MediaFile(
        job_id=1, anime_doc_id="anilist:999", season=1, episode=1,
        resolution="1080p", audio=AudioType.DUAL_AUDIO,
        local_path=str(f1080), final_name=f1080.name, size_bytes=1000,
    )
    mf720 = MediaFile(
        job_id=1, anime_doc_id="anilist:999", season=1, episode=1,
        resolution="720p", audio=AudioType.DUAL_AUDIO,
        local_path=str(f720), final_name=f720.name, size_bytes=500,
    )
    mf480 = MediaFile(
        job_id=1, anime_doc_id="anilist:999", season=1, episode=1,
        resolution="480p", audio=AudioType.DUAL_AUDIO,
        local_path=str(f480), final_name=f480.name, size_bytes=200,
    )

    cfg = SimpleNamespace(
        processing=SimpleNamespace(
            encode=True, encode_heights=[720, 480], encode_preset="faster",
        ),
        downloads=SimpleNamespace(concurrent_downloads=5),
    )
    container = SimpleNamespace(config=cfg, redis=None, progress=None)
    stage = EncodeStage(container)

    req = SimpleNamespace(source="nyaa", code="REQ-TEST", anime_title="Test")
    ctx = StageContext(job_id=1, request=req, files=[mf1080, mf720, mf480])

    async def _noop(*a, **k):
        pass
    import nekofetch.services.processing.stages as stages_mod
    stages_mod._push_stage_progress = AsyncMock(side_effect=_noop)

    await stage.process(ctx)

    # Both 720p and 480p were already present → both skipped (notes confirm).
    assert any("already downloaded" in note and "720p" in note for note in ctx.notes)
    assert any("already downloaded" in note and "480p" in note for note in ctx.notes)
    # No new rows added (ctx.files still has only the original three).
    assert len(ctx.files) == 3
