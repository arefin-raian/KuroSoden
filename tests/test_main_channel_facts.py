"""Phase 2 coverage — main-channel post content corrections (Gojo spec).

The main-channel post must show franchise-level truth, not season-1 leftovers:
  • EPISODES = seasonal episodes plus explicitly labelled extras and movies.
  • RATING   = AVERAGE of every franchise entry's AniList score.
  • OVERVIEW = one clean paragraph (ragged newlines / <br> collapsed).

These pin the pure logic (``_collapse``) and the franchise walk
(``_apply_franchise_facts``) with a fake AniList client — no network, no DB.
"""

from __future__ import annotations

import pytest

from nekofetch.services.main_channel_service import (
    MainChannelService,
    PublicationFacts,
    _collapse,
    is_stale_message_error,
)
from nekofetch.sources.telegram.anilist import FranchiseEntry, FranchiseTotals


# ── _collapse: synopsis flattening ──

def test_collapse_flattens_hard_line_breaks():
    raw = "First line.\nSecond line.\n\nThird paragraph."
    assert _collapse(raw) == "First line. Second line. Third paragraph."


def test_collapse_strips_anilist_br_tags():
    raw = "One.<br>Two.<br/>Three.<br />Four."
    assert _collapse(raw) == "One. Two. Three. Four."


def test_collapse_handles_blank_and_dash():
    assert _collapse(None) == "—"
    assert _collapse("") == "—"
    assert _collapse("—") == "—"


def test_collapse_squeezes_runs_of_whitespace():
    assert _collapse("A   lot\t\tof   space") == "A lot of space"


def test_stale_main_message_errors_are_retryable():
    assert is_stale_message_error(RuntimeError("MESSAGE_ID_INVALID"))
    assert is_stale_message_error(RuntimeError("message not found"))
    assert not is_stale_message_error(RuntimeError("FLOOD_WAIT_10"))


# ── _apply_franchise_facts: episode sum + rating average ──

class _FakeAnilist:
    """Stubs the two franchise walkers MainChannelService relies on."""

    def __init__(self, *, totals: FranchiseTotals, entries: dict[int, FranchiseEntry]):
        self._totals = totals
        self._entries = entries
        self.totals_calls: list[int] = []
        self.walk_calls: list[int] = []

    async def franchise_totals(self, root_id: int, *, max_nodes: int = 120):
        self.totals_calls.append(root_id)
        return self._totals

    async def walk_franchise_full(self, root_id: int, *, max_nodes: int = 120):
        self.walk_calls.append(root_id)
        return self._entries


class _FakeContainer:
    def __init__(self, anilist):
        self.anilist = anilist


def _entry(aid: int, score: float | None, fmt: str = "TV") -> FranchiseEntry:
    return FranchiseEntry(
        anilist_id=aid, format=fmt, english_title=f"Entry {aid}",
        titles=[f"Entry {aid}"], score=score,
    )


def _entry_eps(aid: int, score: float | None, episodes: int | None,
               fmt: str = "TV") -> FranchiseEntry:
    return FranchiseEntry(
        anilist_id=aid, format=fmt, english_title=f"Entry {aid}",
        titles=[f"Entry {aid}"], score=score, episodes=episodes,
    )


@pytest.mark.asyncio
async def test_episodes_are_formatted_by_entry_kind():
    """Seasonal episodes remain the base; extras and movies are labelled."""
    anilist = _FakeAnilist(
        totals=FranchiseTotals(seasons=3, movies=2, episodes=64),
        entries={
            1: _entry_eps(1, 8.0, 12, "TV"),
            2: _entry_eps(2, 8.0, 12, "TV"),
            3: _entry_eps(3, 7.5, 1, "MOVIE"),   # extras COUNT now
            4: _entry_eps(4, 7.0, 2, "OVA"),
        },
    )
    svc = MainChannelService.__new__(MainChannelService)
    svc._c = _FakeContainer(anilist)
    facts = PublicationFacts(anime_doc_id="anilist:100", title="X", episodes="12")

    await svc._apply_franchise_facts("anilist:100", facts)

    assert facts.episodes == "24 + 2 extras + 1 movie"
    assert anilist.walk_calls == [100]


@pytest.mark.asyncio
async def test_rating_is_average_of_all_franchise_scores():
    anilist = _FakeAnilist(
        totals=FranchiseTotals(seasons=2, episodes=24),
        entries={
            1: _entry(1, 8.0),
            2: _entry(2, 9.0),
            3: _entry(3, 7.0),
            4: _entry(4, None),            # missing scores must be ignored
        },
    )
    svc = MainChannelService.__new__(MainChannelService)
    svc._c = _FakeContainer(anilist)
    facts = PublicationFacts(anime_doc_id="anilist:100", title="X")

    await svc._apply_franchise_facts("anilist:100", facts)

    # (8+9+7)/3 = 8.0 on the 0-10 scale → a rounded whole-number percent.
    assert facts.rating == "80%"


@pytest.mark.asyncio
async def test_no_scores_leaves_rating_dash():
    anilist = _FakeAnilist(
        totals=FranchiseTotals(episodes=0),
        entries={1: _entry(1, None)},
    )
    svc = MainChannelService.__new__(MainChannelService)
    svc._c = _FakeContainer(anilist)
    facts = PublicationFacts(anime_doc_id="anilist:100", title="X", episodes="12")

    await svc._apply_franchise_facts("anilist:100", facts)

    assert facts.rating == "—"
    assert facts.episodes == "12"          # zero season episodes -> keep pack count


@pytest.mark.asyncio
async def test_non_numeric_doc_id_is_a_noop():
    anilist = _FakeAnilist(totals=FranchiseTotals(episodes=99), entries={})
    svc = MainChannelService.__new__(MainChannelService)
    svc._c = _FakeContainer(anilist)
    facts = PublicationFacts(anime_doc_id="tmdb:abc", title="X", episodes="5")

    await svc._apply_franchise_facts("tmdb:abc", facts)

    assert facts.episodes == "5"           # untouched — no digit id to walk
    assert anilist.totals_calls == []


@pytest.mark.asyncio
async def test_walk_failure_is_swallowed():
    """A raising franchise walk must not abort the post — facts stay usable."""
    class _Boom:
        async def franchise_totals(self, root_id, *, max_nodes=120):
            raise RuntimeError("anilist down")

        async def walk_franchise_full(self, root_id, *, max_nodes=120):
            raise RuntimeError("anilist down")

    svc = MainChannelService.__new__(MainChannelService)
    svc._c = _FakeContainer(_Boom())
    facts = PublicationFacts(anime_doc_id="anilist:100", title="X", episodes="12")

    await svc._apply_franchise_facts("anilist:100", facts)

    assert facts.episodes == "12"
    assert facts.rating == "—"
