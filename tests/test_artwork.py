"""Tests for per-anime artwork rotation (nekofetch/ui/artwork.py).

The proposition: once an anime is requested, EVERY card tied to it shows that
anime's own artwork, rotating through different pieces so no two consecutive
cards look identical. These tests cover the rotation engine and the seeding
helpers that wire it to franchise data / TMDB.
"""

from __future__ import annotations

import pytest


# ── key derivation ───────────────────────────────────────────────────────────

class TestAnimeArtKey:
    def test_doc_id_wins(self):
        from nekofetch.ui.artwork import anime_art_key
        assert anime_art_key(doc_id="abc", anilist_id=5, title="X") == "doc:abc"

    def test_anilist_id_next(self):
        from nekofetch.ui.artwork import anime_art_key
        assert anime_art_key(anilist_id=5, title="X") == "al:5"

    def test_title_fallback_is_normalized(self):
        from nekofetch.ui.artwork import anime_art_key
        assert anime_art_key(title="  Takopi's Original SIN ") == "t:takopi's original sin"

    def test_same_anime_same_key_across_surfaces(self):
        from nekofetch.ui.artwork import anime_art_key, key_for_franchise
        fr = {"anilist_id": "42", "title": "Takopi"}
        assert key_for_franchise(fr) == anime_art_key(anilist_id="42")


# ── rotation: no back-to-back repeats ────────────────────────────────────────

class TestRotation:
    def test_unseeded_falls_back_to_local_art(self):
        from nekofetch.ui.artwork import next_anime_art
        # A never-seeded key returns whatever pick_artwork yields (a Path or None),
        # never a URL — the anime pool is empty.
        out = next_anime_art("t:never-seeded-anime-xyz")
        assert not isinstance(out, str)

    def test_single_url_repeats(self):
        from nekofetch.ui.artwork import seed_anime_art, next_anime_art
        seed_anime_art("t:single", ["http://img/one.jpg"])
        assert next_anime_art("t:single") == "http://img/one.jpg"
        assert next_anime_art("t:single") == "http://img/one.jpg"

    def test_no_immediate_repeat_with_multiple(self):
        from nekofetch.ui.artwork import seed_anime_art, next_anime_art
        urls = [f"http://img/{i}.jpg" for i in range(4)]
        seed_anime_art("t:multi", urls)
        prev = None
        for _ in range(30):
            cur = next_anime_art("t:multi")
            assert cur in urls
            assert cur != prev  # never the same twice in a row
            prev = cur

    def test_seed_is_idempotent_and_order_preserving(self):
        from nekofetch.ui.artwork import seed_anime_art, _anime_pools
        seed_anime_art("t:idem", ["a", "b"])
        seed_anime_art("t:idem", ["b", "c"])  # 'b' already present
        assert _anime_pools["t:idem"]._urls == ["a", "b", "c"]


# ── ensure_anime_art: seeds once, from franchise + TMDB ──────────────────────

class _FakeTmdb:
    def __init__(self, urls):
        self._urls = urls
        self.calls = 0

    async def backdrops(self, title, *, size="w1280", limit=8):
        self.calls += 1
        return list(self._urls)


class TestEnsureAnimeArt:
    @pytest.mark.asyncio
    async def test_only_fetches_once(self):
        from nekofetch.ui.artwork import ensure_anime_art
        tmdb = _FakeTmdb(["http://t/1.jpg"])
        await ensure_anime_art("t:ensure2", tmdb=tmdb, title="Anime")
        await ensure_anime_art("t:ensure2", tmdb=tmdb, title="Anime")
        assert tmdb.calls == 1  # second call no-ops — no repeat network hit

    @pytest.mark.asyncio
    async def test_tmdb_failure_is_swallowed(self):
        from nekofetch.ui.artwork import ensure_anime_art, next_anime_art

        class Boom:
            async def backdrops(self, *a, **k):
                raise RuntimeError("tmdb down")

        # Franchise art still seeds even when TMDB explodes.
        await ensure_anime_art("t:ensure3", tmdb=Boom(), title="A",
                               franchise={"backdrop_url": "http://fr/x.jpg"})
        assert next_anime_art("t:ensure3") == "http://fr/x.jpg"
