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


@pytest.mark.asyncio
async def test_main_render_persists_under_minus1_with_hosted_url(sessionmaker):
    """The Senku main-channel render persists with anilist_id=None → the -1
    sentinel, carrying a hosted_url in fields — exactly the row + shape
    ``MainChannelService.gather_facts`` reads to use its OWN render for the main
    post (instead of the first-season distribution card)."""
    from nekofetch.infrastructure.database.postgres.models import ThumbnailSource

    await persist_thumbnail_source(
        SimpleNamespace(pg_sessionmaker=sessionmaker),
        "doc-x", None,  # None → stored as the -1 main sentinel
        {"title": "Root", "hosted_url": "https://cdn.test/main.webp"},
        image_path="/tmp/main.webp",
    )

    # Replicate gather_facts' step-1a read: the -1 row for this doc, hosted_url.
    async with sessionmaker() as session:
        row = (await session.execute(
            select(ThumbnailSource).where(
                ThumbnailSource.anime_doc_id == "doc-x",
                ThumbnailSource.anilist_id == -1,
            )
        )).scalars().first()
    assert row is not None
    assert row.anilist_id == -1
    hosted = (row.fields or {}).get("hosted_url")
    assert hosted == "https://cdn.test/main.webp"
    assert hosted.startswith(("http://", "https://"))  # sendable by the main post


@pytest.mark.asyncio
async def test_main_render_row_is_distinct_from_entry_rows(sessionmaker):
    """A distribution entry (real anilist_id) and the main render (-1) coexist as
    SEPARATE rows — the two surfaces never collide on the unique key."""
    from nekofetch.infrastructure.database.postgres.models import ThumbnailSource

    c = SimpleNamespace(pg_sessionmaker=sessionmaker)
    await persist_thumbnail_source(c, "doc-y", 777,
                                   {"title": "S1", "hosted_url": "https://cdn/s1.webp"})
    await persist_thumbnail_source(c, "doc-y", None,
                                   {"title": "Root", "hosted_url": "https://cdn/main.webp"})
    async with sessionmaker() as session:
        rows = (await session.execute(
            select(ThumbnailSource).where(ThumbnailSource.anime_doc_id == "doc-y")
        )).scalars().all()
    by_id = {r.anilist_id: (r.fields or {}).get("hosted_url") for r in rows}
    assert by_id == {777: "https://cdn/s1.webp", -1: "https://cdn/main.webp"}
