"""Tests for the Lelouch batch franchise-selection helpers and the work_items
table registration.

Covers:
  • ``resolve_franchise_candidates`` groups an AniList search page into DISTINCT
    franchises — collapsing seasons/arcs of one title, keeping distinct series
    apart, and picking the most-popular installment as each franchise's rep.
  • ``WorkItem`` registers on the shared ``Base.metadata`` (the fix for the batch
    commit crashing on ``relation "work_items" does not exist``).
"""

from __future__ import annotations

import pytest


# ── work_items table registration ─────────────────────────────────────────────

def test_work_item_registers_on_base_metadata():
    # Importing the module must put ``work_items`` on Base.metadata so create_all
    # emits it (mirrors what create_all() does at boot).
    from kurosoden.shared import work_service  # noqa: F401
    from nekofetch.infrastructure.database.postgres.base import Base

    assert "work_items" in Base.metadata.tables


# ── franchise-candidate grouping ──────────────────────────────────────────────

class _FakeAnilist:
    def __init__(self, page):
        self._page = page

    async def search_candidates(self, query, *, limit=25):
        return list(self._page)


class _FakeContainer:
    def __init__(self, page):
        self.anilist = _FakeAnilist(page)


@pytest.mark.asyncio
async def test_candidates_single_franchise():
    from kurosoden.shared.franchise_resolver import resolve_franchise_candidates

    page = [{"id": 1, "title": "Takopi's Original Sin", "format": "ONA",
             "popularity": 100}]
    out = await resolve_franchise_candidates(_FakeContainer(page), "takopi")
    assert len(out) == 1
    assert out[0]["anilist_id"] == "1"


@pytest.mark.asyncio
async def test_candidates_collapse_seasons_keep_distinct():
    from kurosoden.shared.franchise_resolver import resolve_franchise_candidates

    # AoT S1 + AoT S2 (collapse to ONE franchise) + Attack No.1 (distinct).
    page = [
        {"id": 10, "title": "Attack on Titan", "format": "TV", "popularity": 500},
        {"id": 11, "title": "Attack on Titan Season 2", "format": "TV", "popularity": 300},
        {"id": 12, "title": "Attack No.1", "format": "TV", "popularity": 50},
    ]
    out = await resolve_franchise_candidates(_FakeContainer(page), "attack")
    titles = [c["title"] for c in out]
    # Two distinct franchises: AoT (seasons collapsed) + Attack No.1.
    assert len(out) == 2
    assert "Attack No.1" in titles
    # The AoT franchise is represented once, by its most-popular installment (S1).
    aot = [c for c in out if c["title"].startswith("Attack on Titan")]
    assert len(aot) == 1
    assert aot[0]["anilist_id"] == "10"


@pytest.mark.asyncio
async def test_candidates_empty_page_falls_back_to_single(monkeypatch):
    import kurosoden.shared.franchise_resolver as fr

    async def _fake_single(container, query, **kw):
        return {"title": "Fallback Show", "anilist_id": "77", "format": "TV"}

    monkeypatch.setattr(fr, "resolve_franchise", _fake_single)
    out = await fr.resolve_franchise_candidates(_FakeContainer([]), "obscure")
    assert out == [{"title": "Fallback Show", "anilist_id": "77", "format": "TV"}]


# ── aggregated-fallback relation-type guard ───────────────────────────────────

def test_aggregated_fallback_excludes_spinoffs_and_recaps():
    """The aggregated mapping (used when no walk entries are available) must drop
    SPIN_OFF/ALTERNATIVE/SUMMARY relations, matching the request pipeline. This is
    the AoT batch bug: "No Regrets" (SPIN_OFF OVA) + a recap MOVIE (SUMMARY) were
    being pulled into the batch mapping when the single-request path excludes them.
    """
    from nekofetch.services.franchise_flow import FranchiseFlowService

    svc = FranchiseFlowService.__new__(FranchiseFlowService)
    franchise = {
        "title": "Attack on Titan",
        "franchise_seasons": 1,
        "franchise_episodes": 25,
        "relations": [
            {"anilist_id": 1, "relation": "SIDE_STORY", "format": "OVA",
             "title": "AoT: Lost Girls", "episodes": 3},
            {"anilist_id": 2, "relation": "SPIN_OFF", "format": "OVA",
             "title": "AoT: No Regrets", "episodes": 2},
            {"anilist_id": 3, "relation": "SUMMARY", "format": "MOVIE",
             "title": "AoT: Crimson Bow and Arrow (recap)", "episodes": 1},
            {"anilist_id": 4, "relation": "ALTERNATIVE", "format": "TV",
             "title": "Junior High", "episodes": 12},
        ],
    }
    mapping = svc.build_mapping(franchise, "doc")
    titles = {e.title for e in mapping.entries}
    # The canonical side-story OVA survives; spin-off/recap/alternate do not.
    assert "AoT: Lost Girls" in titles
    assert "AoT: No Regrets" not in titles
    assert "AoT: Crimson Bow and Arrow (recap)" not in titles
    assert "Junior High" not in titles
