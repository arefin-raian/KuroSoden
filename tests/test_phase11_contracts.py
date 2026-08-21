from __future__ import annotations

from pathlib import Path

import pytest

from nekofetch.ui.components import cb
from kurosoden.shared.distribution_cache import DistributionCache, EntryData
from kurosoden.shared.senku_thumbnail_adapter import SenkuThumbnailAdapter


# Keep this test independent of the production Redis client by using the same
# minimal async fake shape as the wizard tests.
class _Redis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)


class _Config:
    class thumbnail_channel:
        telegraph_access_token = ""


class _Container:
    def __init__(self):
        self.redis = _Redis()
        # The adapter's live-fetch fallback passes these handles through to the
        # shared metadata helpers; the fake fetcher below does not use them.
        self.tmdb = object()
        self.env = object()
        self.config = _Config()


@pytest.mark.asyncio
async def test_tmdb_gallery_cache_is_shared_by_franchise_root():
    container = _Container()
    cache = DistributionCache(container)
    assets = [{"url": "https://image.test/a.png", "language": None}]
    await cache.set_tmdb_assets("anilist:root", "logo", assets)
    assert await cache.get_tmdb_assets("anilist:root", "logo") == assets
    # Request cleanup must not destroy the franchise gallery needed by sibling
    # entries, while explicit franchise retirement does remove it.
    await cache.clear("REQ-1")
    assert await cache.get_tmdb_assets("anilist:root", "logo") == assets
    await cache.clear_tmdb_assets("anilist:root")
    assert await cache.get_tmdb_assets("anilist:root", "logo") is None


@pytest.mark.asyncio
async def test_adapter_reuses_gallery_for_two_entries(monkeypatch):
    container = _Container()
    adapter = SenkuThumbnailAdapter(container)
    calls = []
    assets = [{"url": "https://image.test/a.png", "language": None}]

    async def fake_fetch(*args):
        calls.append(args)
        return assets

    import kurosoden.shared.senku_thumbnail_adapter as adapter_module
    monkeypatch.setattr(adapter_module, "fetch_logos", fake_fetch)

    async def fake_root_doc_id(code):
        return "anilist:root"

    monkeypatch.setattr(adapter, "_root_doc_id", fake_root_doc_id)
    first_entry = EntryData(index=10, label="Season 1", title="Root", tmdb_id=10)
    second_entry = EntryData(index=11, label="Season 2", title="Root 2", tmdb_id=11)
    first, _gallery, _rows = await adapter.asset_step("REQ-1", first_entry, "logo")
    second, _gallery, _rows = await adapter.asset_step("REQ-1", second_entry, "logo")
    assert first == second == assets
    assert len(calls) == 1


def test_new_phase11_callbacks_stay_within_telegram_limit():
    values = [
        cb("senku", "wiz", "textweight", "REQ-99999", "12", "montserrat", "900", "0"),
        cb("senku", "wiz", "textitalic", "REQ-99999", "12", "montserrat", "900", "1"),
        cb("senku", "wiz", "textprev_yes", "REQ-99999", "12"),
    ]
    assert max(len(value.encode("utf-8")) for value in values) <= 64


def test_thumbnail_logo_slot_is_slightly_larger_and_wider():
    template = (Path(__file__).resolve().parents[1] / "thumbnail" / "index.html").read_text()
    # The logo HEIGHT is now an owner-tunable {{STYLE_LOGO_HEIGHT}} token; resolve
    # the style tokens (defaults) before asserting the effective dimensions.
    import nekofetch.services.thumbnail_service as ts
    ts.set_thumbnail_style_provider(None)
    for token, value in ts._style_tokens().items():
        template = template.replace(token, value)
    assert 'h-[4.5rem] w-auto max-w-[820px]' in template   # default logo slot
    assert 'w-[12vw] h-[40vh]' in template  # poster remains unchanged
    assert 'max-w-[520px]' in template       # synopsis remains unchanged
