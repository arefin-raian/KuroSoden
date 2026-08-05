"""Phase-4 tests: post-round coverage diff (services/coverage.py).

Coverage is the gate that drives the iterative multi-source loop — after a
download round it must report exactly which (season, episode) units the
franchise still owes, INCLUDING wholly-absent seasons that torrent_mapping's
gap detector is blind to. These tests pin that behaviour against an in-memory
DB and hand-built franchise mappings.
"""

from __future__ import annotations

import pytest

from nekofetch.domain.enums import AudioType, ContentKind
from nekofetch.services.coverage import compute_coverage
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry


def _mapping(*entries: MappingEntry, doc_id: str = "anilist:12345") -> FranchiseMapping:
    return FranchiseMapping(anime_doc_id=doc_id, root_title="Test", entries=list(entries))


def _season(number: int, episodes: int | None, *, part: int | None = None,
            included: bool = True) -> MappingEntry:
    return MappingEntry(kind=ContentKind.SEASON, season_number=number,
                        season_part=part, episodes=episodes, included=included)


async def _add_files(session, req, units: list[tuple[int, int]]) -> None:
    """Insert a recorded MediaFile row for each (season, episode) in ``units``."""
    from nekofetch.infrastructure.database.postgres.models import MediaFile
    for season, episode in units:
        session.add(MediaFile(
            anime_doc_id=req.anime_doc_id, season=season, episode=episode,
            resolution="1080p", audio=AudioType.SUBBED, size_bytes=1,
        ))
    await session.commit()


@pytest.fixture
async def req(session):
    from tests.helpers import _create_request
    return await _create_request(session, source="ddl")


async def test_complete_when_all_episodes_present(session, req):
    # Franchise: S1 (12 eps). We have all 12 → complete, nothing missing.
    await _add_files(session, req, [(1, e) for e in range(1, 13)])
    report = await compute_coverage(session, req, _mapping(_season(1, 12)))
    assert report.complete
    assert report.missing == []
    assert report.empty_seasons == []


async def test_detects_trailing_and_interior_gaps(session, req):
    # S1 expects 12; we have 1-9 (missing 10,11,12) plus a hole at 5.
    have = [(1, e) for e in range(1, 13) if e not in (5, 10, 11, 12)]
    await _add_files(session, req, have)
    report = await compute_coverage(session, req, _mapping(_season(1, 12)))
    assert not report.complete
    assert report.grouped() == {1: [5, 10, 11, 12]}
    assert report.empty_seasons == []


async def test_detects_wholly_missing_season(session, req):
    # The Phase-4 raison d'être: got all of S1+S2, S3 (12 eps) entirely absent.
    # torrent_mapping._detect_gaps would report NOTHING for S3 (zero files);
    # compute_coverage must flag the whole season.
    await _add_files(session, req,
                     [(1, e) for e in range(1, 13)] + [(2, e) for e in range(1, 13)])
    mapping = _mapping(_season(1, 12), _season(2, 12), _season(3, 12))
    report = await compute_coverage(session, req, mapping)
    assert not report.complete
    assert report.empty_seasons == [3]
    assert report.grouped() == {3: list(range(1, 13))}


async def test_multipart_season_episode_counts_sum(session, req):
    # S3 Part 1 (12) + S3 Part 2 (10) → season 3 expects 22 contiguous episodes.
    mapping = _mapping(_season(3, 12, part=1), _season(3, 10, part=2))
    await _add_files(session, req, [(3, e) for e in range(1, 20)])  # have 1-19
    report = await compute_coverage(session, req, mapping)
    assert report.grouped() == {3: [20, 21, 22]}


async def test_unknown_episode_count_does_not_fabricate_holes(session, req):
    # A season whose episode count is unknown (None) can't be diffed — it must
    # NOT invent missing episodes, and must not block completion by itself.
    mapping = _mapping(_season(1, None))
    report = await compute_coverage(session, req, mapping)
    assert report.unknown_seasons == [1]
    assert report.missing == []
    assert report.complete  # nothing resolvable is missing


async def test_excluded_and_non_season_entries_ignored(session, req):
    # Excluded season + a movie entry must not contribute expected units.
    mapping = _mapping(
        _season(1, 12),
        _season(2, 12, included=False),
        MappingEntry(kind=ContentKind.MOVIE, season_number=0, episodes=1),
    )
    await _add_files(session, req, [(1, e) for e in range(1, 13)])
    report = await compute_coverage(session, req, mapping)
    assert report.complete
    assert report.resolved_seasons == [1]
