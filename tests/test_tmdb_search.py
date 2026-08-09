from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.providers.metadata.tmdb import TmdbClient


@pytest.mark.asyncio
async def test_search_strips_season_tokens_and_includes_adult(monkeypatch):
    client = TmdbClient(token="token")
    calls = []

    async def fake_get(path, **params):
        calls.append((path, params))
        return {"results": [{
            "id": 42,
            "origin_country": ["JP"],
            "original_language": "ja",
            "genre_ids": [16],
            "popularity": 1,
        }]}

    async def fake_details(tmdb_id, media_type):
        return SimpleNamespace(id=tmdb_id, media_type=media_type)

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(client, "details", fake_details)

    result = await client.search("The Case Study of Vanitas Part 2")

    assert result.id == 42
    assert calls
    assert all(params["include_adult"] == "true" for _, params in calls)
    assert all(params["query"] == "The Case Study of Vanitas" for _, params in calls)


@pytest.mark.asyncio
async def test_search_retries_raw_title_when_base_has_no_candidates(monkeypatch):
    client = TmdbClient(token="token")
    queries = []

    async def fake_get(path, **params):
        queries.append(params["query"])
        if params["query"] == "Vanitas":
            return {"results": []}
        return {"results": [{
            "id": 7,
            "origin_country": ["JP"],
            "original_language": "ja",
            "genre_ids": [16],
            "popularity": 2,
        }]}

    async def fake_details(tmdb_id, media_type):
        return SimpleNamespace(id=tmdb_id, media_type=media_type)

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(client, "details", fake_details)

    result = await client.search("Vanitas Part 2")

    assert result.id == 7
    assert queries[:2] == ["Vanitas", "Vanitas"]
    assert queries[-1] == "Vanitas Part 2"


@pytest.mark.asyncio
async def test_search_keeps_japanese_anime_above_live_action_namesake(monkeypatch):
    client = TmdbClient(token="token")

    async def fake_get(path, **params):
        media = path.rsplit("/", 1)[-1]
        if media == "tv":
            return {"results": [{
                "id": 1, "origin_country": ["US"],
                "original_language": "en", "genre_ids": [], "popularity": 999,
            }, {
                "id": 2, "origin_country": ["JP"],
                "original_language": "ja", "genre_ids": [16], "popularity": 1,
            }]}
        return {"results": []}

    async def fake_details(tmdb_id, media_type):
        return SimpleNamespace(id=tmdb_id, media_type=media_type)

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(client, "details", fake_details)

    result = await client.search("Vanitas")

    assert result.id == 2
