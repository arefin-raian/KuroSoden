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
from nekofetch.sources.ddl import DdlSource, _archive_name, _name_from_disposition
from nekofetch.sources.registry import build_default_registry


def test_name_from_disposition_parses_both_forms() -> None:
    # Plain filename="…"
    assert _name_from_disposition(
        'attachment; filename="A.Couple.Of.Cuckoos.S01.1080p.zip"'
    ) == "A.Couple.Of.Cuckoos.S01.1080p.zip"
    # RFC 5987 extended filename*=UTF-8''… (percent-encoded) takes precedence
    assert _name_from_disposition(
        "attachment; filename*=UTF-8''A.Couple.Of.Cuckoos.S01.zip"
    ) == "A.Couple.Of.Cuckoos.S01.zip"
    # A path/traversal in the header is reduced to the basename
    assert _name_from_disposition('inline; filename="../../etc/pack.zip"') == "pack.zip"
    # Absent / empty → None (caller falls back to the final-URL basename)
    assert _name_from_disposition(None) is None
    assert _name_from_disposition("attachment") is None


def test_archive_name_from_shortener_url_is_the_tail() -> None:
    # Documents the bug the redirect resolution fixes: the shortener URL's own
    # basename is the useless tail, which is why get_episodes now resolves the
    # FINAL url / Content-Disposition instead.
    assert _archive_name("https://flyn.im/9pYDxXE") == "9pYDxXE"
    assert _archive_name(
        "https://worker.dev/abc/A.Couple.Of.Cuckoos.S01.1080p.zip"
    ) == "A.Couple.Of.Cuckoos.S01.1080p.zip"


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


async def test_extract_count_is_cumulative_across_archives(tmp_path: Path, monkeypatch) -> None:
    # Bug B: a multi-archive release used to end on the LAST archive's local count
    # ("4 / 4" for a 12-file, 3-archive pack). The final extract_done frame must
    # report the TRUE cumulative total (12 / 12).
    def _season_zip(name: str, season: int) -> Path:
        return _make_zip(
            tmp_path / name,
            [f"Show S0{season}E0{ep} 1080p.mkv" for ep in range(1, 5)],  # 4 eps each
        )

    zips = {
        "https://example.test/s1.zip": _season_zip("s1.zip", 1),
        "https://example.test/s2.zip": _season_zip("s2.zip", 2),
        "https://example.test/s3.zip": _season_zip("s3.zip", 3),
    }
    src = DdlSource()
    monkeypatch.setattr(
        "nekofetch.sources.ddl.get_env",
        lambda: SimpleNamespace(storage_path=tmp_path),
    )

    async def fake_fetch(url: str, dest: Path, **kwargs) -> Path:
        shutil.copy(zips[url], dest)
        return dest

    monkeypatch.setattr(src, "_fetch_archive", fake_fetch)
    # Avoid the real HEAD request; give each archive a stable resolved name.
    async def fake_head(url):
        return None, url.rsplit("/", 1)[-1]
    monkeypatch.setattr(src, "_head_meta", fake_head)

    frames: list[dict] = []

    async def on_progress(info: dict) -> None:
        frames.append(info)

    ref = json.dumps({
        "archives": [{"url": u, "season": None} for u in zips],
        "title": "Show",
    })
    episodes = await src.get_episodes(ref, on_progress=on_progress)

    # 12 files extracted across 3 archives.
    done_frames = [f for f in frames if f["stage"] == "extract_done"]
    assert done_frames, "expected a final extract_done frame"
    last = done_frames[-1]
    assert (last["done"], last["total"]) == (12, 12), done_frames
    # Progress climbed cumulatively rather than resetting per archive: some frame
    # reported more than a single archive's 4 files.
    assert max(f.get("done", 0) for f in frames if f["stage"] == "extract") > 4


