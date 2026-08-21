"""Bug D: distribution entry cards must use PER-ENTRY AniList data.

``gather_thumbnail_fields`` used to read the franchise ROOT ``anilist.json
["search"]`` blob for every season card, so season 2 showed season 1's romaji /
native / score / year / runtime. Passing the entry's own ``anilist_id`` now
routes those fields to that installment's node in the cached franchise walk
(``anilist.json["franchise"][anilist_id]``), with the TMDB content rating
(TV-14) kept series-level. These tests pin that routing with a stubbed cache — no
network, no DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import nekofetch.services.thumbnail_service as ts


_ROOT_SEARCH = {
    "romaji": "Root Romaji",
    "titles": ["Root English", "Root Romaji", "ルート"],
    "score": 8.0,                     # → ring 80
    "synopsis": "Root (season 1) synopsis.",
}
_S2_NODE = {
    "titles": ["Show Season 2", "Show Romaji Season 2", "ショー Season 2"],
    "score": 8.2,                     # → ring 82
    "synopsis": "Season 2 synopsis.",
    "duration": 24,                   # → "24m"
    "start_date": {"year": 2025},     # → "2025"
}
_TMDB = {
    "result": {
        "overview": "TMDB franchise overview.",
        "year": "2022", "certification": "TV-14", "runtime": "24m",
    }
}


@pytest.fixture
def _patched_cache(monkeypatch):
    """Stub the on-disk metadata cache the enrichment reads."""
    import nekofetch.services.metadata_prefetch as mp

    async def fake_load_cached(container, code, kind, *, anime_doc_id=None):
        if kind == "tmdb":
            return _TMDB
        if kind == "anilist":
            return {"search": _ROOT_SEARCH, "franchise": {"154768": _S2_NODE}}
        return None

    async def fake_load_cached_jikan(container, code, *, anime_doc_id=None):
        return None

    monkeypatch.setattr(mp, "load_cached", fake_load_cached)
    monkeypatch.setattr(mp, "load_cached_jikan", fake_load_cached_jikan)


def _container():
    # anilist raises if the per-entry LIVE fallback is (wrongly) taken on a cache
    # hit; pg_sessionmaker=None makes the language block degrade to "".
    class _Boom:
        async def _fetch_full(self, *_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("live fetch should not run on a cache hit")
    return SimpleNamespace(tmdb=None, anilist=_Boom(), pg_sessionmaker=None)


@pytest.mark.asyncio
async def test_entry_fields_come_from_the_per_entry_node(_patched_cache):
    fields = await ts.gather_thumbnail_fields(
        _container(), "Show", "132405",
        prefer_anilist_synopsis=True, anilist_id=154768,
    )
    assert fields["romaji_title"] == "Show Romaji Season 2"
    assert fields["native_title"] == "ショー Season 2"
    assert fields["anilist_score"] == 82                 # S2, not root 80
    assert fields["synopsis"] == "Season 2 synopsis."
    # Year + runtime from the entry's AniList node; content rating from TMDB.
    assert fields["meta_label"] == "2025 | TV-14 | 24m"


@pytest.mark.asyncio
async def test_root_render_still_uses_the_search_blob(_patched_cache):
    # Without an anilist_id (the franchise root/season-1 card), the fields come
    # from the search blob + TMDB meta line as before — proving the per-entry
    # path is opt-in and doesn't regress the root card.
    fields = await ts.gather_thumbnail_fields(
        _container(), "Show", "132405", prefer_anilist_synopsis=True,
    )
    assert fields["romaji_title"] == "Root Romaji"
    assert fields["anilist_score"] == 80
    assert fields["meta_label"] == "2022 | TV-14 | 24m"


def test_strip_html_flattens_anilist_markup():
    # AniList synopses ship <i>/<br>/<b> + entities; the card render HTML-ESCAPES
    # the value, so an unstripped tag renders as the literal text "<i>"/"<br>" in
    # the image (the reported season-2 thumbnail bug).
    raw = "A <i>delinquent</i> who sews.<br><br>Then <b>Marin</b> appears &amp; more."
    out = ts._strip_html(raw)
    assert "<" not in out and ">" not in out
    assert "&amp;" not in out and "&" in out
    assert "delinquent" in out and "Marin" in out


def test_strip_html_removes_only_real_tags_not_bracketed_prose():
    # Only WHITELISTED HTML tag names are stripped, so genuine angle-bracketed
    # prose survives — the owner's concern that "<Wakana Gojo>" must not be
    # deleted like an HTML tag. Void tags (<br>) are still handled (a paired-only
    # rule would leave them as literal text).
    assert ts._strip_html("The second season of <i>Sono Bisque Doll</i>.") \
        == "The second season of Sono Bisque Doll."
    assert ts._strip_html("Line one.<br><br>Line two.") == "Line one. Line two."
    assert ts._strip_html('See <a href="https://x">here</a> now.') == "See here now."
    # Preserved: real prose that merely uses angle brackets.
    assert ts._strip_html("A boy named <Wakana Gojo> sews.") \
        == "A boy named <Wakana Gojo> sews."
    assert ts._strip_html("If x < 3 and y > 1 then win.") == "If x < 3 and y > 1 then win."
    assert ts._strip_html("I love this <3 so much.") == "I love this <3 so much."
    assert ts._strip_html("The <AI> takes over.") == "The <AI> takes over."


@pytest.mark.asyncio
async def test_gathered_synopsis_is_tag_free(monkeypatch):
    # End-to-end: a per-entry node whose synopsis carries HTML yields a clean,
    # tag-free synopsis in the fields the renderer/persistence consume.
    import nekofetch.services.metadata_prefetch as mp

    node = {**_S2_NODE, "synopsis": "S2 <i>italic</i> desc.<br>Second line."}

    async def fake_load_cached(container, code, kind, *, anime_doc_id=None):
        if kind == "tmdb":
            return _TMDB
        if kind == "anilist":
            return {"search": _ROOT_SEARCH, "franchise": {"154768": node}}
        return None

    async def fake_jikan(container, code, *, anime_doc_id=None):
        return None

    monkeypatch.setattr(mp, "load_cached", fake_load_cached)
    monkeypatch.setattr(mp, "load_cached_jikan", fake_jikan)

    fields = await ts.gather_thumbnail_fields(
        _container(), "Show", "132405",
        prefer_anilist_synopsis=True, anilist_id=154768,
    )
    assert "<" not in fields["synopsis"] and ">" not in fields["synopsis"]
    assert "italic" in fields["synopsis"] and "Second line." in fields["synopsis"]


@pytest.mark.asyncio
async def test_missing_node_falls_back_to_live_resilient_fetch(monkeypatch):
    # When the entry's node isn't cached, the resilient client resolves it by id
    # (AniList → Kaggle → Jikan → Kitsu). A null anilist_id in the stored data is
    # fine as long as SOME tier carries the data (the owner's requirement).
    import nekofetch.services.metadata_prefetch as mp

    async def fake_load_cached(container, code, kind, *, anime_doc_id=None):
        if kind == "tmdb":
            return _TMDB
        if kind == "anilist":
            return {"search": _ROOT_SEARCH}   # NO franchise walk cached
        return None

    async def fake_jikan(container, code, *, anime_doc_id=None):
        return None

    monkeypatch.setattr(mp, "load_cached", fake_load_cached)
    monkeypatch.setattr(mp, "load_cached_jikan", fake_jikan)

    class _Resilient:
        async def _fetch_full(self, media_id):
            assert media_id == 154768
            return SimpleNamespace(
                titles=["Show Season 2", "Show Romaji Season 2", "ショー Season 2"],
                score=8.2, synopsis="Season 2 synopsis.", duration=24,
                start_date={"year": 2025}, year=2025,
            )

    container = SimpleNamespace(tmdb=None, anilist=_Resilient(), pg_sessionmaker=None)
    fields = await ts.gather_thumbnail_fields(
        container, "Show", "132405",
        prefer_anilist_synopsis=True, anilist_id=154768,
    )
    assert fields["romaji_title"] == "Show Romaji Season 2"
    assert fields["anilist_score"] == 82
    assert fields["meta_label"] == "2025 | TV-14 | 24m"
