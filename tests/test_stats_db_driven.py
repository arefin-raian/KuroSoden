"""Regression: Gojo /stats counts must come from authoritative DB signals.

The old ``StatsService.compute()`` scraped the index channel's letter posts and
fuzzy-matched the text against a canonical-names map. When the scrape returned
nothing (or the match missed), ``published`` collapsed to 0 and ``not_indexed``
inflated to the full catalog — exactly what the owner saw: "total 7, published 0,
not indexed 7" while 6 were live in the main channel and all were in the index.

The fix reads the signals the publish/index flows actually write:

* **total_series**  — distinct ``StoragePack.anime_doc_id`` (deduped in Python so
  inconsistent titles for one id, e.g. Vanitas, don't over-count).
* **published_series** — ``ChannelPost`` rows with ``main_message_id`` set.
* **indexed_series** — series whose title-letter has a posted ``IndexSection``
  (the index lists every StoragePack title per letter, so a live letter section
  means the series is in the index — separate from being published).

This test builds those rows in a private in-memory engine and asserts the counts,
including the Vanitas title-variance case that a SQL ``DISTINCT`` would break.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nekofetch.domain.enums import AudioType
from nekofetch.infrastructure.database.postgres.base import Base
from nekofetch.infrastructure.database.postgres.models import (
    ChannelPost,
    IndexSection,
    StoragePack,
)
from nekofetch.services.stats_service import StatsService


def _section(order: int, letter: str, msg_id: int | None = None) -> IndexSection:
    """A posted index-channel letter slot (message_id set = live)."""
    return IndexSection(
        sort_order=order, label=letter, base_letter=letter,
        message_id=msg_id if msg_id is not None else 1000 + order,
    )


@pytest_asyncio.fixture
async def sessionmaker_():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    yield sm
    await eng.dispose()


def _pack(doc_id: str, title: str, *, season: int = 1, res: str = "1080p") -> StoragePack:
    return StoragePack(
        anime_doc_id=doc_id, anime_title=title, season=season, resolution=res,
        audio=AudioType.SUBBED, channel_id=-100, start_message_id=1, end_message_id=9,
        file_count=12,
    )


def _container(sm):
    return SimpleNamespace(pg_sessionmaker=sm, redis=None)


@pytest.mark.asyncio
async def test_compute_counts_published_and_indexed_separately(sessionmaker_):
    """Published (main_message_id) and indexed (letter section posted) are distinct
    signals; "Not indexed" must be total - indexed, not total - published."""
    sm = sessionmaker_
    async with sm() as s:
        s.add_all([
            _pack("a:1", "Alpha"),
            _pack("a:2", "Bravo"),
            _pack("a:3", "Zeta"),      # letter Z — no posted section → not indexed
            _pack("a:4", "Delta"),
            # A, B, D letter sections are live in the index channel.
            _section(1, "A"), _section(2, "B"), _section(3, "D"),
            # a:1, a:2 also have a main-channel card; a:4 is indexed only.
            ChannelPost(anime_doc_id="a:1", main_channel_id=-100,
                        main_message_id=11, index_letter="A", index_message_id=101),
            ChannelPost(anime_doc_id="a:2", main_channel_id=-100,
                        main_message_id=12, index_letter="B", index_message_id=102),
        ])
        await s.commit()

    stats = await StatsService(_container(sm)).compute()
    assert stats["total_series"] == 4
    assert stats["published_series"] == 2          # a:1, a:2 have a main post
    assert stats["indexed_series"] == 3            # Alpha, Bravo, Delta letters live
    assert stats["not_indexed_series"] == 1        # only Zeta's letter isn't posted
    assert stats["not_indexed_titles"] == ["Zeta"]


@pytest.mark.asyncio
async def test_vanitas_title_variance_does_not_overcount(sessionmaker_):
    """One anime_doc_id with two different titles across packs must count once."""
    sm = sessionmaker_
    async with sm() as s:
        s.add_all([
            _pack("anilist:100", "The Case Study of Vanitas", season=1),
            _pack("anilist:100", "Vanitas no Carte", season=1, res="720p"),
            _section(1, "V"),   # "V" letter section is live → the series is indexed
            _section(2, "T"),   # a stray extra letter with no matching series
            ChannelPost(anime_doc_id="anilist:100", main_channel_id=-100,
                        main_message_id=50, index_letter="V", index_message_id=200),
        ])
        await s.commit()

    stats = await StatsService(_container(sm)).compute()
    assert stats["total_series"] == 1              # NOT 2 — title variance deduped
    assert stats["published_series"] == 1
    assert stats["indexed_series"] == 1
    assert stats["not_indexed_series"] == 0


@pytest.mark.asyncio
async def test_dashboard_index_items_counts_indexed_posts(sessionmaker_):
    sm = sessionmaker_
    async with sm() as s:
        s.add_all([
            _pack("a:1", "Alpha"),
            _pack("a:2", "Bravo"),
            _section(1, "A"),   # only the "A" letter section is posted
            # both have main-channel cards (published), but only Alpha's letter
            # is live in the index → indexed should be 1, published 2.
            ChannelPost(anime_doc_id="a:1", main_channel_id=-100,
                        main_message_id=11, index_letter="A", index_message_id=101),
            ChannelPost(anime_doc_id="a:2", main_channel_id=-100,
                        main_message_id=12, index_letter="B", index_message_id=102),
        ])
        await s.commit()

    stats = await StatsService(_container(sm)).gojo_dashboard()
    # index_items mirrors the catalog-consistent indexed count from compute().
    assert stats["dashboard"]["index_items"] == 1
    assert stats["indexed_series"] == 1            # only Alpha's letter is posted
    assert stats["published_series"] == 2          # both have a main post
    # The rendered dashboard shows the catalog-consistent indexed count.
    assert "Indexed items: <b>1</b>" in StatsService.dashboard_message(stats)
