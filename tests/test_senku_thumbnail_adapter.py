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


# ── numbered pick maps to URL and persists; advances logo→poster→bg ──────────────

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
async def test_store_text_logo_uses_existing_logo_field(adapter, monkeypatch, tmp_path):
    await adapter.cache.set_entries("REQ-1", _entries())
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"PNGDATA")

    async def fake_backup_bytes(container, blob, *, mime="image/jpeg", source_url=""):
        from kurosoden.shared.image_backup import BackupImage
        assert mime == "image/png"
        assert source_url.startswith("file://")
        return BackupImage(source_url=source_url,
                           catbox_url="https://files.catbox.moe/textlogo.png")

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", fake_backup_bytes)

    sel, nxt = await adapter.store_text_logo("REQ-1", 1, logo)
    assert sel.logo_url == "https://files.catbox.moe/textlogo.png"
    assert nxt == "poster"


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


# ── render_entry refuses without all assets ─────────────────────────────────────

@pytest.mark.asyncio
async def test_render_entry_refuses_without_all_assets(adapter):
    await adapter.cache.set_entries("REQ-1", _entries())
    await adapter.store_pick("REQ-1", 1, "logo", 1)  # only logo
    entry = await adapter.cache.get_entry("REQ-1", 1)
    assert await adapter.render_entry("REQ-1", entry) is None


# ── render_entry mirrors the render to the image hosts AT RENDER TIME ────────────
#
# Regression: the rendered card was only ever uploaded at publish time (the
# publisher bridge), so a render that never reached publish existed nowhere but
# the local disk — the "rendered but never uploaded" bug. render_entry now
# uploads the bytes immediately and stores the public URL; the DM preview still
# uses the local path it returns.


def _stub_renderer(adapter, tmp_path):
    """Wire a fake Playwright renderer that writes a local .webp and return it."""
    webp = tmp_path / "thumb.webp"
    webp.write_bytes(b"WEBPRENDER")

    class _FakeRenderer:
        async def render_thumbnail(self, **kw):
            return webp

    adapter._render = _FakeRenderer()
    return webp


async def _seed_assets(adapter):
    """Give entry 1 all three asset picks without touching TMDB."""
    await adapter.cache.set_entries("REQ-1", _entries())
    await adapter.cache.set_selection("REQ-1", 1, asset="logo", value="http://img/logo1.png")
    await adapter.cache.set_selection("REQ-1", 1, asset="poster", value="http://img/p1.webp")
    await adapter.cache.set_selection("REQ-1", 1, asset="bg", value="http://img/bg1.webp")


@pytest.fixture(autouse=True)
def _patch_render_deps(monkeypatch):
    """Stub the enrichment/persist helpers so render tests never touch the DB."""
    import nekofetch.services.thumbnail_service as ts

    async def fake_gather(*a, **k):
        return {}

    async def fake_persist(*a, **k):
        return None

    monkeypatch.setattr(ts, "gather_thumbnail_fields", fake_gather)
    monkeypatch.setattr(ts, "persist_thumbnail_source", fake_persist)


@pytest.mark.asyncio
async def test_render_entry_hosts_thumbnail_at_render_time(adapter, monkeypatch, tmp_path):
    await _seed_assets(adapter)
    webp = _stub_renderer(adapter, tmp_path)

    uploaded: dict = {}

    async def fake_backup_bytes(container, blob, *, mime="image/jpeg", source_url=""):
        from kurosoden.shared.image_backup import BackupImage
        uploaded["bytes"] = blob
        uploaded["mime"] = mime
        return BackupImage(source_url=source_url,
                           catbox_url="https://files.catbox.moe/thumb123.webp")

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", fake_backup_bytes)

    entry = await adapter.cache.get_entry("REQ-1", 1)
    out = await adapter.render_entry("REQ-1", entry)

    # The DM preview keeps using the LOCAL path …
    assert out == webp
    # … but the stored selection is the PUBLIC mirror, and the real bytes + webp
    # mime went to the hosts.
    sel = await adapter.cache.get_selection("REQ-1", 1)
    assert sel.thumbnail_url == "https://files.catbox.moe/thumb123.webp"
    assert uploaded["bytes"] == b"WEBPRENDER"
    assert uploaded["mime"] == "image/webp"


@pytest.mark.asyncio
async def test_render_entry_falls_back_to_file_when_hosts_reject(adapter, monkeypatch, tmp_path):
    await _seed_assets(adapter)
    webp = _stub_renderer(adapter, tmp_path)

    async def all_hosts_down(container, blob, *, mime="image/jpeg", source_url=""):
        from kurosoden.shared.image_backup import BackupImage
        return BackupImage(source_url=source_url)  # every mirror is None

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", all_hosts_down)

    entry = await adapter.cache.get_entry("REQ-1", 1)
    out = await adapter.render_entry("REQ-1", entry)

    # A failed host upload must NOT fail the render — the publisher bridge still
    # understands file:// and mirrors it at publish time.
    assert out == webp
    sel = await adapter.cache.get_selection("REQ-1", 1)
    assert sel.thumbnail_url == f"file://{webp}"


