"""Tests for kurosoden/shared/senku_thumbnail_adapter.py — Phase 3 (PLAN §7).

The adapter wraps NekoFetch's thumbnail machinery but swaps the surface (Senku DM)
and the store (:class:`DistributionCache`). These tests pin the wiring without
touching TMDB, Telegraph, or Playwright:
  • asset fetch delegates to NekoFetch's fetchers (reuse, not fork)
  • numbered buttons lay out in even rows (≤3/row) under the ``senku|wiz|`` namespace
  • a numbered pick maps to the ranked URL and persists to the cache
  • picks advance logo→poster→bg, then report ready-to-render
  • render_entry marks the entry done and next_pending advances past it
  • is_complete follows the cache's all_done
"""

from __future__ import annotations

import pytest

from kurosoden.shared.distribution_cache import DistributionCache, EntryData, Selection
from kurosoden.shared.senku_thumbnail_adapter import SenkuThumbnailAdapter


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


class _TmdbResult:
    def __init__(self, id_, media_type="tv"):
        self.id = id_
        self.media_type = media_type


class FakeTmdb:
    """Records searches; returns a stable id per query."""

    def __init__(self):
        self.searches: list[str] = []

    async def search(self, query):
        self.searches.append(query)
        return _TmdbResult(555, "tv")


class _Cfg:
    class thumbnail_channel:
        telegraph_access_token = ""  # no Telegraph → gallery_url returns None


class FakeContainer:
    def __init__(self, redis):
        self.redis = redis
        self.tmdb = FakeTmdb()
        self.config = _Cfg()


def _entries():
    return [
        EntryData(index=1, label="Season 1", season_number=1, title="Root"),
        EntryData(index=2, label="Season 2", season_number=2, title="Root 2"),
    ]


# Ranked assets each fetcher would return (ordered best-first).
_ASSETS = {
    "logo": [{"url": "http://img/logo1.png", "language": "en", "width": 800, "height": 200},
             {"url": "http://img/logo2.png", "language": None, "width": 400, "height": 100}],
    "poster": [{"url": "http://img/p1.webp", "language": "en"},
               {"url": "http://img/p2.webp", "language": None}],
    "bg": [{"url": "http://img/bg1.webp", "language": None},
           {"url": "http://img/bg2.webp", "language": None},
           {"url": "http://img/bg3.webp", "language": None},
           {"url": "http://img/bg4.webp", "language": None}],
}


@pytest.fixture
def adapter():
    return SenkuThumbnailAdapter(FakeContainer(FakeRedis()))


@pytest.fixture(autouse=True)
def _patch_fetchers(monkeypatch):
    """Stub the three NekoFetch fetchers the adapter reuses (no TMDB network)."""
    import kurosoden.shared.senku_thumbnail_adapter as mod

    async def fake_logos(client, tmdb_id, media_type):
        return list(_ASSETS["logo"])

    async def fake_posters(client, tmdb_id, media_type):
        return list(_ASSETS["poster"])

    async def fake_backdrops(client, tmdb_id, media_type):
        return list(_ASSETS["bg"])

    monkeypatch.setattr(mod, "fetch_logos", fake_logos)
    monkeypatch.setattr(mod, "fetch_posters_ranked", fake_posters)
    monkeypatch.setattr(mod, "fetch_backdrops_ranked", fake_backdrops)


# ── asset fetch delegates to NekoFetch's fetchers ───────────────────────────────

@pytest.mark.asyncio
async def test_fetch_assets_delegates_per_type(adapter):
    assert (await adapter.fetch_assets("logo", 1, "tv"))[0]["url"] == "http://img/logo1.png"
    assert (await adapter.fetch_assets("poster", 1, "tv"))[0]["url"] == "http://img/p1.webp"
    assert len(await adapter.fetch_assets("bg", 1, "tv")) == 4


# ── numbered buttons: even rows, wizard namespace ───────────────────────────────

def test_numbered_button_rows_even_layout():
    rows = SenkuThumbnailAdapter.numbered_button_rows("REQ-1", 1, "bg", 4)
    # 4 numbers → rows of 3 + 1
    assert [len(r) for r in rows] == [3, 1]
    labels = [lbl for row in rows for lbl, _cb in row]
    assert labels == ["1", "2", "3", "4"]
    # callbacks are wizard-namespaced so the existing dispatcher routes them
    first_cb = rows[0][0][1]
    assert first_cb == "senku|wiz|pick|REQ-1|1|bg|1"


def test_numbered_button_rows_single_row():
    rows = SenkuThumbnailAdapter.numbered_button_rows("REQ-1", 2, "logo", 2)
    assert [len(r) for r in rows] == [2]


# ── TMDB resolution searches the entry title, caches id back ─────────────────────

@pytest.mark.asyncio
async def test_resolve_tmdb_searches_entry_title_and_caches(adapter):
    await adapter.cache.set_entries("REQ-1", _entries())
    entry = await adapter.cache.get_entry("REQ-1", 2)
    tmdb_id, mtype = await adapter._resolve_tmdb("REQ-1", entry)
    assert tmdb_id == 555 and mtype == "tv"
    # searched the entry's own title, not the franchise root
    assert adapter._c.tmdb.searches == ["Root 2"]
    # id persisted back onto the cached entry
    cached = await adapter.cache.get_entry("REQ-1", 2)
    assert cached.tmdb_id == 555


# ── numbered pick maps to URL and persists; advances logo→poster→bg ──────────────

