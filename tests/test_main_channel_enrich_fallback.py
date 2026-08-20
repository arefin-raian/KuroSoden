"""Main-channel enrichment fallback: genre / studio / romaji / title / synopsis.

Regression for the empty-genre + "#anime" studio + missing-romaji bug. The
prefetch anilist.json cache is the primary source, but when it's absent every
field kept its default. gather_facts now backfills from franchise_data, then a
live AniList fetch — so an absent cache can never blank the main post again.
These drive _enrich_facts_fallback directly (no prefetch cache present).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.services.main_channel_service import MainChannelService, PublicationFacts
from nekofetch.sources.telegram.anilist import AnilistMedia


class _NoDB:
    """pg_sessionmaker stand-in that yields no Request (franchise_data empty), so
    the test isolates the LIVE-AniList fallback tier."""
    def __call__(self):
        raise RuntimeError("no db in this test")  # gather_facts wraps in try/except


class _FakeAnilist:
    def __init__(self, media):
        self._media = media
        self.fetch_calls: list[int] = []

    async def _fetch_full(self, mid: int):
        self.fetch_calls.append(mid)
        return self._media

    async def search(self, q):
        return self._media


def _media(**kw) -> AnilistMedia:
    base = dict(
        id=162896, format="TV", season=None, year=2024, start_date=None,
        episodes=12, duration=24, status="FINISHED", score=7.7, popularity=1,
        genres=["Action", "Adventure", "Supernatural"], synopsis="A young lord flees.",
        studio="CloverWorks", cover_url=None, banner_url=None,
        english="The Elusive Samurai", romaji="Nige Jouzu no Wakagimi",
        titles=["The Elusive Samurai"], synonyms=[], relations=[], anilist_url="",
        franchise_episodes=12, franchise_seasons=1, franchise_movies=0,
        franchise_ovas=0, franchise_onas=0, franchise_specials=0,
    )
    base.update(kw)
    return AnilistMedia(**base)


def _svc(anilist):
    c = SimpleNamespace(
        config=SimpleNamespace(main_channel=SimpleNamespace(enabled=True, channel_id=-100)),
        anilist=anilist,
        pg_sessionmaker=_NoDB(),   # franchise_data lookup fails → live tier exercised
    )
    return MainChannelService(c)


async def test_live_fallback_fills_all_defaults():
    anilist = _FakeAnilist(_media())
    svc = _svc(anilist)
    facts = PublicationFacts(anime_doc_id="162896", title="162896")
    await svc._enrich_facts_fallback("162896", facts)
    assert anilist.fetch_calls == [162896]                 # resolved BY ID, not search
    assert facts.genres == "Action, Adventure, Supernatural"
    assert facts.tag == "CloverWorks"                       # not the "#anime" default
    assert facts._english == "The Elusive Samurai"
    assert facts._romaji == "Nige Jouzu no Wakagimi"
    assert "flees" in facts.overview


async def test_fallback_never_overwrites_a_good_cached_value():
    # Simulate a cache hit having already set real values — the fallback must NOT
    # replace them with the live fetch (which here carries different data).
    anilist = _FakeAnilist(_media(genres=["WRONG"], studio="WrongStudio",
                                  english="Wrong", romaji="Chigau"))
    svc = _svc(anilist)
    facts = PublicationFacts(anime_doc_id="162896", title="The Elusive Samurai")
    facts.genres = "Action, Adventure, Supernatural"
    facts.tag = "CloverWorks"
    facts._english = "The Elusive Samurai"
    facts._romaji = "Nige Jouzu no Wakagimi"
    facts.overview = "Real overview."
    await svc._enrich_facts_fallback("162896", facts)
    assert facts.genres == "Action, Adventure, Supernatural"
    assert facts.tag == "CloverWorks"
    assert facts._english == "The Elusive Samurai"
    assert facts._romaji == "Nige Jouzu no Wakagimi"
    assert facts.overview == "Real overview."
    assert anilist.fetch_calls == []  # nothing missing → no live call at all


async def test_partial_fallback_only_fills_missing():
    # Genre already good; studio + romaji missing → only those get filled.
    anilist = _FakeAnilist(_media())
    svc = _svc(anilist)
    facts = PublicationFacts(anime_doc_id="162896", title="The Elusive Samurai")
    facts.genres = "Action, Adventure, Supernatural"  # already set (cache hit)
    facts._english = "The Elusive Samurai"
    await svc._enrich_facts_fallback("162896", facts)
    assert facts.genres == "Action, Adventure, Supernatural"  # untouched
    assert facts.tag == "CloverWorks"                          # filled
    assert facts._romaji == "Nige Jouzu no Wakagimi"           # filled
