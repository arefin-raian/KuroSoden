from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nekofetch.services.thumbnail_service import persist_thumbnail_source


@pytest.mark.asyncio
async def test_thumbnail_source_is_upserted_by_entry(sessionmaker):
    await persist_thumbnail_source(
        SimpleNamespace(pg_sessionmaker=sessionmaker),
        "anilist:123", 456,
        {"title": "Example", "logo_url": "old", "genres": ("Action",)},
        image_path="/tmp/old.webp",
    )
    await persist_thumbnail_source(
        SimpleNamespace(pg_sessionmaker=sessionmaker),
        "anilist:123", 456,
        {"title": "Example", "logo_url": "new", "genres": ("Drama",)},
        image_path="/tmp/new.webp",
    )

    from nekofetch.infrastructure.database.postgres.models import ThumbnailSource
    async with sessionmaker() as session:
        rows = (await session.execute(select(ThumbnailSource))).scalars().all()
    assert len(rows) == 1
    assert rows[0].fields["logo_url"] == "new"
    assert rows[0].fields["genres"] == ["Drama"]
    assert rows[0].image_path == "/tmp/new.webp"