async def test_archive_name_season_hint_splits_seasons(tmp_path: Path, monkeypatch) -> None:
    # Bug C: DDL episode filenames are often bare ("Episode 01.mkv") with the
    # season only in the ARCHIVE name. The hint must split S1/S2 instead of
    # collapsing every file onto season 1.
    s1 = _make_zip(tmp_path / "s1.zip", ["Episode 01.mkv", "Episode 02.mkv"])
    s2 = _make_zip(tmp_path / "s2.zip", ["Episode 01.mkv", "Episode 02.mkv"])
    zips = {
        "https://example.test/Show.S01.1080p.zip": s1,
        "https://example.test/Show.S02.1080p.zip": s2,
    }
    src = DdlSource()
    monkeypatch.setattr(
        "nekofetch.sources.ddl.get_env",
        lambda: SimpleNamespace(storage_path=tmp_path),
    )

    async def fake_fetch(url: str, dest: Path, **kwargs) -> Path:
        shutil.copy(zips[url], dest)
        return dest

    monkeypatch.setattr(src, "_fetch_archive", fake_fetch)

    async def fake_head(url):
        # The archive NAME (with S01/S02) is what carries the season signal.
        return None, url.rsplit("/", 1)[-1]
    monkeypatch.setattr(src, "_head_meta", fake_head)

    ref = json.dumps({
        # Explicit archive season is null — the hint must come from the name.
        "archives": [{"url": u, "season": None} for u in zips],
        "title": "Show",
    })
    episodes = await src.get_episodes(ref)

    seasons = {e.season for e in episodes}
    assert seasons == {1, 2}, [(e.season, e.number) for e in episodes]
    # Two distinct episodes per season (E1/E2 in each) — not merged as duplicate
    # "qualities" of one season-1 episode.
    assert len(episodes) == 4


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

    async def fake_fetch(url, dest, *, on_bytes=None, total_hint=0):
        shutil.copy(archive, dest)
        if on_bytes:  # simulate a couple of byte updates (2-arg contract)
            await on_bytes(0, 100)
            await on_bytes(100, 100)
        return dest

    monkeypatch.setattr(src, "_fetch_archive", fake_fetch)

    # The shortener 302 is resolved by _head_meta; simulate it returning the real
    # name so the test stays hermetic (no network) and proves the resolved name —
    # not the shortener tail — reaches the card.
    async def fake_head_meta(url):
        return 0, "Akudama.Drive.S01.480p.zip"

    monkeypatch.setattr(src, "_head_meta", fake_head_meta)

    events: list[dict] = []

    async def on_progress(info: dict) -> None:
        events.append(info)

    ref = json.dumps({
        "archives": [{
            "url": "https://flyn.im/9pYDxXE", "season": None,
        }],
        "title": "Akudama Drive",
    })
    episodes = await src.get_episodes(ref, on_progress=on_progress)

    assert len(episodes) == 1
    stages = [e["stage"] for e in events]
    assert "download" in stages
    assert "extract" in stages and "extract_done" in stages
    # The RESOLVED archive name is surfaced for the card — never the shortener
    # tail "9pYDxXE" (the owner's exact complaint).
    assert any(e.get("archive_name") == "Akudama.Drive.S01.480p.zip" for e in events)
    assert not any(e.get("archive_name") == "9pYDxXE" for e in events)


async def test_extract_progress_reports_true_file_total(tmp_path: Path) -> None:
    """The extraction bar must count the REAL number of video files (e.g. 12), not
    a frozen 1/1. Regression for the owner's report that the 720p/480p zips showed
    "1 out of 1" instead of "1 out of 12"."""
    names = [f"Show S01E{n:02d} 480p.mkv" for n in range(1, 13)]  # 12 episodes
    archive = _make_zip(tmp_path / "pack480.zip", [*names, "readme.nfo"])

    ticks: list[tuple[int, int]] = []

    async def on_file(done, total, name=""):
        ticks.append((done, total))

    videos = await extract_archive(archive, tmp_path / "out", on_file=on_file)

    assert len(videos) == 12
    assert ticks, "no extraction progress was reported"
    # The denominator is the true video count (12), never a bogus 1.
    final_done, final_total = ticks[-1]
    assert final_total == 12
    assert final_done == 12
    # Never reports the frozen 1/1 that the 7z/nested path used to emit.
    assert (1, 1) not in ticks


async def test_extract_progress_true_total_for_nested_archive(tmp_path: Path) -> None:
    """A release that wraps its 12 videos inside an INNER archive (the outer zip
    has ONE member) must still report /12, not /1 — the nested case that produced
    the '1 out of 1' bug."""
    names = [f"Show S01E{n:02d} 720p.mkv" for n in range(1, 13)]
    inner = _make_zip(tmp_path / "inner.zip", names)
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, arcname="Show.S01.720p/inner.zip")

    ticks: list[tuple[int, int]] = []

    async def on_file(done, total, name=""):
        ticks.append((done, total))

    videos = await extract_archive(outer, tmp_path / "out", on_file=on_file)

    assert len(videos) == 12
    # Final tick lands on the real video count, not the outer archive's 1 member.
    assert ticks[-1] == (12, 12)
