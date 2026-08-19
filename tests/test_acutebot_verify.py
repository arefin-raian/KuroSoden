"""Tests for @acutebot AniList cross-verification (nekofetch/providers/acute_bot.py).

Covers the resilience fix: _verify_against_anilist now accepts a ``fetch_full``
resolver (the shared ResilientMetadataClient._fetch_full, which traverses the
FULL chain AniList → Kaggle → LeoRigasaki → Jikan → Kitsu by id). When AniList
is down, verification is answered by Kaggle/Jikan by id instead of a raw direct
AniList POST that would 403 and soft-pass.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.providers.acute_bot import _verify_against_anilist


def _media(titles, synonyms=None):
    """Minimal stand-in — verify only calls media.all_titles()."""
    all_t = list(titles) + list(synonyms or [])
    return SimpleNamespace(all_titles=lambda: all_t)


@pytest.mark.asyncio
async def test_verify_via_chain_match():
    """A chain hit whose titles cover our pick → verified, no mismatch."""
    async def fetch_full(_id):
        return _media(["Frieren: Beyond Journey's End", "Sousou no Frieren"])

    verified, mismatch = await _verify_against_anilist(
        12345, ["Sousou no Frieren"], fetch_full,
    )
    assert verified is True
    assert mismatch is False


@pytest.mark.asyncio
async def test_verify_via_chain_mismatch():
    """A chain hit whose titles DON'T cover our pick → not verified, mismatch."""
    async def fetch_full(_id):
        return _media(["Completely Different Show"])

    verified, mismatch = await _verify_against_anilist(
        12345, ["Sousou no Frieren"], fetch_full,
    )
    assert verified is False
    assert mismatch is True


@pytest.mark.asyncio
async def test_verify_anilist_down_chain_answers():
    """The core fix: AniList 403s, but Kaggle (via the chain) answers by id.

    The chain resolver returns a hit even though AniList itself is unreachable,
    so verification SUCCEEDS instead of soft-passing. The direct-POST path is
    never taken because fetch_full is supplied.
    """
    calls = {"n": 0}

    async def fetch_full(_id):
        calls["n"] += 1
        # Simulates AniList tier missing but Kaggle/Jikan returning the media.
        return _media(["Kaguya-sama: Love Is War", "Kaguya-sama wa Kokurasetai"])

    verified, mismatch = await _verify_against_anilist(
        99, ["Kaguya-sama: Love Is War"], fetch_full,
    )
    assert calls["n"] == 1           # went through the chain resolver
    assert verified is True
    assert mismatch is False


@pytest.mark.asyncio
async def test_verify_chain_miss_soft_passes():
    """Chain returns nothing (all tiers missed) → soft pass, no mismatch."""
    async def fetch_full(_id):
        return None

    verified, mismatch = await _verify_against_anilist(
        7, ["Anything"], fetch_full,
    )
    assert verified is True           # can't compare → trust acutebot's URL
    assert mismatch is False


@pytest.mark.asyncio
async def test_verify_resolver_raises_soft_passes():
    """A resolver that raises must never break the caller → soft pass."""
    async def fetch_full(_id):
        raise RuntimeError("network is down")

    verified, mismatch = await _verify_against_anilist(
        7, ["Anything"], fetch_full,
    )
    assert verified is True
    assert mismatch is False


@pytest.mark.asyncio
async def test_verify_no_id_returns_unverified():
    """No AniList id → nothing to verify (verified=False, no mismatch)."""
    verified, mismatch = await _verify_against_anilist(None, ["X"], lambda _id: None)
    assert verified is False
    assert mismatch is False


@pytest.mark.asyncio
async def test_verify_id_but_no_titles():
    """An id but no expected titles → treated as present/verified, no mismatch."""
    verified, mismatch = await _verify_against_anilist(123, [], None)
    assert verified is True
    assert mismatch is False
