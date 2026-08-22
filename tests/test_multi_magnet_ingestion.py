"""Multi-magnet ingestion: a torrent request may carry MULTIPLE magnets (e.g. one
per season). NyaaSource.get_episodes must resolve ALL of them, merge their files
into one ordered set (so every season maps), and tag each episode's download
variant with ITS OWN magnet so the per-file download hits the right torrent.

Regression for the reported "gave S1 + S2 magnets, S2 showed 0/12" bug.
"""

from __future__ import annotations

import json

import pytest

import nekofetch.sources.nyaa as nyaa_mod
from nekofetch.sources.nyaa import NyaaSource


# Two "torrents": S1 magnet → S1 files, S2 magnet → S2 files. _resolve_magnet is
# stubbed to echo the magnet back as bytes; torrent_files maps those bytes to the
# right file list.
_MAGNET_S1 = "magnet:?xt=urn:btih:1111111111111111111111111111111111111111&dn=Show.S01"
_MAGNET_S2 = "magnet:?xt=urn:btih:2222222222222222222222222222222222222222&dn=Show.S02"

_FILES = {
    _MAGNET_S1: [
        {"index": 0, "path": "Show S01E01 1080p.mkv", "name": "Show S01E01 1080p.mkv", "length": 10},
        {"index": 1, "path": "Show S01E02 1080p.mkv", "name": "Show S01E02 1080p.mkv", "length": 10},
    ],
    _MAGNET_S2: [
        {"index": 0, "path": "Show S02E01 1080p.mkv", "name": "Show S02E01 1080p.mkv", "length": 10},
        {"index": 1, "path": "Show S02E02 1080p.mkv", "name": "Show S02E02 1080p.mkv", "length": 10},
    ],
}


@pytest.fixture
def _stub_torrents(monkeypatch):
    async def fake_resolve(self, magnet):        # echo magnet → bytes
        return magnet.encode()

    def fake_torrent_files(raw: bytes):
        magnet = raw.decode()
        return ("Show", [dict(f) for f in _FILES[magnet]])

    monkeypatch.setattr(NyaaSource, "_resolve_magnet", fake_resolve)
    monkeypatch.setattr(nyaa_mod, "torrent_files", fake_torrent_files)


@pytest.mark.asyncio
async def test_multi_magnet_merges_all_seasons(_stub_torrents):
    src = NyaaSource()
    ref = json.dumps({
        "sources": [
            {"magnet": _MAGNET_S1, "info_hash": "1" * 40},
            {"magnet": _MAGNET_S2, "info_hash": "2" * 40},
        ],
        "title": "Show",
    })
    episodes = await src.get_episodes(ref)

    # BOTH seasons present (the bug: only S1 resolved → S2 was 0/12).
    seasons = sorted({e.season for e in episodes})
    assert seasons == [1, 2], [(e.season, e.number) for e in episodes]
    assert len(episodes) == 4

    # Each episode's variant is routed to ITS OWN magnet, so download hits the
    # correct torrent (S2 episodes must NOT point at the S1 magnet).
    for e in episodes:
        variants = await src.get_variants(e.source_ref)
        for v in variants:
            info = json.loads(v.source_ref)
            expected = _MAGNET_S2 if e.season == 2 else _MAGNET_S1
            assert info["magnet"] == expected, (e.season, info.get("magnet"))


@pytest.mark.asyncio
async def test_single_magnet_still_works_via_legacy_ref(_stub_torrents):
    # A legacy single-magnet ref (no ``sources``) must still resolve.
    src = NyaaSource()
    ref = json.dumps({"magnet": _MAGNET_S1, "info_hash": "1" * 40, "title": "Show"})
    episodes = await src.get_episodes(ref)
    assert sorted({e.season for e in episodes}) == [1]
    assert len(episodes) == 2
    v = (await src.get_variants(episodes[0].source_ref))[0]
    assert json.loads(v.source_ref)["magnet"] == _MAGNET_S1
