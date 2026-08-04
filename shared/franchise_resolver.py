"""Unified franchise resolution — one title in, one normalized dict out.

``resolve_franchise(container, query)`` walks a provider chain and returns the
**same** franchise-dict shape ``confirm_franchise`` (and the request pipeline)
expects, regardless of which source answered:

    AniList ──► Jikan/MAL ──► @acutebot ──► TMDB

The first two hops are already fused inside ``container.anilist`` — it's a
:class:`ResilientMetadataClient`, so a single ``.search()`` tries AniList and
transparently falls back to MyAnimeList (Jikan) on a 403/None/exception. This
resolver adds the two remaining hops (the @acutebot userbot probe, then a TMDB
title search) and normalizes every provider's native result to the canonical
franchise dict via :func:`_media_to_franchise_dict` or a per-source adapter.

Both the single-request flow and the batch flow call this, so a title that
AniList can't find still resolves through the same fallbacks in both paths.
"""

from __future__ import annotations

import re
from typing import Any

from nekofetch.bots.admin.handlers.requests import (
    _media_to_franchise_dict,
    apply_franchise_totals,
)
from nekofetch.core.logging import get_logger

log = get_logger(__name__)


def _blank_franchise(title: str, source: str) -> dict:
    """Canonical franchise dict with everything nulled but ``title``/``_source``."""
    return {
        "title": title,
        "english": title,
        "romaji": None,
        "year": None,
        "format": None,
        "status": None,
        "score": None,
        "studio": None,
        "genres": [],
        "synopsis": None,
        "synopsis_url": None,
        "franchise_episodes": None,
        "franchise_seasons": None,
        "franchise_movies": None,
        "franchise_ovas": None,
        "franchise_onas": None,
        "franchise_specials": None,
        "synonyms": [],
        "relations": [],
        "anilist_id": None,
        "anilist_url": None,
        "cover_url": None,
        "banner_url": None,
        "_source": source,
    }


def _from_acute(meta: dict, query: str) -> dict:
    """Adapt an @acutebot metadata dict to the franchise-dict shape.

    @acutebot returns a flat legacy dict (title/romaji/format/score/genres/
    synopsis/episode_count + anilist_id/verified). We map the overlapping keys
    and leave the franchise breakdown empty — ``apply_franchise_totals`` fills
    it from AniList when @acutebot handed us a verified ``anilist_id``.
    """
    out = _blank_franchise(meta.get("title") or query, "acutebot")
    out["english"] = meta.get("title") or out["english"]
    out["romaji"] = meta.get("romaji")
    out["format"] = meta.get("format")
    out["status"] = meta.get("status")
    out["score"] = meta.get("score")
    out["genres"] = meta.get("genres") or []
    out["synopsis"] = meta.get("synopsis")
    out["franchise_episodes"] = meta.get("episode_count")
    out["cover_url"] = meta.get("poster_url")
    out["banner_url"] = meta.get("banner_url")
    aid = meta.get("anilist_id")
    if aid:
        out["anilist_id"] = str(aid)
        out["anilist_url"] = f"https://anilist.co/anime/{aid}"
        out["synopsis_url"] = out["anilist_url"]
    return out


def _from_tmdb(res: Any, query: str) -> dict:
    """Adapt a :class:`TmdbResult` to the franchise-dict shape."""
    out = _blank_franchise(getattr(res, "title", None) or query, "tmdb")
    out["english"] = getattr(res, "title", None) or out["english"]
    out["year"] = getattr(res, "year", None)
    out["format"] = "TV" if getattr(res, "media_type", "") == "tv" else "MOVIE"
    out["score"] = str(res.rating) if getattr(res, "rating", None) else None
    out["genres"] = getattr(res, "genres", None) or []
    out["synopsis"] = getattr(res, "overview", "") or None
    out["studio"] = getattr(res, "studio", None)
    out["franchise_episodes"] = getattr(res, "episodes", None)
    out["franchise_seasons"] = getattr(res, "seasons", None)
    out["cover_url"] = getattr(res, "poster_url", None)
    out["banner_url"] = getattr(res, "backdrop_url", None)
    return out


async def _try_anilist(container: Any, query: str) -> dict | None:
    """AniList → Jikan/MAL (fused in ResilientMetadataClient.search)."""
    try:
        media = await container.anilist.search(query)
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve.anilist.failed", query=query, error=str(exc)[:200])
        return None
    if media is None:
        return None
    return _media_to_franchise_dict(media)


async def _try_acute(container: Any, query: str) -> dict | None:
    """@acutebot userbot probe. Skipped cleanly when no userbot pool exists."""
    try:
        from nekofetch.providers.acute_bot import fetch_from_acutebot
        from nekofetch.sources.telegram.userbot import UserbotPool
    except Exception:  # noqa: BLE001
        return None
    pool = getattr(container, "_userbot_pool", None)
    if pool is None:
        try:
            pool = UserbotPool.from_env(
                container.env.telegram_api_id,
                container.env.telegram_api_hash,
                str(container.env.session_path),
            )
            container._userbot_pool = pool
        except Exception as exc:  # noqa: BLE001
            log.debug("resolve.acute.no_pool", query=query, error=str(exc)[:200])
            return None
    try:
        photo_dir = str(container.env.storage_path / "acutebot_cards")
        meta = await fetch_from_acutebot(query, pool, photo_dir=photo_dir)
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve.acute.failed", query=query, error=str(exc)[:200])
        return None
    return _from_acute(meta, query) if meta else None


