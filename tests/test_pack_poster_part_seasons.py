"""Per-part AniList poster resolution — storage packs for split seasons.

A split season (Vanitas "The Case Study of Vanitas" + "… Part 2") is TWO separate
AniList entries that share one season number. The storage uploader groups the
files into two packs (S1 and S1 Part 2) but both carried the REQUEST ROOT's
``entry_id``, so ``_pack_poster`` resolved BOTH halves to the FIRST half's cover.

The fix: the (season, season_part) slot now resolves to its OWN AniList id via
the canonical franchise mapping (``part_ids``), so S1 keeps its cover and
S1 Part 2 gets Part 2's — for both the mirrored cover and the walk fallback.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.services.publishing_service import PublishingService


class _FakeContainer:
    """Bare container — every network/DB helper is patched per test."""

    def __init__(self):
        self.config = SimpleNamespace()


def _svc():
    return PublishingService(_FakeContainer())


def _vanitas_walk():
    """The prefetched ``anilist.json`` franchise blob for Vanitas."""
    return {
        "franchise": {
            "101": {
                "anilist_id": 101, "format": "TV",
                "english_title": "The Case Study of Vanitas",
                "episodes": 12, "duration": 24,
                "start_date": {"year": 2021, "month": 7, "day": 2},
                "cover_url": "http://img/s1.jpg",
                "relation": "ROOT",
            },
            "102": {
                "anilist_id": 102, "format": "TV",
                "english_title": "The Case Study of Vanitas Part 2",
                "episodes": 12, "duration": 24,
                "start_date": {"year": 2022, "month": 1, "day": 8},
                "cover_url": "http://img/s1p2.jpg",
                "relation": "SEQUEL",
            },
        }
    }


async def _posters(monkeypatch, blob):
    """Run _anilist_entry_posters against a canned cache blob."""
    import nekofetch.services.metadata_prefetch as mp

    async def fake_load_cached(container, code, kind, *, anime_doc_id=None):
        return blob

    monkeypatch.setattr(mp, "load_cached", fake_load_cached)
    svc = _svc()
    return await svc._anilist_entry_posters("doc", [])


# ── the cache walk exposes (season, part) → own anilist id ───────────────────────

@pytest.mark.asyncio
async def test_entry_posters_build_part_ids_for_split_season(monkeypatch):
    posters = await _posters(monkeypatch, _vanitas_walk())

    # Per-id + chronological season-index lookups behave as before …
    assert posters[("id", 101)] == "http://img/s1.jpg"
    assert posters[("id", 102)] == "http://img/s1p2.jpg"
    assert posters[("season", 1)] == "http://img/s1.jpg"
    assert posters[("season", 2)] == "http://img/s1p2.jpg"
    # … and the NEW part map keys each split-half to its OWN entry.
    assert posters["part_ids"] == {(1, 1): 101, (1, 2): 102}


@pytest.mark.asyncio
async def test_entry_posters_part_ids_empty_without_walk(monkeypatch):
    posters = await _posters(monkeypatch, {})
    assert posters == {}


# ── _pack_poster picks the right half's cover ─────────────────────────────────────

def _fit_recorder(monkeypatch):
    """Patch ThumbnailStage._fit_thumb to record the ref and 'fit' the image."""
    refs: list[str] = []

    async def fake_fit(ref, out):
        refs.append(ref)
        out.write_bytes(b"jpg")
        return True

    monkeypatch.setattr(
        "nekofetch.services.processing.stages.ThumbnailStage._fit_thumb", fake_fit,
    )
    return refs


async def _poster_for(monkeypatch, tmp_path, *, season_part, entry_id=101):
    """Resolve a pack poster with a full cache miss (walk covers used)."""
    import nekofetch.services.metadata_prefetch as mp

    async def no_cached_cover(*a, **k):
        return None

    monkeypatch.setattr(mp, "resolve_cached_cover", no_cached_cover)
    refs = _fit_recorder(monkeypatch)
    posters = await _posters(monkeypatch, _vanitas_walk())
    dest = tmp_path / "pack"
    dest.mkdir()
    out = await _svc()._pack_poster(
        posters, entry_id, 1, dest, season_part=season_part, anime_doc_id="doc",
    )
    return out, refs


@pytest.mark.asyncio
async def test_part_two_pack_uses_part_twos_cover(monkeypatch, tmp_path):
    # The S1P2 pack arrives with the REQUEST ROOT's entry_id (101) — the exact
    # Vanitas bug. The (1, 2) slot must re-route it to entry 102's own cover.
    out, refs = await _poster_for(monkeypatch, tmp_path, season_part=2, entry_id=101)
    assert out is not None
    assert out.name == "poster_anilist_102.jpg"  # per-entry file, no clobber
    assert refs == ["http://img/s1p2.jpg"]


@pytest.mark.asyncio
async def test_first_half_pack_keeps_its_own_cover(monkeypatch, tmp_path):
    out, refs = await _poster_for(monkeypatch, tmp_path, season_part=1, entry_id=101)
    assert out is not None
    assert out.name == "poster_anilist_101.jpg"
    assert refs == ["http://img/s1.jpg"]


@pytest.mark.asyncio
async def test_part_miss_falls_back_to_given_entry_id(monkeypatch, tmp_path):
    # A season_part the walk doesn't know must fall back to the snapshot id —
    # never crash and never reuse the wrong half.
    import nekofetch.services.metadata_prefetch as mp

    async def no_cached_cover(*a, **k):
        return None

    monkeypatch.setattr(mp, "resolve_cached_cover", no_cached_cover)
    refs = _fit_recorder(monkeypatch)
    # No part match → the given entry id itself must be honoured (and its own
    # cover used) — never the first half's.
    posters = {"part_ids": {(1, 1): 101, (1, 2): 102},
               ("id", 777): "http://img/x.jpg"}
    dest = tmp_path / "pack"
    dest.mkdir()
    out = await _svc()._pack_poster(
        posters, 777, 1, dest, season_part=9, anime_doc_id="doc",
    )
    assert out is not None
    assert out.name == "poster_anilist_777.jpg"
    assert refs == ["http://img/x.jpg"]


@pytest.mark.asyncio
async def test_pack_poster_prefers_mirrored_cover_for_resolved_entry(
    monkeypatch, tmp_path,
):
    # When the prefetch mirrored Part 2's cover, resolve_cached_cover(102) must
    # win over the walk URL — offline-first stays intact.
    import nekofetch.services.metadata_prefetch as mp

    resolved: list[int | None] = []

    async def fake_resolve(container, code, *, anilist_id=None, anime_doc_id=None):
        resolved.append(anilist_id)
        return "http://mirror/102.jpg" if anilist_id == 102 else None

    monkeypatch.setattr(mp, "resolve_cached_cover", fake_resolve)
    refs = _fit_recorder(monkeypatch)
    posters = await _posters(monkeypatch, _vanitas_walk())
    dest = tmp_path / "pack"
    dest.mkdir()
    out = await _svc()._pack_poster(
        posters, 101, 1, dest, season_part=2, anime_doc_id="doc",
    )
    assert out is not None
    assert 102 in resolved  # resolved by ITS OWN id, not the root's
    assert refs == ["http://mirror/102.jpg"]