@pytest.mark.asyncio
async def test_store_pick_persists_and_advances(adapter):
    await adapter.cache.set_entries("REQ-1", _entries())

    sel, nxt = await adapter.store_pick("REQ-1", 1, "logo", 2)
    assert sel.logo_url == "http://img/logo2.png"   # #2 → second ranked
    assert nxt == "poster"

    sel, nxt = await adapter.store_pick("REQ-1", 1, "poster", 1)
    assert sel.poster_url == "http://img/p1.webp"
    assert nxt == "bg"

    sel, nxt = await adapter.store_pick("REQ-1", 1, "bg", 3)
    assert sel.backdrop_url == "http://img/bg3.webp"
    assert nxt is None  # all three picked → ready to render


@pytest.mark.asyncio
async def test_store_pick_out_of_range_is_noop(adapter):
    await adapter.cache.set_entries("REQ-1", _entries())
    sel, nxt = await adapter.store_pick("REQ-1", 1, "logo", 99)
    assert sel.logo_url is None
    assert nxt == "logo"  # still needs a logo


# ── manual upload: mirrored URL persists to the same field, advances the loop ────
#
# store_upload now routes bytes through image_backup.backup_bytes (catbox →
# telegraph → ImgBB) so an admin upload gets the same durable mirror as a
# numbered pick; these tests mock that shared pipeline, not the raw host.

@pytest.mark.asyncio
async def test_store_upload_persists_mirror_url_and_advances(adapter, monkeypatch):
    await adapter.cache.set_entries("REQ-1", _entries())

    uploaded: dict = {}

    async def fake_backup_bytes(container, blob, *, mime="image/jpeg", source_url=""):
        from kurosoden.shared.image_backup import BackupImage
        uploaded["bytes"] = blob
        uploaded["mime"] = mime
        return BackupImage(source_url=source_url,
                           catbox_url="https://files.catbox.moe/abc123.jpg")

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", fake_backup_bytes)

    sel, nxt = await adapter.store_upload("REQ-1", 1, "poster", b"\xff\xd8rawjpeg")
    # the mirrored URL lands in the SAME field a numbered poster pick would use
    assert sel.poster_url == "https://files.catbox.moe/abc123.jpg"
    assert uploaded["bytes"] == b"\xff\xd8rawjpeg"
    # a poster upload still leaves logo + bg to collect
    assert nxt == "logo"


@pytest.mark.asyncio
async def test_store_upload_propagates_host_failure(adapter, monkeypatch):
    await adapter.cache.set_entries("REQ-1", _entries())

    async def all_hosts_down(container, blob, *, mime="image/jpeg", source_url=""):
        # every host rejected the bytes → primary is None
        from kurosoden.shared.image_backup import BackupImage
        return BackupImage(source_url=source_url)

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", all_hosts_down)

    with pytest.raises(RuntimeError):
        await adapter.store_upload("REQ-1", 1, "poster", b"data")
    # nothing persisted — the field is still empty so the admin can retry
    sel = await adapter.cache.get_selection("REQ-1", 1)
    assert sel.poster_url is None


# ── next_asset ordering ──────────────────────────────────────────────────────────

def test_next_asset_order():
    assert SenkuThumbnailAdapter.next_asset(Selection()) == "logo"
    assert SenkuThumbnailAdapter.next_asset(Selection(logo_url="x")) == "poster"
    assert SenkuThumbnailAdapter.next_asset(
        Selection(logo_url="x", poster_url="y")) == "bg"
    assert SenkuThumbnailAdapter.next_asset(
        Selection(logo_url="x", poster_url="y", backdrop_url="z")) is None


# ── render_entry marks done; loop advances; is_complete tracks all_done ──────────

@pytest.mark.asyncio
async def test_render_entry_marks_done_and_loop_advances(adapter, monkeypatch):
    await adapter.cache.set_entries("REQ-1", _entries())
    # Pick all three assets for entry 1.
    await adapter.store_pick("REQ-1", 1, "logo", 1)
    await adapter.store_pick("REQ-1", 1, "poster", 1)
    await adapter.store_pick("REQ-1", 1, "bg", 1)

    # Stub the shared enrichment + renderer so no TMDB/AniList/Playwright is hit.
    import nekofetch.services.thumbnail_service as ts

    async def fake_fields(container, title, doc_id=None):
        return {"native_title": "", "romaji_title": "", "synopsis": "",
                "meta_label": "", "language": "", "genres": [], "studio": "",
                "tmdb_rating": None, "anilist_score": None, "country": None}

    monkeypatch.setattr(ts, "gather_thumbnail_fields", fake_fields)

    class FakeRenderer:
        async def render_thumbnail(self, **kw):
            return "/tmp/out.webp"

    adapter._render = FakeRenderer()

    # Before render: entry 1 is the pending one.
    pending = await adapter.next_pending("REQ-1")
    assert pending.index == 1
    assert await adapter.is_complete("REQ-1") is False

    entry = await adapter.cache.get_entry("REQ-1", 1)
    path = await adapter.render_entry("REQ-1", entry)
    assert str(path) == "/tmp/out.webp"

    # After render: entry 1 is done, loop advances to entry 2.
    sel = await adapter.cache.get_selection("REQ-1", 1)
    assert sel.done is True and sel.thumbnail_url == "file:///tmp/out.webp"
    pending = await adapter.next_pending("REQ-1")
    assert pending.index == 2


@pytest.mark.asyncio
async def test_render_entry_refuses_without_all_assets(adapter):
    await adapter.cache.set_entries("REQ-1", _entries())
    await adapter.store_pick("REQ-1", 1, "logo", 1)  # only logo
    entry = await adapter.cache.get_entry("REQ-1", 1)
    assert await adapter.render_entry("REQ-1", entry) is None