async def _try_tmdb(container: Any, query: str) -> dict | None:
    tmdb = getattr(container, "tmdb", None)
    if tmdb is None:
        return None
    try:
        res = await tmdb.search(query)
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve.tmdb.failed", query=query, error=str(exc)[:200])
        return None
    return _from_tmdb(res, query) if res else None


async def resolve_franchise(
    container: Any,
    query: str,
    *,
    with_totals: bool = True,
) -> dict | None:
    """Resolve ``query`` to a normalized franchise dict, or ``None`` if every
    provider missed.

    Chain: AniList → Jikan/MAL (both via ``container.anilist``) → @acutebot →
    TMDB. The first non-empty result wins. When ``with_totals`` and the result
    carries an ``anilist_id``, the franchise breakdown is recomputed across the
    full AniList relation graph (:func:`apply_franchise_totals`) so counts are
    consistent no matter which provider first matched the title.
    """
    query = (query or "").strip()
    if not query:
        return None

    # Guard: an internal request code (``REQ-1058``) is NOT a searchable title.
    # If one ever reaches here it would be sent verbatim as the @acutebot
    # ``/anime`` query and surface as a bogus "title" (the REQ-1058 bug). Reject
    # it loudly so the caller is fixed rather than silently shipping garbage.
    if re.fullmatch(r"REQ-\d+", query, flags=re.IGNORECASE):
        log.warning("resolve.rejected_request_code", query=query,
                    hint="pass the anime title, not the request code")
        return None

    for name, probe in (
        ("anilist", _try_anilist),
        ("acutebot", _try_acute),
        ("tmdb", _try_tmdb),
    ):
        result = await probe(container, query)
        if result:
            log.info("resolve.hit", query=query, source=result.get("_source", name))
            if with_totals and result.get("anilist_id"):
                try:
                    await apply_franchise_totals(container, result)
                except Exception as exc:  # noqa: BLE001
                    log.debug("resolve.totals.failed", query=query,
                              error=str(exc)[:200])
            return result

    log.info("resolve.miss", query=query)
    return None


def _franchise_key(title: str) -> frozenset:
    """A franchise-identity key: the title's meaningful words with season/part
    markers stripped, so "Attack on Titan" and "Attack on Titan Season 2" collapse
    to the same key while "Attack No.1" stays distinct."""
    from nekofetch.providers.metadata.series import _token_is_marker
    from nekofetch.sources.telegram.matching import normalize_words

    words = normalize_words(title)
    core = frozenset(w for w in words if not _token_is_marker(w))
    return core or frozenset(words)


async def resolve_franchise_candidates(
    container: Any, query: str, *, limit: int = 25,
) -> list[dict]:
    """Return the DISTINCT franchises a query could mean, for the batch picker.

    Groups AniList's search page (``search_candidates``) into franchises by
    collapsing seasons/arcs of the same title (:func:`_franchise_key`), keeping the
    most-popular installment as each franchise's representative. Ordering follows
    the search ranking (best match first). Each item is a light dict
    ``{title, anilist_id, format}``.

    Falls back to the single-best :func:`resolve_franchise` chain (acutebot → TMDB)
    when AniList returns no page — so a title AniList can't page still yields one
    candidate rather than nothing.
    """
    query = (query or "").strip()
    if not query:
        return []

    candidates: list[dict] = []
    search_candidates = getattr(container.anilist, "search_candidates", None)
    if search_candidates is not None:
        try:
            candidates = await search_candidates(query, limit=max(limit, 10))
        except Exception as exc:  # noqa: BLE001
            log.debug("resolve.candidates.page_failed", query=query,
                      error=str(exc)[:200])
            candidates = []

    # Group by franchise identity, keeping the most-popular representative and the
    # order of first appearance (search rank).
    grouped: dict[frozenset, dict] = {}
    order: list[frozenset] = []
    for c in candidates:
        title = c.get("title") or query
        key = _franchise_key(title)
        cur = grouped.get(key)
        if cur is None:
            grouped[key] = c
            order.append(key)
        elif (c.get("popularity") or 0) > (cur.get("popularity") or 0):
            grouped[key] = c

    franchises = [
        {
            "title": grouped[k].get("title") or query,
            "anilist_id": str(grouped[k]["id"]) if grouped[k].get("id") else None,
            "format": grouped[k].get("format"),
        }
        for k in order
    ][:limit]

    if franchises:
        return franchises

    # No AniList page — fall back to the single-best resolver so a match found only
    # via @acutebot/TMDB still shows up as one franchise the admin can approve.
    single = await resolve_franchise(container, query)
    if single:
        return [{
            "title": single.get("title") or query,
            "anilist_id": single.get("anilist_id"),
            "format": single.get("format"),
        }]
    return []