@pytest.mark.asyncio
async def test_render_entry_host_failure_is_nonfatal(adapter, monkeypatch, tmp_path):
    """A throwing host layer must not turn a good render into a failure."""
    await _seed_assets(adapter)
    webp = _stub_renderer(adapter, tmp_path)

    async def explode(*a, **k):
        raise RuntimeError("imgbb down")

    import kurosoden.shared.image_backup as image_backup
    monkeypatch.setattr(image_backup, "backup_bytes", explode)

    entry = await adapter.cache.get_entry("REQ-1", 1)
    out = await adapter.render_entry("REQ-1", entry)
    assert out == webp
    sel = await adapter.cache.get_selection("REQ-1", 1)
    assert sel.thumbnail_url == f"file://{webp}"


# ── surface split: distribution entry = AniList synopsis; main = TMDB + avg ring ──

@pytest.mark.asyncio
async def test_render_entry_prefers_anilist_synopsis(adapter, monkeypatch, tmp_path):
    """A distribution entry card must describe THAT season → AniList synopsis
    (prefer_anilist_synopsis=True), not the franchise-level TMDB overview."""
    await _seed_assets(adapter)
    _stub_renderer(adapter, tmp_path)
    captured: dict = {}

    import nekofetch.services.thumbnail_service as ts

    async def spy_gather(container, title, anime_doc_id=None, *, prefer_anilist_synopsis=False, anilist_id=None):
        captured["prefer"] = prefer_anilist_synopsis
        return {}
    monkeypatch.setattr(ts, "gather_thumbnail_fields", spy_gather)

    import kurosoden.shared.image_backup as image_backup

    async def _bk(container, blob, *, mime="image/jpeg", source_url=""):
        from kurosoden.shared.image_backup import BackupImage
        return BackupImage(source_url=source_url)
    monkeypatch.setattr(image_backup, "backup_bytes", _bk)

    entry = await adapter.cache.get_entry("REQ-1", 1)
    await adapter.render_entry("REQ-1", entry)
    assert captured["prefer"] is True


@pytest.mark.asyncio
async def test_render_main_uses_tmdb_synopsis_avg_ring_and_persists_minus1(
    adapter, monkeypatch, tmp_path,
):
    """The main-channel render: TMDB franchise synopsis (prefer_anilist_synopsis=
    False), the franchise-AVERAGE AniList ring, variant_key='main', and a
    ThumbnailSource row persisted under the -1 sentinel with a hosted_url."""
    await _seed_assets(adapter)
    # base entry (index 1) already has all three assets from _seed_assets.
    await adapter.cache.set_franchise("REQ-1", {"anime_doc_id": "doc-x", "english": "Root"})
    webp = _stub_renderer(adapter, tmp_path)

    captured: dict = {}
    import nekofetch.services.thumbnail_service as ts

    async def spy_gather(container, title, anime_doc_id=None, *, prefer_anilist_synopsis=False, anilist_id=None):
        captured["prefer"] = prefer_anilist_synopsis
        return {"anilist_score": 70}  # per-entry value that must be OVERRIDDEN

    async def spy_persist(container, anime_doc_id, anilist_id, fields, *, image_path=None):
        captured["persist_anilist_id"] = anilist_id
        captured["persist_fields"] = fields
    monkeypatch.setattr(ts, "gather_thumbnail_fields", spy_gather)
    monkeypatch.setattr(ts, "persist_thumbnail_source", spy_persist)

    # Wrap the fake renderer to capture the kwargs it's called with.
    class _CapRenderer:
        async def render_thumbnail(self, **kw):
            captured["render_kw"] = kw
            return webp
    adapter._render = _CapRenderer()

    # Franchise-average score source: the cached AniList walk (scores 0-10).
    import nekofetch.services.metadata_prefetch as mp

    async def fake_load_cached(container, doc, kind, *, anime_doc_id=None):
        if kind == "anilist":
            return {"franchise": [{"score": 8.0}, {"score": 9.0}]}  # avg 8.5 → 85
        return None
    monkeypatch.setattr(mp, "load_cached", fake_load_cached)

    import kurosoden.shared.image_backup as image_backup

    async def fake_backup(container, blob, *, mime="image/jpeg", source_url=""):
        from kurosoden.shared.image_backup import BackupImage
        return BackupImage(source_url=source_url,
                           catbox_url="https://files.catbox.moe/main.webp")
    monkeypatch.setattr(image_backup, "backup_bytes", fake_backup)

    out = await adapter.render_main("REQ-1")

    assert out == webp
    assert captured["prefer"] is False                       # TMDB franchise synopsis
    assert captured["render_kw"]["variant_key"] == "main"
    assert captured["render_kw"]["anilist_score"] == 85       # avg 8.5 ×10, overrides 70
    assert captured["persist_anilist_id"] is None             # → stored as -1
    assert captured["persist_fields"]["hosted_url"] == "https://files.catbox.moe/main.webp"


@pytest.mark.asyncio
async def test_render_main_needs_base_assets(adapter, monkeypatch):
    """No base-entry assets → render_main bails (None), never crashes publish."""
    await adapter.cache.set_entries("REQ-1", _entries())  # no selections
    await adapter.cache.set_franchise("REQ-1", {"anime_doc_id": "doc-x"})
    _stub = type("_R", (), {"render_thumbnail": staticmethod(lambda **k: None)})()
    adapter._render = _stub
    assert await adapter.render_main("REQ-1") is None
