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


async def test_get_episodes_keeps_every_quality(tmp_path: Path, monkeypatch) -> None:
    # Episode 1 ships in both 1080p and 720p; episode 2 only in 1080p. The reversed
    # encode logic wants ALL of them downloaded (no collapse to one file/episode).
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

    async def fake_fetch(url: str, dest: Path) -> Path:
        shutil.copy(archive, dest)
        return dest

    monkeypatch.setattr(src, "_fetch_archive", fake_fetch)

    ref = json.dumps({
        "archives": [{"url": "https://example.test/pack.zip", "season": None}],
        "title": "Show",
    })
    episodes = await src.get_episodes(ref)

    # No collapse: all three files survive as distinct episodes.
    assert len(episodes) == 3
    resolutions = sorted(
        json.loads(e.source_ref)["resolution"] for e in episodes
    )
    assert resolutions == ["1080p", "1080p", "720p"]
    assert all(e.season == 1 for e in episodes)


async def test_registry_activates_and_resolves_ddl() -> None:
    registry = build_default_registry()
    registry.activate(["ddl"])

    assert registry.get("ddl").name == "ddl"
    assert isinstance(registry.resolve("ddl"), DdlSource)
