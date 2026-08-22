"""TMDB asset language priority (owner spec): logos + posters = EN → JP → neutral.

Other languages are dropped; within a tier, higher vote_count wins. Backdrops are
unchanged (neutral-first) and not covered here.
"""

from __future__ import annotations

import pytest

from nekofetch.providers.metadata import tmdb_assets


class _FakeTmdb:
    def __init__(self, images: dict):
        self._images = images
        self.calls: list[dict] = []

    async def _get(self, path: str, **params):
        self.calls.append(params)
        return self._images


def _img(lang, fp, votes=0, w=800, h=300):
    return {"iso_639_1": lang, "file_path": fp, "vote_count": votes,
            "vote_average": 5.0, "width": w, "height": h}


@pytest.mark.asyncio
async def test_logos_rank_en_then_ja_then_neutral_dropping_others():
    client = _FakeTmdb({"logos": [
        _img(None, "/neutral.png", votes=99),   # neutral, high votes
        _img("ja", "/jp.png", votes=1),
        _img("en", "/en.png", votes=1),
        _img("fr", "/fr.png", votes=500),        # other language → dropped
    ]})
    out = await tmdb_assets.fetch_logos(client, 1, "tv")
    langs = [o["language"] for o in out]
    assert langs == ["en", "ja", None], langs        # EN → JP → neutral
    assert "en,ja,null" in client.calls[0].values()   # requested all three
    assert all((o["language"] or "") != "fr" for o in out)  # other langs dropped


@pytest.mark.asyncio
async def test_posters_rank_en_then_ja_then_neutral():
    client = _FakeTmdb({"posters": [
        _img("", "/neutral.jpg", votes=10),
        _img("en", "/en.jpg", votes=1),
        _img("ja", "/jp.jpg", votes=1),
        _img("de", "/de.jpg", votes=999),        # dropped
    ]})
    out = await tmdb_assets.fetch_posters_ranked(client, 1, "tv")
    langs = [o["language"] for o in out]
    assert langs == ["en", "ja", ""], langs
    assert all((o["language"] or "") != "de" for o in out)
