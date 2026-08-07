"""Regression tests for thumbnail workflow boundary normalization."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nekofetch.services.thumbnail_orchestrator_service import (
    ThumbnailOrchestratorService,
)


class FakeRedis:
    def __init__(self, values: dict[str, str]):
        self.values = values

    async def get(self, key: str):
        return self.values.get(key)


@pytest.mark.asyncio
async def test_root_thumbnail_is_normalized_but_not_used_as_first_season():
    key = "nf:thumbcc:workflow:anime-1"
    redis = FakeRedis({
        key: json.dumps([
            {
                "index": 0,
                "status": "done",
                "anilist_id": -1,
                "thumbnail_url": "https://img/root.webp",
            },
            {
                "index": 1,
                "status": "done",
                "anilist_id": 200,
                "thumbnail_url": "https://img/season-1.webp",
            },
            {
                "index": 2,
                "status": "done",
                "anilist_id": 100,
                "thumbnail_url": "https://img/season-2.webp",
            },
        ]),
    })
    service = ThumbnailOrchestratorService(SimpleNamespace(redis=redis))

    generated = await service.get_generated_thumbnails("anime-1")

    assert generated[None] == "https://img/root.webp"
    assert generated[200] == "https://img/season-1.webp"
    assert await service.get_first_season_thumbnail("anime-1") == (
        "https://img/season-1.webp"
    )


@pytest.mark.asyncio
async def test_first_season_uses_workflow_order_not_anilist_id():
    key = "nf:thumbcc:workflow:anime-2"
    redis = FakeRedis({
        key: json.dumps([
            {
                "index": 1,
                "status": "done",
                "anilist_id": 900,
                "thumbnail_url": "https://img/first.webp",
            },
            {
                "index": 2,
                "status": "done",
                "anilist_id": 100,
                "thumbnail_url": "https://img/second.webp",
            },
        ]),
    })
    service = ThumbnailOrchestratorService(SimpleNamespace(redis=redis))

    assert await service.get_first_season_thumbnail("anime-2") == (
        "https://img/first.webp"
    )
