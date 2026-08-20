"""Regression: the coverage gate must honor the admin's entry selection.

The stuck-at-Storing bug: the admin toggled the franchise to only Season 1, the
job downloaded + uploaded S1's 12 files, then hung at "Storing" forever because
``_reconstruct_franchise_mapping`` rebuilt the mapping with every entry
``included=True`` (throwing away the toggle) — so ``compute_coverage`` expected a
de-selected / phantom Season 2 that would never arrive.

``_reconstruct_franchise_mapping`` must reapply the persisted ``included`` flags
from ``franchise_data['_torrent_mapping']`` so the gate only expects the seasons
the admin actually enabled. These tests drive that reconstruction directly (no
AniList network — the aggregated fallback builds S1/S2 from franchise_seasons).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.services.coverage import compute_coverage
from nekofetch.services.download_service import DownloadWorker


def _worker():
    container = SimpleNamespace(
        config=SimpleNamespace(
            downloads=SimpleNamespace(concurrent_downloads=5,
                                      multi_source_coverage=True),
        ),
        progress=None, redis=None, pg_sessionmaker=None,
    )
    return DownloadWorker(container)


def _franchise_data(*, s2_included: bool) -> dict:
    """Franchise blob for a 2-season title, with a persisted torrent mapping
    whose Season 2 is included/excluded per ``s2_included``."""
    return {
        "title": "Test Show",
        "english": "Test Show",
        "anilist_id": "12345",
        "franchise_seasons": 2,
        "franchise_episodes": 24,
        "relations": [],
        "_torrent_mapping": {
            "entries": [
                {"kind": "season", "season_number": 1, "title": "Season 01",
                 "episodes": 12, "included": True},
                {"kind": "season", "season_number": 2, "title": "Season 02",
                 "episodes": 12, "included": s2_included},
            ],
        },
    }


@pytest.fixture
def _walk_entries(monkeypatch):
    """Force ``resolve_franchise_entries`` to return a 2-season walk with KNOWN
    per-season episode counts (12 each) — mirrors the real scenario where a
    franchise walk gave S2 a concrete count (the phantom-hole the gate saw),
    isolating the ``included`` reapplication as the variable under test."""
    from nekofetch.sources.telegram.anilist import FranchiseEntry

    async def _fake_entries(self, franchise_data, anime_doc_id):
        return {
            111: FranchiseEntry(anilist_id=111, format="TV",
                                english_title="Test Show", episodes=12,
                                relation="ROOT", status="FINISHED"),
            222: FranchiseEntry(anilist_id=222, format="TV",
                                english_title="Test Show Season 2", episodes=12,
                                relation="SEQUEL", status="FINISHED"),
        }
    monkeypatch.setattr(
        "nekofetch.services.franchise_flow.FranchiseFlowService."
        "resolve_franchise_entries", _fake_entries,
    )


@pytest.fixture
async def req(session):
    from tests.helpers import _create_request
    return await _create_request(session, source="ddl")


async def _add_files(session, req, units):
    from nekofetch.infrastructure.database.postgres.models import MediaFile
    from nekofetch.domain.enums import AudioType
    for season, episode in units:
        session.add(MediaFile(anime_doc_id=req.anime_doc_id, season=season,
                              episode=episode, resolution="1080p",
                              audio=AudioType.SUBBED, size_bytes=1))
    await session.commit()


async def test_reconstruct_reapplies_deselected_season(session, req, _walk_entries):
    # S2 toggled OFF → the reconstructed mapping must mark S2 included=False.
    req.franchise_data = _franchise_data(s2_included=False)
    mapping = await _worker()._reconstruct_franchise_mapping(req)
    assert mapping is not None
    by_season = {e.season_number: e for e in mapping.entries if e.season_number}
    assert by_season[1].included is True
    assert by_season[2].included is False
    # included_entries (what coverage diffs) excludes the de-selected S2.
    assert 2 not in {e.season_number for e in mapping.included_entries}


async def test_deselected_season_makes_coverage_complete(session, req, _walk_entries):
    # The end-to-end point: with only S1's 12 files present and S2 toggled OFF,
    # coverage over the reconstructed mapping is COMPLETE (no phantom S2 hole),
    # so the gate would finalize instead of hanging at "Storing".
    req.franchise_data = _franchise_data(s2_included=False)
    await _add_files(session, req, [(1, e) for e in range(1, 13)])
    mapping = await _worker()._reconstruct_franchise_mapping(req)
    report = await compute_coverage(session, req, mapping)
    assert report.complete
    assert report.empty_seasons == []


async def test_included_season_still_expected(session, req, _walk_entries):
    # Control: with S2 toggled ON but no S2 files, coverage stays INCOMPLETE —
    # the fix must not blanket-drop seasons, only honor the real selection.
    req.franchise_data = _franchise_data(s2_included=True)
    await _add_files(session, req, [(1, e) for e in range(1, 13)])
    mapping = await _worker()._reconstruct_franchise_mapping(req)
    report = await compute_coverage(session, req, mapping)
    assert not report.complete
    assert report.empty_seasons == [2]
