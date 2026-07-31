"""Section artwork — random selection with no back-to-back repeats.

Every major UI surface shows a 16:9 image. We rotate through the pool in
``images/`` randomly, but never return the same artwork twice in a row, so a
message that re-renders (e.g. an edit) doesn't keep showing the same picture.

Kuro Sōden: each bot character (levi / senku / gojo / lelouch) has its own
subdirectory under ``images/``. Pass ``bot_name`` to ``pick_artwork()`` to
pick from a character-specific pool; omit it for the shared default pool.
"""

from __future__ import annotations

import random
from pathlib import Path

# kurosoden flattens the src/ layer — parents[2] reaches the project root
ART_DIR = Path(__file__).resolve().parents[2] / "images"


class ArtworkPicker:
    """Picks a random artwork, avoiding an immediate repeat of the last pick."""

    def __init__(self, directory: Path = ART_DIR) -> None:
        self.directory = directory
        self._last: Path | None = None

    def available(self) -> list[Path]:
        imgs = sorted(self.directory.glob("art_*.jpg"))
        return imgs or sorted(self.directory.glob("*.jpg"))

    def pick(self) -> Path | None:
        imgs = self.available()
        if not imgs:
            return None
        if len(imgs) == 1:
            self._last = imgs[0]
            return imgs[0]
        choices = [p for p in imgs if p != self._last]
        choice = random.choice(choices)
        self._last = choice
        return choice


# Module-level default so callers share the same no-repeat history.
_default = ArtworkPicker()
# Per-character pools (lazy, created on first use).
_pools: dict[str, ArtworkPicker] = {}


def pick_artwork(bot_name: str | None = None) -> Path | None:
    """Return the path to a random section artwork (never the same one twice
    consecutively), or ``None`` if the image pool is empty.

    When ``bot_name`` is given, picks from ``images/<bot_name>/`` first;
    falls back to the shared pool if the character directory is empty.
    """
    if bot_name:
        char_dir = ART_DIR / bot_name.lower()
        if char_dir.is_dir():
            if bot_name not in _pools:
                _pools[bot_name] = ArtworkPicker(char_dir)
            result = _pools[bot_name].pick()
            if result is not None:
                return result
    return _default.pick()


# ── Per-anime artwork rotation ───────────────────────────────────────────────
#
# The proposition: once an anime is requested, EVERY card tied to it (the
# receipt, the "already requested" notice, the downloader wizard, admin pings)
# shows that anime's OWN artwork — and rotates through different pieces of it so
# no two consecutive cards look identical. State lives in-process; all four bots
# share one process under the pipeline manager, so the rotation is consistent
# across Lelouch → Levi → Senku → Gojo for the life of the run.


class _AnimeArtRotator:
    """Rotates through one anime's backdrop URLs in a stable circle."""

    def __init__(self) -> None:
        self._urls: list[str] = []
        self._index = 0

    @property
    def seeded(self) -> bool:
        return bool(self._urls)

    def seed(self, urls: list[str] | None) -> None:
        for u in urls or []:
            if u and u not in self._urls:
                self._urls.append(u)

    def next(self) -> str | None:
        if not self._urls:
            return None
        choice = self._urls[self._index % len(self._urls)]
        self._index += 1
        return choice


_anime_pools: dict[str, _AnimeArtRotator] = {}


def anime_art_key(*, doc_id: str | None = None, anilist_id: str | int | None = None,
                  title: str | None = None) -> str:
    """Stable key for an anime's artwork pool.

    Prefers the durable ids (doc/AniList) and falls back to a normalized title
    so the same anime maps to the same pool no matter which surface asks.
    """
    if doc_id:
        return f"doc:{doc_id}"
    if anilist_id:
        return f"al:{anilist_id}"
    return f"t:{(title or '').strip().casefold()}"


def key_for_franchise(franchise: dict | None, *, title: str | None = None) -> str:
    """Derive the pool key from a franchise dict (or a bare title)."""
    fr = franchise or {}
    return anime_art_key(
        doc_id=fr.get("anime_doc_id"),
        anilist_id=fr.get("anilist_id"),
        title=fr.get("title") or fr.get("english") or title,
    )


def seed_anime_art(key: str, urls: list[str] | None) -> None:
    """Add artwork URLs to an anime's rotation pool (idempotent, order-preserving)."""
    _anime_pools.setdefault(key, _AnimeArtRotator()).seed(urls)


def next_anime_art(key: str, *, fallback_bot: str | None = None):
    """Next artwork for this anime, or a generic recurring art when unseeded.

    Returns a URL string (anime art) or a local ``Path`` (generic fallback) —
    both are accepted by ``Screen.image`` / ``send_photo``.
    """
    pool = _anime_pools.get(key)
    if pool is not None:
        img = pool.next()
        if img:
            return img
    return pick_artwork(fallback_bot)


def _urls_from_franchise(franchise: dict | None) -> list[str]:
    """Pull any already-known **backdrop** URLs out of a franchise dict.

    Recurring card artwork must be 16:9 backdrops only — NEVER posters. So we
    deliberately skip ``cover_url``/``poster_url`` (portrait key art) and only
    take the pre-fetched backdrop list plus the single backdrop/banner fields.
    The TMDB gallery seeded alongside these (see ``ensure_anime_art``) is
    already English-backdrop-first then language-neutral, so posters never
    enter the rotation from any path.
    """
    fr = franchise or {}
    urls: list[str] = []
    # A pre-fetched backdrop list wins; then the single card backdrop / banner.
    # ``banner_url`` on AniList is the wide 16:9 banner (a backdrop), NOT the
    # portrait cover — safe to include. ``cover_url``/``poster_url`` are posters
    # and are intentionally excluded.
    for u in (fr.get("backdrops") or []):
        if u:
            urls.append(u)
    for k in ("backdrop_url", "_backdrop_url", "banner_url"):
        v = fr.get(k)
        if v:
            urls.append(v)
    return urls


async def ensure_anime_art(
    key: str,
    *,
    tmdb=None,
    title: str | None = None,
    franchise: dict | None = None,
    limit: int = 8,
    container=None,
    anime_doc_id: str | None = None,
) -> None:
    """Seed an anime's artwork pool once — from known franchise URLs, the
    prefetch cache, and (only as a last resort) a live TMDB backdrop gallery.

    Safe to call repeatedly: it no-ops once the pool already holds art. When
    ``container`` + ``anime_doc_id`` are given, the prefetched ``tmdb.json``
    backdrops are used FIRST so the recurring card art never triggers a live
    TMDB call for artwork already downloaded at acceptance. The live
    ``tmdb.backdrops`` fetch only runs when neither the franchise URLs nor the
    cache yielded anything.
    """
    pool = _anime_pools.get(key)
    if pool is not None and pool.seeded:
        return

    urls = _urls_from_franchise(franchise)

    # Prefetch cache: the ranked TMDB backdrops saved at acceptance.
    if not urls and container is not None and anime_doc_id:
        try:
            from nekofetch.services.metadata_prefetch import load_cached_tmdb_assets

            cached = await load_cached_tmdb_assets(
                container, anime_doc_id, "backdrop", anime_doc_id=anime_doc_id)
            if cached:
                urls += [a["url"] for a in cached if a.get("url")][:limit]
        except Exception:  # noqa: BLE001 — artwork is decorative
            pass

    # Live TMDB only when nothing was cached/known.
    if not urls and tmdb is not None and title:
        try:
            urls += await tmdb.backdrops(title, limit=limit)
        except Exception:  # noqa: BLE001 — artwork is decorative; never fail a flow
            pass
    if urls:
        seed_anime_art(key, urls)
