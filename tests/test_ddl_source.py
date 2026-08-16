"""Phase-1 tests for the DDL archive source.

Covers the three moving parts wired up in Phase 1:

* :func:`nekofetch.sources._archive.extract_archive` pulls video files out of a
  plain ``.zip`` with no external tooling (7-Zip is only needed for rar/7z).
* :class:`nekofetch.sources.ddl.DdlSource.get_episodes` extracts an archive and
  orders EP1..EPN while KEEPING every quality (no 1080p-only collapse), so each
  tier downloads and only genuinely-missing tiers get encoded downstream.
* the source registry activates and resolves ``"ddl"`` to :class:`DdlSource`.

Network is never touched — the archive fetch is monkeypatched to copy a local
zip fixture, and the extract cache is redirected under ``tmp_path``.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

from nekofetch.sources._archive import extract_archive
from nekofetch.sources.ddl import DdlSource
from nekofetch.sources.registry import build_default_registry


def _make_zip(path: Path, names: list[str]) -> Path:
    """Write a zip containing each ``names`` entry (tiny placeholder content)."""
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"x")
    return path


async def test_extract_archive_zip_returns_only_videos(tmp_path: Path) -> None:
    archive = _make_zip(
        tmp_path / "pack.zip",
        ["Show S01E01 1080p.mkv", "Show S01E02 1080p.mkv", "readme.nfo"],
    )
    videos = await extract_archive(archive, tmp_path / "out")

    assert {v.name for v in videos} == {
        "Show S01E01 1080p.mkv",
        "Show S01E02 1080p.mkv",
    }
    assert all(v.exists() for v in videos)


async def test_extract_archive_recurses_into_nested_zip(tmp_path: Path) -> None:
    # Release hosts (MoviesMod et al.) wrap the video in an INNER archive, so a
    # first-pass extract yields only another zip — the Akudama Drive failure.
    inner = _make_zip(tmp_path / "inner.zip", ["Akudama Drive S01E01 480p.mkv"])
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, arcname="Akudama.Drive.S01.480p/inner.zip")

    videos = await extract_archive(outer, tmp_path / "out")
    assert [v.name for v in videos] == ["Akudama Drive S01E01 480p.mkv"]
    assert videos[0].exists()


async def test_extract_archive_rejects_html_interstitial(tmp_path: Path) -> None:
    # An expired worker link returns an HTML page, not a file. The old code failed
    # deep in 7-Zip with a misleading "no video files"; now it says so plainly.
    import pytest

    fake = tmp_path / "2b8ddef4.zip"
    fake.write_bytes(b"<!DOCTYPE html><html><body>Link expired</body></html>")
    with pytest.raises(RuntimeError, match="not an archive"):
        await extract_archive(fake, tmp_path / "out")


async def test_extract_archive_no_video_message_mentions_nested(tmp_path: Path) -> None:
    # A real archive with no video anywhere → actionable error (not a crash).
    import pytest

    archive = _make_zip(tmp_path / "subsonly.zip", ["episode.ass", "readme.nfo"])
    with pytest.raises(RuntimeError, match="no video files found"):
        await extract_archive(archive, tmp_path / "out")


async def test_get_episodes_keeps_every_quality(tmp_path: Path, monkeypatch) -> None:
    # Episode 1 ships in both 1080p and 720p; episode 2 only in 1080p. The reversed
    # encode logic wants ALL qualities available for download — but as ONE episode
    # per real (season, episode), each exposing a VideoVariant per tier (the
    # anikoto model), NOT one episode per file (which would corrupt numbering).
    archive = _make_zip(
        tmp_path / "pack.zip",
        [
            "Show S01E01 1080p.mkv",
            "Show S01E01 720p.mkv",
            "Show S01E02 1080p.mkv",
        ],
    )

    src = DdlSource()
    # Redirect the extract cache under tmp_path and skip the network fetch.
    monkeypatch.setattr(
        "nekofetch.sources.ddl.get_env",
        lambda: SimpleNamespace(storage_path=tmp_path),
    )

    async def fake_fetch(url: str, dest: Path, **kwargs) -> Path:
        shutil.copy(archive, dest)
        return dest

    monkeypatch.setattr(src, "_fetch_archive", fake_fetch)

    ref = json.dumps({
        "archives": [{"url": "https://example.test/pack.zip", "season": None}],
        "title": "Show",
    })
    episodes = await src.get_episodes(ref)

    # Grouped: two real episodes, numbered by episode (not a global file seq).
    assert len(episodes) == 2
    assert {e.number for e in episodes} == {1, 2}
    assert all(e.season == 1 for e in episodes)

    by_number = {e.number: e for e in episodes}
    # Ep1 keeps BOTH qualities as distinct variants; ep2 keeps its one.
    ep1_variants = await src.get_variants(by_number[1].source_ref)
    ep2_variants = await src.get_variants(by_number[2].source_ref)
    assert sorted(v.resolution for v in ep1_variants) == ["1080p", "720p"]
    assert sorted(v.resolution for v in ep2_variants) == ["1080p"]
    # Each variant points at its OWN extracted file (distinct paths per tier).
    ep1_paths = {json.loads(v.source_ref)["path"] for v in ep1_variants}
    assert len(ep1_paths) == 2


async def test_registry_activates_and_resolves_ddl() -> None:
    registry = build_default_registry()
    registry.activate(["ddl"])

    assert registry.get("ddl").name == "ddl"
    assert isinstance(registry.resolve("ddl"), DdlSource)


def test_multi_audio_pack_matches_subbed_dubbed_requests() -> None:
    # The real DDL failure: MoviesMod packs are "Hindi.Japanese.English" → tagged
    # AudioType.MULTI, but the worker only ever asks for DUBBED/SUBBED. The exact
    # `v.audio == audio` match found nothing → every unit skipped → "0 files".
    # A MULTI file contains every language, so it must satisfy those requests.
    from nekofetch.domain.enums import AudioType
    from nekofetch.services.download_service import DownloadWorker, _select_variant
    from nekofetch.sources.base import VideoVariant

    def _v(res, audio=AudioType.MULTI):
        return VideoVariant(source_ref="{}", resolution=res, audio=audio,
                            container="mkv", size_bytes=1)

    multi = [_v("1080p"), _v("720p"), _v("480p")]

    # A [DUBBED, SUBBED] request against MULTI variants collapses to ONE MULTI
    # acquisition (single download pass), not two skipped ones.
    assert DownloadWorker._resolve_audio_targets(
        [AudioType.DUBBED, AudioType.SUBBED], multi, "1080p",
    ) == [AudioType.MULTI]

    # _select_variant falls back to the MULTI file for a single-language request.
    assert _select_variant(multi, "1080p", AudioType.SUBBED, False).audio == AudioType.MULTI
    assert _select_variant(multi, "1080p", AudioType.DUBBED, False).audio == AudioType.MULTI
    assert _select_variant(multi, "1080p", AudioType.MULTI, False).audio == AudioType.MULTI

    # A normal SUBBED-only source is unaffected (no MULTI to fall back to).
    subs = [_v("1080p", AudioType.SUBBED)]
    assert DownloadWorker._resolve_audio_targets(
        [AudioType.SUBBED], subs, "1080p",
    ) == [AudioType.SUBBED]
    assert _select_variant(subs, "1080p", AudioType.DUBBED, False) is None


async def test_get_episodes_emits_download_and_extract_progress(
    tmp_path: Path, monkeypatch,
) -> None:
    # The owner's core complaint: download+extract were invisible, so the naming
    # prompt LOOKED like it came first. get_episodes must now emit a download bar
    # (with the ZIP filename) and an extract stage BEFORE returning.
    archive = _make_zip(tmp_path / "pack.zip", ["Show S01E01 1080p.mkv"])
    src = DdlSource()
    monkeypatch.setattr(
        "nekofetch.sources.ddl.get_env",
        lambda: SimpleNamespace(storage_path=tmp_path),
    )

    async def fake_fetch(url, dest, *, on_bytes=None):
        shutil.copy(archive, dest)
        if on_bytes:  # simulate a couple of byte updates (2-arg contract)
            await on_bytes(0, 100)
            await on_bytes(100, 100)
        return dest

    monkeypatch.setattr(src, "_fetch_archive", fake_fetch)

    events: list[dict] = []

    async def on_progress(info: dict) -> None:
        events.append(info)

    ref = json.dumps({
        "archives": [{
            "url": "https://worker.dev/abc/Akudama.Drive.S01.480p.zip", "season": None,
        }],
        "title": "Akudama Drive",
    })
    episodes = await src.get_episodes(ref, on_progress=on_progress)

    assert len(episodes) == 1
    stages = [e["stage"] for e in events]
    assert "download" in stages
    assert "extract" in stages and "extract_done" in stages
    # The ZIP filename (last path segment, url-decoded) is surfaced for the card.
    assert any(e.get("archive_name") == "Akudama.Drive.S01.480p.zip" for e in events)
